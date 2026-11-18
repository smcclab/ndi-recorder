"""Drive NDI's own command-line recorder, one process per camera.

`Application.Mac.NDI.Recording` (installed with NDI Tools) writes the camera's
compressed NDI stream straight to a .mov with no re-encode. We launch every camera
with -noautostart, give them a moment to connect, then send <start/> to all at once.
Each file also carries NDI timecode (UTC), so files can be aligned exactly later.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .ndi import NDISource

RECORDER_CANDIDATES = [
    os.environ.get("NDI_RECORD", ""),
    "/usr/local/bin/Application.Mac.NDI.Recording",
    "/Library/NDI SDK for Apple/bin/Application.Mac.NDI.Recording",
]

ATTR_RE = re.compile(r'(\w+)="([^"]*)"')
TAG_RE = re.compile(r"<(\w+)\s")


def find_recorder() -> str | None:
    for p in RECORDER_CANDIDATES:
        if p and os.access(p, os.X_OK):
            return p
    return shutil.which("Application.Mac.NDI.Recording")


def safe_filename(name: str) -> str:
    name = re.sub(r'[/\\:*?"<>|]+', "-", name).strip(" .")
    return name or "camera"


def _ndi_tc_to_iso(tc: str | None) -> str | None:
    """NDI timecodes are 100ns ticks. The SDK default is time since the Unix epoch, but
    cameras (e.g. BirdDog) may send UTC time-of-day instead, so handle both."""
    if not tc:
        return None
    try:
        ticks = int(tc)
    except ValueError:
        return None
    if ticks < 2 * 86400 * 10**7:  # time of day
        s = (ticks / 1e7) % 86400
        return f"{int(s // 3600):02d}:{int(s % 3600 // 60):02d}:{s % 60:06.3f} UTC (time of day)"
    return datetime.fromtimestamp(ticks / 1e7).astimezone().isoformat(timespec="milliseconds")


@dataclass
class CameraRecorder:
    source: NDISource
    label: str
    path: Path
    proxy: bool
    state: str = "connecting"  # connecting | recording | stopping | stopped | error
    frames: int = 0
    vu_db: float | None = None
    start_timecode: str | None = None
    last_timecode: str | None = None
    resolution: str | None = None
    frame_rate: str | None = None
    files: list[str] = field(default_factory=list)
    error: str | None = None
    log: list[str] = field(default_factory=list)
    started_wall: float | None = None
    _proc: subprocess.Popen | None = None
    _last_frame_time: float = 0.0

    def launch(self, exe: str) -> None:
        cmd = [exe, "-i", self.source.name, "-o", str(self.path), "-noautostart"]
        if self.source.url:
            cmd += ["-u", self.source.url]
        if not self.proxy:
            cmd.append("-nothumbnail")
        self._proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
            start_new_session=True,  # don't die with the server on Ctrl+C; we stop it cleanly
        )
        threading.Thread(target=self._read, args=(self._proc.stdout,), daemon=True).start()
        threading.Thread(target=self._read, args=(self._proc.stderr,), daemon=True).start()
        threading.Thread(target=self._wait, daemon=True).start()

    def send(self, cmd: str) -> None:
        if self._proc and self._proc.poll() is None and self._proc.stdin:
            try:
                self._proc.stdin.write(cmd + "\n")
                self._proc.stdin.flush()
            except (BrokenPipeError, OSError):
                pass

    def start(self) -> None:
        self.send("<start/>")

    def stop(self) -> None:
        if self.state in ("connecting", "recording"):
            self.state = "stopping"
        self.send("<exit/>")

    def wait(self, timeout: float) -> None:
        if not self._proc:
            return
        try:
            self._proc.wait(timeout)
        except subprocess.TimeoutExpired:
            # Same as Ctrl+C: the recorder finalises the file on SIGINT. Never SIGKILL first.
            self._proc.send_signal(signal.SIGINT)
            try:
                self._proc.wait(15)
            except subprocess.TimeoutExpired:
                self._proc.kill()
                self.error = "Recorder had to be killed; file may need repair (-rebuild)."

    @property
    def alive(self) -> bool:
        return bool(self._proc and self._proc.poll() is None)

    @property
    def stalled(self) -> bool:
        return self.state == "recording" and time.monotonic() - self._last_frame_time > 5

    def bytes_written(self) -> int:
        total = 0
        for f in self.files or [str(self.path)]:
            try:
                total += os.path.getsize(f)
            except OSError:
                pass
        return total

    def _read(self, stream) -> None:
        for line in stream:
            line = line.strip()
            if not line:
                continue
            m = TAG_RE.search(line)
            if not m:
                if not line.startswith("[") and "Copyright" not in line and "NDI Stream Record" not in line:
                    self._log(line)
                continue
            tag, attrs = m.group(1), dict(ATTR_RE.findall(line))
            if tag == "record_started":
                self.state = "recording"
                self.started_wall = self.started_wall or time.time()
                self._last_frame_time = time.monotonic()
                if attrs.get("filename"):
                    self.files.append(attrs["filename"])
                if attrs.get("xres"):
                    self.resolution = f"{attrs['xres']}×{attrs['yres']}"
                if attrs.get("frame_rate_d") not in (None, "0"):
                    fr = int(attrs["frame_rate_n"]) / int(attrs["frame_rate_d"])
                    self.frame_rate = f"{fr:.2f}".rstrip("0").rstrip(".")
                self._log(f"recording → {attrs.get('filename')}")
            elif tag == "recording":
                self.frames = int(attrs.get("no_frames", self.frames))
                self.last_timecode = attrs.get("timecode", self.last_timecode)
                self.start_timecode = self.start_timecode or attrs.get("start_timecode")
                try:
                    self.vu_db = float(attrs.get("vu_dB", "nan"))
                except ValueError:
                    self.vu_db = None
                self._last_frame_time = time.monotonic()
            elif tag == "record_stopped":
                self.frames = int(attrs.get("no_frames", self.frames))
                self.last_timecode = attrs.get("last_timecode", self.last_timecode)
                self._log(f"stopped after {self.frames} frames")
            elif tag == "record_error":
                self.error = attrs.get("error", line)
                self._log(f"ERROR: {self.error}")

    def _wait(self) -> None:
        assert self._proc
        rc = self._proc.wait()
        if self.state in ("connecting", "recording"):
            self.state = "error"
            self.error = self.error or f"Recorder exited unexpectedly (code {rc})"
        elif self.state != "error":
            self.state = "stopped"

    def _log(self, msg: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {msg}")
        del self.log[:-50]

    def to_dict(self) -> dict:
        vu = self.vu_db if self.vu_db is not None and self.vu_db == self.vu_db and self.vu_db > -200 else None
        return {
            "source": self.source.name,
            "label": self.label,
            "state": "stalled" if self.stalled else self.state,
            "frames": self.frames,
            "vu_db": vu,
            "resolution": self.resolution,
            "frame_rate": self.frame_rate,
            "files": self.files or [str(self.path)],
            "bytes": self.bytes_written(),
            "error": self.error,
            "log": self.log[-8:],
            "start_timecode": _ndi_tc_to_iso(self.start_timecode),
        }


class Session:
    """One take: all selected cameras recorded into <folder>/<timestamp>[ label]/."""

    MIN_FREE_BYTES = 5 * 1024**3  # auto-stop below this so files are finalised cleanly

    def __init__(self, exe: str, folder: str, cameras: list[tuple[NDISource, str]], name: str = "", proxy: bool = True):
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        self.name = name.strip()
        self.dir = Path(folder) / safe_filename(f"{stamp} {self.name}".strip())
        self.dir.mkdir(parents=True, exist_ok=False)
        self.exe = exe
        self.created = time.time()
        self.started: float | None = None
        self.stopped: float | None = None
        self.stop_reason: str | None = None
        used: set[str] = set()
        self.recorders: list[CameraRecorder] = []
        for src, label in cameras:
            base = safe_filename(label or src.name)
            fname, i = base, 2
            while fname in used:
                fname, i = f"{base} {i}", i + 1
            used.add(fname)
            self.recorders.append(CameraRecorder(src, label or src.name, self.dir / f"{fname}.mov", proxy))
        self._rate_samples: list[tuple[float, int]] = []
        self._stop_lock = threading.Lock()

    def start(self) -> None:
        for r in self.recorders:
            r.launch(self.exe)
        time.sleep(1.5)  # let every recorder connect to its source
        for r in self.recorders:  # then start all together
            r.start()
        self.started = time.time()
        threading.Thread(target=self._monitor, daemon=True).start()

    def stop(self, reason: str = "stopped by user") -> None:
        with self._stop_lock:
            if self.stopped:
                return
            self.stop_reason = reason
            for r in self.recorders:
                r.stop()
            threads = [threading.Thread(target=r.wait, args=(30,)) for r in self.recorders]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.stopped = time.time()
            self._write_manifest()

    @property
    def active(self) -> bool:
        return self.stopped is None

    def bytes_per_sec(self) -> float:
        if len(self._rate_samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._rate_samples[0], self._rate_samples[-1]
        return (b1 - b0) / (t1 - t0) if t1 > t0 else 0.0

    def _monitor(self) -> None:
        while self.active:
            time.sleep(2)
            if not self.active:
                break
            total = sum(r.bytes_written() for r in self.recorders)
            self._rate_samples.append((time.monotonic(), total))
            del self._rate_samples[:-10]
            if not self.dir.exists():
                for r in self.recorders:
                    r.error = "Recording folder disappeared — was the drive unplugged?"
                threading.Thread(target=self.stop, args=("drive disappeared",), daemon=True).start()
                break
            try:
                free = shutil.disk_usage(self.dir).free
            except OSError:
                continue
            if free < self.MIN_FREE_BYTES:
                threading.Thread(target=self.stop, args=("disk nearly full",), daemon=True).start()
                break
            if all(not r.alive for r in self.recorders):
                self.stop("all recorders exited")
                break

    def _write_manifest(self) -> None:
        data = {
            "name": self.name,
            "folder": str(self.dir),
            "started": datetime.fromtimestamp(self.started or self.created).astimezone().isoformat(),
            "stopped": datetime.fromtimestamp(self.stopped or time.time()).astimezone().isoformat(),
            "stop_reason": self.stop_reason,
            "note": "start_timecode values are NDI timecodes (UTC); use them to align cameras.",
            "cameras": [
                {**r.to_dict(), "start_timecode_ticks": r.start_timecode, "last_timecode_ticks": r.last_timecode}
                for r in self.recorders
            ],
        }
        for c in data["cameras"]:
            c.pop("log", None)
        try:
            (self.dir / "session.json").write_text(json.dumps(data, indent=2))
        except OSError:
            pass

    def to_dict(self) -> dict:
        free = None
        try:
            free = shutil.disk_usage(self.dir).free
        except OSError:
            pass
        rate = self.bytes_per_sec()
        return {
            "name": self.name,
            "dir": str(self.dir),
            "active": self.active,
            "started": self.started,
            "stopped": self.stopped,
            "elapsed": (self.stopped or time.time()) - (self.started or self.created),
            "stop_reason": self.stop_reason,
            "bytes_per_sec": rate,
            "free": free,
            "seconds_left": (free - self.MIN_FREE_BYTES) / rate if rate > 0 and free else None,
            "cameras": [r.to_dict() for r in self.recorders],
        }

"""Volume/folder helpers for choosing where recordings go (macOS)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import uuid
from pathlib import Path

_INTERNAL_DEVS = {os.stat(p).st_dev for p in ("/", "/System/Volumes/Data") if os.path.exists(p)}


def _mounts() -> dict[str, str]:
    """mount point -> filesystem type, parsed from `mount`."""
    out = subprocess.run(["mount"], capture_output=True, text=True).stdout
    result = {}
    for line in out.splitlines():
        m = re.match(r"^\S+ on (.+) \(([^,)]+)", line)
        if m:
            result[m.group(1)] = m.group(2)
    return result


def _mount_point(path: Path) -> Path:
    p = path.resolve()
    while not os.path.ismount(p) and p != p.parent:
        p = p.parent
    return p


def is_internal(path: str | Path) -> bool:
    try:
        return os.stat(path).st_dev in _INTERNAL_DEVS
    except OSError:
        return False


def volumes() -> list[dict]:
    mounts = _mounts()
    vols = []
    for entry in sorted(Path("/Volumes").iterdir()):
        try:
            if not entry.is_dir():
                continue
            usage = shutil.disk_usage(entry)
        except OSError:
            continue
        real = str(entry.resolve())
        vols.append(
            {
                "name": entry.name,
                "path": str(entry),
                "fs": mounts.get(real, mounts.get(str(entry), "?")),
                "internal": is_internal(entry),
                "free": usage.free,
                "total": usage.total,
            }
        )
    return vols


def folder_info(path: str) -> dict:
    """Everything the UI needs to judge whether `path` is a good place to record."""
    info: dict = {"path": path, "exists": False, "writable": False, "warnings": []}
    if not path:
        return info
    p = Path(path)
    info["exists"] = p.is_dir()
    if not info["exists"]:
        info["warnings"].append("Folder does not exist (is the drive plugged in?)")
        return info
    usage = shutil.disk_usage(p)
    mp = _mount_point(p)
    fs = _mounts().get(str(mp), "?")
    info.update(
        free=usage.free,
        total=usage.total,
        volume=mp.name if str(mp) != "/" else "Macintosh HD",
        fs=fs,
        internal=is_internal(p),
    )
    probe = p / f".ndi-recorder-write-test-{uuid.uuid4().hex}"
    try:
        probe.write_bytes(b"ok")
        probe.unlink()
        info["writable"] = True
    except OSError as e:
        info["warnings"].append(f"Not writable: {e.strerror}")
    if fs == "msdos":
        info["warnings"].append("FAT32 volume: files are limited to 4 GB (~5 min per camera). Reformat as exFAT or APFS.")
    if info["internal"]:
        info["warnings"].append("This is the internal disk, not an external drive.")
    return info


def list_dirs(path: str) -> dict:
    p = Path(path or "/Volumes").expanduser()
    if not p.is_dir():
        p = Path("/Volumes")
    dirs = []
    try:
        for c in sorted(p.iterdir(), key=lambda c: c.name.lower()):
            if c.name.startswith(".") or not c.is_dir():
                continue
            dirs.append(c.name)
    except PermissionError:
        pass
    return {"path": str(p), "parent": str(p.parent) if p != p.parent else None, "dirs": dirs}


def choose_folder_native(start: str | None = None) -> str | None:
    """Pop the native macOS folder chooser on this Mac. Returns None if cancelled."""
    script = 'POSIX path of (choose folder with prompt "Save NDI recordings to:"'
    if start and Path(start).is_dir():
        quoted = start.replace("\\", "\\\\").replace('"', '\\"')
        script += f' default location (POSIX file "{quoted}")'
    script += ")"
    r = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if r.returncode != 0:
        return None
    return r.stdout.strip().rstrip("/") or "/"

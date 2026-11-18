"""Web app: discover NDI cameras, preview + zoom them, record them all to a chosen folder."""

from __future__ import annotations

import argparse
import json
import subprocess
import threading
import time
import webbrowser
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from . import storage
from .ndi import Finder, NDISource, Receiver
from .recorder import Session, find_recorder

STATIC = Path(__file__).parent / "static"
SETTINGS_PATH = Path.home() / "Library" / "Application Support" / "ndi-recorder" / "settings.json"
DEFAULT_SETTINGS = {
    "folder": "",
    "extra_ips": "",  # comma-separated camera IPs, if mDNS discovery can't see them
    "labels": {},  # NDI name -> friendly label (also used as the file name)
    "deselected": [],  # NDI names not to record
    "hidden": [],  # NDI names not shown at all
    "previews": True,
    "proxy": True,  # also write NDI's low-res .preview proxy files
}


class Settings:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.data = dict(DEFAULT_SETTINGS)
        try:
            self.data.update(json.loads(SETTINGS_PATH.read_text()))
        except (OSError, ValueError):
            pass

    def update(self, changes: dict) -> dict:
        with self.lock:
            for k, v in changes.items():
                if k in DEFAULT_SETTINGS:
                    self.data[k] = v
            SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_PATH.write_text(json.dumps(self.data, indent=2))
            return dict(self.data)

    def __getitem__(self, k):
        return self.data[k]


settings = Settings()
finder = Finder(settings["extra_ips"])
receivers: dict[str, Receiver] = {}
last_seen: dict[str, float] = {}
known: dict[str, NDISource] = {}
session: Session | None = None
session_lock = threading.Lock()
RECORDER_EXE = find_recorder()


def _sync_receivers() -> None:
    """Keep one low-bandwidth receiver per visible source (preview + PTZ)."""
    while True:
        now = time.monotonic()
        for src in finder.sources:
            last_seen[src.name] = now
            known[src.name] = src
            if src.name in settings["hidden"]:
                continue
            r = receivers.get(src.name)
            if r and r.source.url != src.url:  # camera got a new address
                r.close()
                r = None
            if r is None:
                try:
                    receivers[src.name] = Receiver(src, preview=settings["previews"])
                except RuntimeError:
                    pass
            else:
                r.set_preview(settings["previews"])
        for name in list(receivers):
            if name in settings["hidden"] or now - last_seen.get(name, 0) > 15:
                receivers.pop(name).close()
        time.sleep(1)


@asynccontextmanager
async def lifespan(app: FastAPI):
    threading.Thread(target=_sync_receivers, daemon=True, name="receivers").start()
    yield
    if session and session.active:
        await run_in_threadpool(session.stop, "server shut down")


app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


def _camera_list() -> list[dict]:
    now = time.monotonic()
    names = set(known) | set(settings["labels"])
    cams = []
    for name in sorted(names):
        src = known.get(name)
        r = receivers.get(name)
        online = now - last_seen.get(name, -1e9) < 5
        if not src and not online and name not in settings["labels"]:
            continue
        cams.append(
            {
                "name": name,
                "url": src.url if src else None,
                "host": src.host if src else None,
                "web": f"http://{src.host}/" if src and src.host and not src.host.startswith("127.") else None,
                "label": settings["labels"].get(name, ""),
                "selected": name not in settings["deselected"],
                "hidden": name in settings["hidden"],
                "online": online,
                "connected": bool(r and r.connected),
                "ptz": bool(r and r.ptz_supported()),
                "resolution": r.resolution if r else None,
                "frame_rate": r.frame_rate if r else None,
                "preview_age": (now - r.jpeg_time) if r and r.jpeg else None,
            }
        )
    return cams


@app.get("/api/state")
def state():
    return {
        "recorder": RECORDER_EXE,
        "settings": settings.data,
        "folder": storage.folder_info(settings["folder"]),
        "cameras": _camera_list(),
        "session": session.to_dict() if session else None,
    }


@app.get("/api/camera/preview.jpg")
def preview(source: str):
    r = receivers.get(source)
    if not r or not r.jpeg:
        raise HTTPException(404, "no preview yet")
    return Response(r.jpeg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


def _receiver(source: str) -> Receiver:
    r = receivers.get(source)
    if not r:
        raise HTTPException(404, f"camera not connected: {source}")
    return r


@app.post("/api/camera/zoom")
def zoom(source: str = Query(...), speed: float | None = Body(None, embed=True), value: float | None = Body(None, embed=True)):
    r = _receiver(source)
    ok = r.zoom_speed(speed) if speed is not None else r.zoom_to(value or 0.0)
    if not ok:
        raise HTTPException(409, "camera did not accept the PTZ command")
    return {"ok": True}


@app.post("/api/camera/preset")
def preset(source: str = Query(...), n: int = Body(..., embed=True), store: bool = Body(False, embed=True)):
    r = _receiver(source)
    ok = r.store_preset(n) if store else r.recall_preset(n)
    if not ok:
        raise HTTPException(409, "camera did not accept the preset command")
    return {"ok": True}


@app.patch("/api/settings")
def patch_settings(changes: dict = Body(...)):
    if "folder" in changes and session and session.active:
        raise HTTPException(409, "can't change folder while recording")
    data = settings.update(changes)
    finder.set_extra_ips(data["extra_ips"])
    return data


@app.post("/api/camera/settings")
def camera_settings(source: str = Query(...), changes: dict = Body(...)):
    labels = dict(settings["labels"])
    deselected = set(settings["deselected"])
    hidden = set(settings["hidden"])
    if "label" in changes:
        label = (changes["label"] or "").strip()
        if label:
            labels[source] = label
        else:
            labels.pop(source, None)
    if "selected" in changes:
        (deselected.discard if changes["selected"] else deselected.add)(source)
    if "hidden" in changes:
        (hidden.add if changes["hidden"] else hidden.discard)(source)
    return settings.update({"labels": labels, "deselected": sorted(deselected), "hidden": sorted(hidden)})


@app.get("/api/volumes")
def volumes():
    return storage.volumes()


@app.get("/api/fs/list")
def fs_list(path: str = ""):
    return storage.list_dirs(path)


@app.post("/api/fs/mkdir")
def fs_mkdir(path: str = Body(..., embed=True), name: str = Body(..., embed=True)):
    name = name.strip()
    if not name or "/" in name or name.startswith("."):
        raise HTTPException(400, "invalid folder name")
    target = Path(path) / name
    try:
        target.mkdir()
    except OSError as e:
        raise HTTPException(400, str(e))
    return {"path": str(target)}


@app.post("/api/fs/choose")
async def fs_choose():
    chosen = await run_in_threadpool(storage.choose_folder_native, settings["folder"] or "/Volumes")
    return {"path": chosen}


@app.post("/api/fs/reveal")
def fs_reveal(path: str = Body(..., embed=True)):
    if Path(path).exists():
        subprocess.run(["open", path])
    return {"ok": True}


@app.post("/api/record/start")
def record_start(name: str = Body("", embed=True)):
    global session
    with session_lock:
        if session and session.active:
            raise HTTPException(409, "already recording")
        if not RECORDER_EXE:
            raise HTTPException(500, "NDI recorder (Application.Mac.NDI.Recording) not found — install NDI Tools")
        info = storage.folder_info(settings["folder"])
        if not info["exists"] or not info["writable"]:
            raise HTTPException(400, "Choose a writable recording folder first. " + " ".join(info["warnings"]))
        cams = [c for c in _camera_list() if c["selected"] and not c["hidden"] and c["online"]]
        if not cams:
            raise HTTPException(400, "No online cameras selected")
        sources = [(known[c["name"]], c["label"]) for c in cams]
        try:
            session = Session(RECORDER_EXE, settings["folder"], sources, name=name, proxy=settings["proxy"])
        except OSError as e:
            raise HTTPException(400, f"Could not create session folder: {e}")
        session.start()
    return session.to_dict()


@app.post("/api/record/stop")
def record_stop():
    if not session or not session.active:
        raise HTTPException(409, "not recording")
    session.stop()
    return session.to_dict()


def main() -> None:
    import uvicorn

    ap = argparse.ArgumentParser(description="NDI multi-camera recorder")
    ap.add_argument("--host", default="127.0.0.1", help="use 0.0.0.0 to allow control from other devices (no auth!)")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    if not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(f"http://127.0.0.1:{args.port}/",)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

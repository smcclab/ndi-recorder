# NDI Recorder

A small local web app for recording several NDI cameras at once (built for three
BirdDog Maki Live cameras). It discovers NDI sources on the network, shows a live
preview of each, controls zoom/presets over NDI PTZ, links to each camera's web
page, and records every selected camera to its own file on a drive you choose.

## Run

```sh
uv run ndi-recorder          # opens http://127.0.0.1:8765/
```

Requires **NDI Tools for Mac** (provides `libndi.dylib` and the
`Application.Mac.NDI.Recording` command-line recorder in `/usr/local/bin`).
Override locations with `NDI_LIB=…` / `NDI_RECORD=…` if needed.

`--host 0.0.0.0` lets you control it from another device (e.g. a tablet), but there
is **no authentication** and the UI can browse folders on this Mac — only do that on
a trusted network.

## How recording works

* Each camera is recorded by NDI's own recorder process. It writes the camera's
  compressed NDI stream straight into a `.mov` — **no re-encoding**, so CPU stays low
  and quality is exactly what the camera sent.
* All recorders connect first (`-noautostart`), then `<start/>` is sent to all at
  once. Every file carries the camera's NDI timecode; in testing all three Maki
  Lives started on the identical timecode.
* Stopping sends `<exit/>` so files are finalised properly. Recording auto-stops
  cleanly if free space drops below 5 GB or the drive disappears.
* Each take goes in `<folder>/<YYYY-MM-DD_HHMMSS> <take name>/`, with one
  `<label>.mov` per camera and a `session.json` (cameras, frame counts, start
  timecodes, stop reason).

### What you get on disk

| File | What it is |
|---|---|
| `Cam A.mov` | Full-quality video (SpeedHQ, 1080p30 from the Maki Live) + 48 kHz float audio |
| `Cam A.mov.preview` | Low-res proxy (turn off in Settings) |
| `*.ndi` | NDI sidecar index files — keep them next to the .mov |

**Data rate:** the Maki Lives send full-bandwidth NDI at ~107 Mbit/s, i.e. **~48 GB
per camera per hour, ~145 GB/hour for three** (~42 MB/s sustained). Fine for a USB 3
HDD or SSD. Avoid FAT32-formatted drives (4 GB file limit) — the app warns about it.

**Editing:** SpeedHQ plays in NDI Tools and ffmpeg/VLC. For editors without NDI
codec support, transcode, e.g.

```sh
ffmpeg -i "Cam A.mov" -c:v prores_ks -profile:v 1 -c:a pcm_s16le "Cam A ProRes.mov"
```

## Zoom

Zoom and presets use NDI PTZ control (the Maki Lives report PTZ support over NDI —
no VISCA setup needed). Hold **− Wide / Tele +**; the slider sets zoom speed.
Presets: click to recall; tick *save* then click a number to store the current shot.

## Settings

Stored in `~/Library/Application Support/ndi-recorder/settings.json`: recording
folder, camera labels (used as file names), which cameras to record, hidden sources,
preview/proxy toggles, and extra camera IPs for networks where mDNS discovery fails.

## Layout

```
ndi_recorder/ndi.py       ctypes bindings: discovery, preview receiver, PTZ
ndi_recorder/recorder.py  one Application.Mac.NDI.Recording process per camera
ndi_recorder/storage.py   volumes, folder checks, native folder chooser
ndi_recorder/app.py       FastAPI server + JSON API
ndi_recorder/static/      the web UI (vanilla JS, no build step)
```

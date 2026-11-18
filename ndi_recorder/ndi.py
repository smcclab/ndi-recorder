"""Minimal ctypes bindings to the NDI runtime (libndi.dylib) installed by NDI Tools.

Only what we need: source discovery (find), a low-bandwidth receiver for preview
snapshots, and NDI PTZ zoom control.
"""

from __future__ import annotations

import ctypes as C
import os
import threading
import time
from dataclasses import dataclass

LIB_CANDIDATES = [
    os.environ.get("NDI_LIB", ""),
    "/usr/local/lib/libndi.dylib",
    "/Library/NDI SDK for Apple/lib/macOS/libndi.dylib",
    "/Library/NDI Advanced SDK for Apple/lib/macOS/libndi_advanced.dylib",
]


def _load() -> C.CDLL:
    for path in LIB_CANDIDATES:
        if path and os.path.exists(path):
            return C.CDLL(path)
    raise OSError("libndi.dylib not found — install NDI Tools (https://ndi.video/tools/) or set NDI_LIB")


lib = _load()


class FindCreate(C.Structure):
    _fields_ = [("show_local_sources", C.c_bool), ("p_groups", C.c_char_p), ("p_extra_ips", C.c_char_p)]


class Source(C.Structure):
    _fields_ = [("p_ndi_name", C.c_char_p), ("p_url_address", C.c_char_p)]


class RecvCreateV3(C.Structure):
    _fields_ = [
        ("source_to_connect_to", Source),
        ("color_format", C.c_int),
        ("bandwidth", C.c_int),
        ("allow_video_fields", C.c_bool),
        ("p_ndi_recv_name", C.c_char_p),
    ]


class VideoFrameV2(C.Structure):
    _fields_ = [
        ("xres", C.c_int),
        ("yres", C.c_int),
        ("FourCC", C.c_uint32),
        ("frame_rate_N", C.c_int),
        ("frame_rate_D", C.c_int),
        ("picture_aspect_ratio", C.c_float),
        ("frame_format_type", C.c_int),
        ("timecode", C.c_int64),
        ("p_data", C.POINTER(C.c_uint8)),
        ("line_stride_in_bytes", C.c_int),
        ("p_metadata", C.c_char_p),
        ("timestamp", C.c_int64),
    ]


COLOR_BGRX_BGRA = 0
BANDWIDTH_METADATA_ONLY = -10
BANDWIDTH_LOWEST = 0
FRAME_VIDEO = 1
FRAME_ERROR = 4

lib.NDIlib_initialize.restype = C.c_bool
lib.NDIlib_find_create_v2.argtypes = [C.POINTER(FindCreate)]
lib.NDIlib_find_create_v2.restype = C.c_void_p
lib.NDIlib_find_wait_for_sources.argtypes = [C.c_void_p, C.c_uint32]
lib.NDIlib_find_wait_for_sources.restype = C.c_bool
lib.NDIlib_find_get_current_sources.argtypes = [C.c_void_p, C.POINTER(C.c_uint32)]
lib.NDIlib_find_get_current_sources.restype = C.POINTER(Source)
lib.NDIlib_find_destroy.argtypes = [C.c_void_p]

lib.NDIlib_recv_create_v3.argtypes = [C.POINTER(RecvCreateV3)]
lib.NDIlib_recv_create_v3.restype = C.c_void_p
lib.NDIlib_recv_destroy.argtypes = [C.c_void_p]
lib.NDIlib_recv_capture_v2.argtypes = [C.c_void_p, C.POINTER(VideoFrameV2), C.c_void_p, C.c_void_p, C.c_uint32]
lib.NDIlib_recv_capture_v2.restype = C.c_int
lib.NDIlib_recv_free_video_v2.argtypes = [C.c_void_p, C.POINTER(VideoFrameV2)]
lib.NDIlib_recv_get_no_connections.argtypes = [C.c_void_p]
lib.NDIlib_recv_get_no_connections.restype = C.c_int
lib.NDIlib_recv_ptz_is_supported.argtypes = [C.c_void_p]
lib.NDIlib_recv_ptz_is_supported.restype = C.c_bool
lib.NDIlib_recv_ptz_zoom_speed.argtypes = [C.c_void_p, C.c_float]
lib.NDIlib_recv_ptz_zoom_speed.restype = C.c_bool
lib.NDIlib_recv_ptz_zoom.argtypes = [C.c_void_p, C.c_float]
lib.NDIlib_recv_ptz_zoom.restype = C.c_bool
lib.NDIlib_recv_ptz_recall_preset.argtypes = [C.c_void_p, C.c_int, C.c_float]
lib.NDIlib_recv_ptz_recall_preset.restype = C.c_bool
lib.NDIlib_recv_ptz_store_preset.argtypes = [C.c_void_p, C.c_int]
lib.NDIlib_recv_ptz_store_preset.restype = C.c_bool

if not lib.NDIlib_initialize():
    raise OSError("NDIlib_initialize failed (unsupported CPU?)")


@dataclass(frozen=True)
class NDISource:
    name: str
    url: str  # usually "ip:port"

    @property
    def host(self) -> str:
        return self.url.rsplit(":", 1)[0] if ":" in self.url else self.url


class Finder:
    """Background NDI discovery. `sources` always holds the latest list."""

    def __init__(self, extra_ips: str = ""):
        self._lock = threading.Lock()
        self._sources: list[NDISource] = []
        self._extra_ips = extra_ips
        self._restart = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="ndi-find").start()

    def set_extra_ips(self, extra_ips: str) -> None:
        if extra_ips != self._extra_ips:
            self._extra_ips = extra_ips
            self._restart.set()

    @property
    def sources(self) -> list[NDISource]:
        with self._lock:
            return list(self._sources)

    def _run(self) -> None:
        while True:
            self._restart.clear()
            ips = self._extra_ips.encode() if self._extra_ips else None
            cfg = FindCreate(True, None, ips)
            inst = lib.NDIlib_find_create_v2(C.byref(cfg))
            try:
                while not self._restart.is_set():
                    lib.NDIlib_find_wait_for_sources(inst, 1000)
                    n = C.c_uint32(0)
                    arr = lib.NDIlib_find_get_current_sources(inst, C.byref(n))
                    found = [
                        NDISource(arr[i].p_ndi_name.decode(), (arr[i].p_url_address or b"").decode())
                        for i in range(n.value)
                    ]
                    with self._lock:
                        self._sources = sorted(found, key=lambda s: s.name)
            finally:
                lib.NDIlib_find_destroy(inst)


class Receiver:
    """A low-bandwidth NDI receiver for one source: keeps a JPEG preview and exposes PTZ.

    With previews off it connects metadata-only, which still allows PTZ control but
    costs (almost) no bandwidth or decode CPU.
    """

    PREVIEW_WIDTH = 480
    PREVIEW_INTERVAL = 0.5

    def __init__(self, source: NDISource, preview: bool = True):
        self.source = source
        self.preview = preview
        self.jpeg: bytes | None = None
        self.jpeg_time = 0.0
        self.resolution: str | None = None
        self.frame_rate: str | None = None
        self._inst_lock = threading.Lock()
        self._inst = self._create()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"ndi-recv-{source.name}")
        self._thread.start()

    def _create(self) -> int:
        cfg = RecvCreateV3(
            Source(self.source.name.encode(), self.source.url.encode() or None),
            COLOR_BGRX_BGRA,
            BANDWIDTH_LOWEST if self.preview else BANDWIDTH_METADATA_ONLY,
            False,
            b"NDI Recorder preview",
        )
        inst = lib.NDIlib_recv_create_v3(C.byref(cfg))
        if not inst:
            raise RuntimeError(f"could not create NDI receiver for {self.source.name}")
        return inst

    def set_preview(self, on: bool) -> None:
        if on == self.preview:
            return
        with self._inst_lock:
            lib.NDIlib_recv_destroy(self._inst)
            self.preview = on
            self._inst = self._create()
            if not on:
                self.jpeg = None

    def close(self) -> None:
        self._stop.set()
        self._thread.join(timeout=3)
        with self._inst_lock:
            lib.NDIlib_recv_destroy(self._inst)

    @property
    def connected(self) -> bool:
        with self._inst_lock:
            return lib.NDIlib_recv_get_no_connections(self._inst) > 0

    # --- PTZ (returns False if camera doesn't support NDI PTZ) ---
    def ptz_supported(self) -> bool:
        with self._inst_lock:
            return bool(lib.NDIlib_recv_ptz_is_supported(self._inst))

    def zoom_speed(self, speed: float) -> bool:
        """speed in [-1, 1]: -1 = zoom out fast, 0 = stop, 1 = zoom in fast."""
        with self._inst_lock:
            return bool(lib.NDIlib_recv_ptz_zoom_speed(self._inst, C.c_float(max(-1.0, min(1.0, speed)))))

    def zoom_to(self, value: float) -> bool:
        """value in [0, 1]: 0 = wide, 1 = tele."""
        with self._inst_lock:
            return bool(lib.NDIlib_recv_ptz_zoom(self._inst, C.c_float(max(0.0, min(1.0, value)))))

    def recall_preset(self, n: int, speed: float = 1.0) -> bool:
        with self._inst_lock:
            return bool(lib.NDIlib_recv_ptz_recall_preset(self._inst, n, C.c_float(speed)))

    def store_preset(self, n: int) -> bool:
        with self._inst_lock:
            return bool(lib.NDIlib_recv_ptz_store_preset(self._inst, n))

    def _run(self) -> None:
        from PIL import Image  # imported lazily so discovery works without Pillow

        frame = VideoFrameV2()
        while not self._stop.is_set():
            with self._inst_lock:
                inst = self._inst
                ftype = lib.NDIlib_recv_capture_v2(inst, C.byref(frame), None, None, 0)
                if ftype == FRAME_VIDEO:
                    try:
                        now = time.monotonic()
                        self.resolution = f"{frame.xres}×{frame.yres}"
                        if frame.frame_rate_D:
                            self.frame_rate = f"{frame.frame_rate_N / frame.frame_rate_D:.2f}".rstrip("0").rstrip(".")
                        if self.preview and now - self.jpeg_time >= self.PREVIEW_INTERVAL:
                            size = frame.line_stride_in_bytes * frame.yres
                            buf = C.string_at(frame.p_data, size)
                            img = Image.frombuffer(
                                "RGB", (frame.xres, frame.yres), buf, "raw", "BGRX", frame.line_stride_in_bytes, 1
                            )
                            img.thumbnail((self.PREVIEW_WIDTH, self.PREVIEW_WIDTH))
                            import io

                            out = io.BytesIO()
                            img.save(out, "JPEG", quality=70)
                            self.jpeg = out.getvalue()
                            self.jpeg_time = now
                    finally:
                        lib.NDIlib_recv_free_video_v2(inst, C.byref(frame))
            if ftype != FRAME_VIDEO:
                # Nothing queued: wait a bit outside the lock so PTZ calls aren't blocked.
                time.sleep(0.02)

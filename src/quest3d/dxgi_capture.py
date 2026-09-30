"""Explicit standalone DXGI CPU→CUDA candidate; never selected by the product CLI.

The pinned DLL returns actual LastPresentTime. No repeated/timeout image receives
a fabricated heartbeat. experimental_dxgi=True selects the documented fixed HDR
fused policy; Windows display state and other backends are never changed.
"""
from __future__ import annotations

import copy
import ctypes as C
from dataclasses import asdict, dataclass
import hashlib
from functools import lru_cache
import math
import secrets
import sys
import threading
from time import perf_counter_ns

from .capture import (CapturedGPUFrame, DesktopCapture, _require_physical_pixels,
                      list_monitors)
from .display_color import read_display_colors, require_hdr_color
from .geometry import ScreenRect, SourceGeometry, validate_roi
from .paths import ROOT
from .source_identity import SourceIdentity, SourceKind

DLL_PATH = ROOT / "artifacts/host/dxgi_snapshot_capture.dll"
DLL_SHA256 = "466468a0932d3f8cce1ab4138d3ea83fcec173d3b39bf49e2c22fcd0c2464e09"
TIMESTAMP_KIND = "dxgi_last_present_qpc_ns_verified"
MAX_CAPTURE_AGE_NS = 500_000_000


class NativeSource(C.Structure):
    _fields_ = [("version", C.c_uint32), ("size", C.c_uint32), ("hmonitor", C.c_uint64),
                ("adapter_luid", C.c_uint64), ("left", C.c_int32), ("top", C.c_int32),
                ("width", C.c_uint32), ("height", C.c_uint32), ("rotation", C.c_uint32),
                ("reserved", C.c_uint32), ("device_name", C.c_uint16 * 32)]


class NativeFrame(C.Structure):
    _fields_ = [("version", C.c_uint32), ("size", C.c_uint32), ("source", NativeSource)] + [
        (name, C.c_uint32) for name in ("format", "row_bytes", "rows", "bytes_per_pixel")
    ] + [(name, C.c_uint64) for name in ("copied_bytes", "frame_id", "last_present_qpc",
          "acquire_started_qpc", "acquire_returned_qpc", "map_completed_qpc",
          "copy_completed_qpc", "qpc_frequency", "last_mouse_qpc")] + [
        ("accumulated_frames", C.c_uint32), ("protected_content_masked", C.c_uint32),
        ("reserved", C.c_uint64)]


class NativeError(C.Structure):
    _fields_ = [("version", C.c_uint32), ("size", C.c_uint32), ("status", C.c_int32),
                ("hresult", C.c_int32), ("required_capacity", C.c_uint64),
                ("stage", C.c_uint32), ("reserved", C.c_uint32)]


def _load_native():
    if sys.platform != "win32" or C.sizeof(C.c_void_p) != 8:
        raise RuntimeError("DXGI candidate requires Windows x64")
    if tuple(map(C.sizeof, (NativeSource, NativeFrame, NativeError))) != (112, 224, 32):
        raise RuntimeError("DXGI candidate ABI sizes differ")
    if hashlib.sha256(DLL_PATH.read_bytes()).hexdigest() != DLL_SHA256:
        raise RuntimeError("DXGI candidate DLL SHA256 differs from the explicitly verified binary")
    return _load_verified_native(str(DLL_PATH.resolve()))


@lru_cache(maxsize=1)
def _load_verified_native(path):
    # Keep exactly one process-lifetime DLL reference. Reopening a capture must
    # not accumulate LoadLibrary references; each public load still checks hash.
    dll = C.CDLL(path)
    # Recheck after load as a local packaging consistency check. This is not a
    # trust boundary against arbitrary code already running inside this process.
    if hashlib.sha256(DLL_PATH.read_bytes()).hexdigest() != DLL_SHA256:
        raise RuntimeError("DXGI candidate DLL changed while loading")
    dll.q3d_dxgi_open.argtypes = [C.c_uint64, C.POINTER(C.c_uint64), C.POINTER(NativeSource),
                                 C.c_uint32, C.POINTER(NativeError)]
    dll.q3d_dxgi_open.restype = C.c_int32
    dll.q3d_dxgi_snapshot.argtypes = [C.c_uint64, C.c_void_p, C.c_uint64, C.c_uint32, C.c_uint32,
                                     C.POINTER(NativeFrame), C.c_uint32, C.POINTER(NativeError)]
    dll.q3d_dxgi_snapshot.restype = C.c_int32
    dll.q3d_dxgi_close.argtypes = [C.c_uint64, C.POINTER(NativeError)]
    dll.q3d_dxgi_close.restype = C.c_int32
    return dll


def _qpc_api():
    api = C.WinDLL("kernel32", use_last_error=True)
    api.QueryPerformanceCounter.argtypes = api.QueryPerformanceFrequency.argtypes = [C.POINTER(C.c_int64)]
    api.QueryPerformanceCounter.restype = api.QueryPerformanceFrequency.restype = C.c_int
    return api


def _ticks_to_ns(ticks, frequency):
    if type(ticks) is not int or type(frequency) is not int or ticks < 0 or frequency <= 0:
        raise RuntimeError("Invalid native QPC domain")
    return ticks * 1_000_000_000 // frequency


def _verify_qpc_domain(samples=8):
    api = _qpc_api()
    frequency = C.c_int64()
    if not api.QueryPerformanceFrequency(C.byref(frequency)) or frequency.value <= 0:
        raise RuntimeError("Cannot read QPC frequency")
    envelopes = []
    for _ in range(samples):
        ticks = C.c_int64()
        before = perf_counter_ns()
        okay = api.QueryPerformanceCounter(C.byref(ticks))
        after = perf_counter_ns()
        sample = _ticks_to_ns(ticks.value, frequency.value)
        if not okay or not before <= sample <= after or after - before > 20_000_000:
            raise RuntimeError("Native QPC and Python perf_counter_ns do not share a verified clock envelope")
        envelopes.append((before, sample, after))
    return frequency.value, tuple(envelopes)


def _color_for(device_name):
    colors = read_display_colors()
    matches = [c for c in colors if c.device_name.casefold() == device_name.casefold()]
    if len(matches) != 1:
        raise RuntimeError("DXGI requires one unambiguous monitor color profile")
    color = matches[0]
    if color.query_errors:
        raise RuntimeError("DXGI color metadata is incomplete")
    if color.active_mode == "hdr":
        return copy.deepcopy(require_hdr_color(device_name, colors))
    if (color.active_mode != "sdr" or color.hdr_enabled is not False
            or color.bits_per_channel != 8 or color.sc_rgb_sdr_white_scale != 1.0):
        raise RuntimeError("DXGI supports verified HDR scRGB or 8-bit SDR metadata only")
    return copy.deepcopy(color)


def _native_name(source):
    raw = bytes(source.device_name)
    name = raw.decode("utf-16-le").split("\0", 1)[0]
    if not name:
        raise RuntimeError("Native source name is empty")
    return name


@dataclass(frozen=True, slots=True)
class DXGINativeMetadata:
    frame_id: int
    format: int
    row_bytes: int
    copied_bytes: int
    qpc_frequency: int
    last_present_qpc: int
    acquire_started_qpc: int
    acquire_returned_qpc: int
    map_completed_qpc: int
    copy_completed_qpc: int
    adapter_luid: int


@dataclass(frozen=True, slots=True)
class DXGIMetrics:
    native_api_ms: float
    native_acquire_ms: float
    native_readback_ms: float
    native_cpu_copy_ms: float
    cuda_upload_wall_ms: float
    cuda_upload_event_ms: float
    tone_map_ms: float
    total_grab_ms: float
    capture_age_ms: float
    pinned_cpu_buffer_bytes: int
    cuda_allocated_bytes: int
    cuda_reserved_bytes: int


@dataclass(frozen=True, slots=True)
class DXGICapturedGPUFrame(CapturedGPUFrame):
    timestamp_kind: str = TIMESTAMP_KIND
    native_metadata: DXGINativeMetadata | None = None
    metrics: DXGIMetrics | None = None


class DXGISnapshotCapture:
    """One-thread, single-use, explicitly selected monitor capture candidate.

    ``experimental_dxgi=True`` selects this backend and its fixed verified HDR
    fused policy. No implicit Windows HDR changes or other backend fallback.
    A timeout/stale frame is retryable without pixels. Geometry/color/protocol
    errors terminate this object until close; construct a new source scope.
    """
    backend = "dxgi-snapshot-cpu-cuda-candidate-v1"
    timestamp_kind = TIMESTAMP_KIND
    cursor_exclusion_supported = False
    cursor_exclusion_verified = False

    def __init__(self, *, monitor=1, device=0, experimental_dxgi=False, rect=None):
        if type(monitor) is not int or monitor <= 0:
            raise ValueError("DXGI requires one positive monitor index")
        if type(device) is not int or device < 0:
            raise ValueError("CUDA device must be a nonnegative integer")
        if type(experimental_dxgi) is not bool:
            raise ValueError("experimental_dxgi must be boolean")
        if rect is not None and not isinstance(rect, ScreenRect):
            raise TypeError("rect must be a ScreenRect")
        self._monitor, self.device, self.experimental_dxgi, self._rect = monitor, device, experimental_dxgi, rect
        self._dll = self._owner = self._host_buffer = self._tone_mapper = None
        self._handle = C.c_uint64()
        self._source = NativeSource()
        self._geometry = self._identity = self._color = None
        self._failure = None
        self._used = False
        self._format = None
        self._frame_id = 0
        self._last_present = 0
        self.color_profile = None
        self.last_metrics = None
        self.qpc_envelopes = ()

    @property
    def source_id(self):
        if self._rect is not None:
            r = self._rect
            return f"desktop:rect:{r.left},{r.top},{r.width},{r.height}"
        return f"desktop:monitor:{self._monitor}"

    @property
    def source_geometry(self):
        if self._geometry is None or not self._handle.value:
            raise RuntimeError("DXGI capture is not open")
        return self._geometry

    @property
    def geometry(self):
        return self.source_geometry

    def _check_open(self):
        if self._owner is not threading.current_thread():
            raise RuntimeError("DXGI grab must run on its opening thread")
        if not self._handle.value:
            raise RuntimeError("DXGI capture is not open")
        if self._failure:
            raise RuntimeError(f"DXGI capture scope failed; close and recreate: {self._failure}")

    def _validate_scope(self):
        _require_physical_pixels()
        monitors = list_monitors()
        if DesktopCapture._topology_key(monitors) != self._topology:
            raise RuntimeError("DXGI display topology changed; recreate source")
        if _color_for(self._monitor_info.device_name) != self._color:
            raise RuntimeError("DXGI color/SDR white changed; recreate source")

    def __enter__(self):
        if not self.experimental_dxgi:
            raise RuntimeError("DXGI candidate requires explicit experimental_dxgi=True")
        if self._used:
            raise RuntimeError("DXGI objects are single-use; create a new source scope")
        self._used = True
        import torch
        if torch.__version__ != "2.7.1+cu126" or not torch.cuda.is_available() or self.device >= torch.cuda.device_count():
            raise RuntimeError("DXGI CUDA candidate requires verified Torch2.7.1+cu126 and an available device")
        self._owner = threading.current_thread()
        try:
            self.dpi_status = _require_physical_pixels()
            monitors = list_monitors()
            if self._monitor >= len(monitors):
                raise ValueError("Selected DXGI monitor does not exist")
            self._monitor_info = monitors[self._monitor]
            bounds = self._monitor_info.bounds
            if not self._monitor_info.native_handle:
                raise RuntimeError("Selected monitor has no authoritative HMONITOR")
            if self._rect is not None:
                validate_roi(self._rect, bounds)
            self._topology = DesktopCapture._topology_key(monitors)
            self._geometry = SourceGeometry(self._rect or bounds, 0)
            self._identity = SourceIdentity(SourceKind.MONITOR, 0, secrets.randbits(64) or 1,
                                            self._monitor_info.native_handle, 0, bounds)
            self._color = _color_for(self._monitor_info.device_name)
            self._frequency, self.qpc_envelopes = _verify_qpc_domain()
            self._dll = _load_native()
            error = NativeError()
            status = self._dll.q3d_dxgi_open(self._identity.native_handle, C.byref(self._handle),
                                            C.byref(self._source), C.sizeof(self._source), C.byref(error))
            if status != 0:
                raise RuntimeError(f"DXGI open refused status={status}, stage={error.stage}, hr={error.hresult & 0xffffffff:08x}")
            if (self._source.version != 1 or self._source.size != 112 or self._source.reserved
                    or self._source.hmonitor != self._identity.native_handle or self._source.rotation != 1
                    or ScreenRect(self._source.left, self._source.top, self._source.width, self._source.height) != bounds
                    or _native_name(self._source).casefold() != self._monitor_info.device_name.casefold()):
                raise RuntimeError("DXGI source metadata differs from selected physical monitor")
            self._source_bytes = bytes(self._source)
            self._capacity = bounds.width * bounds.height * 8
            # DLL writes directly into reusable pinned host storage; no extra
            # pageable NumPy→pinned copy. Upload completion precedes any reuse.
            self._host_buffer = torch.empty(self._capacity, dtype=torch.uint8, pin_memory=True)
            self._upload_start = torch.cuda.Event(enable_timing=True)
            self._upload_end = torch.cuda.Event(enable_timing=True)
            if self._color.hdr_enabled:
                from .tonemap_cuda import CudaToneMapper
                self._tone_mapper = CudaToneMapper(device=self.device)
            self.color_profile = {"policy": "experimental-scrgb-sdr-shoulder-v1" if self._color.hdr_enabled else "verified-sdr-channel-copy",
                                  "knee": .75 if self._color.hdr_enabled else None, "display": asdict(self._color),
                                  "implementation": "fused" if self._color.hdr_enabled else "channel-copy",
                                  "visually_verified": False, "native_format": None, "output_format": "bgra8-srgb"}
            self._validate_scope()
            return self
        except BaseException:
            self.close()
            raise

    def _validate_frame(self, f, started, returned):
        if (f.version != 1 or f.size != 224 or f.reserved or bytes(f.source) != self._source_bytes
                or f.frame_id != self._frame_id + 1 or f.qpc_frequency != self._frequency
                or f.protected_content_masked or f.format not in (10, 87, 88, 28)
                or (self._format is not None and f.format != self._format)):
            raise RuntimeError("DXGI immutable frame/source/format metadata mismatch")
        bpp = 8 if f.format == 10 else 4
        if (f.bytes_per_pixel != bpp or f.row_bytes != f.source.width * bpp or f.rows != f.source.height
                or f.copied_bytes != f.row_bytes * f.rows or f.copied_bytes > self._capacity):
            raise RuntimeError("DXGI returned invalid packed frame bytes")
        if ((f.format == 10 and self._color.hdr_enabled is not True)
                or (f.format != 10 and self._color.hdr_enabled is not False)):
            raise RuntimeError("DXGI native pixel format does not match verified HDR/SDR color metadata")
        times = [_ticks_to_ns(int(getattr(f, name)), self._frequency) for name in (
            "last_present_qpc", "acquire_started_qpc", "acquire_returned_qpc", "map_completed_qpc", "copy_completed_qpc")]
        present, acquire_start, acquire_end, mapped, copied = times
        if not (started <= acquire_start <= acquire_end <= mapped <= copied <= returned
                and self._last_present < present <= acquire_end and 0 <= returned - present <= MAX_CAPTURE_AGE_NS):
            raise RuntimeError("DXGI native timestamps are stale or outside the Python call envelope")
        return times

    def _upload_convert(self, f):
        import torch
        raw = self._host_buffer[:f.copied_bytes]
        if f.format == 10:
            raw = raw.view(torch.float16)
        raw = raw.reshape(f.rows, f.source.width, 4)
        with torch.cuda.device(self.device):
            start = perf_counter_ns()
            self._upload_start.record()
            uploaded = raw.to(device=f"cuda:{self.device}", non_blocking=True)
            self._upload_end.record()
            self._upload_end.synchronize()
            upload_ms = (perf_counter_ns() - start) / 1e6
            event_ms = self._upload_start.elapsed_time(self._upload_end)
            if self._rect is not None:
                x, y = self._rect.left - f.source.left, self._rect.top - f.source.top
                uploaded = uploaded[y:y+self._rect.height, x:x+self._rect.width]
            start = perf_counter_ns()
            if f.format == 10:
                output = self._tone_mapper(uploaded, sdr_white_scale=self._color.sc_rgb_sdr_white_scale, knee=.75)
            else:
                output = uploaded[..., [2, 1, 0, 3]].contiguous() if f.format == 28 else uploaded.contiguous()
                output[..., 3] = 255
                torch.cuda.current_stream(self.device).synchronize()
            color_ms = (perf_counter_ns() - start) / 1e6
        return output, upload_ms, event_ms, color_ms

    def grab(self, *, timeout_seconds=None):
        self._check_open()
        timeout = .15 if timeout_seconds is None else timeout_seconds
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 1:
            raise ValueError("DXGI timeout must be finite in (0, 1] seconds")
        timeout_ms = max(1, math.ceil(timeout * 1000))
        total_started = perf_counter_ns()
        try:
            self._validate_scope()
            frame, error = NativeFrame(), NativeError()
            started = perf_counter_ns()
            status = self._dll.q3d_dxgi_snapshot(self._handle.value, self._host_buffer.data_ptr(), self._capacity,
                       timeout_ms, 500, C.byref(frame), C.sizeof(frame), C.byref(error))
            returned = perf_counter_ns()
            if status in (5, 9):
                raise TimeoutError(f"DXGI has no fresh frame (status={status}); no old image returned")
            if status:
                raise RuntimeError(f"DXGI snapshot refused status={status}, stage={error.stage}, hr={error.hresult & 0xffffffff:08x}")
            present, acq_start, acq_end, mapped, copied = self._validate_frame(frame, started, returned)
            # A native success consumes its sequence even if Python conversion
            # later expires. Retrying may return a newer frame, never relabel the
            # expired one or mistake that legitimate skip for source corruption.
            self._frame_id, self._last_present, self._format = frame.frame_id, present, frame.format
            output, upload_ms, event_ms, color_ms = self._upload_convert(frame)
            self._validate_scope()
            completed = perf_counter_ns()
            if not 0 <= completed - present <= MAX_CAPTURE_AGE_NS:
                raise TimeoutError("DXGI frame expired during upload/color validation; no image returned")
            import torch
            if (not output.is_cuda or output.device.index != self.device or output.dtype != torch.uint8
                    or tuple(output.shape) != (self.geometry.bounds.height, self.geometry.bounds.width, 4)
                    or not output.is_contiguous()):
                raise RuntimeError("DXGI CUDA conversion returned invalid owned BGRA")
            metrics = DXGIMetrics((returned-started)/1e6, (acq_end-acq_start)/1e6, (mapped-acq_end)/1e6,
                (copied-mapped)/1e6, upload_ms, event_ms, color_ms, (completed-total_started)/1e6,
                (completed-present)/1e6, self._capacity, torch.cuda.memory_allocated(self.device),
                torch.cuda.memory_reserved(self.device))
            native = DXGINativeMetadata(frame.frame_id, frame.format, frame.row_bytes, frame.copied_bytes,
                frame.qpc_frequency, frame.last_present_qpc, frame.acquire_started_qpc, frame.acquire_returned_qpc,
                frame.map_completed_qpc, frame.copy_completed_qpc, frame.source.adapter_luid)
            self._frame_id, self._last_present, self._format = frame.frame_id, present, frame.format
            self.last_metrics = metrics
            self.color_profile["native_format"] = frame.format
            return DXGICapturedGPUFrame(output, present, self.source_id, frame.frame_id, self.geometry.generation,
                                       self.geometry, color_ms, self._identity, TIMESTAMP_KIND, native, metrics)
        except TimeoutError:
            raise
        except BaseException as exc:
            self._failure = str(exc)
            raise

    def close(self):
        if self._handle.value:
            if self._owner is not threading.current_thread() and self._owner.is_alive():
                raise RuntimeError("DXGI close must run on its live opening thread")
            error = NativeError()
            status = self._dll.q3d_dxgi_close(self._handle.value, C.byref(error))
            if status:
                raise RuntimeError(f"DXGI close refused status={status}; native handle retained for cleanup")
            self._handle.value = 0
        if self._tone_mapper is not None:
            self._tone_mapper.close()
            self._tone_mapper = None
        self._host_buffer = None
        self._upload_start = self._upload_end = None
        self._geometry = None

    def __exit__(self, *_):
        self.close()

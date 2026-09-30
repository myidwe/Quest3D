"""Windows desktop capture using mss CPU or pinned wc_cuda GPU BGRA frames.

captured_ns is host receive time, NOT a hardware presentation/capture timestamp.
This backend captures the visible desktop; it cannot see an occluded HWND and
does not promise cursor exclusion, DRM capture or synchronized audio.
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import asdict, dataclass
import sys
import secrets
import threading
from time import perf_counter_ns
from typing import TYPE_CHECKING, Any

import numpy as np

from .geometry import ScreenRect, SourceGeometry, validate_roi
from .source_identity import SourceIdentity, SourceKind

if TYPE_CHECKING:
    import torch


@dataclass(frozen=True, slots=True)
class CapturedFrame:
    bgra: np.ndarray
    captured_ns: int
    source_id: str
    frame_id: int
    geometry_generation: int
    geometry: SourceGeometry | None = None
    source_identity: SourceIdentity | None = None


@dataclass(frozen=True, slots=True)
class CapturedGPUFrame:
    """Owned CUDA BGRA tensor; receive timestamp, not WGC acquisition time."""

    bgra: torch.Tensor
    captured_ns: int
    source_id: str
    frame_id: int
    geometry_generation: int
    geometry: SourceGeometry
    color_processing_ms: float = 0.0
    source_identity: SourceIdentity | None = None


@dataclass(frozen=True, slots=True)
class _CapturedGPUBuffer:
    """Private native-format buffer, converted only when a consumer takes it."""
    pixels: torch.Tensor
    captured_ns: int
    frame_id: int
    copy_processing_ms: float


@dataclass(frozen=True, slots=True)
class DpiAwarenessStatus:
    active: str
    request_succeeded: bool
    error_code: int = 0


@dataclass(frozen=True, slots=True)
class MonitorInfo:
    index: int
    bounds: ScreenRect
    device_name: str
    is_primary: bool
    native_handle: int = 0


@dataclass(frozen=True, slots=True)
class WindowInfo:
    hwnd: int
    process_id: int
    title: str
    bounds: ScreenRect
    minimized: bool
    cloaked: bool
    dpi: int | None
    bounds_kind: str


class _MONITORINFOEXW(ctypes.Structure):
    # POINTER(Structure) caches its class process-wide. Defining this per
    # enumeration retained one class/pointer pair for every captured frame.
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def _user32() -> Any:
    if sys.platform != "win32":
        raise OSError("this capture provider supports Windows only")
    return ctypes.WinDLL("user32", use_last_error=True)


def enable_per_monitor_dpi_awareness() -> DpiAwarenessStatus:
    """Run before constructing GUI/capture objects; verify the calling context."""
    user32 = _user32()
    user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
    ctypes.set_last_error(0)
    succeeded = bool(user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)))
    error = 0 if succeeded else ctypes.get_last_error()
    user32.GetThreadDpiAwarenessContext.argtypes = []
    user32.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    user32.GetAwarenessFromDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.GetAwarenessFromDpiAwarenessContext.restype = ctypes.c_int
    context = user32.GetThreadDpiAwarenessContext()
    awareness = user32.GetAwarenessFromDpiAwarenessContext(context)
    active = {0: "unaware", 1: "system", 2: "per_monitor"}.get(awareness, "invalid")
    if active == "per_monitor":
        user32.AreDpiAwarenessContextsEqual.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.AreDpiAwarenessContextsEqual.restype = wintypes.BOOL
        if user32.AreDpiAwarenessContextsEqual(context, ctypes.c_void_p(-4)):
            active = "per_monitor_v2"
    return DpiAwarenessStatus(active, succeeded, error)


def _require_physical_pixels() -> DpiAwarenessStatus:
    status = enable_per_monitor_dpi_awareness()
    if status.active not in ("per_monitor", "per_monitor_v2"):
        raise RuntimeError(
            "Physical-pixel capture requires per-monitor DPI awareness. "
            f"Current context={status.active}, Win32 error={status.error_code}. "
            "Start a fresh process and initialize capture before GUI libraries."
        )
    return status


def _rect(value: wintypes.RECT) -> ScreenRect:
    return ScreenRect(value.left, value.top, value.right - value.left, value.bottom - value.top)


def list_monitors() -> list[MonitorInfo]:
    """Index 0 is the virtual bounding rectangle; 1..N are Win32 enumeration order."""
    _require_physical_pixels()
    user32 = _user32()
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]
    user32.GetSystemMetrics.restype = ctypes.c_int
    desktop = ScreenRect(*(user32.GetSystemMetrics(index) for index in (76, 77, 78, 79)))
    result = [MonitorInfo(0, desktop, "virtual-desktop", False)]

    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                     ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(_MONITORINFOEXW)]
    user32.GetMonitorInfoW.restype = wintypes.BOOL
    errors: list[int] = []

    @callback_type
    def callback(handle: int, _dc: Any, rectangle: Any, _param: Any) -> bool:
        info = _MONITORINFOEXW()
        info.cbSize = ctypes.sizeof(info)
        if not user32.GetMonitorInfoW(handle, ctypes.byref(info)):
            errors.append(ctypes.get_last_error())
            return False
        result.append(MonitorInfo(len(result), _rect(rectangle.contents), info.szDevice,
                                  bool(info.dwFlags & 1), int(handle)))
        return True

    user32.EnumDisplayMonitors.argtypes = [wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                         callback_type, wintypes.LPARAM]
    user32.EnumDisplayMonitors.restype = wintypes.BOOL
    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise ctypes.WinError(errors[0] if errors else ctypes.get_last_error())
    return result


def list_windows(*, include_minimized: bool = True, include_cloaked: bool = False) -> list[WindowInfo]:
    """Enumerate visible titled top-level windows, without capturing their pixels.

    Selecting these bounds for mss still captures whatever is visible there.
    Handles/process IDs are observations, not durable IDs for later injection.
    """
    _require_physical_pixels()
    user32 = _user32()
    dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD,
                                        ctypes.c_void_p, wintypes.DWORD]
    dwm.DwmGetWindowAttribute.restype = ctypes.c_long
    for name in ("IsWindowVisible", "IsIconic"):
        function = getattr(user32, name)
        function.argtypes, function.restype = [wintypes.HWND], wintypes.BOOL
    user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetWindowRect.restype = wintypes.BOOL
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    result: list[WindowInfo] = []

    @callback_type
    def callback(hwnd: int, _param: Any) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        minimized = bool(user32.IsIconic(hwnd))
        if minimized and not include_minimized:
            return True
        title_length = user32.GetWindowTextLengthW(hwnd)
        if title_length <= 0:
            return True
        title = ctypes.create_unicode_buffer(title_length + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        cloaked = wintypes.DWORD()
        dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
        if cloaked.value and not include_cloaked:
            return True
        bounds = wintypes.RECT()
        kind = "dwm_extended_frame_bounds"
        if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(bounds), ctypes.sizeof(bounds)) != 0:
            kind = "window_rect"
            if not user32.GetWindowRect(hwnd, ctypes.byref(bounds)):
                return True
        if bounds.right <= bounds.left or bounds.bottom <= bounds.top:
            return True
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        result.append(WindowInfo(int(hwnd), pid.value, title.value, _rect(bounds),
                                 minimized, bool(cloaked.value), user32.GetDpiForWindow(hwnd) or None, kind))
        return True

    user32.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user32.EnumWindows.restype = wintypes.BOOL
    if not user32.EnumWindows(callback, 0):
        raise ctypes.WinError(ctypes.get_last_error())
    return result


class DesktopCapture:
    """One-thread synchronous real desktop/ROI capture with owned CPU frames.

    Use ``with DesktopCapture(monitor=1) as capture: frame = capture.grab()``.
    A ``rect=ScreenRect(...)`` overrides monitor selection. An index of zero
    explicitly selects the full virtual desktop. Source changes are validated
    before replacing the current source; a failed change leaves it intact.
    """

    backend = "mss-cpu"
    timestamp_kind = "host_receive_perf_counter_ns"

    def __init__(self, *, monitor: int = 1, rect: ScreenRect | None = None) -> None:
        self._validate_monitor(monitor)
        if rect is not None and not isinstance(rect, ScreenRect):
            raise TypeError("rect must be a ScreenRect")
        self._monitor = monitor
        self._rect = rect
        self._sct: Any = None
        self._thread_id: int | None = None
        self._frame_id = 0
        self._generation = 0
        self._geometry: SourceGeometry | None = None
        self._topology: tuple[Any, ...] | None = None
        self.dpi_status: DpiAwarenessStatus | None = None
        self._selection_id = secrets.randbits(64) or 1
        self._source_identity = None

    @staticmethod
    def _validate_monitor(monitor: int) -> None:
        if isinstance(monitor, bool) or not isinstance(monitor, int) or monitor < 0:
            raise ValueError("monitor index must be a nonnegative integer")

    @property
    def source_id(self) -> str:
        if self._rect is not None:
            return f"desktop:rect:{self._rect.left},{self._rect.top},{self._rect.width},{self._rect.height}"
        return f"desktop:monitor:{self._monitor}"

    @property
    def geometry(self) -> SourceGeometry:
        if self._geometry is None:
            raise RuntimeError("capture is not open")
        return self._geometry

    @property
    def source_geometry(self) -> SourceGeometry:
        """Geometry snapshot corresponding to the current source/generation."""
        return self.geometry

    def _check_thread(self) -> None:
        if self._thread_id != threading.get_ident():
            raise RuntimeError("open, grab, set_source and close capture on the same thread")

    @staticmethod
    def _resolve(monitor: int, rect: ScreenRect | None, monitors: list[MonitorInfo]) -> ScreenRect:
        if rect is not None:
            return validate_roi(rect, monitors[0].bounds)
        if monitor >= len(monitors):
            raise ValueError(f"monitor {monitor} does not exist; available indices: 0..{len(monitors) - 1}")
        return monitors[monitor].bounds

    @staticmethod
    def _topology_key(monitors: list[MonitorInfo]) -> tuple[Any, ...]:
        return tuple((monitor.bounds, monitor.device_name, monitor.is_primary, monitor.native_handle) for monitor in monitors)

    @staticmethod
    def _identity(monitors, bounds, selection_id):
        parents = [monitor for monitor in monitors[1:] if monitor.bounds.contains(bounds)]
        if len(parents) != 1 or not parents[0].native_handle:
            return None  # Virtual/spanning surfaces need a separate input contract.
        monitor = parents[0]
        return SourceIdentity(SourceKind.MONITOR, 0, selection_id, monitor.native_handle, 0, monitor.bounds)

    def __enter__(self) -> DesktopCapture:
        if self._sct is not None:
            raise RuntimeError("capture is already open")
        self.dpi_status = _require_physical_pixels()
        monitors = list_monitors()
        bounds = self._resolve(self._monitor, self._rect, monitors)
        import mss
        self._sct = mss.mss()
        self._thread_id = threading.get_ident()
        self._topology = self._topology_key(monitors)
        self._geometry = SourceGeometry(bounds, self._generation)
        self._selection_id = secrets.randbits(64) or 1
        self._source_identity = self._identity(monitors, bounds, self._selection_id)
        return self

    def set_source(self, *, monitor: int = 1, rect: ScreenRect | None = None) -> SourceGeometry:
        self._check_thread()
        if self._sct is None:
            raise RuntimeError("capture is not open")
        self._validate_monitor(monitor)
        if rect is not None and not isinstance(rect, ScreenRect):
            raise TypeError("rect must be a ScreenRect")
        monitors = list_monitors()
        bounds = self._resolve(monitor, rect, monitors)
        topology = self._topology_key(monitors)
        changed = (monitor, rect) != (self._monitor, self._rect) or topology != self._topology
        if changed:
            self._generation += 1
            self._selection_id = secrets.randbits(64) or 1
        self._monitor, self._rect, self._topology = monitor, rect, topology
        self._geometry = SourceGeometry(bounds, self._generation)
        self._source_identity = self._identity(monitors, bounds, self._selection_id)
        return self._geometry

    def grab(self) -> CapturedFrame:
        self._check_thread()
        if self._sct is None:
            raise RuntimeError("capture is not open")
        # Win32 enumeration avoids mss's cached monitor geometry on mode changes.
        self.set_source(monitor=self._monitor, rect=self._rect)
        raw = self._sct.grab(self.geometry.bounds.as_mss())
        received_ns = perf_counter_ns()
        bgra = np.array(raw, dtype=np.uint8, copy=True, order="C")
        expected = (self.geometry.bounds.height, self.geometry.bounds.width, 4)
        if bgra.shape != expected:
            raise RuntimeError(f"capture size changed: expected {expected}, got {bgra.shape}")
        self._frame_id += 1
        return CapturedFrame(bgra, received_ns, self.source_id, self._frame_id,
                             self._generation, self.geometry, self._source_identity)

    def close(self) -> None:
        if self._sct is not None:
            self._check_thread()
            self._sct.close()
            self._sct = None
            self._geometry = None
            self._thread_id = None

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()


class GPUDesktopCapture:
    """Experimental wc_cuda 0.1.2 WGC-to-CUDA latest-frame adapter.

    Captures one monitor or a physical ROI contained by that monitor. The pinned
    upstream 0.1.2 wheel does not implement cursor exclusion or texture leasing.
    The local 0.1.2+quest1 patch adds an explicit producer/consumer texture lease;
    cursor exclusion is supported there but still needs visual acceptance checks.
    No CPU frame readback happens here. Consumers explicitly choose readback if
    they need the current CPU stereo/IPC path.
    """

    backend = "wc_cuda-0.1.2-experimental"
    timestamp_kind = "host_receive_perf_counter_ns"
    cursor_exclusion_supported = False
    cursor_exclusion_verified = False
    native_texture_lease_verified = False

    def __init__(self, *, monitor: int = 1, rect: ScreenRect | None = None,
                 device: int = 0, timeout_seconds: float = 3.0,
                 experimental_hdr: bool = False, hdr_tonemap: str = "torch") -> None:
        DesktopCapture._validate_monitor(monitor)
        if monitor == 0:
            raise ValueError("GPU capture supports one monitor; monitor 0 requires the mss backend")
        if rect is not None and not isinstance(rect, ScreenRect):
            raise TypeError("rect must be a ScreenRect")
        if isinstance(device, bool) or not isinstance(device, int) or device < 0:
            raise ValueError("CUDA device must be a nonnegative integer")
        if not 0 < timeout_seconds <= 30:
            raise ValueError("capture timeout must be in (0, 30] seconds")
        self._monitor, self._rect, self.device = monitor, rect, device
        self.timeout_seconds = timeout_seconds
        if not isinstance(experimental_hdr, bool):
            raise ValueError("experimental_hdr must be a boolean")
        self.experimental_hdr = experimental_hdr
        if hdr_tonemap not in ("torch", "fused") or (hdr_tonemap == "fused" and not experimental_hdr):
            raise ValueError("HDR tone mapper must be torch or fused, and fused requires experimental HDR")
        self.hdr_tonemap = hdr_tonemap
        self._tone_mapper = None
        self.color_profile = None
        self._display_color = None
        self._last_color_check_ns = 0
        self._condition = threading.Condition()
        self._latest: _CapturedGPUBuffer | None = None
        self._error: BaseException | None = None
        self._worker: threading.Thread | None = None
        self._capture: Any = None
        self._closing = False
        self._ended = False
        self._geometry: SourceGeometry | None = None
        self._monitor_bounds: ScreenRect | None = None
        self._selection_id = secrets.randbits(64) or 1
        self._source_identity = None
        self._topology: tuple[Any, ...] | None = None
        self._frame_id = 0
        self._generation = 0
        self._consumed_frame_id = 0
        self.dropped_frames = 0
        self.native_frames_skipped_during_copy = 0
        self.dpi_status: DpiAwarenessStatus | None = None

    @property
    def source_id(self) -> str:
        if self._rect is not None:
            r = self._rect
            return f"desktop:rect:{r.left},{r.top},{r.width},{r.height}"
        return f"desktop:monitor:{self._monitor}"

    @property
    def source_geometry(self) -> SourceGeometry:
        if self._geometry is None:
            raise RuntimeError("capture is not open")
        return self._geometry

    def __enter__(self) -> GPUDesktopCapture:
        if self._worker is not None:
            raise RuntimeError("capture is already open")
        import importlib.metadata
        import torch

        try:
            version = importlib.metadata.version("wc_cuda")
        except importlib.metadata.PackageNotFoundError as exc:
            raise RuntimeError("Install the pinned official wc_cuda GPU-capture extra before selecting this backend") from exc
        allowed = ("0.1.2+quest2",) if self.experimental_hdr else ("0.1.2", "0.1.2+quest1")
        if version not in allowed:
            raise RuntimeError(f"Expected pinned wc_cuda {allowed}, found {version}; HDR candidates require explicit opt-in")
        import wc_cuda
        patch = {"0.1.2": 0, "0.1.2+quest1": 1, "0.1.2+quest2": 2}[version]
        patched = patch > 0
        if getattr(wc_cuda, "QUEST3D_PATCH_VERSION", 0) != patch:
            raise RuntimeError("wc_cuda distribution version and native patch identity disagree")
        self.backend = f"wc_cuda-{version}" + ("" if patched else "-experimental")
        self.cursor_exclusion_supported = patched
        self.native_texture_lease_verified = patched
        if not torch.cuda.is_available() or self.device >= torch.cuda.device_count():
            raise RuntimeError("Selected CUDA device is unavailable")
        self.dpi_status = _require_physical_pixels()
        monitors = list_monitors()
        self._monitor_bounds = DesktopCapture._resolve(self._monitor, None, monitors)
        if self.experimental_hdr:
            from .display_color import read_display_colors, require_hdr_color
            self._display_color = require_hdr_color(monitors[self._monitor].device_name, read_display_colors())
            self.color_profile = {"policy": "experimental-scrgb-sdr-shoulder-v1", "knee": .75,
                                  "native_format": "rgba16f", "output_format": "bgra8-srgb",
                                  "display": asdict(self._display_color), "visually_verified": False,
                                  "implementation": self.hdr_tonemap}
            self._last_color_check_ns = perf_counter_ns()
        if self._rect is not None:
            validate_roi(self._rect, self._monitor_bounds)
        self._topology = DesktopCapture._topology_key(monitors)
        self._geometry = SourceGeometry(self._rect or self._monitor_bounds, self._generation)
        self._selection_id = secrets.randbits(64) or 1
        self._source_identity = DesktopCapture._identity(monitors, self._geometry.bounds, self._selection_id)
        if self.experimental_hdr and self.hdr_tonemap == "fused":
            from .tonemap_cuda import CudaToneMapper
            self._tone_mapper = CudaToneMapper(device=self.device)
            self.color_profile["compiled_kernel"] = self._tone_mapper.metadata
        self._closing = self._ended = False
        self._error = self._latest = None
        self._worker = threading.Thread(target=self._run, name="Quest3D GPU capture", daemon=True)
        self._worker.start()
        return self

    def _run(self) -> None:
        try:
            import wc_cuda
            color_options = {"color_format": "rgba16f"} if self.experimental_hdr else {}
            capture = wc_cuda.WindowsCapture(monitor_index=self._monitor, device_id=self.device,
                                             cursor_capture=False, **color_options)
            self._capture = capture

            @capture.event
            def on_frame_arrived(frame: Any, _control: Any) -> None:
                import torch
                received_ns = perf_counter_ns()
                tensor = frame.frame_buffer
                dtype = torch.float16 if self.experimental_hdr else torch.uint8
                if tensor.dtype != dtype or not tensor.is_cuda or tensor.ndim != 3 or tensor.shape[2] != 4:
                    raise RuntimeError("wc_cuda returned an invalid CUDA frame for the selected color format")
                if self.experimental_hdr and getattr(frame, "color_format", None) != "rgba16f":
                    raise RuntimeError("Native FP16 color format identity is missing or mismatched")
                monitors = list_monitors()
                if DesktopCapture._topology_key(monitors) != self._topology:
                    raise RuntimeError("Display topology changed; reopen GPU capture with current geometry")
                if self.experimental_hdr and received_ns - self._last_color_check_ns >= 1_000_000_000:
                    from .display_color import read_display_colors, require_hdr_color
                    color = require_hdr_color(monitors[self._monitor].device_name, read_display_colors())
                    if color != self._display_color:
                        raise RuntimeError("Display color/SDR white changed; reopen capture with the new profile")
                    self._last_color_check_ns = received_ns
                bounds = self._monitor_bounds
                if (tensor.shape[1], tensor.shape[0]) != (bounds.width, bounds.height):
                    raise RuntimeError("WGC source size differs from selected monitor geometry")
                if self._rect is not None:
                    roi = self._rect
                    x, y = roi.left - bounds.left, roi.top - bounds.top
                    tensor = tensor[y:y + roi.height, x:x + roi.width]
                # Upstream returns a fresh allocation per callback. Remove WGC
                # alignment padding/crop strides and complete that GPU-only copy.
                color_started_ns = perf_counter_ns()
                if not self.experimental_hdr:
                    tensor = tensor.contiguous()
                    torch.cuda.current_stream(self.device).synchronize()
                # The patched native FP16 allocation is already owned and its
                # CUDA copy completed before callback. Keep its crop as a view;
                # transform only frames actually consumed, not every WGC tick.
                color_processing_ms = (perf_counter_ns() - color_started_ns) / 1e6
                if self.native_texture_lease_verified:
                    self.native_frames_skipped_during_copy = capture.lease_stats()[1]
                with self._condition:
                    if self._closing:
                        return
                    self._frame_id += 1
                    if self._latest is not None and self._latest.frame_id > self._consumed_frame_id:
                        self.dropped_frames += 1
                    self._latest = _CapturedGPUBuffer(tensor, received_ns, self._frame_id, color_processing_ms)
                    self._condition.notify_all()

            @capture.event
            def on_closed() -> None:
                with self._condition:
                    self._ended = True
                    self._condition.notify_all()

            if not self._closing:
                capture.start()
        except BaseException as exc:
            with self._condition:
                self._error = exc
                self._condition.notify_all()
        finally:
            with self._condition:
                self._ended = True
                self._condition.notify_all()

    def grab(self, *, timeout_seconds: float | None = None) -> CapturedGPUFrame:
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        if not 0 < timeout <= 30:
            raise ValueError("capture timeout must be in (0, 30] seconds")
        return self._grab_ready(timeout, allow_empty=False)

    def try_grab(self) -> CapturedGPUFrame | None:
        """Consume the latest completed native copy without waiting for a tick.

        None means no new frame, never a new timestamp or a repeated capture.
        Capture errors/end still propagate. HDR conversion is completed by the
        same path as grab; the source allocation remains owned and immutable.
        """
        return self._grab_ready(0, allow_empty=True)

    def _grab_ready(self, timeout: float, *, allow_empty: bool) -> CapturedGPUFrame | None:
        if self._worker is None:
            raise RuntimeError("capture is not open")
        with self._condition:
            ready = self._condition.wait_for(
                lambda: (self._latest is not None and self._latest.frame_id > self._consumed_frame_id)
                or self._error is not None or self._ended, timeout)
            if self._error is not None:
                raise RuntimeError(f"GPU capture failed: {self._error}") from self._error
            if self._latest is not None and self._latest.frame_id > self._consumed_frame_id:
                self._consumed_frame_id = self._latest.frame_id
                pending = self._latest
            elif self._ended:
                raise RuntimeError("GPU capture ended before a new frame arrived")
            elif not ready:
                if allow_empty:
                    return None
                raise TimeoutError("No new WGC frame; source may be static, minimized, or unavailable")
            else:
                raise RuntimeError("GPU capture woke without a valid frame")
        tensor, color_ms = pending.pixels, pending.copy_processing_ms
        if self.experimental_hdr:
            import torch
            from .tonemap import scrgb_to_bgra8
            started = perf_counter_ns()
            mapper = self._tone_mapper or scrgb_to_bgra8
            tensor = mapper(tensor, sdr_white_scale=self._display_color.sc_rgb_sdr_white_scale)
            torch.cuda.current_stream(self.device).synchronize()
            color_ms += (perf_counter_ns() - started) / 1e6
        return CapturedGPUFrame(tensor, pending.captured_ns, self.source_id, pending.frame_id,
                                self._generation, self.source_geometry, color_ms, self._source_identity)

    def close(self) -> None:
        self._closing = True
        capture = self._capture
        if capture is not None:
            capture.stop()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=5)
            if worker.is_alive():
                raise RuntimeError("GPU capture worker did not stop within 5 seconds")
        self._worker = self._capture = None
        if self._tone_mapper is not None:
            self._tone_mapper.close()
            self._tone_mapper = None
        self._latest = None
        self._geometry = None

    def __exit__(self, _type: Any, _value: Any, _traceback: Any) -> None:
        self.close()

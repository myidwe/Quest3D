"""Read-only HWND identity/geometry tracking; no capture or input authorization.

Windows can recycle HWNDs. Process creation time and lifecycle notifications
reduce that ambiguity but cannot make asynchronous Win32 observation atomic.
"""
from contextlib import contextmanager
import ctypes
from ctypes import wintypes as w
from dataclasses import dataclass
import sys
import threading
import time

from .geometry import ScreenRect


class _MonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT),
                ("flags", w.DWORD), ("device", w.WCHAR * 32)]


@dataclass(frozen=True, slots=True)
class WindowIdentity:
    hwnd: int
    process_id: int
    process_created_filetime: int
    thread_id: int
    class_name: str


@dataclass(frozen=True, slots=True)
class SourceExclusions:
    hwnds: frozenset[int] = frozenset()
    process_ids: frozenset[int] = frozenset()

    def __post_init__(self):
        for name in ("hwnds", "process_ids"):
            values = frozenset(getattr(self, name))
            if any(type(value) is not int or not 0 < value <= 0x7FFFFFFFFFFFFFFF for value in values):
                raise ValueError(f"Invalid excluded {name}")
            object.__setattr__(self, name, values)

    def matches(self, hwnd, pid):
        return hwnd in self.hwnds or pid in self.process_ids


@dataclass(frozen=True, slots=True)
class WindowObservation:
    hwnd: int
    observed_ns: int
    identity: WindowIdentity | None
    title: str = ""
    window_bounds: ScreenRect | None = None
    frame_bounds: ScreenRect | None = None
    client_bounds: ScreenRect | None = None
    dpi: int | None = None
    monitor_device: str | None = None
    monitor_bounds: ScreenRect | None = None
    virtual_desktop: ScreenRect | None = None
    visible: bool = False
    minimized: bool = False
    cloaked: bool | None = None
    foreground: bool = False
    top_level: bool = False
    occlusion: str = "unknown"
    occluders: tuple[int, ...] = ()
    output_overlap: bool = False
    excluded: bool = False
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class TrackedWindow:
    observation: WindowObservation
    generation: int
    valid_identity: bool
    invalid_reason: str | None
    crop_bounds: ScreenRect | None
    desktop_crop_eligible: bool
    reasons: tuple[str, ...]
    lifecycle_monitor_active: bool
    input_authorized: bool = False


def overlaps(a, b):
    return bool(a and b and a.left < b.right and b.left < a.right and a.top < b.bottom and b.top < a.bottom)


def _integer_handle(hwnd):
    if type(hwnd) is not int or not 0 < hwnd <= 0x7FFFFFFFFFFFFFFF:
        raise ValueError("HWND must be a positive native handle")
    return hwnd


class Win32WindowProvider:
    """Native queries run briefly under PMv2 thread DPI, then restore the caller."""
    def __init__(self, *, include_titles=True):
        if sys.platform != "win32":
            raise OSError("Window sources require Windows")
        self.include_titles = include_titles
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
        specs = {
            "SetThreadDpiAwarenessContext": ([ctypes.c_void_p], ctypes.c_void_p),
            "GetWindowThreadProcessId": ([w.HWND, ctypes.POINTER(w.DWORD)], w.DWORD),
            "GetWindowRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "GetClientRect": ([w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
            "ClientToScreen": ([w.HWND, ctypes.POINTER(w.POINT)], w.BOOL),
            "GetDpiForWindow": ([w.HWND], w.UINT),
            "GetWindowTextLengthW": ([w.HWND], ctypes.c_int),
            "GetWindowTextW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetClassNameW": ([w.HWND, w.LPWSTR, ctypes.c_int], ctypes.c_int),
            "GetAncestor": ([w.HWND, w.UINT], w.HWND),
            "GetWindow": ([w.HWND, w.UINT], w.HWND),
            "GetForegroundWindow": ([], w.HWND),
            "MonitorFromWindow": ([w.HWND, w.DWORD], w.HMONITOR),
            "GetMonitorInfoW": ([w.HMONITOR, ctypes.c_void_p], w.BOOL),
            "GetSystemMetrics": ([ctypes.c_int], ctypes.c_int),
        }
        specs.update({name: ([w.HWND], w.BOOL) for name in ("IsWindow", "IsWindowVisible", "IsIconic")})
        for name, (args, result) in specs.items():
            function = getattr(self.user, name)
            function.argtypes, function.restype = args, result
        self.kernel.OpenProcess.argtypes, self.kernel.OpenProcess.restype = [w.DWORD, w.BOOL, w.DWORD], w.HANDLE
        self.kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
        self.kernel.GetProcessTimes.restype = w.BOOL
        self.kernel.CloseHandle.argtypes, self.kernel.CloseHandle.restype = [w.HANDLE], w.BOOL
        self.dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
        self.dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    @contextmanager
    def physical_pixels(self):
        old = self.user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        if not old:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            yield
        finally:
            if not self.user.SetThreadDpiAwarenessContext(old):
                raise RuntimeError("Failed to restore caller DPI awareness")

    def _pid(self, hwnd):
        pid = w.DWORD()
        tid = self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return int(pid.value), int(tid)

    def _identity(self, hwnd):
        if not self.user.IsWindow(hwnd):
            raise OSError("window_missing")
        pid, tid = self._pid(hwnd)
        process = self.kernel.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not process:
            raise OSError(f"process_identity_unavailable:{ctypes.get_last_error()}")
        try:
            created, exited, kernel, user = (w.FILETIME() for _ in range(4))
            if not self.kernel.GetProcessTimes(process, ctypes.byref(created), ctypes.byref(exited),
                                               ctypes.byref(kernel), ctypes.byref(user)):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.kernel.CloseHandle(process)
        name = ctypes.create_unicode_buffer(256)
        if not self.user.GetClassNameW(hwnd, name, len(name)) or self._pid(hwnd) != (pid, tid):
            raise OSError("window_identity_changed_during_query")
        return WindowIdentity(hwnd, pid, (created.dwHighDateTime << 32) | created.dwLowDateTime, tid, name.value)

    @staticmethod
    def _rectangle(rect):
        if rect.right <= rect.left or rect.bottom <= rect.top:
            return None
        return ScreenRect(rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top)

    def _bounds(self, hwnd, attribute=None):
        rect = w.RECT()
        success = (self.dwm.DwmGetWindowAttribute(hwnd, attribute, ctypes.byref(rect), ctypes.sizeof(rect)) == 0
                   if attribute is not None else bool(self.user.GetWindowRect(hwnd, ctypes.byref(rect))))
        return self._rectangle(rect) if success else None

    def _cloaked(self, hwnd):
        value = w.DWORD()
        if self.dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            return None
        return bool(value.value)

    def _client(self, hwnd):
        rect = w.RECT()
        if not self.user.GetClientRect(hwnd, ctypes.byref(rect)):
            return None
        first, last = w.POINT(rect.left, rect.top), w.POINT(rect.right, rect.bottom)
        if not self.user.ClientToScreen(hwnd, ctypes.byref(first)) or not self.user.ClientToScreen(hwnd, ctypes.byref(last)):
            return None
        return self._rectangle(w.RECT(first.x, first.y, last.x, last.y))

    def _occlusion(self, hwnd, bounds, exclusions):
        if bounds is None:
            return "unknown", (), False
        previous, seen, blockers = self.user.GetWindow(hwnd, 3), {hwnd}, []  # GW_HWNDPREV
        uncertain = output_overlap = False
        while previous:
            other = int(previous)
            if other in seen or len(seen) >= 4096:
                uncertain = True
                break
            seen.add(other)
            if self.user.IsWindowVisible(other) and not self.user.IsIconic(other):
                cloak, other_bounds = self._cloaked(other), self._bounds(other, 9)
                if cloak is not True:
                    other_bounds = other_bounds or self._bounds(other)
                    if other_bounds is None:
                        uncertain = True
                    elif overlaps(bounds, other_bounds):
                        if len(blockers) < 64:
                            blockers.append(other)
                        pid, _ = self._pid(other)
                        output_overlap |= exclusions.matches(other, pid)
            previous = self.user.GetWindow(other, 3)
        return ("possible" if blockers else "unknown" if uncertain else "no_overlapping_top_level_window",
                tuple(blockers), output_overlap)

    def observe(self, hwnd, exclusions=SourceExclusions(), *, include_occlusion=True):
        hwnd = _integer_handle(hwnd)
        with self.physical_pixels():
            try:
                identity = self._identity(hwnd)
            except OSError as exc:
                return WindowObservation(hwnd, time.perf_counter_ns(), None, errors=(str(exc),))
            errors = []
            title = ""
            if self.include_titles:
                buffer = ctypes.create_unicode_buffer(min(32767, self.user.GetWindowTextLengthW(hwnd)) + 1)
                self.user.GetWindowTextW(hwnd, buffer, len(buffer))
                title = buffer.value
            frame, window, client = self._bounds(hwnd, 9), self._bounds(hwnd), self._client(hwnd)
            if frame is None:
                errors.append("dwm_frame_bounds_unavailable")
            if client is None:
                errors.append("client_bounds_unavailable")
            cloak = self._cloaked(hwnd)
            if cloak is None:
                errors.append("cloaked_state_unavailable")
            monitor = _MonitorInfo()
            monitor.size = ctypes.sizeof(monitor)
            handle = self.user.MonitorFromWindow(hwnd, 2)
            monitor_ok = bool(handle and self.user.GetMonitorInfoW(handle, ctypes.byref(monitor)))
            desktop = ScreenRect(*(self.user.GetSystemMetrics(index) for index in (76, 77, 78, 79)))
            occlusion, blockers, output = (self._occlusion(hwnd, frame or window, exclusions) if include_occlusion
                                           else ("unknown", (), False))
            try:
                if self._identity(hwnd) != identity:
                    raise OSError("window_identity_changed_during_query")
            except OSError as exc:
                return WindowObservation(hwnd, time.perf_counter_ns(), None, errors=(str(exc),))
            return WindowObservation(hwnd, time.perf_counter_ns(), identity, title, window, frame, client,
                self.user.GetDpiForWindow(hwnd) or None, monitor.device if monitor_ok else None,
                self._rectangle(monitor.monitor) if monitor_ok else None, desktop,
                bool(self.user.IsWindowVisible(hwnd)), bool(self.user.IsIconic(hwnd)), cloak,
                int(self.user.GetForegroundWindow() or 0) == hwnd,
                int(self.user.GetAncestor(hwnd, 2) or 0) == hwnd, occlusion, blockers, output,
                exclusions.matches(hwnd, identity.process_id), tuple(errors))

    def list(self, *, exclusions=SourceExclusions(), include_unselectable=False):
        callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
        handles = []
        @callback_type
        def callback(hwnd, _):
            handles.append(int(hwnd))
            return True
        self.user.EnumWindows.argtypes, self.user.EnumWindows.restype = [callback_type, w.LPARAM], w.BOOL
        with self.physical_pixels():
            if not self.user.EnumWindows(callback, 0):
                raise ctypes.WinError(ctypes.get_last_error())
        result = [self.observe(hwnd, exclusions, include_occlusion=False) for hwnd in handles]
        return [item for item in result if include_unselectable or
                (item.identity is not None and item.top_level and item.visible and item.title and not item.excluded)]


class WindowEvents:
    """One selected HWND, bounded state, no injection or in-process hook DLL."""
    def __init__(self, hwnd):
        self.hwnd = _integer_handle(hwnd)
        self.lock = threading.Lock()
        self.ready, self.stopping = threading.Event(), threading.Event()
        self.invalidated, self.revision, self.error = False, 0, None
        self.thread = threading.Thread(target=self._run, name="Quest3D HWND events", daemon=True)

    def __enter__(self):
        self.thread.start()
        if not self.ready.wait(3) or self.error:
            self.stopping.set()
            self.thread.join(3)
            raise RuntimeError(f"Window lifecycle hook failed: {self.error or 'timeout'}")
        return self

    def _run(self):
        hooks = []
        try:
            user = ctypes.WinDLL("user32", use_last_error=True)
            callback_type = ctypes.WINFUNCTYPE(None, w.HANDLE, w.DWORD, w.HWND, w.LONG, w.LONG, w.DWORD, w.DWORD)
            @callback_type
            def callback(_hook, event, hwnd, object_id, child_id, _thread, _time):
                if int(hwnd or 0) == self.hwnd and object_id == 0 and child_id == 0:
                    with self.lock:
                        if event in (0x8000, 0x8001):  # create/destroy after selection
                            self.invalidated = True
                        if event in (0x16, 0x17, 0x8000, 0x8001, 0x8002, 0x8003, 0x800A, 0x800B):
                            self.revision += 1
            user.SetWinEventHook.argtypes = [w.DWORD, w.DWORD, w.HMODULE, callback_type, w.DWORD, w.DWORD, w.DWORD]
            user.SetWinEventHook.restype = w.HANDLE
            user.UnhookWinEvent.argtypes, user.UnhookWinEvent.restype = [w.HANDLE], w.BOOL
            user.PeekMessageW.argtypes, user.PeekMessageW.restype = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT], w.BOOL
            user.TranslateMessage.argtypes = [ctypes.POINTER(w.MSG)]
            user.DispatchMessageW.argtypes, user.DispatchMessageW.restype = [ctypes.POINTER(w.MSG)], ctypes.c_ssize_t
            message = w.MSG()
            user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            for first, last in ((0x8000, 0x800B), (0x16, 0x17)):
                hook = user.SetWinEventHook(first, last, None, callback, 0, 0, 0)
                if not hook:
                    raise ctypes.WinError(ctypes.get_last_error())
                hooks.append(hook)
            self.ready.set()
            while not self.stopping.wait(.005):
                while user.PeekMessageW(ctypes.byref(message), None, 0, 0, 1):
                    user.TranslateMessage(ctypes.byref(message))
                    user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            with self.lock:
                self.error = str(exc)
            self.ready.set()
        finally:
            for hook in hooks:
                user.UnhookWinEvent(hook)

    def snapshot(self):
        with self.lock:
            return self.invalidated, self.revision, self.error

    def __exit__(self, *_):
        self.stopping.set()
        self.thread.join(3)
        if self.thread.is_alive():
            raise RuntimeError("Window event worker did not stop")


class WindowTracker:
    """Terminal invalidation on identity loss; never silently selects a replacement."""
    def __init__(self, identity, *, exclusions=SourceExclusions(), bounds_kind="client", provider=None, events=None):
        if not isinstance(identity, WindowIdentity):
            raise ValueError("Select a WindowIdentity from a fresh observation")
        _integer_handle(identity.hwnd)
        if bounds_kind not in ("client", "frame"):
            raise ValueError("bounds_kind must be client or frame")
        self.identity, self.exclusions, self.bounds_kind = identity, exclusions, bounds_kind
        self.provider = provider if provider is not None else Win32WindowProvider()
        self.events = events if events is not None else WindowEvents(identity.hwnd)
        self.lock = threading.Lock()
        self.generation = 0
        self.invalid_reason = None
        self.previous = None
        self.active = False

    def __enter__(self):
        self.events.__enter__()
        self.active = True
        return self

    def poll(self):
        if not self.active:
            raise RuntimeError("WindowTracker requires an active context")
        with self.lock:
            before = self.events.snapshot()
            observation = self.provider.observe(self.identity.hwnd, self.exclusions)
            after = self.events.snapshot()
            if not self.invalid_reason:
                if before[2] or after[2]:
                    self.invalid_reason = "lifecycle_monitor_failed"
                elif before[0] or after[0]:
                    self.invalid_reason = "window_lifecycle_changed"
                elif observation.identity != self.identity:
                    self.invalid_reason = "window_identity_lost_or_reused"
                elif observation.excluded:
                    self.invalid_reason = "source_is_excluded_output"
            bounds = observation.client_bounds if self.bounds_kind == "client" else observation.frame_bounds
            reasons = []
            if self.invalid_reason:
                reasons.append(self.invalid_reason)
            if before[1] != after[1]:
                reasons.append("geometry_changed_during_query")
            if not observation.top_level:
                reasons.append("not_top_level")
            if not observation.visible:
                reasons.append("not_visible")
            if observation.minimized:
                reasons.append("minimized")
            if observation.cloaked is not False:
                reasons.append("cloaked_or_unknown")
            if bounds is None:
                reasons.append("bounds_unavailable")
            if not observation.dpi or not observation.monitor_device:
                reasons.append("dpi_or_monitor_unknown")
            if bounds and (observation.virtual_desktop is None or not observation.virtual_desktop.contains(bounds)):
                reasons.append("outside_virtual_desktop")
            if bounds and (observation.monitor_bounds is None or not observation.monitor_bounds.contains(bounds)):
                # A virtual-desktop bounding box includes gaps between monitors.
                # Dedicated HWND WGC may support spanning; this desktop crop
                # eligibility intentionally requires one completely covering monitor.
                reasons.append("cross_monitor_or_outside_monitor")
            if observation.occlusion != "no_overlapping_top_level_window":
                reasons.append("occlusion_" + observation.occlusion)
            if observation.output_overlap:
                reasons.append("output_overlap_recursion_risk")
            signature = (observation.identity, observation.window_bounds, observation.frame_bounds, observation.client_bounds,
                observation.dpi, observation.monitor_device, observation.monitor_bounds, observation.virtual_desktop,
                observation.visible, observation.minimized, observation.cloaked, observation.foreground,
                observation.occlusion, observation.occluders, observation.output_overlap, observation.excluded,
                after[1], tuple(reasons))
            if signature != self.previous:
                self.generation += 1
                self.previous = signature
            return TrackedWindow(observation, self.generation, self.invalid_reason is None, self.invalid_reason,
                bounds, not reasons, tuple(reasons), not bool(before[2] or after[2]))

    def __exit__(self, *args):
        self.active = False
        return self.events.__exit__(*args)

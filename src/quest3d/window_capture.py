"""Explicit quest3 HWND candidate; frame capture never authorizes Windows input.

The receiver gets completed, owned SDR BGRA CUDA frames. Ambiguous geometry or
color waits for a fresh frame; exact source lifetime loss is terminal.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import importlib.metadata
import math
import os
import secrets
import ctypes
from pathlib import Path
import threading
from time import perf_counter_ns
from typing import Any
from types import SimpleNamespace

from .capture import CapturedGPUFrame
from .display_color import DisplayColor, read_display_colors
from .geometry import SourceGeometry
from .source_identity import SourceIdentity, SourceKind
from .window_sources import SourceExclusions, TrackedWindow, WindowIdentity, WindowTracker, Win32WindowProvider


class WindowCaptureUnavailable(RuntimeError):
    """Reversible state; retain the same source and retry after it changes."""
    retryable = True


class WindowCaptureFailed(RuntimeError):
    """Closed/reused HWND or capture failure; do not silently reselect a source."""
    retryable = False


def _color_for_window(tracked: TrackedWindow, colors) -> DisplayColor:
    observation = tracked.observation
    bounds = observation.frame_bounds
    if bounds is None or observation.monitor_bounds is None or not observation.monitor_bounds.contains(bounds):
        raise WindowCaptureUnavailable("cross_monitor_or_outside_monitor")
    matching = [color for color in colors
                if observation.monitor_device and color.device_name.casefold() == observation.monitor_device.casefold()]
    if len(matching) != 1:
        raise WindowCaptureUnavailable("missing_or_ambiguous_display_color")
    color = matching[0]
    scale = color.sc_rgb_sdr_white_scale
    if (color.active_mode not in ("hdr", "sdr") or color.hdr_enabled is not (color.active_mode == "hdr")
            or type(scale) not in (int, float) or not .001 <= scale <= 100 or not math.isfinite(scale)
            or (color.active_mode == "sdr" and scale != 1.0)):
        raise WindowCaptureUnavailable("unknown_color_or_sdr_white")
    return color


class _WindowFrameGate:
    """Pure policy boundary. Fixture states test races; they are not live pixels."""
    def __init__(self, stable_ns):
        self.stable_ns = stable_ns
        self.generation = 0
        self.changed_ns = 0
        self.signature = None
        self.tracked = None
        self.color = None
        self.reason = "opening"
        self.terminal = None
        self.geometry = None
        self.accepted = False

    def update(self, tracked, colors, now_ns, color_error=None):
        observation = tracked.observation
        reason = None
        color = None
        if not tracked.valid_identity or not tracked.lifecycle_monitor_active:
            self.terminal = tracked.invalid_reason or "lifecycle_monitor_failed"
        # Dedicated HWND capture can see pixels behind both ordinary and output
        # windows. Desktop-crop occlusion policy must not be applied to it.
        blocked = [r for r in tracked.reasons if not r.startswith("occlusion_")
                   and r != "output_overlap_recursion_risk"]
        if self.terminal:
            reason = self.terminal
        elif blocked:
            reason = blocked[0]
        elif observation.errors:
            reason = "window_observation_error"
        elif color_error:
            reason = "display_color_query_failed"
        else:
            try:
                color = _color_for_window(tracked, colors)
            except WindowCaptureUnavailable as exc:
                reason = str(exc)
        color_key = None if color is None else repr(asdict(color))
        signature = (tracked.generation, color_key, reason)
        if signature != self.signature:
            self.signature = signature
            self.generation += 1
            self.changed_ns = now_ns
            self.accepted = False
        self.tracked, self.color, self.reason = tracked, color, reason
        self.geometry = (SourceGeometry(observation.frame_bounds, self.generation)
                         if observation.frame_bounds else None)

    def check_frame(self, *, width, height, received_ns, generation=None):
        if self.terminal:
            raise WindowCaptureFailed(self.terminal)
        if self.reason:
            raise WindowCaptureUnavailable(self.reason)
        if generation is not None and generation != self.generation:
            raise WindowCaptureUnavailable("frame_generation_changed")
        if self.geometry is None or (width, height) != (self.geometry.bounds.width, self.geometry.bounds.height):
            raise WindowCaptureUnavailable("frame_content_size_mismatch")
        if received_ns < self.changed_ns + self.stable_ns:
            raise WindowCaptureUnavailable("awaiting_fresh_stable_frame")

    def reject_content_shape(self, now_ns):
        self.generation += 1
        self.changed_ns = now_ns
        self.accepted = False
        if self.geometry:
            self.geometry = SourceGeometry(self.geometry.bounds, self.generation)


@dataclass(frozen=True, slots=True)
class _WindowBuffer:
    pixels: Any
    received_ns: int
    source_time_ns: int
    frame_id: int
    native_frame_id: int
    geometry: SourceGeometry
    color: DisplayColor
    source_identity: SourceIdentity


class GPUWindowCapture:
    """Frame-bounds-only WGC capture; +quest3 opt-in, single-output color only."""
    backend = "wc_cuda-0.1.2+quest3-window-experimental"
    timestamp_kind = "host_receive_perf_counter_ns"
    cursor_exclusion_supported = True
    cursor_exclusion_verified = False
    native_texture_lease_verified = True
    input_authorized = False
    expected_wheel_sha256 = "7f0c8b3033eceb560632204bf2cc94e0f819de11c287c555bd156716720cf634"
    _package_hashes = {
        "__init__.py": "7e2fcfd201f3edf9c3e5516af8df3df7065b1ead33682f14d1d231518c0bc1cc",
        "_wc_cuda.pyd": "298c72cec1586680ba2fee5fc2b2ccc025567f00058a83b490ec4963a5dc8e45",
    }

    def __init__(self, identity: WindowIdentity, *, experimental_window=False,
                 exclusions=SourceExclusions(), device=0, timeout_seconds=3.0,
                 stable_seconds=.1):
        if experimental_window is not True:
            raise ValueError("HWND +quest3 requires explicit experimental_window=True")
        if not isinstance(identity, WindowIdentity) or type(identity.hwnd) is not int or identity.hwnd <= 0:
            raise ValueError("Select an exact WindowIdentity")
        if (any(type(value) is not int or value <= 0 for value in
                (identity.process_id, identity.process_created_filetime, identity.thread_id))
                or not isinstance(identity.class_name, str) or not identity.class_name):
            raise ValueError("Invalid exact process/window identity")
        if not isinstance(exclusions, SourceExclusions):
            raise TypeError("exclusions must be SourceExclusions")
        if exclusions.matches(identity.hwnd, identity.process_id):
            raise ValueError("Selected HWND is an excluded output")
        if type(device) is not int or device < 0:
            raise ValueError("CUDA device must be a nonnegative integer")
        self._validate_timeout(timeout_seconds)
        if type(stable_seconds) not in (int, float) or not .05 <= stable_seconds <= 1 or not math.isfinite(stable_seconds):
            raise ValueError("stable_seconds must be between .05 and 1")
        self._identity = identity
        self._selection_id = secrets.randbelow((1 << 64) - 1) + 1
        self.exclusions, self.device = exclusions, device
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Condition(threading.RLock())
        self._consumer_lock = threading.Lock()
        self._gate = _WindowFrameGate(round(stable_seconds * 1e9))
        self._tracker = self._capture = self._mapper = self._worker = None
        self._latest = None
        self._consumed_frame_id = self._frame_id = self._last_native_frame_id = 0
        self._closing, self._ended, self._opened = False, False, False
        self._error = None
        self._cleanup_error = None
        self._last_blocked = "opening"
        self.dropped_frames = self.blocked_frames = 0
        self.native_frames_skipped_during_copy = 0
        self.last_frame_diagnostics = None

    @staticmethod
    def _validate_timeout(timeout):
        if type(timeout) not in (int, float) or not 0 < timeout <= 30 or not math.isfinite(timeout):
            raise ValueError("capture timeout must be in (0, 30] seconds")

    @property
    def identity(self):
        return self._identity

    @property
    def selection_id(self):
        return self._selection_id

    @property
    def source_id(self):
        i = self.identity
        class_hash = hashlib.sha256(i.class_name.encode("utf-8")).hexdigest()[:16]
        return f"window:hwnd:{i.hwnd}:pid:{i.process_id}:created:{i.process_created_filetime}:tid:{i.thread_id}:class:{class_hash}:selection:{self.selection_id}"

    def _frame_source_identity(self, geometry):
        i = self.identity
        if self._gate.tracked.observation.identity != i:
            raise WindowCaptureFailed("frame_observation_identity_mismatch")
        return SourceIdentity(SourceKind.WINDOW, i.process_id, self.selection_id,
                              i.hwnd, i.process_created_filetime, geometry.bounds)

    def _check_buffer_identity(self, pending):
        expected = SourceIdentity(SourceKind.WINDOW, self.identity.process_id, self.selection_id,
            self.identity.hwnd, self.identity.process_created_filetime, pending.geometry.bounds)
        if pending.source_identity != expected:
            raise WindowCaptureFailed("buffer_source_identity_mismatch")

    @property
    def source_geometry(self):
        with self._lock:
            self._refresh()
            if self._gate.terminal:
                raise WindowCaptureFailed(self._gate.terminal)
            if self._gate.geometry is None:
                raise WindowCaptureUnavailable(self._gate.reason or "bounds_unavailable")
            return self._gate.geometry

    @property
    def color_profile(self):
        with self._lock:
            color = self._gate.color
            return None if color is None else {
                "policy": "experimental-scrgb-sdr-shoulder-v1", "implementation": "fused", "knee": .75,
                "native_format": "rgba16f", "output_format": "bgra8-srgb", "display": asdict(color),
                "visually_verified": False, "window_color_generalization_verified": False,
            }

    @property
    def status(self):
        with self._lock:
            if self._tracker and not self._closing:
                self._refresh()
            tracked = self._gate.tracked
            return {
                "state": ("failed" if self._error or self._gate.terminal else "closed" if self._closing else
                          "ready" if self._gate.accepted and not self._gate.reason else "waiting"),
                "reason": str(self._error) if self._error else self._gate.reason or self._last_blocked,
                "generation": self._gate.generation, "selection_id": self.selection_id, "input_authorized": False,
                "frame_bounds": asdict(self._gate.geometry.bounds) if self._gate.geometry else None,
                "monitor": tracked.observation.monitor_device if tracked else None,
                "occlusion": tracked.observation.occlusion if tracked else "unknown",
                "output_overlap": tracked.observation.output_overlap if tracked else False,
                "blocked_frames": self.blocked_frames, "latest_slot_overwrites": self.dropped_frames,
                "native_frames_skipped_during_copy": self.native_frames_skipped_during_copy,
                "last_frame": self.last_frame_diagnostics,
            }

    def __enter__(self):
        if self._opened or self._closing:
            raise RuntimeError("Create a new GPUWindowCapture instance for another session")
        if (self.identity.process_id == os.getpid()
                and ctypes.windll.kernel32.GetCurrentThreadId() == self.identity.thread_id):
            raise RuntimeError("Capture must run off the selected source's UI thread; keep its message loop running")
        import torch
        import wc_cuda
        if (importlib.metadata.version("wc_cuda") != "0.1.2+quest3"
                or getattr(wc_cuda, "QUEST3D_PATCH_VERSION", None) != 3):
            raise RuntimeError("Use the explicit separately unpacked +quest3 candidate")
        package = Path(wc_cuda.__file__).resolve().parent
        if any(hashlib.sha256((package / name).read_bytes()).hexdigest() != digest
               for name, digest in self._package_hashes.items()):
            raise RuntimeError("Loaded HWND candidate differs from the reviewed pinned wheel")
        if not torch.cuda.is_available() or self.device >= torch.cuda.device_count():
            raise RuntimeError("Selected CUDA device is unavailable")
        from .tonemap_cuda import CudaToneMapper
        try:
            # GetWindowTextW synchronously messages same-process HWND owners.
            # A capture worker must never wait on a UI thread blocked in grab().
            self._tracker = WindowTracker(self.identity, exclusions=self.exclusions, bounds_kind="frame",
                                          provider=Win32WindowProvider(include_titles=False))
            self._tracker.__enter__()
            self._opened = True
            with self._lock:
                self._refresh()
                if self._gate.terminal:
                    raise WindowCaptureFailed(self._gate.terminal)
            self._mapper = CudaToneMapper(device=self.device)
            self._worker = threading.Thread(target=self._run, name="Quest3D HWND capture", daemon=True)
            self._worker.start()
            return self
        except BaseException:
            self.close()
            raise

    def _refresh(self):
        if not self._opened or self._tracker is None or self._closing:
            raise RuntimeError("window capture is not open")
        tracked = self._tracker.poll()
        colors, color_error = (), None
        try:
            colors = read_display_colors()
        except Exception as exc:
            color_error = str(exc)
        before = self._gate.generation
        self._gate.update(tracked, colors, perf_counter_ns(), color_error)
        if before != self._gate.generation:
            self._latest = None
            self._last_blocked = self._gate.reason or "awaiting_fresh_stable_frame"
        if self._gate.terminal:
            self._error = WindowCaptureFailed(self._gate.terminal)
        self._lock.notify_all()

    def _run(self):
        capture = None
        try:
            import torch
            import wc_cuda
            i = self.identity
            capture = wc_cuda.WindowsCapture(window_hwnd=i.hwnd,
                window_identity=(i.process_id, i.process_created_filetime, i.thread_id, i.class_name),
                device_id=self.device, color_format="rgba16f", cursor_capture=False)
            with self._lock:
                self._capture = capture
                if self._closing:
                    return
                capture._stream = torch.cuda.Stream(device=self.device)
                # Native start is nonblocking. Serialize start with close so a
                # stop during startup cannot be lost by the wrapper's run loop.
                capture._inner.start()
            startup_deadline = perf_counter_ns() + 1_000_000_000
            last_id = 0
            while not self._closing:
                result = capture._inner.get_frame(last_id, timeout=.05)
                error = capture._inner.get_last_error()
                if error:
                    raise WindowCaptureFailed(error)
                if result:
                    native, last_id = result
                    pixels = capture._copy_leased_frame(native, last_id)
                    self._receive(SimpleNamespace(
                        frame_buffer=pixels[:native.original_height, :native.original_width],
                        width=native.original_width, height=native.original_height,
                        color_format=native.color_format, frame_id=last_id,
                        source_hwnd=native.source_hwnd, source_identity=native.source_identity,
                        system_relative_time_ns=native.system_relative_time_ns,
                    ), capture)
                elif capture._inner.window_status()[0] or (perf_counter_ns() > startup_deadline and not capture._inner.is_alive()):
                    raise WindowCaptureFailed("native_window_capture_ended")
        except BaseException as exc:
            with self._lock:
                self._error = exc
        finally:
            if capture:
                try:
                    capture._inner.request_stop()
                    if capture._bridge:
                        capture._bridge.close()
                        capture._bridge = None
                    capture.stop()
                except BaseException as exc:
                    with self._lock:
                        self._cleanup_error = exc
                        self._error = self._error or exc
            with self._lock:
                self._ended = True
                self._lock.notify_all()

    def _receive(self, frame, capture):
        import torch
        received_ns = perf_counter_ns()  # Receipt only; never raw SystemRelativeTime.
        tensor = frame.frame_buffer
        expected_identity = (self.identity.process_id, self.identity.process_created_filetime,
                             self.identity.thread_id, self.identity.class_name)
        if (frame.source_hwnd != self.identity.hwnd or tuple(frame.source_identity or ()) != expected_identity
                or type(frame.frame_id) is not int or frame.frame_id <= self._last_native_frame_id
                or type(frame.system_relative_time_ns) is not int or frame.system_relative_time_ns < 0):
            raise WindowCaptureFailed("native_frame_identity_or_sequence_mismatch")
        self._last_native_frame_id = frame.frame_id
        if (not isinstance(tensor, torch.Tensor) or not tensor.is_cuda or tensor.device.index != self.device
                or tensor.dtype != torch.float16 or tensor.ndim != 3 or tensor.shape[2] != 4
                or frame.color_format != "rgba16f" or (frame.width, frame.height) != (tensor.shape[1], tensor.shape[0])):
            raise WindowCaptureFailed("invalid_native_fp16_frame")
        with self._lock:
            if self._closing:
                return
            self._refresh()
            try:
                self._gate.check_frame(width=frame.width, height=frame.height, received_ns=received_ns)
            except WindowCaptureUnavailable as exc:
                self.blocked_frames += 1
                self._last_blocked = str(exc)
                self._latest = None
                if str(exc) == "frame_content_size_mismatch":
                    self._gate.reject_content_shape(perf_counter_ns())
                return
            self._frame_id += 1
            if self._latest and self._latest.frame_id > self._consumed_frame_id:
                self.dropped_frames += 1
            self._latest = _WindowBuffer(tensor, received_ns, frame.system_relative_time_ns,
                self._frame_id, frame.frame_id, self._gate.geometry, self._gate.color,
                self._frame_source_identity(self._gate.geometry))
            self._gate.accepted, self._last_blocked = True, None
            self.native_frames_skipped_during_copy = capture.lease_stats()[1]
            self._lock.notify_all()

    def grab(self, *, timeout_seconds=None):
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        self._validate_timeout(timeout)
        deadline = perf_counter_ns() + round(timeout * 1e9)
        # A single consumer preserves CUDA mapper lifetime and frame ordering.
        with self._consumer_lock:
            while True:
                with self._lock:
                    self._refresh()
                    if self._error:
                        raise WindowCaptureFailed(f"HWND capture failed: {self._error}") from self._error
                    if self._ended:
                        raise WindowCaptureFailed("HWND capture ended; explicit source selection required")
                    if self._gate.terminal:
                        raise WindowCaptureFailed(self._gate.terminal)
                    if self._gate.reason:
                        raise WindowCaptureUnavailable(self._gate.reason)
                    pending = self._latest
                    if pending and pending.frame_id > self._consumed_frame_id:
                        self._check_buffer_identity(pending)
                        self._gate.check_frame(width=pending.pixels.shape[1], height=pending.pixels.shape[0],
                            received_ns=pending.received_ns, generation=pending.geometry.generation)
                        self._consumed_frame_id = pending.frame_id
                        break
                    remaining = (deadline - perf_counter_ns()) / 1e9
                    if remaining <= 0:
                        if not self._gate.accepted:
                            raise WindowCaptureUnavailable(self._last_blocked or "awaiting_fresh_stable_frame")
                        raise TimeoutError("No new HWND frame; unchanged content and driver freeze are not distinguishable")
                    self._lock.wait(min(remaining, .02))
            started = perf_counter_ns()
            pixels = self._mapper(pending.pixels, sdr_white_scale=pending.color.sc_rgb_sdr_white_scale)
            color_ms = (perf_counter_ns() - started) / 1e6
            with self._lock:
                self._refresh()
                if self._error:
                    raise WindowCaptureFailed(str(self._error)) from self._error
                self._gate.check_frame(width=pixels.shape[1], height=pixels.shape[0], received_ns=pending.received_ns,
                                       generation=pending.geometry.generation)
                self.last_frame_diagnostics = {
                    "frame_id": pending.frame_id, "native_frame_id": pending.native_frame_id,
                    "system_relative_time_ns": pending.source_time_ns, "received_ns": pending.received_ns,
                    "raw_wgc_to_receive_ms": (pending.received_ns - pending.source_time_ns) / 1e6,
                    "raw_wgc_time_used_for_latency": False,
                }
            return CapturedGPUFrame(pixels, pending.received_ns, self.source_id, pending.frame_id,
                                    pending.geometry.generation, pending.geometry, color_ms,
                                    source_identity=pending.source_identity)

    def close(self):
        with self._lock:
            self._closing = True
            self._lock.notify_all()
            capture = self._capture
        if capture:
            capture._inner.request_stop()
        worker = self._worker
        if worker and worker is not threading.current_thread():
            worker.join(5)
            if worker.is_alive():
                raise RuntimeError("HWND worker did not stop within 5 seconds")
        if self._cleanup_error:
            raise RuntimeError(f"HWND cleanup failed; resources retained: {self._cleanup_error}") from self._cleanup_error
        with self._consumer_lock:
            if self._mapper:
                self._mapper.close()
                self._mapper = None
        if self._tracker:
            self._tracker.__exit__(None, None, None)
            self._tracker = None
        # A saved worker traceback owns its local WcGpuFrame/D3D resources.
        # Retain the diagnostic message after successful cleanup, not that stack.
        if self._error:
            self._error = WindowCaptureFailed(str(self._error))
        self._worker = self._capture = self._latest = None

    def __exit__(self, *_):
        self.close()

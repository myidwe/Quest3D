"""Explicit HWND capture worker isolation; CPU BGRA transport, never OS input."""
from __future__ import annotations

from collections import deque
import copy
import ctypes as C
from ctypes import wintypes as W
from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import secrets
import subprocess
import sys
import threading
import time

import numpy as np

from .bridge import CAPTURE_RECEIPT, MAX_WIDTH, MAX_HEIGHT
from .bridge_v3 import SourceFrameHeader, HEADER_SIZE_V3
from .capture import CapturedFrame
from .geometry import ScreenRect, SourceGeometry
from .source_identity import SourceKind
from .window_capture import GPUWindowCapture, WindowCaptureFailed, WindowCaptureUnavailable
from .window_sources import SourceExclusions, WindowIdentity, WindowTracker, Win32WindowProvider

WIRE_PREFIX = "Q3DWW1 "
PRIVATE_PREFIX = "Local\\Quest3D.WindowWorker."
MAX_MESSAGE = 65536
CAPACITY = HEADER_SIZE_V3 + MAX_WIDTH * MAX_HEIGHT * 4


class _BasicLimits(C.Structure):
    _fields_ = [("process_time", C.c_int64), ("job_time", C.c_int64), ("flags", W.DWORD),
        ("min_working", C.c_size_t), ("max_working", C.c_size_t), ("active_limit", W.DWORD),
        ("affinity", C.c_size_t), ("priority", W.DWORD), ("scheduling", W.DWORD)]


class _JobLimits(C.Structure):
    _fields_ = [("basic", _BasicLimits), ("io", C.c_uint64 * 6),
        ("process_memory", C.c_size_t), ("job_memory", C.c_size_t),
        ("peak_process", C.c_size_t), ("peak_job", C.c_size_t)]


class _ProcessBasic(C.Structure):
    _fields_ = [("reserved", C.c_void_p), ("peb", C.c_void_p), ("reserved2", C.c_void_p * 2),
                ("pid", C.c_size_t), ("parent_pid", C.c_size_t)]


class _WinAPI:
    def __init__(self):
        self.k = C.WinDLL("kernel32", use_last_error=True)
        definitions = {
            "OpenMutexW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            "OpenFileMappingW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            "MapViewOfFile": ([W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t], C.c_void_p),
            "UnmapViewOfFile": ([C.c_void_p], W.BOOL),
            "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
            "ReleaseMutex": ([W.HANDLE], W.BOOL), "CloseHandle": ([W.HANDLE], W.BOOL),
            "CreateJobObjectW": ([C.c_void_p, W.LPCWSTR], W.HANDLE),
            "SetInformationJobObject": ([W.HANDLE, C.c_int, C.c_void_p, W.DWORD], W.BOOL),
            "AssignProcessToJobObject": ([W.HANDLE, W.HANDLE], W.BOOL),
            "IsProcessInJob": ([W.HANDLE, W.HANDLE, C.POINTER(W.BOOL)], W.BOOL),
            "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            "GetProcessTimes": ([W.HANDLE] + [C.POINTER(W.FILETIME)] * 4, W.BOOL),
            "GetCurrentProcess": ([], W.HANDLE),
        }
        for name, (args, result) in definitions.items():
            method = getattr(self.k, name)
            method.argtypes, method.restype = args, result
        self.nt = C.WinDLL("ntdll")
        self.nt.NtQueryInformationProcess.argtypes = [W.HANDLE, W.ULONG, C.c_void_p, W.ULONG, C.POINTER(W.ULONG)]
        self.nt.NtQueryInformationProcess.restype = C.c_long

    def birth(self, handle):
        values = [W.FILETIME() for _ in range(4)]
        if not self.k.GetProcessTimes(handle, *(C.byref(value) for value in values)):
            raise C.WinError(C.get_last_error())
        return (values[0].dwHighDateTime << 32) | values[0].dwLowDateTime

    def create_job(self, process_handle):
        job = self.k.CreateJobObjectW(None, None)
        if not job:
            raise C.WinError(C.get_last_error())
        try:
            limits = _JobLimits()
            limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not self.k.SetInformationJobObject(job, 9, C.byref(limits), C.sizeof(limits)):
                raise C.WinError(C.get_last_error())
            if not self.k.AssignProcessToJobObject(job, process_handle):
                raise C.WinError(C.get_last_error())
            return job
        except BaseException:
            self.k.CloseHandle(job)
            raise

    def attach_actual_worker(self, pid, birth, launcher_pid, launcher_birth, job):
        handle = self.k.OpenProcess(0x101501, False, pid)
        if not handle:
            raise C.WinError(C.get_last_error())
        try:
            basic = _ProcessBasic()
            returned = W.ULONG()
            if self.nt.NtQueryInformationProcess(handle, 0, C.byref(basic), C.sizeof(basic), C.byref(returned)) != 0:
                raise WindowCaptureFailed("Cannot inspect actual worker parent identity")
            expected_parent = os.getpid() if pid == launcher_pid else launcher_pid
            if basic.pid != pid or basic.parent_pid != expected_parent or self.birth(handle) != birth or birth < launcher_birth:
                raise WindowCaptureFailed("Bootstrap process is not the exact launched worker")
            member = W.BOOL()
            if not self.k.IsProcessInJob(handle, job, C.byref(member)):
                raise C.WinError(C.get_last_error())
            if not member.value and not self.k.AssignProcessToJobObject(job, handle):
                raise C.WinError(C.get_last_error())
            if not self.k.IsProcessInJob(handle, job, C.byref(member)) or not member.value:
                raise WindowCaptureFailed("Actual worker was not attached to its cleanup Job")
            return handle
        except BaseException:
            self.k.CloseHandle(handle)
            raise


def _validate_header(header, identity, selection_id, epoch):
    source = header.source_identity
    if header.flags != CAPTURE_RECEIPT or header.stream_epoch != epoch:
        raise WindowCaptureFailed("Wrong raw worker flags or stream lifetime")
    if source.kind != SourceKind.WINDOW or (source.process_id, source.native_handle,
            source.creation_filetime, source.selection_id) != (
            identity.process_id, identity.hwnd, identity.process_created_filetime, selection_id):
        raise WindowCaptureFailed("Worker frame belongs to a different source selection")
    bounds = ScreenRect(header.source_left, header.source_top, header.source_width, header.source_height)
    if source.parent_bounds != bounds or (header.width, header.height) != (bounds.width, bounds.height):
        raise WindowCaptureFailed("Raw worker pixels do not match immutable frame bounds")
    if (header.content_left, header.content_top, header.content_width, header.content_height) != (
            0, 0, header.width, header.height):
        raise WindowCaptureFailed("Worker raw frame has a crop or stereo transform")
    now = time.perf_counter_ns()
    if not 0 < header.capture_ns <= header.publish_ns <= now or header.frame_id <= 0:
        raise WindowCaptureFailed("Invalid worker frame timestamps or ID")
    return SourceGeometry(bounds, header.generation)


class _RawReader:
    """Open existing private objects only. Pixel copy and metadata share one lock."""
    def __init__(self, prefix):
        self.win = _WinAPI()
        self.mapping = self.mutex = self.owner = self.pointer = None
        try:
            self.mutex = self.win.k.OpenMutexW(0x100001, False, prefix + ".Mutex.v3")
            self.owner = self.win.k.OpenMutexW(0x100001, False, prefix + ".Producer.v3")
            self.mapping = self.win.k.OpenFileMappingW(4, False, prefix + ".v3")
            if not all((self.mutex, self.owner, self.mapping)):
                raise WindowCaptureFailed("Private worker mapping/owner does not exist")
            self.pointer = self.win.k.MapViewOfFile(self.mapping, 4, 0, 0, CAPACITY)
            if not self.pointer:
                raise C.WinError(C.get_last_error())
        except BaseException:
            self.close()
            raise

    def _check_owner(self):
        value = self.win.k.WaitForSingleObject(self.owner, 0)
        if value == 258:
            return
        if value in (0, 128):
            self.win.k.ReleaseMutex(self.owner)
        raise WindowCaptureFailed("Worker producer owner has ended or was abandoned")

    def read(self, identity, selection_id, epoch, after_frame_id):
        self._check_owner()
        acquired = self.win.k.WaitForSingleObject(self.mutex, 5)
        if acquired == 258:
            return None
        if acquired == 128:
            self.win.k.ReleaseMutex(self.mutex)
            raise WindowCaptureFailed("Worker died while writing pixels")
        if acquired != 0:
            raise WindowCaptureFailed("Private frame mutex wait failed")
        try:
            self._check_owner()
            packed = C.string_at(self.pointer, HEADER_SIZE_V3)
            if not any(packed):
                return None
            try:
                header = SourceFrameHeader.unpack(packed)
                geometry = _validate_header(header, identity, selection_id, epoch)
            except (ValueError, TypeError) as exc:
                raise WindowCaptureFailed(f"Malformed private worker frame: {exc}") from exc
            if header.frame_id <= after_frame_id:
                return None
            started = time.perf_counter_ns()
            pixels = np.empty((header.height, header.width, 4), dtype=np.uint8)
            C.memmove(pixels.ctypes.data, self.pointer + HEADER_SIZE_V3, pixels.nbytes)
            copy_ms = (time.perf_counter_ns() - started) / 1e6
            return header, geometry, pixels, copy_ms
        finally:
            self.win.k.ReleaseMutex(self.mutex)

    def close(self):
        if self.pointer:
            self.win.k.UnmapViewOfFile(self.pointer)
            self.pointer = None
        for name in ("mapping", "owner", "mutex"):
            if handle := getattr(self, name, None):
                self.win.k.CloseHandle(handle)
                setattr(self, name, None)


class ProcessWindowCapture:
    """One child lifetime per explicit HWND selection; no implicit restart."""
    backend = "contained-hwnd-cuda-cpu-v3-experimental"
    timestamp_kind = "child_capture_receive_perf_counter_ns"
    input_authorized = False
    cursor_exclusion_supported = True
    cursor_exclusion_verified = False

    def __init__(self, identity: WindowIdentity, *, experimental_window=False,
                 exclusions=SourceExclusions(), device=0, startup_timeout_seconds=20,
                 close_timeout_seconds=8):
        # Pure validation only: this does not load CUDA or select the candidate.
        GPUWindowCapture(identity, experimental_window=experimental_window, exclusions=exclusions, device=device)
        for value in (startup_timeout_seconds, close_timeout_seconds):
            if type(value) not in (int, float) or not 1 <= value <= 30 or not math.isfinite(value):
                raise ValueError("Worker lifecycle timeouts must be in [1,30] seconds")
        self._identity, self.exclusions, self.device = identity, exclusions, device
        self.startup_timeout_seconds, self.close_timeout_seconds = startup_timeout_seconds, close_timeout_seconds
        self.nonce = secrets.token_hex(32)
        self.prefix = PRIVATE_PREFIX + secrets.token_hex(32)
        self._condition = threading.Condition(threading.RLock())
        self._consumer = threading.Lock()
        self._close_lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._writers = []
        self._command_failed = self._pipe_aborted = False
        self._process = self._reader_thread = self._mapping = self._tracker = None
        self._job = self._win = None
        self._hello = self._child_state = None
        self._failure = None
        self._closing = self._opened = False
        self._geometry = None
        self._signature = None
        self._changed_ns = 0
        self._consumed = 0
        self._logs = deque(maxlen=16)
        self._child_birth = None
        self._launcher_birth = self._worker_pid = self._worker_handle = None
        self._last_message_ns = 0
        self.last_frame_diagnostics = None
        self.close_result = None
        self.started_ns = None

    @property
    def identity(self):
        return self._identity

    @property
    def source_id(self):
        if not self._hello:
            raise WindowCaptureUnavailable("worker_starting")
        return self._hello["source_id"]

    @property
    def selection_id(self):
        return self._hello["selection_id"] if self._hello else None

    @property
    def source_geometry(self):
        self._observe()
        if self._geometry is None:
            raise WindowCaptureUnavailable("awaiting_first_worker_frame")
        return self._geometry

    @property
    def status(self):
        with self._condition:
            code = self._process.poll() if self._process else None
            return dict(state="failed" if self._failure else "closed" if self._closing else
                (self._child_state or {}).get("state", "starting"), reason=self._failure or
                (self._child_state or {}).get("reason"), child_pid=self._worker_pid,
                launcher_pid=self._process.pid if self._process else None,
                child_creation_filetime=self._child_birth, child_exit_code=code,
                selection_id=self.selection_id, input_authorized=False,
                child_status=copy.deepcopy(self._child_state), last_frame=copy.deepcopy(self.last_frame_diagnostics),
                close_result=copy.deepcopy(self.close_result), diagnostic_lines=list(self._logs))

    def _send(self, value, *, timeout_seconds=.5):
        payload = json.dumps(value, separators=(",", ":")).encode() + b"\n"
        if len(payload) > MAX_MESSAGE:
            raise ValueError("Worker command too large")
        finished = threading.Event()
        errors = []

        def write():
            try:
                with self._write_lock:
                    if self._pipe_aborted:
                        raise WindowCaptureFailed("Worker command pipe is being torn down")
                    self._process.stdin.write(payload)
                    self._process.stdin.flush()
            except BaseException as exc:
                errors.append(exc)
            finally:
                finished.set()

        writer = threading.Thread(target=write, name="HWND worker command", daemon=True)
        self._writers.append(writer)
        writer.start()
        if not finished.wait(timeout_seconds):
            self._command_failed = True
            raise WindowCaptureFailed("Worker command write exceeded its deadline")
        writer.join()
        if errors:
            raise errors[0]

    def _messages(self):
        try:
            while True:
                line = self._process.stdout.readline(MAX_MESSAGE + 1)
                if not line:
                    break
                if len(line) > MAX_MESSAGE or not line.endswith(b"\n"):
                    raise WindowCaptureFailed("Oversized or truncated worker message")
                text = line.decode("utf-8", errors="strict").rstrip()
                if not text.startswith(WIRE_PREFIX):
                    with self._condition:
                        self._logs.append(text[:2048])
                    continue
                message = json.loads(text[len(WIRE_PREFIX):])
                if not isinstance(message, dict) or message.get("nonce") != self.nonce:
                    raise WindowCaptureFailed("Worker message nonce does not match")
                if message.get("type") == "bootstrap":
                    with self._condition:
                        if self._closing:
                            raise WindowCaptureFailed("Worker bootstrap arrived after shutdown began")
                        if self._worker_handle is not None or type(message.get("pid")) is not int or type(message.get("birth")) is not int:
                            raise WindowCaptureFailed("Invalid or duplicate worker bootstrap")
                        self._worker_handle = self._win.attach_actual_worker(message["pid"], message["birth"],
                            self._process.pid, self._launcher_birth, self._job)
                        self._worker_pid, self._child_birth = message["pid"], message["birth"]
                        self._send(dict(type="armed", nonce=self.nonce, pid=self._worker_pid, birth=self._child_birth))
                    continue
                if (self._worker_handle is None or type(message.get("pid")) is not int
                        or type(message.get("birth")) is not int
                        or message.get("pid") != self._worker_pid or message.get("birth") != self._child_birth):
                    raise WindowCaptureFailed("Worker message lifetime does not match launched child")
                with self._condition:
                    if message.get("type") == "hello":
                        if self._hello is not None or type(message.get("selection_id")) is not int or not 0 < message["selection_id"] < 1 << 64:
                            raise WindowCaptureFailed("Invalid or duplicate worker hello")
                        if type(message.get("epoch")) is not int or not 0 < message["epoch"] < 1 << 64:
                            raise WindowCaptureFailed("Invalid private mapping epoch")
                        if not isinstance(message.get("source_id"), str) or len(message["source_id"]) > 1024:
                            raise WindowCaptureFailed("Invalid source description")
                        self._hello = message
                    elif message.get("type") == "state":
                        if message.get("state") not in ("starting", "waiting", "ready", "failed", "closed"):
                            raise WindowCaptureFailed("Invalid worker state")
                        self._child_state = message
                        if message["state"] == "failed":
                            self._failure = str(message.get("reason", "Worker failed"))
                    else:
                        raise WindowCaptureFailed("Unknown worker message")
                    self._last_message_ns = time.perf_counter_ns()
                    self._condition.notify_all()
        except BaseException as exc:
            with self._condition:
                self._failure = f"Worker protocol failed: {exc}"
                self._condition.notify_all()
        finally:
            with self._condition:
                self._condition.notify_all()

    def __enter__(self):
        # Startup installs processes, a Job and pipes. Close must not finish
        # before those installations complete; startup's own failure cleanup
        # re-enters this lock on the same thread.
        with self._close_lock:
            return self._enter()

    def _enter(self):
        if self._opened or self._closing:
            raise RuntimeError("Create a new ProcessWindowCapture for another selection")
        if (self.identity.process_id == os.getpid()
                and C.windll.kernel32.GetCurrentThreadId() == self.identity.thread_id):
            raise RuntimeError("Keep the selected source's UI thread pumping; start capture from another thread")
        self._opened = True
        self.started_ns = time.perf_counter_ns()
        root = Path(__file__).resolve().parents[2]
        package = root / "artifacts/capture/window-experimental" / ("package-" + GPUWindowCapture.expected_wheel_sha256[:12])
        for name, digest in GPUWindowCapture._package_hashes.items():
            if hashlib.sha256((package / "wc_cuda" / name).read_bytes()).hexdigest() != digest:
                raise WindowCaptureFailed("Pinned child candidate package mismatch")
        try:
            self._tracker = WindowTracker(self.identity, exclusions=self.exclusions, bounds_kind="frame",
                                          provider=Win32WindowProvider(include_titles=False))
            self._tracker.__enter__()
            self._observe(check_child=False)
            self._win = _WinAPI()
            env = dict(os.environ, PYTHONPATH=os.pathsep.join((str(package), str(root / "src"))))
            self._process = subprocess.Popen([sys.executable, "-u", "-m", "quest3d.window_worker"],
                cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW)
            deadline = time.monotonic() + self.startup_timeout_seconds
            self._launcher_birth = self._win.birth(int(self._process._handle))
            self._job = self._win.create_job(int(self._process._handle))
            self._reader_thread = threading.Thread(target=self._messages, name="HWND worker status", daemon=True)
            self._reader_thread.start()
            self._send(dict(type="start", nonce=self.nonce, prefix=self.prefix, identity=asdict(self.identity),
                exclusions=dict(hwnds=sorted(self.exclusions.hwnds), process_ids=sorted(self.exclusions.process_ids)), device=self.device),
                timeout_seconds=max(.001, deadline - time.monotonic()))
            with self._condition:
                while self._hello is None:
                    self._check_child(require_hello=False)
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise WindowCaptureFailed("Worker startup exceeded its deadline")
                    self._condition.wait(min(remaining, .02))
            self._mapping = _RawReader(self.prefix)
            return self
        except BaseException as exc:
            try:
                self.close()
            except BaseException as cleanup:
                exc.add_note(f"Worker cleanup also failed: {cleanup}")
            raise

    def _check_child(self, *, require_hello=True):
        if self._closing:
            raise WindowCaptureFailed("Worker capture is closed")
        if self._failure:
            raise WindowCaptureFailed(self._failure)
        if self._process is None or self._process.poll() is not None:
            raise WindowCaptureFailed("Capture worker exited; explicit new selection required")
        if self._win.birth(int(self._process._handle)) != self._launcher_birth:
            raise WindowCaptureFailed("Capture launcher lifetime changed")
        if self._worker_handle and (self._win.k.WaitForSingleObject(self._worker_handle, 0) != 258
                or self._win.birth(self._worker_handle) != self._child_birth):
            raise WindowCaptureFailed("Actual capture worker ended or its lifetime changed")
        if require_hello and self._hello is None:
            raise WindowCaptureUnavailable("worker_starting")
        if self._child_state and self._child_state.get("state") in ("closed", "failed"):
            raise WindowCaptureFailed(self._child_state.get("reason") or "worker_ended")

    def _observe(self, *, check_child=True):
        if check_child:
            self._check_child()
        if self._tracker is None:
            raise WindowCaptureFailed("Source tracking is inactive")
        tracked = self._tracker.poll()
        if not tracked.valid_identity or not tracked.lifecycle_monitor_active:
            self._failure = tracked.invalid_reason or "source_lifetime_ended"
            raise WindowCaptureFailed(self._failure)
        observation = tracked.observation
        blocked = [r for r in tracked.reasons if not r.startswith("occlusion_") and r != "output_overlap_recursion_risk"]
        signature = (observation.frame_bounds, observation.client_bounds, observation.window_bounds,
            observation.dpi, observation.monitor_device, observation.monitor_bounds,
            observation.visible, observation.minimized, observation.cloaked, tuple(blocked),
            self._tracker.events.snapshot()[1])
        if signature != self._signature:
            self._signature = signature
            self._changed_ns = time.perf_counter_ns()
            self._geometry = None
        if blocked or observation.errors:
            raise WindowCaptureUnavailable(blocked[0] if blocked else "source_observation_error")
        return signature, observation.frame_bounds

    def grab(self, *, timeout_seconds=3.0):
        GPUWindowCapture._validate_timeout(timeout_seconds)
        with self._consumer:
            deadline = time.monotonic() + timeout_seconds
            while True:
                signature, bounds = self._observe()
                with self._condition:
                    state = self._child_state
                result = self._mapping.read(self.identity, self.selection_id, self._hello["epoch"], self._consumed)
                if result is not None:
                    header, geometry, pixels, copy_ms = result
                    latest_signature, latest_bounds = self._observe()
                    with self._condition:
                        state = self._child_state
                    if (state and state.get("state") == "ready" and state.get("publication") == [header.frame_id, header.generation]
                            and state.get("generation") == header.generation
                            and latest_signature == signature and geometry.bounds == bounds == latest_bounds
                            and header.capture_ns >= self._changed_ns and time.perf_counter_ns() - self._last_message_ns < 1_000_000_000):
                        self._check_child()
                        self._consumed = header.frame_id
                        self._geometry = geometry
                        self.last_frame_diagnostics = dict(child_pid=self._worker_pid, child_birth=self._child_birth,
                            stream_epoch=header.stream_epoch, parent_copy_ms=copy_ms,
                            parent_receive_ns=time.perf_counter_ns(), child_publish_ns=header.publish_ns,
                            captured_ns=header.capture_ns, child_metrics=state.get("metrics"),
                            zero_copy=False, gpu_upload_verified=False)
                        return CapturedFrame(pixels, header.capture_ns, self.source_id, header.frame_id,
                            header.generation, geometry, header.source_identity)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if state and state.get("state") == "waiting":
                        raise WindowCaptureUnavailable(state.get("reason") or "awaiting_valid_worker_frame")
                    raise TimeoutError("No new matching raw worker frame")
                with self._condition:
                    self._condition.wait(min(remaining, .01))

    def close(self):
        with self._close_lock:
            with self._condition:
                self._closing = True
            # A reader may be copying from the mapping. Do not unmap it until
            # that read has returned; its next observation sees _closing.
            with self._consumer:
                self._close()

    def _close(self):
        if self.close_result is not None:
            if not self.close_result["normal_exit"]:
                raise WindowCaptureFailed(self.close_result["reason"])
            return
        self._closing = True
        started = time.perf_counter_ns()
        forced = False
        errors = []
        process = self._process
        worker_exited = self._worker_handle is None

        def attempt(label, action):
            try:
                return action()
            except BaseException as exc:
                errors.append(f"{label}: {type(exc).__name__}: {exc}")
                return None

        try:
            if process and process.poll() is None:
                if not self._command_failed:
                    try:
                        self._send(dict(type="stop", nonce=self.nonce))
                    except (BrokenPipeError, OSError):
                        pass
                    except WindowCaptureFailed:
                        self._command_failed = True
                if self._command_failed:
                    forced = True
                    errors.append("Worker command pipe failed; forced containment required")
                else:
                    process.stdin.close()
                    try:
                        process.wait(timeout=self.close_timeout_seconds)
                    except subprocess.TimeoutExpired:
                        forced = True
                        errors.append("Worker missed normal shutdown deadline; forced termination was required")
        except BaseException as exc:
            errors.append(f"Normal shutdown failed: {type(exc).__name__}: {exc}")
        finally:
            self._pipe_aborted = True
            # A venv launcher can exit independently of its real child. A
            # cleanup ACK is insufficient unless the actual retained process
            # handle is signalled. Closing a live Job is failure containment.
            if self._worker_handle:
                worker_exited = self._win.k.WaitForSingleObject(self._worker_handle, 0) == 0
                if not worker_exited:
                    forced = True
                    errors.append("Actual worker was still alive at cleanup; Job termination required")
            if self._job:
                def close_job():
                    if not self._win.k.CloseHandle(self._job):
                        raise C.WinError(C.get_last_error())
                attempt("Close worker Job", close_job)
                self._job = None
            if process and process.poll() is None:
                if not self._worker_handle:
                    forced = True
                    attempt("Terminate unarmed launcher", process.terminate)
                attempt("Reap launcher after containment", lambda: process.wait(timeout=3))
            if self._worker_handle and not worker_exited:
                worker_exited = self._win.k.WaitForSingleObject(self._worker_handle, 3000) == 0
                if not worker_exited:
                    errors.append("Actual worker did not exit after Job cleanup")
            for writer in self._writers:
                attempt("Join worker command writer", lambda writer=writer: writer.join(2))
                if writer.is_alive():
                    errors.append("Worker command writer failed to stop")
            if self._reader_thread:
                attempt("Join worker status reader", lambda: self._reader_thread.join(2))
                if self._reader_thread.is_alive():
                    errors.append("Worker status reader failed to stop")
            if self._mapping:
                attempt("Unmap raw worker pixels", self._mapping.close)
                self._mapping = None
            if self._tracker:
                attempt("Stop source tracking", lambda: self._tracker.__exit__(None, None, None))
                self._tracker = None
            if self._worker_handle:
                attempt("Close retained worker handle", lambda: self._win.k.CloseHandle(self._worker_handle))
                self._worker_handle = None
            if process:
                for stream in (process.stdin, process.stdout):
                    if stream and not stream.closed:
                        # BufferedReader.close can block behind a live readline.
                        if stream is process.stdin and any(writer.is_alive() for writer in self._writers):
                            errors.append("Command pipe retained because its writer has not exited")
                        elif stream is process.stdout and self._reader_thread and self._reader_thread.is_alive():
                            errors.append("Status pipe retained because its reader has not exited")
                        else:
                            attempt("Close worker pipe", stream.close)
                if process.poll() is not None:
                    attempt("Close launcher handle", process._handle.Close)
            self._geometry = None
            state = self._child_state or {}
            acknowledged = bool(process and process.returncode == 0 and state.get("state") == "closed"
                                and state.get("cleanup_ok") is True and worker_exited)
            if process and not acknowledged:
                errors.append(f"Worker exit was not acknowledged cleanly: code={process.returncode}, state={state.get('state')}")
            normal = (acknowledged or not process) and not errors and not forced
            error = "; ".join(errors) or None
            self.close_result = dict(normal_exit=bool(normal), forced=forced, reason=error,
                elapsed_ms=(time.perf_counter_ns() - started) / 1e6,
                exit_code=process.returncode if process else None, actual_worker_exited=worker_exited)
        if error:
            raise WindowCaptureFailed(error)

    def __exit__(self, *_):
        self.close()

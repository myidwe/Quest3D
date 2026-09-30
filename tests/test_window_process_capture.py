"""Wire/lifetime validation. Synthetic pixels are transport tests, not AI evidence."""
from dataclasses import replace
import secrets
import io
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d.bridge import CAPTURE_RECEIPT, FramePublisher
from quest3d.bridge_v3 import SourceFrameHeader
from quest3d.geometry import ScreenRect
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.window_capture import WindowCaptureFailed
from quest3d.window_process_capture import ProcessWindowCapture, _RawReader, _validate_header, PRIVATE_PREFIX
from quest3d.window_process_capture import WIRE_PREFIX
from quest3d.window_sources import WindowIdentity
from quest3d.window_worker import validate_start

IDENTITY = WindowIdentity(123, 42, 99999, 2, "test_class")
BOUNDS = ScreenRect(-500, -20, 20, 10)
SOURCE = SourceIdentity(SourceKind.WINDOW, 42, 567, 123, 99999, BOUNDS)


def header(**changes):
    now = time.perf_counter_ns()
    value = SourceFrameHeader(20, 10, 1, now - 100, now - 50, 2, CAPTURE_RECEIPT,
        -500, -20, 20, 10, 888, 0, 0, 20, 10, SOURCE)
    return replace(value, **changes)


@pytest.mark.parametrize("change", [dict(flags=5), dict(flags=12), dict(stream_epoch=889),
    dict(width=10), dict(source_left=-499), dict(content_width=10), dict(capture_ns=0),
    dict(capture_ns=time.perf_counter_ns() + 10**15), dict(frame_id=0),
    dict(source_identity=replace(SOURCE, selection_id=568)),
    dict(source_identity=replace(SOURCE, process_id=43)),
    dict(source_identity=replace(SOURCE, creation_filetime=99998))])
def test_wrong_frame_identity_or_raw_layout_is_rejected(change):
    with pytest.raises(WindowCaptureFailed):
        _validate_header(header(**change), IDENTITY, 567, 888)


def test_negative_coordinates_and_exact_metadata_are_preserved():
    value = header()
    assert _validate_header(value, IDENTITY, 567, 888).bounds == BOUNDS
    assert SourceFrameHeader.unpack(value.pack()) == value


@pytest.mark.parametrize("kwargs", [{}, {"experimental_window": 1}, {"experimental_window": False}])
def test_parent_optin_required_before_any_process_start(kwargs):
    with pytest.raises(ValueError, match="explicit"):
        ProcessWindowCapture(IDENTITY, **kwargs)


def request():
    return dict(type="start", nonce="a" * 64, prefix=PRIVATE_PREFIX + "b" * 64,
        identity=dict(hwnd=123, process_id=42, process_created_filetime=99999, thread_id=2, class_name="test_class"),
        exclusions=dict(hwnds=[], process_ids=[]), device=0)


@pytest.mark.parametrize("changes", [dict(type="other"), dict(nonce="a"), dict(prefix="Local\\Quest3D.Frame"),
    dict(device=True), dict(exclusions={"hwnds": [123]}), dict(extra=True)])
def test_child_rejects_nonprivate_or_invalid_start(changes):
    value = request()
    value.update(changes)
    with pytest.raises((ValueError, TypeError)):
        validate_start(value)


def test_child_start_validation_is_pure_and_exact():
    identity, exclusions = validate_start(request())
    assert identity == IDENTITY and not exclusions.matches(123, 42)


def test_raw_v3_copy_is_coherent_owned_and_duplicate_is_not_returned():
    prefix = PRIVATE_PREFIX + secrets.token_hex(32)
    ready, finish = threading.Event(), threading.Event()
    errors, info = [], {}
    pixels = np.arange(800, dtype=np.uint8).reshape(10, 20, 4)
    def writer():
        try:
            with FramePublisher(prefix, version=3) as publisher:
                now = time.perf_counter_ns()
                publisher.publish(pixels, frame_id=1, capture_ns=now, generation=2,
                    flags=CAPTURE_RECEIPT, source_rect=(-500, -20, 20, 10), content_rect=(0, 0, 20, 10), source_identity=SOURCE)
                info["epoch"] = publisher.epoch
                ready.set()
                assert finish.wait(5)
        except BaseException as exc:
            errors.append(exc)
            ready.set()
    thread = threading.Thread(target=writer)
    thread.start()
    reader = None
    try:
        assert ready.wait(5) and not errors
        reader = _RawReader(prefix)
        result = reader.read(IDENTITY, 567, info["epoch"], 0)
        assert result is not None
        h, geometry, actual, elapsed = result
        assert h.source_identity == SOURCE and geometry.bounds == BOUNDS
        assert np.array_equal(actual, pixels) and actual.flags.owndata and elapsed >= 0
        assert reader.read(IDENTITY, 567, info["epoch"], 1) is None
        finish.set()
        thread.join(5)
        assert not thread.is_alive() and not errors
        with pytest.raises(WindowCaptureFailed, match="owner"):
            reader.read(IDENTITY, 567, info["epoch"], 0)
        assert np.array_equal(actual, pixels)
    finally:
        finish.set()
        thread.join(5)
        if reader:
            reader.close()


def test_reader_does_not_create_an_absent_mapping():
    prefix = PRIVATE_PREFIX + secrets.token_hex(32)
    with pytest.raises(WindowCaptureFailed, match="does not exist"):
        _RawReader(prefix)


def test_public_identity_and_status_cannot_rewrite_internal_lifetime():
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    with pytest.raises(AttributeError):
        capture.identity = replace(IDENTITY, hwnd=124)
    capture._child_state = dict(state="ready", publication=[1, 2])
    status = capture.status
    status["child_status"]["publication"][0] = 999
    assert capture._child_state["publication"] == [1, 2]
    capture.close()


def test_capture_does_not_block_its_own_source_ui_thread():
    import ctypes
    import os
    identity = replace(IDENTITY, process_id=os.getpid(), thread_id=ctypes.windll.kernel32.GetCurrentThreadId())
    capture = ProcessWindowCapture(identity, experimental_window=True)
    with pytest.raises(RuntimeError, match="UI thread"):
        capture.__enter__()
    assert capture._process is None
    capture.close()


@pytest.mark.parametrize("message", [dict(type="hello", pid=None, birth=None, selection_id=1, epoch=1, source_id="x"),
    dict(type="state", pid=None, birth=None, state="closed", cleanup_ok=True)])
def test_no_messages_are_accepted_before_actual_worker_job_handshake(message):
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    message["nonce"] = capture.nonce
    stream = io.BytesIO((WIRE_PREFIX + json.dumps(message) + "\n").encode())
    capture._process = SimpleNamespace(stdout=stream)
    capture._messages()
    assert "lifetime" in capture._failure and capture._hello is None and capture._child_state is None


def test_close_waits_for_inflight_pixel_copy_before_unmapping():
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    unmapped = threading.Event()
    capture._mapping = SimpleNamespace(close=unmapped.set)
    capture._consumer.acquire()
    thread = threading.Thread(target=capture.close)
    thread.start()
    try:
        deadline = time.monotonic() + 2
        while not capture._closing and time.monotonic() < deadline:
            time.sleep(.001)
        assert capture._closing and not unmapped.is_set()
    finally:
        capture._consumer.release()
        thread.join(2)
    assert not thread.is_alive() and unmapped.is_set()
    assert capture.close_result["normal_exit"]


def test_cleanup_exception_does_not_skip_other_owned_resources():
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    calls = []
    def broken_mapping():
        calls.append("mapping")
        raise OSError("injected unmap failure")
    capture._mapping = SimpleNamespace(close=broken_mapping)
    capture._tracker = SimpleNamespace(__exit__=lambda *_: calls.append("tracker"))
    with pytest.raises(WindowCaptureFailed, match="unmap failure"):
        capture.close()
    assert calls == ["mapping", "tracker"]
    assert not capture.close_result["normal_exit"] and not capture.close_result["forced"]
    with pytest.raises(WindowCaptureFailed, match="unmap failure"):
        capture.close()


@pytest.mark.parametrize("alive", [False, True])
def test_clean_ack_requires_actual_process_exit_not_only_launcher_exit(alive):
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    closed = []
    class Kernel:
        def WaitForSingleObject(self, handle, timeout):
            return 258 if alive and 12 not in closed else 0
        def CloseHandle(self, handle):
            closed.append(handle)
            return 1
    capture._win = SimpleNamespace(k=Kernel())
    capture._job, capture._worker_handle = 12, 13
    capture._process = SimpleNamespace(returncode=0, poll=lambda: 0, stdin=io.BytesIO(), stdout=io.BytesIO(),
        _handle=SimpleNamespace(Close=lambda: closed.append(14)))
    capture._child_state = dict(state="closed", cleanup_ok=True)
    if alive:
        with pytest.raises(WindowCaptureFailed, match="still alive"):
            capture.close()
        assert capture.close_result["forced"] and not capture.close_result["normal_exit"]
    else:
        capture.close()
        assert not capture.close_result["forced"] and capture.close_result["normal_exit"]
    assert capture.close_result["actual_worker_exited"] and closed == [12, 13, 14]


def test_close_cannot_finish_before_late_popen_resources_are_installed(monkeypatch):
    """The former race recorded success then leaked a late process/Job/pipe."""
    import quest3d.window_process_capture as module
    entered, release, close_done = threading.Event(), threading.Event(), threading.Event()
    calls, failures = [], []
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    capture._observe = lambda **_: None
    class Tracker:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *_):
            calls.append("tracker")
    class Handle:
        def __int__(self):
            return 777
        def Close(self):
            calls.append("launcher")
    class Process:
        pid, returncode = 123, None
        _handle = Handle()
        stdin, stdout = io.BytesIO(), io.BytesIO()
        def poll(self):
            return self.returncode
        def wait(self, timeout):
            self.returncode = 0
            capture._child_state = dict(state="closed", cleanup_ok=True)
            return 0
    process = Process()
    def popen(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return process
    def messages():
        with capture._condition:
            capture._hello = dict(selection_id=1, epoch=1, source_id="fixture")
            capture._condition.notify_all()
    monkeypatch.setattr(module.GPUWindowCapture, "_package_hashes", {})
    monkeypatch.setattr(module, "WindowTracker", Tracker)
    monkeypatch.setattr(module, "Win32WindowProvider", lambda **_: None)
    monkeypatch.setattr(module, "_WinAPI", lambda: SimpleNamespace(birth=lambda _: 1,
        create_job=lambda _: 321, k=SimpleNamespace(CloseHandle=lambda _: calls.append("job") or 1)))
    monkeypatch.setattr(module.subprocess, "Popen", popen)
    monkeypatch.setattr(module, "_RawReader", lambda _: SimpleNamespace(close=lambda: calls.append("mapping")))
    capture._messages = messages
    def enter():
        try:
            capture.__enter__()
        except BaseException as exc:
            failures.append(exc)
    def close():
        try:
            capture.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            close_done.set()
    starter = threading.Thread(target=enter)
    closer = threading.Thread(target=close)
    starter.start()
    assert entered.wait(3)
    closer.start()
    try:
        assert not close_done.wait(.05) and capture.close_result is None
    finally:
        release.set()
        starter.join(3)
        closer.join(3)
    assert not starter.is_alive() and not closer.is_alive() and not failures
    assert capture.close_result["normal_exit"]
    assert capture._job is capture._mapping is capture._tracker is None
    assert process.stdin.closed and process.stdout.closed
    assert sorted(calls) == ["job", "launcher", "mapping", "tracker"]


def test_blocked_command_write_is_bounded_and_reported_not_completed():
    capture = ProcessWindowCapture(IDENTITY, experimental_window=True)
    release = threading.Event()
    class Pipe:
        def write(self, value):
            assert release.wait(3)
        def flush(self):
            pass
    capture._process = SimpleNamespace(stdin=Pipe())
    started = time.monotonic()
    try:
        with pytest.raises(WindowCaptureFailed, match="write exceeded"):
            capture._send(dict(type="start"), timeout_seconds=.05)
        assert capture._command_failed and time.monotonic() - started < 1
    finally:
        release.set()
        for writer in capture._writers:
            writer.join(2)
        capture._process = None
        capture.close()
    assert all(not writer.is_alive() for writer in capture._writers)

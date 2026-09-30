"""Real child pipes/Job/interpreter; injected capture, no WGC or AI claim."""
from dataclasses import asdict
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from quest3d.window_capture import WindowCaptureFailed
from quest3d.window_process_capture import ProcessWindowCapture, _WinAPI
from quest3d.window_sources import WindowIdentity


pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Windows child/Job lifetime")
IDENTITY = WindowIdentity(123, 42, 99999, 2, "test_class")

# Execute production window_worker.main(), replacing only capture/publisher and
# source observation. The real buffered stdin, threads, protocol, process exit,
# parent reader, close, actual-child handle and Job remain in use.
CHILD = r'''
import os, sys, threading, time
from pathlib import Path
from types import SimpleNamespace
from quest3d import window_worker as worker

directory, mode = Path(sys.argv[1]), sys.argv[2]
real_stdin = sys.stdin
class Input:
    def readline(self, size):
        if threading.current_thread().name == "Private worker stop":
            (directory / "reader-entered").write_text("reading")
        return real_stdin.buffer.readline(size)
sys.stdin = SimpleNamespace(buffer=Input())

class Capture:
    selection_id, source_id = 1234, "fixture-selection"
    status = dict(state="waiting", reason=None, generation=1)
    def __init__(self, identity, **kwargs):
        self.identity = identity
    def __enter__(self):
        if mode == "startup_failure":
            deadline = time.monotonic() + 3
            while not (directory / "reader-entered").exists():
                if time.monotonic() > deadline:
                    raise RuntimeError("fixture reader did not start")
                time.sleep(.001)
            raise RuntimeError("injected startup failure")
        return self
    def grab(self, **kwargs):
        if (directory / "end-source").exists():
            if mode == "failure":
                raise RuntimeError("injected capture failure")
            raise worker.WindowCaptureFailed("injected source destruction")
        time.sleep(.005)
        raise TimeoutError("fixture awaiting frame")
    def close(self):
        (directory / "capture-closed").write_text("closed")
        if mode == "cleanup_failure":
            raise RuntimeError("injected capture cleanup failure")
class Publisher:
    epoch = 5678
    def __init__(self, *args, **kwargs):
        pass
    def close(self):
        (directory / "publisher-closed").write_text("closed")
worker.GPUWindowCapture, worker.FramePublisher = Capture, Publisher
worker.Win32WindowProvider = lambda **kwargs: SimpleNamespace(
    observe=lambda *args, **kwargs: SimpleNamespace(identity=None))
result = worker.main()
(directory / "main-returned").write_text(str(result))
raise SystemExit(result)
'''


def await_condition(predicate, capture, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.005)
    pytest.fail(f"Child condition deadline: {capture.status}")


@pytest.fixture
def child(tmp_path):
    captures = []

    def launch(mode):
        capture = ProcessWindowCapture(IDENTITY, experimental_window=True, close_timeout_seconds=3)
        captures.append(capture)
        root = Path(__file__).resolve().parents[1]
        env = dict(os.environ, PYTHONPATH=str(root / "src"))
        capture._process = subprocess.Popen([sys.executable, "-u", "-c", CHILD, str(tmp_path), mode],
            cwd=root, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW)
        capture._win = _WinAPI()
        capture._launcher_birth = capture._win.birth(int(capture._process._handle))
        capture._job = capture._win.create_job(int(capture._process._handle))
        capture._reader_thread = threading.Thread(target=capture._messages, name="fixture status reader")
        capture._reader_thread.start()
        capture._send(dict(type="start", nonce=capture.nonce, prefix=capture.prefix,
            identity=asdict(IDENTITY), exclusions=dict(hwnds=[], process_ids=[]), device=0))
        await_condition(lambda: (tmp_path / "reader-entered").exists()
            or capture._child_state is not None and capture._child_state["state"] == "failed", capture)
        return capture

    yield launch
    for capture in captures:
        try:
            capture.close()
        except WindowCaptureFailed:
            pass
        assert capture._process.poll() is not None
        assert not capture._reader_thread.is_alive()


@pytest.mark.parametrize("release", ["parent_close", "stdin_eof"])
def test_source_loss_waits_for_reader_rendezvous_before_cleanup_ack(child, tmp_path, release):
    capture = child("source_ended")
    (tmp_path / "end-source").touch()
    await_condition(lambda: (tmp_path / "capture-closed").exists()
        and (tmp_path / "publisher-closed").exists()
        and (capture._child_state or {}).get("source_ended"), capture)
    # The parent's pipe is intentionally still open. Completed GPU cleanup is
    # not permission to finalize Python while its buffered reader is blocked.
    with pytest.raises(subprocess.TimeoutExpired):
        capture._process.wait(timeout=.15)
    assert capture._child_state["cleanup_ok"] is False
    assert not (tmp_path / "main-returned").exists()
    with pytest.raises(WindowCaptureFailed, match="source_lifetime_ended|worker_ended"):
        capture._check_child()
    if release == "stdin_eof":
        capture._process.stdin.close()
        capture._process.wait(timeout=3)
    capture.close()
    assert capture.close_result["normal_exit"] and not capture.close_result["forced"]
    assert capture.close_result["actual_worker_exited"]
    assert capture._child_state["cleanup_ok"] is True
    assert (tmp_path / "main-returned").read_text() == "0"
    assert not any("Fatal Python" in line for line in capture._logs)


@pytest.mark.parametrize("release", ["parent_close", "stdin_eof"])
def test_stop_or_parent_eof_joins_reader_and_exits_cleanly(child, tmp_path, release):
    capture = child("running")
    if release == "stdin_eof":
        capture._process.stdin.close()
        capture._process.wait(timeout=3)
    capture.close()
    assert capture.close_result["normal_exit"] and not capture.close_result["forced"]
    assert capture._child_state["cleanup_ok"] is True
    assert (tmp_path / "main-returned").read_text() == "0"


@pytest.mark.parametrize("mode", ["failure", "cleanup_failure", "startup_failure"])
def test_failure_rendezvous_preserves_exit_failure_without_reader_abort(child, tmp_path, mode):
    capture = child(mode)
    if mode != "startup_failure":
        (tmp_path / "end-source").touch()
    await_condition(lambda: (tmp_path / "publisher-closed").exists()
        and (capture._child_state or {}).get("state") == "failed", capture)
    assert capture._child_state["cleanup_ok"] is False
    assert not (tmp_path / "main-returned").exists()
    with pytest.raises(WindowCaptureFailed):
        capture.close()
    assert not capture.close_result["normal_exit"] and not capture.close_result["forced"]
    assert capture.close_result["exit_code"] == 1
    assert capture.close_result["actual_worker_exited"]
    assert (tmp_path / "main-returned").read_text() == "1"
    if mode == "cleanup_failure":
        assert capture._child_state["cleanup_ok"] is False
    assert not any("Fatal Python" in line for line in capture._logs)


def test_invalid_stop_after_source_loss_remains_protocol_failure(child, tmp_path):
    capture = child("source_ended")
    (tmp_path / "end-source").touch()
    await_condition(lambda: (tmp_path / "publisher-closed").exists(), capture)
    capture._send(dict(type="stop", nonce="0" * 64))
    capture._process.wait(timeout=3)
    with pytest.raises(WindowCaptureFailed):
        capture.close()
    assert capture.close_result["exit_code"] == 1
    assert not capture.close_result["normal_exit"] and not capture.close_result["forced"]
    assert "Only this worker's stop" in capture._child_state["reason"]
    assert (tmp_path / "main-returned").read_text() == "1"

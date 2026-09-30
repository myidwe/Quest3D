"""Capture scheduling/ownership contract with completed stand-in native copies."""
import threading
from types import SimpleNamespace

import pytest

from quest3d.capture import GPUDesktopCapture


class ImmediateCondition(threading.Condition):
    def wait_for(self, predicate, timeout=None):
        assert timeout == 0, 'Poll must never wait for a future capture'
        return predicate()


def capture():
    value = GPUDesktopCapture.__new__(GPUDesktopCapture)
    value._worker = object()
    value._condition = ImmediateCondition()
    value._latest = None
    value._error = None
    value._ended = False
    value._consumed_frame_id = 0
    value.experimental_hdr = False
    value._rect = None
    value._monitor = 1
    value._generation = 2
    value._source_identity = 123
    value._geometry = SimpleNamespace(bounds=None)
    value.timeout_seconds = 3
    return value


def test_empty_poll_does_not_wait_or_invent_a_source():
    instance = capture()
    assert instance.try_grab() is None
    assert instance._consumed_frame_id == 0


def test_latest_completed_copy_is_owned_and_consumed_once():
    instance = capture()
    pixels = object()
    instance._latest = SimpleNamespace(frame_id=7, pixels=pixels, captured_ns=1234, copy_processing_ms=2.)
    first = instance.try_grab()
    assert first.bgra is pixels
    assert first.frame_id == 7 and first.captured_ns == 1234
    assert instance.try_grab() is None
    instance._latest = SimpleNamespace(frame_id=10, pixels=object(), captured_ns=2345, copy_processing_ms=3.)
    second = instance.try_grab()
    assert second.frame_id == 10 and second.captured_ns == 2345
    assert first.bgra is pixels


def test_poll_does_not_hide_native_error_with_a_retained_frame():
    instance = capture()
    instance._latest = SimpleNamespace(frame_id=1)
    instance._error = ValueError('native copy failed')
    with pytest.raises(RuntimeError, match='native copy failed'):
        instance.try_grab()
    assert instance._consumed_frame_id == 0


def test_poll_reports_end_and_closed_capture():
    instance = capture()
    instance._ended = True
    with pytest.raises(RuntimeError, match='ended'):
        instance.try_grab()
    instance._worker = None
    with pytest.raises(RuntimeError, match='not open'):
        instance.try_grab()


def test_waiting_grab_still_rejects_zero_timeout():
    with pytest.raises(ValueError, match='timeout'):
        capture().grab(timeout_seconds=0)

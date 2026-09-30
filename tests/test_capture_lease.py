"""Quest patch tests: synthetic failure injection and opt-in real WGC/CUDA leases.

Run against an unpacked patched wheel with PYTHONPATH set to its directory.
The hardware tests capture real pixels but never save a screenshot.
"""

import os
import time
from types import SimpleNamespace

import pytest
import torch
wc_cuda = pytest.importorskip("wc_cuda", reason="optional GPU capture package is not installed")


pytestmark = pytest.mark.skipif(getattr(wc_cuda, "QUEST3D_PATCH_VERSION", 0) != 1,
                                reason="requires the Quest-patched wheel")


@pytest.mark.parametrize("failure", [RuntimeError("copy failed"), KeyboardInterrupt()])
def test_failed_or_interrupted_copy_stops_writer_before_releasing_lease(failure):
    operations = []

    class Bridge:
        width, height, texture_ptr = 64, 32, 123

        def update(self, stream, device):
            operations.append("copy")
            raise failure

    instance = wc_cuda.WindowsCapture.__new__(wc_cuda.WindowsCapture)
    instance._bridge = Bridge()
    instance._stream, instance.device_id, instance._failed = None, 0, False
    instance._inner = SimpleNamespace(stop=lambda: operations.append("stop"),
                                      release_frame=lambda number: operations.append(("release", number)))
    frame = SimpleNamespace(width=64, height=32, texture_ptr=123)
    with pytest.raises(type(failure)):
        instance._copy_leased_frame(frame, 9)
    assert operations == ["copy", "stop", ("release", 9)]
    assert instance._failed is True


def test_successful_copy_releases_lease_without_stopping_writer():
    operations = []
    token = object()
    instance = wc_cuda.WindowsCapture.__new__(wc_cuda.WindowsCapture)
    instance._bridge = SimpleNamespace(width=64, height=32, texture_ptr=123,
                                       update=lambda stream, device: token)
    instance._stream, instance.device_id, instance._failed = None, 0, False
    instance._inner = SimpleNamespace(stop=lambda: operations.append("stop"),
                                      release_frame=lambda number: operations.append(("release", number)))
    assert instance._copy_leased_frame(SimpleNamespace(width=64, height=32, texture_ptr=123), 9) is token
    assert operations == [("release", 9)]
    assert instance._failed is False


def _get_frame(native, last_id):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        result = native.get_frame(last_id, timeout=0.1)
        if result is not None:
            return result
        error = native.get_last_error()
        if error:
            raise RuntimeError(error)
    raise TimeoutError("Real WGC source produced no frame")


@pytest.mark.gpu
@pytest.mark.capture
@pytest.mark.skipif(os.environ.get("QUEST3D_TEST_GPU_CAPTURE") != "1", reason="real desktop capture opt-in")
def test_real_cuda_frame_lease_blocks_overwrite_and_survives_stop_restart():
    from quest3d.capture import list_monitors

    monitor = next(item for item in list_monitors() if item.is_primary)
    native = wc_cuda._NativeWcCapture(luid=wc_cuda.get_luid(), monitor_index=monitor.index,
                                     cursor_capture=False)
    bridge = None
    native.start()
    try:
        frame, frame_id = _get_frame(native, 0)
        assert native.lease_protocol_version == 1
        with pytest.raises(RuntimeError, match="outstanding CUDA frame lease"):
            native.get_frame(frame_id, timeout=0.1)
        with pytest.raises(RuntimeError, match="Incorrect"):
            native.release_frame(frame_id + 1)
        bridge = wc_cuda._DX11ToPyTorchBridge(frame.texture_ptr, frame.width, frame.height)
        stream = torch.cuda.Stream()
        first = bridge.update(stream, 0)
        # A held lease covers an arbitrary consumer stall, not only the copy.
        time.sleep(0.35)
        second = bridge.update(stream, 0)
        assert torch.equal(first, second)
        owner, skipped, latest = native.lease_stats()
        assert (owner, latest) == (frame_id, frame_id)
        assert skipped >= 0  # WGC may receive no updates on a static desktop.
        native.stop()
        with pytest.raises(RuntimeError, match="before restarting"):
            native.start()
        native.release_frame(frame_id)
        assert native.lease_stats()[0] is None
        bridge.close()
        bridge = None
        native.start()
        restarted, restarted_id = _get_frame(native, frame_id)
        assert restarted_id > frame_id
        assert restarted.original_width == monitor.bounds.width
        native.release_frame(restarted_id)
    finally:
        native.stop()
        owner = native.lease_stats()[0]
        if owner is not None:
            native.release_frame(owner)
        if bridge is not None:
            bridge.close()


@pytest.mark.gpu
@pytest.mark.capture
@pytest.mark.skipif(os.environ.get("QUEST3D_TEST_GPU_CAPTURE") != "1", reason="real desktop capture opt-in")
def test_real_consumer_exception_and_callback_stop_leave_no_native_lease():
    from quest3d.capture import list_monitors

    monitor = next(item for item in list_monitors() if item.is_primary)
    capture = wc_cuda.WindowsCapture(monitor_index=monitor.index, cursor_capture=False)
    received = []

    @capture.event
    def on_frame_arrived(frame, control):
        received.append(frame.frame_buffer.clone())
        assert capture._inner.lease_stats()[0] is None
        raise RuntimeError("injected consumer failure")

    with pytest.raises(RuntimeError, match="injected consumer failure"):
        capture.start()
    assert capture._inner.lease_stats()[0] is None
    assert not capture._inner.is_alive()
    assert received and int(torch.count_nonzero(received[0]).item()) > 0

    @capture.event
    def on_frame_arrived(frame, control):
        assert capture._inner.lease_stats()[0] is None
        control.stop()

    capture.start()
    assert capture._inner.lease_stats()[0] is None
    assert not capture._inner.is_alive()

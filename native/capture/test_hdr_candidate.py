"""Format/stride regressions; fake CUDA only checks copy arguments, not HDR quality."""

from types import SimpleNamespace

import pytest
import torch
import wc_cuda


pytestmark = pytest.mark.skipif(getattr(wc_cuda, "QUEST3D_PATCH_VERSION", 0) not in (2, 3),
                                reason="requires unpacked experimental FP16 candidate wheel")


@pytest.mark.parametrize("color_format,dtype,bpp", [("bgra8", torch.uint8, 4), ("rgba16f", torch.float16, 8)])
def test_copy_uses_format_byte_pitch_and_preserves_unmap_before_return(monkeypatch, color_format, dtype, bpp):
    calls = []

    class Cuda:
        def __getattr__(self, name):
            def call(*args):
                calls.append((name, args))
                return 0
            return call

    real_empty = torch.empty
    monkeypatch.setattr(wc_cuda, "_get_cudart", lambda: Cuda())
    monkeypatch.setattr(torch, "empty", lambda shape, *, dtype, device: real_empty(shape, dtype=dtype))
    stream = SimpleNamespace(cuda_stream=19, synchronize=lambda: calls.append(("sync", ())))
    bridge = wc_cuda._DX11ToPyTorchBridge(123, 128, 64, color_format=color_format)
    try:
        tensor = bridge.update(stream, 0)
        assert tensor.dtype == dtype
        assert tensor.shape == (64, 128, 4)
        copy = next(args for name, args in calls if name == "cudaMemcpy2DFromArrayAsync")
        assert copy[1] == copy[5] == 128 * bpp
        assert copy[6:8] == (64, 3)
        sequence = [name for name, _ in calls]
        assert sequence[-3:] == ["sync", "cudaGraphicsUnmapResources", "sync"]
        # Cropping padded storage leaves a stride which must remain explicit.
        frame = wc_cuda.Frame(tensor[:33, :65], 65, 33, color_format)
        assert frame.row_pitch_bytes == 128 * bpp
        assert frame.color_format == color_format
    finally:
        bridge.close()


@pytest.mark.parametrize("value", [None, "rgba8", "RGBA16F", "", 16])
def test_unknown_format_rejected_before_initializing_cuda(monkeypatch, value):
    monkeypatch.setattr(wc_cuda, "get_luid", lambda *_: pytest.fail("CUDA must not initialize"))
    with pytest.raises(ValueError):
        wc_cuda.WindowsCapture(color_format=value)


def test_native_unknown_format_rejected_before_start():
    with pytest.raises(ValueError):
        wc_cuda._NativeWcCapture(color_format="rgba8")


def test_wrong_native_pitch_stops_writer_and_releases_lease():
    calls = []
    instance = wc_cuda.WindowsCapture.__new__(wc_cuda.WindowsCapture)
    instance._inner = SimpleNamespace(stop=lambda: calls.append("stop"),
                                     release_frame=lambda frame_id: calls.append(("release", frame_id)))
    frame = SimpleNamespace(width=64, height=32, color_format="rgba16f", row_pitch_bytes=256)
    with pytest.raises(RuntimeError, match="row pitch"):
        instance._copy_leased_frame(frame, 9)
    assert calls == ["stop", ("release", 9)]


def test_bridge_recreated_when_format_changes_with_same_texture_pointer(monkeypatch):
    calls = []
    instance = wc_cuda.WindowsCapture.__new__(wc_cuda.WindowsCapture)
    instance.device_id, instance._stream = 0, None
    instance._inner = SimpleNamespace(stop=lambda: calls.append("stop"),
                                     release_frame=lambda number: calls.append(("release", number)))
    instance._bridge = SimpleNamespace(width=64, height=32, texture_ptr=123, color_format="bgra8",
                                      close=lambda: calls.append("close"))
    def factory(pointer, width, height, device, color_format):
        calls.append(("create", color_format))
        return SimpleNamespace(update=lambda *_: "half tensor")
    monkeypatch.setattr(wc_cuda, "_DX11ToPyTorchBridge", factory)
    frame = SimpleNamespace(width=64, height=32, texture_ptr=123, color_format="rgba16f", row_pitch_bytes=512)
    assert instance._copy_leased_frame(frame, 10) == "half tensor"
    assert calls == ["close", ("create", "rgba16f"), ("release", 10)]

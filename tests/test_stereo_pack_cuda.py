"""Packing lifetime checks and opt-in real CUDA numerical boundary checks.

GPU tests: $env:QUEST3D_RUN_GPU_TESTS='1'; python -m pytest tests/test_stereo_pack_cuda.py
The environment gate prevents ordinary test collection from initializing CUDA.
"""
from contextlib import nullcontext
import ctypes as C
import os
from types import SimpleNamespace
import threading

import numpy as np
import pytest
import torch

from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer
from quest3d.stereo_pack_cuda import CudaStereoPacker


def _uninitialized_owner():
    owner = object.__new__(CudaStereoPacker)
    owner.module = C.c_void_p(123)
    owner.closed = False
    owner.faulted = False
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = ("retained stream", "retained source", "retained GPU/CPU output")
    return owner


def test_failed_completion_retains_inputs_and_blocks_new_calls():
    owner = _uninitialized_owner()
    retained = owner._inflight
    def fail():
        raise RuntimeError("injected asynchronous completion error")
    with pytest.raises(RuntimeError, match="completion error"):
        owner._complete(SimpleNamespace(synchronize=fail))
    assert owner.faulted and owner._inflight is retained
    # Faulted owners refuse even otherwise malformed inputs without another launch.
    with pytest.raises(RuntimeError, match="closed or faulted"):
        owner(None, 16, 16, (0, 0, 16, 16))


def test_failed_close_keeps_module_until_successful_completion(monkeypatch):
    owner = _uninitialized_owner()
    retained = owner._inflight
    unloaded = []
    owner.driver = SimpleNamespace(cuModuleUnload=lambda module: unloaded.append(module.value) or 0)
    monkeypatch.setattr(torch.cuda, "device", lambda device: nullcontext())
    def fail(device):
        raise RuntimeError("injected close synchronization error")
    monkeypatch.setattr(torch.cuda, "synchronize", fail)
    with pytest.raises(RuntimeError, match="close synchronization"):
        owner.close()
    assert owner.module.value == 123 and owner._inflight is retained
    assert owner.faulted and not owner.closed and not unloaded
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: None)
    owner.close()
    assert unloaded == [123] and owner.module.value is None
    assert owner.closed and owner._inflight is None
    owner.close()
    assert unloaded == [123]


def test_synth_close_retains_failed_packer_and_still_closes_other_owner(monkeypatch):
    from quest3d import forward_warp_cuda
    calls = []
    class Packer:
        fail = True
        def close(self):
            calls.append("packer")
            if self.fail:
                raise RuntimeError("packer completion unavailable")
    synth = StereoSynthesizer(32, 18, 1, stereo_method="forward-cuda")
    packer = Packer()
    synth._stereo_packer = packer
    monkeypatch.setattr(forward_warp_cuda, "close_cached_forward_warp_cuda", lambda: calls.append("projector"))
    with pytest.raises(RuntimeError, match="stereo pack cleanup"):
        synth.close()
    assert calls == ["projector", "packer"] and synth._stereo_packer is packer
    packer.fail = False
    synth.close()
    assert calls == ["projector", "packer", "projector", "packer"]
    assert synth._stereo_packer is None


def test_original_2d_does_not_initialize_cuda_packer(monkeypatch):
    from quest3d import stereo_pack_cuda
    monkeypatch.setattr(stereo_pack_cuda, "CudaStereoPacker", lambda **kw: pytest.fail("2D initialized a packer"))
    source = np.full((18, 32, 4), 123, dtype=np.uint8)
    source[:, :, 3] = 255
    synth = StereoSynthesizer(32, 18, 0, stereo_method="forward-cuda")
    depth = DepthResult(1, 0, torch.ones(1, 4, 7), (4, 7), 0, 0)
    result = synth.synthesize(source, depth, frame_id=1, generation=0)
    np.testing.assert_array_equal(result.bgra[:, :32], source)
    np.testing.assert_array_equal(result.bgra[:, 32:], source)
    assert synth._stereo_packer is None


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                               reason="Opt-in local CUDA test; set QUEST3D_RUN_GPU_TESTS=1")


def _reference(eyes, ew, eh, rect):
    x, y, width, height = rect
    quantized = (eyes.clamp(0, 1) * 255).round().to(torch.uint8).permute(0, 2, 3, 1)
    packed = torch.zeros((eh, ew * 2, 4), dtype=torch.uint8, device=eyes.device)
    packed[:, :, 3] = 255
    packed[y:y + height, x:x + width, :3] = quantized[0]
    packed[y:y + height, ew + x:ew + x + width, :3] = quantized[1]
    return packed.cpu()


@pytest.mark.gpu
@_real_gpu
def test_cuda_rounding_letterbox_and_frame_ownership_exact():
    half = (torch.arange(255, dtype=torch.float32) + .5) / 255
    values = torch.cat((half, torch.nextafter(half, torch.full_like(half, -float("inf"))),
                        torch.nextafter(half, torch.full_like(half, float("inf"))),
                        torch.tensor([-float("inf"), -1, 0, 1, 2, float("inf"), float("nan")]))).cuda()
    with CudaStereoPacker() as pack:
        for width, height, ew, eh, ox, oy in ((129, 1, 133, 7, 1, 3),
                                           (41, 53, 76, 62, 11, 4),
                                           (1920, 1080, 1920, 1080, 0, 0)):
            total = 6 * width * height
            eyes = values.repeat((total + values.numel() - 1) // values.numel())[:total].reshape(2, 3, height, width)
            original = eyes.clone()
            rect = (ox, oy, width, height)
            expected = _reference(eyes, ew, eh, rect)
            actual = pack(eyes, ew, eh, rect)
            assert torch.equal(actual, expected)
            torch.testing.assert_close(eyes, original, atol=0, rtol=0, equal_nan=True)
            assert actual.device.type == "cpu" and actual.is_pinned()
            held = actual.clone()
            pack(torch.zeros_like(eyes), ew, eh, rect)
            assert torch.equal(actual, held)
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            result = pack(eyes, ew, eh, rect)
        assert torch.equal(result, expected)


@pytest.mark.gpu
@_real_gpu
def test_cuda_packer_rejects_wrong_shapes_devices_and_rectangles():
    with CudaStereoPacker() as pack:
        eyes = torch.ones((2, 3, 9, 16), device="cuda")
        for invalid in (eyes.cpu(), eyes.half(), eyes[:, :, :, ::2], eyes[:1], eyes[0]):
            with pytest.raises(ValueError, match="contiguous float32"):
                pack(invalid, 16, 9, (0, 0, 16, 9))
        for rect in ((-1, 0, 16, 9), (0, 1, 16, 9), (0, 0, 15, 9), (0, 0, 16, 0)):
            with pytest.raises(ValueError, match="Content rectangle"):
                pack(eyes, 16, 9, rect)
        with pytest.raises(ValueError, match="Eye dimensions"):
            pack(eyes, 8193, 9, (0, 0, 16, 9))
        assert not pack.faulted and pack._inflight is None

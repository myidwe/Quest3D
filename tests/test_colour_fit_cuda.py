"""CPU lifetime/contracts and explicitly gated Torch-CUDA AA exact checks."""
from collections import OrderedDict
from contextlib import nullcontext
import ctypes as C
import os
import threading
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d.colour_fit import fit_bgra_float
from quest3d.colour_fit_cuda import CudaFloatColourFit


def _owner():
    owner = object.__new__(CudaFloatColourFit)
    owner.module = C.c_void_p(123)
    owner.closed = owner.faulted = False
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = None
    owner._cache = OrderedDict()
    owner.cache_hits = owner.cache_misses = 0
    owner.last_execution = {"effective": "not_run", "reason": None}
    owner.metadata = {}
    owner.functions = {name: C.c_void_p(456) for name in ("same_size", "horizontal", "vertical")}
    return owner


@pytest.mark.parametrize("source,eye,rectangle,taps", [
    ((3840, 2160), (1920, 1080), (0, 0, 1920, 1080), (11, 11)),
    ((1920, 1080), (1920, 1080), (0, 0, 1920, 1080), (1, 1)),
    ((31, 31), (48, 36), (6, 0, 36, 36), (7, 7)),
    ((121, 79), (200, 100), (23, 0, 153, 100), (7, 7)),
])
def test_metadata_geometry_uses_original_rounding_and_bounds(source, eye, rectangle, taps):
    status = CudaFloatColourFit.support_status(*source, *eye)
    assert status["supported"] and status["reason"] is None
    assert status["content_rect"] == rectangle
    assert (status["taps_x"], status["taps_y"]) == taps


@pytest.mark.parametrize("source,eye,reason", [
    ((20000, 1), (1920, 1080), "dimension_limit"),
    ((128, 128), (1, 1), "axis_tap_limit"),
    ((8192, 8192), (8191, 8191), "intermediate_limit"),
])
def test_bounded_out_geometry_has_explicit_same_quality_fallback_reason(source, eye, reason):
    status = CudaFloatColourFit.support_status(*source, *eye)
    assert not status["supported"] and status["reason"] == reason


@pytest.mark.parametrize("image,width,height", [
    (None, 16, 16), (torch.zeros((2, 3, 3), dtype=torch.uint8), 16, 16),
    (torch.zeros((2, 3, 4)), 16, 16), (torch.zeros((0, 3, 4), dtype=torch.uint8), 16, 16),
    (torch.zeros((2, 3, 4), dtype=torch.uint8), True, 16),
    (torch.zeros((2, 3, 4), dtype=torch.uint8), 16, 0),
])
def test_invalid_input_is_not_a_fallback(image, width, height):
    owner = _owner()
    with pytest.raises(ValueError):
        owner(image, width, height)
    assert not owner.faulted and owner._inflight is None


def test_cpu_input_is_not_uploaded_or_fallback():
    owner = _owner()
    with pytest.raises(ValueError, match="owner's CUDA device"):
        owner(torch.zeros((2, 3, 4), dtype=torch.uint8), 3, 2)


def test_lazy_negative_view_is_explicitly_rejected_before_pointer_access():
    source = torch._neg_view(torch.ones((2, 3, 4), dtype=torch.uint8))
    assert source.is_neg()
    with pytest.raises(ValueError, match="Lazy-negative"):
        CudaFloatColourFit._validate_layout(source, 3, 2)


def test_failed_completion_retains_every_kernel_input_and_blocks_next_call():
    owner = _owner()
    owner.driver = SimpleNamespace(cuLaunchKernel=lambda *args: 0)
    source = torch.zeros((5, 7, 4), dtype=torch.uint8)
    output, temporary = torch.empty((1, 3, 3, 4)), torch.empty((1, 3, 5, 4))
    coefficients = tuple(torch.empty((4, 7)) for _ in range(4))
    def fail():
        raise RuntimeError("injected completion failure")
    stream = SimpleNamespace(cuda_stream=11, synchronize=fail)
    with pytest.raises(RuntimeError, match="completion failure"):
        owner._execute(source, output, temporary, coefficients, stream)
    assert owner.faulted
    assert all(a is b for a, b in zip(owner._inflight, (stream, source, output, temporary, *coefficients)))
    with pytest.raises(RuntimeError, match="closed or faulted"):
        owner(source, 4, 3)


def test_kernel_launch_failure_propagates_even_when_stream_completes():
    owner = _owner()
    owner.driver = SimpleNamespace(cuLaunchKernel=lambda *args: 999)
    completed = []
    stream = SimpleNamespace(cuda_stream=11, synchronize=lambda: completed.append(True))
    with pytest.raises(RuntimeError, match="code 999"):
        owner._execute(torch.zeros((2, 3, 4), dtype=torch.uint8), torch.empty((1, 3, 2, 3)), None, (), stream)
    assert completed == [True] and owner.faulted and owner._inflight is None


def test_close_failure_retains_module_and_cache_until_success(monkeypatch):
    owner = _owner()
    owner._inflight = ("stream", "source", "temporary", "output")
    retained = owner._inflight
    owner._cache["axis"] = object()
    unloaded = []
    owner.driver = SimpleNamespace(cuModuleUnload=lambda module: unloaded.append(module.value) or 0)
    monkeypatch.setattr(torch.cuda, "device", lambda device: nullcontext())
    def fail(device):
        raise RuntimeError("injected close failure")
    monkeypatch.setattr(torch.cuda, "synchronize", fail)
    with pytest.raises(RuntimeError, match="close failure"):
        owner.close()
    assert owner._inflight is retained and owner._cache and owner.module.value == 123
    assert owner.faulted and not owner.closed and not unloaded
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: None)
    owner.close()
    assert owner.closed and not owner._cache and owner._inflight is None
    assert owner.module.value is None and unloaded == [123]
    owner.close()
    assert unloaded == [123]


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                              reason="Requires the coordinator's exclusive CUDA slot")


@pytest.mark.gpu
@_real_gpu
def test_actual_cuda_aa_exact_for_boundaries_stride_letterbox_autocast_and_lifetime():
    rng = np.random.default_rng(8172)
    cases = [((1440, 2560), (1920, 1080)), ((2160, 3840), (1920, 1080)),
             ((23, 39), (63, 41)), ((31, 31), (48, 36)), ((79, 121), (200, 100)),
             ((17, 29), (11, 7)), ((1, 2), (9, 7)), ((3, 1), (1, 13)),
             ((19, 19), (19, 19)), ((1, 1), (1, 1)), ((1, 1), (9, 7))]
    retained = []
    with CudaFloatColourFit() as candidate:
        for (height, width), (ew, eh) in cases:
            backing = torch.from_numpy(rng.integers(0, 256, (height + 2, width * 2 + 3, 8), dtype=np.uint8)).cuda()
            source = backing[1:height + 1, 1:width * 2 + 1:2, ::2]
            original = source.clone()
            expected = fit_bgra_float(source, ew, eh)
            with torch.autocast("cuda", dtype=torch.float16):
                actual = candidate(source, ew, eh)
            assert actual.content_rect == expected.content_rect
            assert torch.equal(actual.image.view(torch.int32), expected.image.view(torch.int32))
            assert actual.image.dtype == torch.float32 and actual.image.is_contiguous()
            assert actual.image.data_ptr() != source.data_ptr() and torch.equal(source, original)
            retained.append((actual.image, actual.image.clone()))
        for value in (0, 1, 127, 128, 254, 255):
            source = torch.full((17, 31, 4), value, dtype=torch.uint8, device="cuda")
            for eye in ((31, 17), (48, 36), (13, 9)):
                expected = fit_bgra_float(source, *eye).image
                actual = candidate(source, *eye).image
                assert torch.equal(actual.view(torch.int32), expected.view(torch.int32))
        source = torch.zeros((17, 31, 4), dtype=torch.uint8, device="cuda")
        source[:, 15:, :3] = 255
        expected = fit_bgra_float(source, 93, 51).image
        actual = candidate(source, 93, 51).image
        assert torch.equal(actual.view(torch.int32), expected.view(torch.int32))
        assert float(actual.min()) == 0 and float(actual.max()) == 1
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            changed_stream = candidate(source, 93, 51).image
        assert torch.equal(changed_stream.view(torch.int32), expected.view(torch.int32))
        assert all(torch.equal(value, saved) for value, saved in retained)
        assert candidate.status()["coefficient_cache_entries"] <= candidate.MAX_CACHE_AXES
    assert all(torch.equal(value.view(torch.int32), saved.view(torch.int32)) for value, saved in retained)


@pytest.mark.gpu
@_real_gpu
def test_actual_cuda_unsupported_ratio_falls_back_to_unchanged_torch():
    with CudaFloatColourFit() as candidate:
        source = torch.arange(32 * 32 * 4, dtype=torch.int64, device="cuda").remainder(256).byte().reshape(32, 32, 4)
        expected = fit_bgra_float(source, 1, 1)
        actual = candidate(source, 1, 1)
        assert torch.equal(actual.image.view(torch.int32), expected.image.view(torch.int32))
        assert candidate.status()["effective"] == "torch"
        assert candidate.status()["reason"] == "axis_tap_limit"
        assert not candidate.faulted


@pytest.mark.gpu
@_real_gpu
def test_actual_cuda_preserves_original_torch_extreme_ratio_failure():
    source = torch.zeros((128, 128, 4), dtype=torch.uint8, device="cuda")
    with pytest.raises(RuntimeError, match="Too much shared memory required"):
        fit_bgra_float(source, 1, 1)
    with CudaFloatColourFit() as candidate:
        with pytest.raises(RuntimeError, match="Too much shared memory required"):
            candidate(source, 1, 1)
        assert candidate.faulted
        assert candidate.status()["effective"] == "torch"
        assert candidate.status()["reason"] == "axis_tap_limit"

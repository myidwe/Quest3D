"""Same-contract CUDA value validation, with explicit GPU/benchmark gates."""
from contextlib import nullcontext
import ctypes as C
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading
import time

import numpy as np
import pytest
import torch

from quest3d import forward_warp_cuda
from quest3d.forward_warp import _validate, _validate_metadata
from quest3d.forward_warp_cuda import CudaForwardWarper


def _owner():
    owner = object.__new__(CudaForwardWarper)
    owner.module = C.c_void_p(123)
    owner.closed = owner.faulted = False
    owner.fused_fill = True
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = None
    return owner


def _small_pair():
    return torch.full((1, 3, 2, 5), .5), torch.full((2, 5), .5)


def test_metadata_does_not_read_values_but_validate_still_does(monkeypatch):
    image, depth = _small_pair()
    depth[1, 4] = float("nan")
    original = torch.isfinite
    monkeypatch.setattr(torch, "isfinite", lambda *args: pytest.fail("Metadata read pixel values"))
    _validate_metadata(image, depth, 0, .5)
    monkeypatch.setattr(torch, "isfinite", original)
    with pytest.raises(ValueError, match="finite and in"):
        _validate(image, depth, 0, .5)


@pytest.mark.parametrize("case", ["not_tensor", "shape", "depth_shape", "dtype", "device",
    "negative_d", "nan_d", "infinite_d", "too_large_d", "bool_d", "string_d",
    "negative_c", "above_c", "nan_c", "infinite_c", "bool_c", "string_c"])
def test_metadata_and_original_validation_keep_same_errors(case):
    image, depth = _small_pair()
    disparity, convergence = 1, .5
    if case == "not_tensor": image = None
    elif case == "shape": image = image[0]
    elif case == "depth_shape": depth = depth[:1]
    elif case == "dtype": image = image.half()
    elif case == "device": depth = torch.empty(depth.shape, device="meta")
    elif case == "negative_d": disparity = -1
    elif case == "nan_d": disparity = float("nan")
    elif case == "infinite_d": disparity = float("inf")
    elif case == "too_large_d": disparity = torch.finfo(torch.float32).max / 2
    elif case == "bool_d": disparity = True
    elif case == "string_d": disparity = "1"
    elif case == "negative_c": convergence = -.01
    elif case == "above_c": convergence = 1.01
    elif case == "nan_c": convergence = float("nan")
    elif case == "infinite_c": convergence = float("inf")
    elif case == "bool_c": convergence = False
    else: convergence = "0.5"
    messages = []
    for validate in (_validate_metadata, _validate):
        with pytest.raises(ValueError) as error:
            validate(image, depth, disparity, convergence)
        messages.append(str(error.value))
    assert messages[0] == messages[1]


@pytest.mark.parametrize("bad", [None, 0, 1, "yes", [], {}])
def test_opt_in_rejects_non_boolean_without_cuda(bad, monkeypatch):
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("Invalid flag initialized CUDA"))
    owner = _owner()
    with pytest.raises(ValueError, match="explicit boolean"):
        owner.packed(None, None, 0, .5, 1, 1, (0, 0, 1, 1), fused_validation=bad)
    with pytest.raises(ValueError, match="explicit boolean"):
        forward_warp_cuda.synthesize_forward_packed_cuda(None, None, 0, .5, 1, 1, (0, 0, 1, 1),
                                                        fused_validation=bad)


def test_default_packed_still_calls_original_torch_validation(monkeypatch):
    image, depth = _small_pair()
    owner = _owner()
    seen = []
    class ReachedOriginal(Exception): pass
    def reference(*args):
        seen.append(args)
        raise ReachedOriginal()
    monkeypatch.setattr(forward_warp_cuda, "_validate", reference)
    with pytest.raises(ReachedOriginal):
        owner.packed(image, depth, 0, .5, 5, 2, (0, 0, 5, 2))
    assert len(seen) == 1 and not owner.faulted


def test_fused_convenience_reuses_same_cached_owner(monkeypatch):
    created, calls = [], []
    class Image:
        is_cuda = True
        device = SimpleNamespace(index=0)
    class Owner:
        def __init__(self, *, device, fused_fill):
            assert device == 0 and fused_fill
            created.append(self)
        def packed(self, *args, **kwargs):
            calls.append(kwargs)
            return kwargs
    monkeypatch.setattr(forward_warp_cuda, "torch", SimpleNamespace(Tensor=Image))
    monkeypatch.setattr(forward_warp_cuda, "CudaForwardWarper", Owner)
    monkeypatch.setattr(forward_warp_cuda, "_owners", {})
    args = (Image(), None, 0, .5, 1, 1, (0, 0, 1, 1))
    forward_warp_cuda.synthesize_forward_packed_cuda(*args)
    forward_warp_cuda.synthesize_forward_packed_cuda(*args, fused_validation=True)
    forward_warp_cuda.synthesize_forward_packed_cuda(*args, fused_validation=False)
    assert len(created) == 1 and calls == [{}, {"fused_validation": True}, {}]


@pytest.mark.parametrize("invalid", [False, True])
def test_value_receipt_read_only_after_completion_and_bad_values_do_not_fault(invalid):
    owner = _owner()
    image, depth = _small_pair()
    flag = torch.zeros((), dtype=torch.int32)
    events = []
    owner._launch = lambda *args: events.append("validate")
    def read():
        assert events == ["validate", "copy", "complete"]
        events.append("read")
        return int(invalid)
    receipt = SimpleNamespace(copy_=lambda *args, **kw: events.append("copy"), item=read)
    stream = SimpleNamespace(synchronize=lambda: events.append("complete"))
    context = pytest.raises(ValueError, match="finite and in") if invalid else nullcontext()
    with context:
        owner._execute_validation(image, depth, image, depth, flag, receipt, stream)
    assert events == ["validate", "copy", "complete", "read"]
    assert owner._inflight is None and not owner.faulted


@pytest.mark.parametrize("failure", ["launch", "copy", "sync"])
def test_validation_failure_retains_sources_flag_receipt_and_failed_close(failure, monkeypatch):
    owner = _owner()
    image, depth = _small_pair()
    rgb, contiguous_depth = image.clone(), depth.clone()
    flag = torch.zeros((), dtype=torch.int32)
    def launch(*args):
        if failure == "launch": raise RuntimeError("injected launch")
    def copy(*args, **kwargs):
        if failure == "copy": raise RuntimeError("injected copy")
    def incomplete():
        raise RuntimeError("injected incomplete validation")
    owner._launch = launch
    receipt = SimpleNamespace(copy_=copy, item=lambda: pytest.fail("Read an incomplete receipt"))
    stream = SimpleNamespace(synchronize=incomplete)
    with pytest.raises(RuntimeError, match="incomplete validation"):
        owner._execute_validation(image, depth, rgb, contiguous_depth, flag, receipt, stream)
    retained = owner._inflight
    expected = (stream, image, depth, rgb, contiguous_depth, flag, receipt)
    assert all(a is b for a, b in zip(retained, expected)) and owner.faulted
    unloaded = []
    owner.driver = SimpleNamespace(cuModuleUnload=lambda module: unloaded.append(module.value) or 0)
    monkeypatch.setattr(torch.cuda, "device", lambda device: nullcontext())
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: incomplete())
    with pytest.raises(RuntimeError, match="incomplete validation"):
        owner.close()
    assert owner._inflight is retained and owner.module.value == 123 and not unloaded
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: None)
    owner.close()
    assert unloaded == [123] and owner._inflight is None and owner.closed


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                               reason="Explicit serialized CUDA window required")


def _check_values(owner, image, depth):
    _validate_metadata(image, depth, 0, .5)
    with torch.cuda.device(owner.device):
        owner._validate_values_cuda(image, depth, image.contiguous(), depth.contiguous(),
                                    torch.cuda.current_stream(owner.device))


def _same_value_outcome(owner, image, depth):
    errors = []
    for check in (lambda: _validate(image, depth, 0, .5), lambda: _check_values(owner, image, depth)):
        try:
            check()
            errors.append(None)
        except ValueError as error:
            errors.append(str(error))
    assert errors[0] == errors[1]
    assert not owner.faulted and owner._inflight is None
    return errors[0] is None


@pytest.mark.gpu
@_real_gpu
def test_fused_validation_exact_boundaries_strides_negative_views_and_flag_reset():
    with CudaForwardWarper(fused_fill=True) as owner:
        for height, width in ((1, 1), (3, 7), (17, 257), (1080, 1920)):
            image = torch.rand((1, 3, height, width), device="cuda")
            depth = torch.rand((height, width), device="cuda")
            assert _same_value_outcome(owner, image, depth)
            for target in (image, depth):
                before = target.clone()
                last = (-1,) * target.ndim
                values = [-0., 0., 1., torch.nextafter(torch.tensor(0.), torch.tensor(1.)).item(),
                          float("nan"), float("inf"), -float("inf"),
                          torch.nextafter(torch.tensor(0.), torch.tensor(-float("inf"))).item(),
                          torch.nextafter(torch.tensor(1.), torch.tensor(float("inf"))).item()]
                for value in values:
                    target[last] = value
                    assert _same_value_outcome(owner, image, depth) == (0 <= value <= 1)
                    target.copy_(before)
                    assert _same_value_outcome(owner, image, depth)
        backing = torch.full((1, 3, 21, 35), float("nan"), device="cuda")
        depth_backing = torch.full((21, 35), float("nan"), device="cuda")
        image, depth = backing[:, :, 1:20:2, 1:34:2], depth_backing[1:20:2, 1:34:2]
        image.fill_(.5)
        depth.fill_(.5)
        assert _same_value_outcome(owner, image, depth)  # outside-view NaNs ignored
        for a, b in ((image.transpose(-1, -2), depth.T),
                     (image[:, :, :1, :].expand(-1, -1, 10, -1), depth[:1].expand(10, -1)),
                     (torch._neg_view(-image.contiguous()), torch._neg_view(-depth.contiguous()))):
            assert _same_value_outcome(owner, a, b)
        assert not _same_value_outcome(owner, torch._neg_view(image.contiguous()), depth)
        image[0, 2, -1, -1] = float("nan")
        assert not _same_value_outcome(owner, image, depth)


@pytest.mark.gpu
@_real_gpu
def test_fused_validation_packed_pixels_masks_bad_input_and_current_stream(monkeypatch):
    with CudaForwardWarper(fused_fill=True) as owner:
        image = torch.rand((1, 3, 23, 131), device="cuda")[:, :, 1:22:2, 1:130:2]
        depth = torch.rand((23, 131), device="cuda")[1:22:2, 1:130:2]
        height, width = depth.shape
        args = (24.24, .625, width + 5, height + 7, (2, 3, width, height))
        expected = owner.packed(image, depth, *args)
        actual = owner.packed(image, depth, *args, fused_validation=True)
        fields = ("cpu_bgra", "hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")
        held = {name: getattr(actual, name).clone() for name in fields}
        for name in fields:
            assert torch.equal(getattr(actual, name), getattr(expected, name))
        assert actual.owner_metadata["validation"] == "fused-cuda"
        original_launch = owner._launch
        launched = []
        def record(name, *params):
            launched.append(name)
            return original_launch(name, *params)
        monkeypatch.setattr(owner, "_launch", record)
        bad = depth.clone()
        bad[-1, -1] = float("nan")
        for disparity in (0., 24.24):
            launched.clear()
            with pytest.raises(ValueError, match="finite and in"):
                owner.packed(image, bad, disparity, *args[1:], fused_validation=True)
            assert launched == ["quest3d_forward_validate_values"] and not owner.faulted
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            changed = owner.packed(image, depth, *args, fused_validation=True)
        for name in fields:
            assert torch.equal(getattr(changed, name), getattr(expected, name))
            assert torch.equal(getattr(actual, name), held[name])
    for name in fields:
        assert torch.equal(getattr(actual, name), held[name])


@pytest.mark.gpu
@_real_gpu
def test_lazy_negative_native_projection_matches_logical_positive_copies():
    height, width = 9, 65
    positive_image = torch.linspace(0, 1, 3 * height * width, device="cuda").reshape(1, 3, height, width)
    positive_depth = torch.linspace(0, 1, width, device="cuda")[None].expand(height, -1).clone()
    image_storage, depth_storage = -positive_image, -positive_depth
    image, depth = torch._neg_view(image_storage), torch._neg_view(depth_storage)
    saved_image, saved_depth = image_storage.clone(), depth_storage.clone()
    assert image.is_neg() and depth.is_neg()
    fields = ("hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")
    for fused_fill in (False, True):
        with CudaForwardWarper(fused_fill=fused_fill) as owner:
            for disparity in (0., 24.24):
                expected = owner(positive_image, positive_depth, disparity, .625)
                actual = owner(image, depth, disparity, .625)
                assert torch.equal(actual.eyes.view(torch.int32), expected.eyes.view(torch.int32))
                for field in fields:
                    assert torch.equal(getattr(actual, field), getattr(expected, field))
                if fused_fill:
                    args = (disparity, .625, width + 5, height + 7, (2, 3, width, height))
                    expected_packed = owner.packed(positive_image, positive_depth, *args)
                    for fused_validation in (False, True):
                        packed = owner.packed(image, depth, *args, fused_validation=fused_validation)
                        assert torch.equal(packed.cpu_bgra, expected_packed.cpu_bgra)
                        for field in fields:
                            assert torch.equal(getattr(packed, field), getattr(expected_packed, field))
                assert image.is_neg() and depth.is_neg()
                assert torch.equal(image_storage.view(torch.int32), saved_image.view(torch.int32))
                assert torch.equal(depth_storage.view(torch.int32), saved_depth.view(torch.int32))
                assert torch.equal(image, positive_image) and torch.equal(depth, positive_depth)


@pytest.mark.gpu
@_real_gpu
@pytest.mark.skipif(not os.environ.get("QUEST3D_VALIDATION_BENCHMARK_DIR"),
                    reason="Set a fresh benchmark directory during the granted GPU window")
def test_validation_component_microbenchmark():
    directory = Path(os.environ["QUEST3D_VALIDATION_BENCHMARK_DIR"])
    directory.mkdir(parents=True, exist_ok=False)
    generator = torch.Generator(device="cuda").manual_seed(5005)
    image = torch.rand((1, 3, 1080, 1920), generator=generator, device="cuda")
    depth = torch.rand((1080, 1920), generator=generator, device="cuda")
    stream = torch.cuda.current_stream()
    rows = []
    with CudaForwardWarper(fused_fill=True) as owner:
        methods = {"torch": lambda: _validate(image, depth, 24.24, .625),
                   "fused": lambda: _check_values(owner, image, depth)}
        for iteration in range(132):
            order = list(methods) if iteration % 2 == 0 else list(reversed(methods))
            for name in order:
                stream.synchronize()
                begin, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                started = time.perf_counter_ns()
                begin.record(stream)
                methods[name]()
                end.record(stream)
                end.synchronize()
                if iteration >= 12:
                    rows.append({"method": name, "wall_ms": (time.perf_counter_ns() - started) / 1e6,
                                 "cuda_event_ms": begin.elapsed_time(end)})
        root = Path(__file__).resolve().parents[1]
        result = {"scope": "value validation only, fixed valid synthetic float32 input; no FPS claim",
            "shape": [1080, 1920], "samples_per_method": 120, "torch": torch.__version__,
            "gpu": torch.cuda.get_device_name(), "owner": owner.metadata,
            "source_sha256": {str(path): hashlib.sha256((root / path).read_bytes()).hexdigest() for path in
                ("src/quest3d/forward_warp.py", "src/quest3d/forward_warp_cuda.py", "src/quest3d/shaders/forward_warp.cu")},
            "timings": {name: {key: dict(zip(("p50", "p95", "p99"), map(float,
                np.percentile([r[key] for r in rows if r["method"] == name], [50, 95, 99]))))
                for key in ("wall_ms", "cuda_event_ms")} for name in methods}}
    (directory / "result.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    (directory / "timings.json").write_text(json.dumps(rows), encoding="utf-8")
    print(json.dumps(result, indent=2))

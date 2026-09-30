"""Conservative source-search bounds; GPU work requires an explicit reserved slot."""
from contextlib import nullcontext
import ctypes as C
import json
import os
from pathlib import Path
from types import SimpleNamespace
import threading

import numpy as np
import pytest
import torch

from quest3d import forward_warp_cuda as module
from quest3d.forward_warp_cuda import CudaForwardWarper


def owner_stub():
    owner = object.__new__(CudaForwardWarper)
    owner.module = C.c_void_p(123)
    owner.closed = owner.faulted = False
    owner.fused_fill = True
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = None
    return owner


@pytest.mark.parametrize("value", [None, [0, 1], (), (0,), (0, 1, 1), "0,1", (False, 1),
    (0, True), ("0", 1), (0, object()), (float("nan"), 1), (0, float("nan")),
    (-float("inf"), 1), (0, float("inf")), (-.01, 1), (0, 1.01), (.6, .5), (0, 10**1000)])
def test_bad_range_metadata_rejected_at_every_public_boundary_without_cuda(value, monkeypatch):
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("Range validation started CUDA"))
    owner = owner_stub()
    calls = [
        lambda: owner(None, None, 0, .5, projection_depth_range=value),
        lambda: owner.packed(None, None, 0, .5, 1, 1, (0, 0, 1, 1), projection_depth_range=value),
        lambda: module.synthesize_forward_cuda(None, None, 0, .5, projection_depth_range=value),
        lambda: module.synthesize_forward_packed_cuda(None, None, 0, .5, 1, 1, (0, 0, 1, 1), projection_depth_range=value),
    ]
    for call in calls:
        with pytest.raises(ValueError, match="projection_depth_range"):
            call()
    assert not owner.faulted


@pytest.mark.parametrize("bounds", [(0, 1), (.375, 1), (0, 0), (.5, .5), (1, 1)])
def test_valid_metadata_has_no_tensor_readback(bounds, monkeypatch):
    monkeypatch.setattr(torch, "isfinite", lambda *args: pytest.fail("Metadata read tensor"))
    assert module._validate_projection_depth_range(bounds) == tuple(float(v) for v in bounds)


@pytest.mark.parametrize("bad", [np.nextafter(np.float32(.375), np.float32(0)),
                                np.nextafter(np.float32(.75), np.float32(1)), float("nan")])
@pytest.mark.parametrize("packed", [False, True])
def test_false_range_rejected_before_device_or_projection_even_zero_disparity(bad, packed):
    image = torch.full((1, 3, 2, 7), .5)
    depth = torch.full((2, 7), .5)
    depth[-1, -1] = float(bad)
    owner = owner_stub()
    owner._launch = lambda *args: pytest.fail("Invalid range reached projection")
    call = (lambda: owner.packed(image, depth, 0., .5, 7, 2, (0, 0, 7, 2), projection_depth_range=(.375, .75))) if packed else (
        lambda: owner(image, depth, 0., .5, projection_depth_range=(.375, .75)))
    with pytest.raises(ValueError, match="projection_depth_range"):
        call()
    assert not owner.faulted


def test_narrow_validation_preserves_image_checks_and_logical_negative_views():
    image = torch.full((1, 3, 2, 7), .5)
    depth = torch.full((2, 7), .5)
    module._validate_projection_inputs(torch._neg_view(-image), torch._neg_view(-depth), 20, .625, (.375, 1.))
    for tensor in (image, depth):
        saved = tensor.clone()
        tensor.flatten()[-1] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            module._validate_projection_inputs(image, depth, 20, .625, (.375, 1.))
        tensor.copy_(saved)
    image.flatten()[-1] = 1.01
    with pytest.raises(ValueError, match="finite"):
        module._validate_projection_inputs(image, depth, 20, .625, (.375, 1.))


def test_fused_receipt_uses_same_scan_bounds_and_completes_before_rejecting():
    owner = owner_stub()
    image, depth = torch.full((1, 3, 2, 7), .5), torch.full((2, 7), .5)
    flag = torch.zeros((), dtype=torch.int32)
    events, launches = [], []
    def launch(name, blocks, values, stream):
        events.append("validate")
        launches.append((name, [value.value for value in values]))
    owner._launch = launch
    def item():
        assert events == ["validate", "copy", "complete"]
        events.append("read")
        return 1
    receipt = SimpleNamespace(copy_=lambda *args, **kw: events.append("copy"), item=item)
    stream = SimpleNamespace(synchronize=lambda: events.append("complete"))
    with pytest.raises(ValueError, match="projection_depth_range"):
        owner._execute_validation(image, depth, image, depth, flag, receipt, stream, (.375, .75))
    assert launches[0][0] == "quest3d_forward_validate_values"
    assert launches[0][1][-2:] == [.375, .75]
    assert events == ["validate", "copy", "complete", "read"]
    assert not owner.faulted and owner._inflight is None


def test_cached_helpers_forward_only_nondefault_range_and_reuse_owners(monkeypatch):
    created, calls = [], []
    class Image:
        is_cuda = True
        device = SimpleNamespace(index=0)
    class Owner:
        def __init__(self, **kwargs): created.append(kwargs)
        def __call__(self, *args, **kwargs): calls.append(("planar", kwargs))
        def packed(self, *args, **kwargs): calls.append(("packed", kwargs))
    monkeypatch.setattr(module, "torch", SimpleNamespace(Tensor=Image))
    monkeypatch.setattr(module, "CudaForwardWarper", Owner)
    monkeypatch.setattr(module, "_owners", {})
    args = (Image(), None, 1., .625)
    module.synthesize_forward_cuda(*args, fused_fill=True)
    module.synthesize_forward_cuda(*args, fused_fill=True, projection_depth_range=(.375, 1))
    module.synthesize_forward_packed_cuda(*args, 1, 1, (0, 0, 1, 1), fused_validation=True, projection_depth_range=(.375, 1))
    module.synthesize_forward_packed_cuda(*args, 1, 1, (0, 0, 1, 1))
    assert created == [{"device": 0, "fused_fill": True}]
    assert calls == [("planar", {}), ("planar", {"projection_depth_range": (.375, 1.)}),
                     ("packed", {"fused_validation": True, "projection_depth_range": (.375, 1.)}), ("packed", {})]


def test_conservative_bounds_do_not_drop_any_nonzero_source_footprint_cpu():
    # Independently enumerate every source footprint that contributes coverage.
    # Bounds are checked against that set, not against another bounded search.
    f = np.float32
    for width in (1, 2, 9, 33):
        sx = np.arange(width, dtype=np.float32)[:, None]
        for low, high in ((0., 1.), (.375, 1.), (.125, .75), (.5, .5)):
            z = np.unique(np.linspace(low, high, 13, dtype=np.float32))[None, :]
            for disparity in (0., .1, 1.99, 27.648, 128., 1e6):
                half = f(disparity / 2)
                for convergence in (0., .625, 1.):
                    for sign in (f(1), f(-1)):
                        projected = sx + (sign * (z - f(convergence))) * half
                        lower = np.floor(projected)
                        fraction = projected - lower
                        a = (sign * (f(low) - f(convergence))) * half
                        b = (sign * (f(high) - f(convergence))) * half
                        for x in range(width):
                            begin = int(min(f(width), max(f(0), np.floor(f(f(x) - max(a, b)) - f(1)))))
                            end = int(max(f(-1), min(f(width - 1), np.ceil(f(f(x) - min(a, b)) + f(1)))))
                            contributes = ((lower == x) & (1 - fraction > 1e-6)) | ((lower + 1 == x) & (fraction > 1e-6))
                            needed = np.where(contributes.any(axis=1))[0]
                            assert np.all((needed >= begin) & (needed <= end)), (width, low, high, disparity, convergence, sign, x)


_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                          reason="Only the root's reserved GPU window may run this harness")
FIELDS = ("hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")


@pytest.mark.gpu
@_gpu
def test_real_gpu_bounded_planar_and_packed_are_exact_and_false_bounds_never_project(monkeypatch):
    results = []
    for fused in (False, True):
        with CudaForwardWarper(fused_fill=fused) as owner:
            for height, width in ((1, 1), (3, 9), (17, 257)):
                generator = torch.Generator(device="cpu").manual_seed(8300 + width)
                image = torch.rand((1, 3, height, width), generator=generator).cuda()
                depth = (torch.rand((height, width), generator=generator) * .625 + .375).cuda()
                depth.flatten()[0] = .375
                depth.flatten()[-1] = 1.
                saved_image, saved_depth = image.clone(), depth.clone()
                for disparity, convergence in ((0., .625), (.1, 0.), (27.648, .625), (128., 1.)):
                    baseline = owner(image, depth, disparity, convergence)
                    candidate = owner(image, depth, disparity, convergence, projection_depth_range=(.375, 1.))
                    for field in ("eyes", *FIELDS):
                        assert torch.equal(getattr(baseline, field), getattr(candidate, field)), (fused, height, width, disparity, field)
                    if fused:
                        args = (disparity, convergence, width + 5, height + 7, (2, 3, width, height))
                        for validation in (False, True):
                            baseline_p = owner.packed(image, depth, *args, fused_validation=validation)
                            candidate_p = owner.packed(image, depth, *args, fused_validation=validation, projection_depth_range=(.375, 1.))
                            for field in ("cpu_bgra", *FIELDS):
                                assert torch.equal(getattr(baseline_p, field), getattr(candidate_p, field)), (height, width, disparity, validation, field)
                            assert candidate_p.owner_metadata["projection_depth_range"] == [.375, 1.]
                    assert torch.equal(image, saved_image) and torch.equal(depth, saved_depth)
                    results.append({"fused": fused, "shape": [height, width], "disparity": disparity, "convergence": convergence, "exact": True})
            if fused:
                original_launch = owner._launch
                launches = []
                def record(name, *args):
                    launches.append(name)
                    return original_launch(name, *args)
                monkeypatch.setattr(owner, "_launch", record)
                for bad_value in (np.nextafter(np.float32(.375), np.float32(0)), np.float32(.751), np.float32("nan")):
                    bad = torch.full_like(depth, .5)
                    bad[-1, -1] = float(bad_value)
                    for validation in (False, True):
                        for disparity in (0., 27.648):
                            launches.clear()
                            with pytest.raises(ValueError, match="projection_depth_range"):
                                owner.packed(image, bad, disparity, .625, width, height, (0, 0, width, height),
                                             fused_validation=validation, projection_depth_range=(.375, .75))
                            assert launches == (["quest3d_forward_validate_values"] if validation else [])
                            assert not owner.faulted and owner._inflight is None
                            # A completed invalid-value receipt must not poison
                            # the owner or carry a stale invalid flag forward.
                            restored = owner.packed(image, saved_depth, 27.648, .625,
                                width, height, (0, 0, width, height), fused_validation=validation,
                                projection_depth_range=(.375, 1.))
                            assert restored.cpu_bgra.shape == (height, 2 * width, 4)
    report = os.environ.get("QUEST3D_PROJECTION_RANGE_REPORT")
    if report:
        output = Path(report)
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as stream:
            json.dump({"passed": True, "cases": results, "exact_fields": ["eyes", "cpu_bgra", *FIELDS],
                       "false_bounds_rejected_before_projection": True, "production_changed": False}, stream, indent=2)


@pytest.mark.gpu
@_gpu
def test_real_gpu_narrow_intervals_strides_lazy_negative_and_current_stream():
    with CudaForwardWarper(fused_fill=True) as owner:
        image = torch.rand((1, 3, 15, 39), device="cuda")[:, :, 1:14:2, 1:38:2]
        backing = torch.full((15, 39), float("nan"), device="cuda")
        depth = backing[1:14:2, 1:38:2]
        depth.copy_(torch.linspace(.1, .9, depth.numel(), device="cuda").reshape(depth.shape))
        height, width = depth.shape
        args = (27.648, .625, width + 4, height + 4, (2, 2, width, height))
        for bounds, data in (((.1, .9), depth), ((.5, .5), torch.full_like(depth, .5))):
            for rgb, z in ((image, data), (torch._neg_view(-image), torch._neg_view(-data))):
                baseline = owner.packed(rgb, z, *args, fused_validation=True)
                held = {field: getattr(baseline, field).clone() for field in ("cpu_bgra", *FIELDS)}
                stream = torch.cuda.Stream()
                stream.wait_stream(torch.cuda.current_stream())
                with torch.cuda.stream(stream):
                    actual = owner.packed(rgb, z, *args, fused_validation=True,
                                          projection_depth_range=bounds)
                for field in ("cpu_bgra", *FIELDS):
                    assert torch.equal(getattr(actual, field), held[field])
                    assert torch.equal(getattr(baseline, field), held[field])

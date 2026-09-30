"""Opt-in numerical proof for fused forward-fill kernels; no ordinary GPU use."""
from contextlib import ExitStack
import os
from types import SimpleNamespace

import pytest
import torch

from quest3d.forward_warp_cuda import CudaForwardWarper
from quest3d import forward_warp_cuda


RESULT_FIELDS = ("eyes", "hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")


def assert_result_equal(a, b):
    for name in RESULT_FIELDS:
        left, right = getattr(a, name), getattr(b, name)
        # Compare float bits too, including signed zero; masks are byte exact.
        if left.dtype == torch.float32:
            left, right = left.view(torch.int32), right.view(torch.int32)
        assert torch.equal(left, right), name


def cases():
    generator = torch.Generator().manual_seed(17809)
    for width, height in ((1, 1), (7, 3), (33, 9), (129, 11)):
        image = torch.rand((1, 3, height, width), generator=generator)
        planes = {
            "constant": torch.full((height, width), .75),
            "slope": torch.linspace(0, 1, width)[None].expand(height, -1).clone(),
            "thin": (torch.arange(width)[None].expand(height, -1) % 4 == 0).float(),
            "noisy": torch.rand((height, width), generator=generator),
        }
        for name, depth in planes.items():
            for disparity, convergence in ((0., .5), (1.e-6, .5), (.5, 0.), (1., .5),
                                            (7.25, .625), (24.24, 1.), (76.8, .625)):
                yield f"{width}x{height}-{name}-{disparity}-{convergence}", image, depth, disparity, convergence
    # Extreme accepted scalar bounds still clamp projected coordinates before
    # integer conversion; width-one must not read the -1 donor sentinel.
    for width in (1, 7):
        image = torch.rand((1, 3, 1, width), generator=generator)
        depth = torch.linspace(0, 1, width)[None]
        for convergence in (0., .5, 1.):
            yield f"max-disparity-{width}-{convergence}", image, depth, float(torch.finfo(torch.float32).max / 4), convergence


@pytest.mark.parametrize("bad", [None, 0, 1, "yes", [], {}])
def test_fused_option_rejects_non_boolean_before_cuda_access(bad, monkeypatch):
    monkeypatch.setattr(torch.cuda, "device_count", lambda: pytest.fail("Invalid option reached CUDA"))
    with pytest.raises(ValueError, match="explicit boolean"):
        CudaForwardWarper(fused_fill=bad)
    with pytest.raises(ValueError, match="explicit boolean"):
        forward_warp_cuda.synthesize_forward_cuda(None, None, 1, .5, fused_fill=bad)


def test_helper_keeps_reference_fused_owners_separate_and_retains_failed_close(monkeypatch):
    created = []
    class Image:
        is_cuda = True
        device = SimpleNamespace(index=0)
    class Owner:
        def __init__(self, *, device, fused_fill):
            self.fused_fill, self.failed, self.closed = fused_fill, False, False
            created.append(self)
        def __call__(self, *args):
            assert not self.closed
            return self.fused_fill
        def close(self):
            if self.failed:
                raise RuntimeError("injected pending work")
            self.closed = True
    monkeypatch.setattr(forward_warp_cuda, "torch", SimpleNamespace(Tensor=Image))
    monkeypatch.setattr(forward_warp_cuda, "CudaForwardWarper", Owner)
    monkeypatch.setattr(forward_warp_cuda, "_owners", {})
    image = Image()
    assert forward_warp_cuda.synthesize_forward_cuda(image, None, 1, .5) is False
    assert forward_warp_cuda.synthesize_forward_cuda(image, None, 1, .5, fused_fill=True) is True
    assert forward_warp_cuda.synthesize_forward_cuda(image, None, 1, .5) is False
    assert len(created) == 2
    created[1].failed = True
    with pytest.raises(RuntimeError, match="pending work"):
        forward_warp_cuda.close_cached_forward_warp_cuda()
    assert created[0].closed and not created[1].closed
    assert list(forward_warp_cuda._owners.values()) == [created[1]]
    created[1].failed = False
    forward_warp_cuda.close_cached_forward_warp_cuda()
    assert created[1].closed and forward_warp_cuda._owners == {}


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                               reason="Set QUEST3D_RUN_GPU_TESTS=1 for serialized CUDA verification")


@pytest.mark.gpu
@_real_gpu
def test_fused_fill_is_exact_for_visibility_partial_holes_edges_and_mask_ownership():
    counts = {"hole_mask": 0, "filled_mask": 0, "reconstructed_mask": 0, "estimated_donor_mask": 0}
    with ExitStack() as stack:
        reference = stack.enter_context(CudaForwardWarper(fused_fill=False))
        fused = stack.enter_context(CudaForwardWarper(fused_fill=True))
        retained = None
        for label, image, depth, disparity, convergence in cases():
            image, depth = image.cuda(), depth.cuda()
            before_image, before_depth = image.clone(), depth.clone()
            old = reference(image, depth, disparity, convergence)
            new = fused(image, depth, disparity, convergence)
            assert_result_equal(old, new)
            assert torch.equal(image, before_image) and torch.equal(depth, before_depth), label
            if retained is not None:
                result, saved = retained
                for field in RESULT_FIELDS:
                    assert torch.equal(getattr(result, field), saved[field]), (label, field)
            retained = (new, {field: getattr(new, field).clone() for field in RESULT_FIELDS})
            for field in counts:
                counts[field] += int(getattr(new, field).sum().item())
        assert all(value > 0 for value in counts.values()), counts
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            streamed = fused(image, depth, disparity, convergence)
        assert_result_equal(old, streamed)
        for invalid in (float("nan"), float("inf"), -1., 1.01):
            bad = depth.clone()
            bad[0, 0] = invalid
            with pytest.raises(ValueError, match="finite and in"):
                fused(image, bad, disparity, convergence)
        # A validation error must not poison the owner or alter retained results.
        assert not fused.faulted
        assert_result_equal(old, fused(image, depth, disparity, convergence))

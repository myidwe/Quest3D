"""Packed forward lifetime contracts and opt-in exact GPU comparison.

Normal collection does not initialize CUDA. Set QUEST3D_RUN_GPU_TESTS=1 only
during a granted GPU window. Existing 5/3-kernel owners and stereo packer form
the independent reference; no image/depth quality setting changes are allowed.
"""
from contextlib import ExitStack, nullcontext
import ctypes as C
import os
from types import SimpleNamespace
import threading

import pytest
import torch

from quest3d import forward_warp_cuda
from quest3d.forward_warp_cuda import CudaForwardWarper
from quest3d.stereo_pack_cuda import CudaStereoPacker


MASK_FIELDS = ("hole_mask", "filled_mask", "reconstructed_mask", "estimated_donor_mask")


def _owner():
    owner = object.__new__(CudaForwardWarper)
    owner.module = C.c_void_p(123)
    owner.closed = owner.faulted = False
    owner.fused_fill = True
    owner.device = 0
    owner._lock = threading.RLock()
    owner._inflight = None
    owner.metadata = {"fused_fill": True}
    return owner


@pytest.mark.parametrize("ew,eh,rect,shape", [
    (0, 5, (0, 0, 1, 1), (1, 1)), (8193, 5, (0, 0, 1, 1), (1, 1)),
    (8, True, (0, 0, 1, 1), (1, 1)), (8., 8, (0, 0, 1, 1), (1, 1)),
    (8, 8, [0, 0, 1, 1], (1, 1)), (8, 8, (0, 0, 1), (1, 1)),
    (8, 8, (-1, 0, 1, 1), (1, 1)), (8, 8, (0, 0, 0, 1), (1, 0)),
    (8, 8, (1, 0, 8, 8), (8, 8)), (8, 8, (0, 1, 8, 8), (8, 8)),
    (8, 8, (0, 0, 8, True), (1, 8)), (8, 8, (0, 0, 8, 8), (7, 8)),
])
def test_invalid_pack_geometry_rejected_without_cuda(ew, eh, rect, shape, monkeypatch):
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("Geometry initialized CUDA"))
    with pytest.raises(ValueError):
        CudaForwardWarper._validate_pack_geometry(ew, eh, rect, shape)


def test_closed_faulted_and_reference_owners_refuse_packed_launch():
    owner = _owner()
    owner.fused_fill = False
    with pytest.raises(RuntimeError, match="fused_fill=True"):
        owner.packed(None, None, 0, .5, 1, 1, (0, 0, 1, 1))
    owner.fused_fill = True
    for attribute in ("closed", "faulted"):
        setattr(owner, attribute, True)
        with pytest.raises(RuntimeError, match="closed or faulted"):
            owner.packed(None, None, 0, .5, 1, 1, (0, 0, 1, 1))
        setattr(owner, attribute, False)


def test_launch_copy_and_single_completion_are_ordered():
    owner = _owner()
    events = []
    packed, source, mask, scratch = (object() for _ in range(4))
    def launch(name, blocks, values, stream):
        assert all(item in owner._inflight for item in (packed, source, mask, scratch, cpu))
        events.append(name)
    owner._launch = launch
    owner._launch_packed_grid = launch
    def copy(value, *, non_blocking):
        assert value is packed and non_blocking
        events.append("copy")
    cpu = SimpleNamespace(copy_=copy)
    stream = SimpleNamespace(synchronize=lambda: events.append("synchronize"))
    owner._execute_packed(stream, [("project", 1, []), ("bounds", 1, []), ("fill-pack", (1, 1, 2), [])],
                          packed, cpu, (source, mask, scratch))
    assert events == ["project", "bounds", "fill-pack", "copy", "synchronize"]
    assert owner._inflight is None and not owner.faulted


def test_packed_launch_uses_two_eye_grid_and_one_warp_per_row():
    owner = _owner()
    captured = []
    owner.functions = {"packed": C.c_void_p(456)}
    owner.driver = SimpleNamespace(cuLaunchKernel=lambda *args: captured.append(args) or 0)
    stream = SimpleNamespace(cuda_stream=789)
    owner._launch_packed_grid("packed", (60, 135, 2), [C.c_int(1920), C.c_int(1080)], stream)
    assert len(captured) == 1
    call = captured[0]
    assert call[0].value == 456
    assert call[1:8] == (60, 135, 2, 32, 8, 1, 0)
    assert call[8].value == 789


def test_packed_sequence_routes_only_final_kernel_to_tiled_launcher():
    owner = _owner()
    events = []
    owner._launch = lambda name, blocks, values, stream: events.append((name, blocks))
    owner._launch_packed_grid = lambda name, grid, values, stream: events.append((name, grid))
    stream = SimpleNamespace(synchronize=lambda: events.append("complete"))
    cpu = SimpleNamespace(copy_=lambda *args, **kwargs: events.append("copy"))
    owner._execute_packed(stream, [("project", 16200, []), ("bounds", 2160, []),
                                  ("fill-pack", (60, 135, 2), [])], object(), cpu, ())
    assert events == [("project", 16200), ("bounds", 2160),
                      ("fill-pack", (60, 135, 2)), "copy", "complete"]


@pytest.mark.parametrize("failure", ["launch", "copy", "synchronize"])
def test_packed_failure_retains_all_storage_until_completed_close(failure, monkeypatch):
    owner = _owner()
    events = []
    packed, source, depth, rgb, masks, scratch = (object() for _ in range(6))
    def launch(name, blocks, values, stream):
        events.append(name)
        if failure == "launch" and name == "fill-pack":
            raise RuntimeError("injected launch failure")
    owner._launch = launch
    owner._launch_packed_grid = launch
    def copy(value, *, non_blocking):
        events.append("copy")
        if failure == "copy":
            raise RuntimeError("injected copy failure")
    cpu = SimpleNamespace(copy_=copy)
    # Even after a reported launch/copy failure a queued earlier operation may
    # still be reading inputs. A failed sync never permits releasing them.
    def fail_sync():
        events.append("synchronize")
        raise RuntimeError("injected synchronize failure")
    stream = SimpleNamespace(synchronize=fail_sync)
    keepalive = (source, depth, rgb, masks, scratch)
    with pytest.raises(RuntimeError, match="synchronize failure"):
        owner._execute_packed(stream, [("project", 1, []), ("fill-pack", (1, 1, 2), [])],
                              packed, cpu, keepalive)
    retained = owner._inflight
    assert retained == (stream, *keepalive, packed, cpu) and owner.faulted
    assert events[-1] == "synchronize"
    with pytest.raises(RuntimeError, match="closed or faulted"):
        owner.packed(None, None, 0, .5, 1, 1, (0, 0, 1, 1))
    unloaded = []
    owner.driver = SimpleNamespace(cuModuleUnload=lambda module: unloaded.append(module.value) or 0)
    monkeypatch.setattr(torch.cuda, "device", lambda device: nullcontext())
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: fail_sync())
    with pytest.raises(RuntimeError, match="synchronize failure"):
        owner.close()
    assert owner._inflight is retained and owner.module.value == 123 and not unloaded
    monkeypatch.setattr(torch.cuda, "synchronize", lambda device: None)
    owner.close()
    assert owner._inflight is None and owner.module.value is None and owner.closed
    assert unloaded == [123]
    owner.close()
    assert unloaded == [123]


@pytest.mark.parametrize("failure", ["launch", "copy"])
def test_successful_completion_after_error_releases_storage_but_owner_stays_faulted(failure):
    owner = _owner()
    calls = []
    def launch(*args):
        if failure == "launch":
            raise RuntimeError("injected launch error")
    def copy(*args, **kwargs):
        if failure == "copy":
            raise RuntimeError("injected copy error")
    owner._launch = launch
    stream = SimpleNamespace(synchronize=lambda: calls.append("complete"))
    with pytest.raises(RuntimeError, match=f"{failure} error"):
        owner._execute_packed(stream, [("kernel", 1, [])], object(), SimpleNamespace(copy_=copy), ())
    assert owner.faulted and owner._inflight is None and calls == ["complete"]


def test_convenience_shares_fused_owner_and_retains_cache_on_close_failure(monkeypatch):
    created = []
    class Image:
        is_cuda = True
        device = SimpleNamespace(index=0)
    class Owner:
        def __init__(self, *, device, fused_fill):
            assert fused_fill is True
            self.failed = False
            created.append(self)
        def __call__(self, *args):
            return "float"
        def packed(self, *args):
            assert len(args) == 7
            return "packed"
        def close(self):
            if self.failed:
                raise RuntimeError("injected unfinished copy")
    monkeypatch.setattr(forward_warp_cuda, "torch", SimpleNamespace(Tensor=Image))
    monkeypatch.setattr(forward_warp_cuda, "CudaForwardWarper", Owner)
    monkeypatch.setattr(forward_warp_cuda, "_owners", {})
    image = Image()
    assert forward_warp_cuda.synthesize_forward_cuda(image, None, 1, .5, fused_fill=True) == "float"
    assert forward_warp_cuda.synthesize_forward_packed_cuda(image, None, 1, .5, 1, 1, (0, 0, 1, 1)) == "packed"
    assert len(created) == 1
    created[0].failed = True
    with pytest.raises(RuntimeError, match="unfinished copy"):
        forward_warp_cuda.close_cached_forward_warp_cuda()
    assert forward_warp_cuda._owners == {(0, True): created[0]}
    created[0].failed = False
    forward_warp_cuda.close_cached_forward_warp_cuda()
    assert forward_warp_cuda._owners == {}


_real_gpu = pytest.mark.skipif(os.environ.get("QUEST3D_RUN_GPU_TESTS") != "1",
                               reason="Set QUEST3D_RUN_GPU_TESTS=1 during a granted GPU window")


def _save(result):
    return {field: getattr(result, field).clone() for field in ("cpu_bgra", *MASK_FIELDS)}


def _assert_retained(result, saved):
    for field in saved:
        assert torch.equal(getattr(result, field), saved[field]), field


@pytest.mark.gpu
@_real_gpu
def test_packed_matches_five_three_kernel_and_packer_for_all_visibility_masks():
    from test_forward_warp_cuda_fusion import cases
    counts = {name: 0 for name in MASK_FIELDS}
    with ExitStack() as stack:
        reference = stack.enter_context(CudaForwardWarper(fused_fill=False))
        fused = stack.enter_context(CudaForwardWarper(fused_fill=True))
        packer = stack.enter_context(CudaStereoPacker())
        retained = None
        for label, image, depth, disparity, convergence in cases():
            image, depth = image.cuda(), depth.cuda()
            image_before, depth_before = image.clone(), depth.clone()
            height, width = depth.shape
            rect, ew, eh = (2, 1, width, height), width + 7, height + 5
            five = reference(image, depth, disparity, convergence)
            three = fused(image, depth, disparity, convergence)
            expected = packer(three.eyes, ew, eh, rect)
            actual = fused.packed(image, depth, disparity, convergence, ew, eh, rect)
            assert torch.equal(five.eyes.view(torch.int32), three.eyes.view(torch.int32)), label
            assert torch.equal(actual.cpu_bgra, expected), label
            assert actual.cpu_bgra.is_pinned() and actual.cpu_bgra.device.type == "cpu"
            assert actual.cpu_bgra.dtype == torch.uint8 and actual.cpu_bgra.is_contiguous()
            assert bool((actual.cpu_bgra[:, :, 3] == 255).all())
            for field in MASK_FIELDS:
                assert torch.equal(getattr(five, field), getattr(three, field)), (label, field)
                assert torch.equal(getattr(actual, field), getattr(three, field)), (label, field)
                counts[field] += int(getattr(actual, field).sum().item())
            assert torch.equal(image, image_before) and torch.equal(depth, depth_before), label
            assert actual.owner_metadata["packed_output"] is True
            assert actual.owner_metadata["kernels"] == (1 if disparity == 0 else 3)
            if retained is not None:
                _assert_retained(*retained)
            retained = (actual, _save(actual))
        assert all(counts.values()), counts
    _assert_retained(*retained)


@pytest.mark.gpu
@_real_gpu
def test_packed_round_to_even_zero_disparity_strides_stream_and_large_frame():
    with ExitStack() as stack:
        fused = stack.enter_context(CudaForwardWarper(fused_fill=True))
        packer = stack.enter_context(CudaStereoPacker())
        half = (torch.arange(255, dtype=torch.float32, device="cuda") + .5) / 255
        values = torch.cat((half, torch.nextafter(half, torch.zeros_like(half)),
                            torch.nextafter(half, torch.ones_like(half)),
                            torch.tensor([0., 1., -0.], device="cuda")))
        retained = None
        for width, height, ew, eh, x, y in ((129, 7, 138, 12, 3, 2),
                                           (1920, 1080, 1920, 1080, 0, 0),
                                           (1, 1, 3, 5, 1, 3)):
            total = 3 * height * width
            image = values.repeat((total + values.numel() - 1) // values.numel())[:total].reshape(1, 3, height, width)
            depth = torch.full((height, width), .75, device="cuda")
            rect = (x, y, width, height)
            for disparity in (0., .5):
                old = fused(image, depth, disparity, .5)
                actual = fused.packed(image, depth, disparity, .5, ew, eh, rect)
                assert torch.equal(actual.cpu_bgra, packer(old.eyes, ew, eh, rect))
                for field in MASK_FIELDS:
                    assert torch.equal(getattr(actual, field), getattr(old, field))
                    if disparity == 0:
                        assert not bool(getattr(actual, field).any())
                if retained:
                    _assert_retained(*retained)
                retained = (actual, _save(actual))
        # Production fitting may be strided; packed matches the existing owner's
        # contiguous copy rules without ever changing the caller's source.
        image = torch.rand((1, 3, 19, 67), device="cuda")[:, :, 1:18:2, 1:66:2]
        depth = torch.rand((19, 67), device="cuda")[1:18:2, 1:66:2]
        height, width = depth.shape
        rect = (1, 3, width, height)
        before_image, before_depth = image.clone(), depth.clone()
        old = fused(image, depth, 24.24, .625)
        expected = packer(old.eyes, width + 5, height + 4, rect)
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(stream):
            actual = fused.packed(image, depth, 24.24, .625, width + 5, height + 4, rect)
        assert torch.equal(actual.cpu_bgra, expected)
        for field in MASK_FIELDS:
            assert torch.equal(getattr(actual, field), getattr(old, field))
        assert torch.equal(image, before_image) and torch.equal(depth, before_depth)
        _assert_retained(*retained)
    _assert_retained(*retained)


@pytest.mark.gpu
@_real_gpu
def test_bad_inputs_do_not_poison_owner_or_overwrite_retained_results():
    with CudaForwardWarper(fused_fill=True) as fused:
        image = torch.rand((1, 3, 9, 17), device="cuda")
        depth = torch.rand((9, 17), device="cuda")
        args = (1., .5, 20, 14, (1, 2, 17, 9))
        retained = fused.packed(image, depth, *args)
        saved = _save(retained)
        for invalid in (float("nan"), float("inf"), -1., 1.01):
            bad = depth.clone()
            bad[0, 0] = invalid
            with pytest.raises(ValueError, match="finite and in"):
                fused.packed(image, bad, *args)
            assert not fused.faulted and fused._inflight is None
        for invalid in (image.cpu(), image.half(), image[0]):
            with pytest.raises(ValueError):
                fused.packed(invalid, depth, *args)
        with pytest.raises(ValueError, match="Content rectangle"):
            fused.packed(image, depth, 1., .5, 20, 14, (4, 2, 17, 9))
        assert not fused.faulted
        _assert_retained(retained, saved)

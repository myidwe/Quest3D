"""Synthetic CPU contracts for explicit disparity mapping, not wearer benefit."""
from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import numpy as np
import pytest
import torch
import torch.nn.functional as F

from quest3d import forward_warp, stereo as stereo_module
from quest3d.depth import DepthResult
from quest3d.disparity_mapping import map_disparity_depth, validate_disparity_profile
from quest3d.stereo import StereoFrame, StereoSynthesizer


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    old = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("CPU regression must not initialize CUDA"))
    yield
    torch.set_num_threads(old)


def image(width=83, height=47):
    y, x = np.indices((height, width))
    return np.stack(((3*x+y) % 256, (x+2*y) % 256, (7*x+5*y) % 256,
                     np.full_like(x, 255)), axis=-1).astype(np.uint8)


def depth(frame=7, generation=3, low=2., high=6.):
    value = torch.linspace(low, high, 77).reshape(1, 7, 11)
    return DepthResult(frame, generation, value, (7, 11), 0., 0.)


@pytest.mark.parametrize("dtype", [torch.float32, torch.float64])
def test_curve_monotone_bounded_preserves_near_values_without_renormalizing(dtype):
    z = torch.linspace(0, 1, 1001, dtype=dtype).reshape(7, 143)
    original, version = z.clone(), z._version
    mapped, c = map_disparity_depth(z, .5, "comfort")
    assert mapped.data_ptr() != z.data_ptr() and z._version == version
    torch.testing.assert_close(z, original, rtol=0, atol=0)
    assert mapped.min() == .375 and mapped.max() == 1 and c == .625
    assert torch.all(mapped.flatten().diff() >= 0)
    torch.testing.assert_close(mapped[z >= .75], z[z >= .75], rtol=0, atol=0)
    torch.testing.assert_close(mapped[z < .75], .75 + .5 * (z[z < .75] - .75), rtol=0, atol=0)


def test_local_far_slope_halved_and_near_relief_preserved_at_current_d():
    z = torch.tensor([0., .25, .5, .75, .875, 1.], dtype=torch.float64)
    out, c = map_disparity_depth(z, .5, "comfort")
    before, after = 22.8 * (z - .5), 22.8 * (out - c)
    torch.testing.assert_close(after[:3].diff(), before[:3].diff() * .5)
    torch.testing.assert_close(after[3:].diff(), before[3:].diff())
    assert after[2] == 0
    assert after[0] == pytest.approx(-5.7) and after[-1] == pytest.approx(8.55)
    # Internal near relief stays; absolute near placement/background gap changes.
    assert after[-1] != before[-1]


@pytest.mark.parametrize("c", [0., .1, .5, .75, .9, 1.])
def test_original_convergence_plane_stays_zero(c):
    mapped, effective_c = map_disparity_depth(torch.tensor([c]), c, "comfort")
    torch.testing.assert_close(22.8 * (mapped - effective_c), torch.zeros(1), rtol=0, atol=1e-6)


def test_noncontiguous_input_and_linear_identity():
    base = torch.linspace(0, 1, 80).reshape(8, 10)
    view = base[::2, 1::2]
    assert not view.is_contiguous()
    original = base.clone()
    same, c = map_disparity_depth(view, .4, "linear")
    assert same is view and c == .4
    mapped, _ = map_disparity_depth(view, .4, "comfort")
    assert mapped.shape == view.shape and mapped.dtype == view.dtype and mapped.device == view.device
    torch.testing.assert_close(base, original, rtol=0, atol=0)


@pytest.mark.parametrize("bad", [None, 0, True, {}, "COMFORT", "far"])
def test_unknown_profile_rejected_at_constructor_and_each_job(bad):
    with pytest.raises(ValueError, match="Disparity profile"):
        StereoSynthesizer(disparity_profile=bad)
    with pytest.raises(ValueError, match="Disparity profile"):
        validate_disparity_profile(bad)
    synth = StereoSynthesizer(128, 72, disparity_px=4)
    synth.disparity_profile = bad
    with pytest.raises(ValueError, match="Disparity profile"):
        synth.synthesize(image(), depth(), frame_id=7, generation=3)
    assert synth.previous_thumbnail is None and synth.depth_limits is None


# Characterized directly from before-core/stereo.py SHA48a3004e... before edits.
# Two frames exercise both initial normalization and its existing EMA. These
# byte hashes are not AI or CUDA images and require no artifact at test runtime.
LEGACY = [
    ("backward", "area", "uint8", False, (
        "2cb7aed8e538f4cbaf7b76ec737c2bf4ee4c2d3d5f0f4f82cd19614808cd6cbd",
        "b9a9399103f2e9a8cb28e5dfce6422568cff3bc24a1fb009bad48cacb2985716")),
    ("forward", "bicubic-aa", "uint8", False, (
        "119f1e8a2edff6abfa90d2247988f6bc09fbf2e38859df41fd26e227c70e6bf0",
        "456921d1a9cb7201b7a2f82c9a7f3819d99a50ffd272cb00aa87ff0d0c4894fa")),
    ("forward", "bicubic-aa", "float", True, (
        "fcb99b74142c7ebe02a244e47414b031649eaee78e0f4d336acea0df53ac60fe",
        "f74519ba9ee87086fdd6c7e6225c063604f8c686862bd9c84fa5fcc4569df3ac")),
]


@pytest.mark.parametrize("explicit", [False, True])
@pytest.mark.parametrize("method,resize,precision,tensor,expected", LEGACY)
def test_default_and_explicit_linear_match_before_change_pixel_bytes(explicit, method, resize, precision, tensor, expected):
    options = {"disparity_profile": "linear"} if explicit else {}
    synth = StereoSynthesizer(128, 72, disparity_px=4, convergence=.4, stereo_method=method,
        resize_filter=resize, colour_precision=precision, **options)
    source = torch.from_numpy(image()) if tensor else image()
    for index, (frame, low, high) in enumerate([(7, 2., 6.), (8, 3., 9.)]):
        result = synth.synthesize(source, depth(frame, low=low, high=high), frame_id=frame, generation=3)
        assert hashlib.sha256(result.bgra.tobytes()).hexdigest() == expected[index]
        assert result.disparity_profile == "linear" and result.disparity_profile_reason is None
        assert result.effective_convergence == .4


@pytest.mark.parametrize("method", ["backward", "forward", "forward-cuda"])
@pytest.mark.parametrize("profile", ["linear", "comfort"])
def test_original_2d_and_zero_d_bypass_mapping_and_gpu(monkeypatch, method, profile):
    monkeypatch.setattr(stereo_module, "map_disparity_depth", lambda *a: pytest.fail("2D must bypass mapping"))
    src = image()
    synth = StereoSynthesizer(128, 72, disparity_px=0, stereo_method=method,
                              resize_filter="bicubic-aa", disparity_profile=profile)
    baseline = StereoSynthesizer(128, 72, disparity_px=0, resize_filter="bicubic-aa").original_2d(src, frame_id=7, generation=3)
    # Existing zero-D behavior never samples depth, while pair identity is required.
    unused_depth = DepthResult(7, 3, torch.full((1, 7, 11), float("nan")), (7, 11), 0., 0.)
    for result in (synth.original_2d(src, frame_id=7, generation=3),
                   synth.synthesize(src, unused_depth, frame_id=7, generation=3)):
        np.testing.assert_array_equal(result.bgra, baseline.bgra)
        assert result.mode == "2d" and result.disparity_profile == "linear"
        assert result.disparity_profile_reason == ("original_2d" if profile == "comfort" else None)
        assert result.effective_convergence is None


@pytest.mark.parametrize("profile", ["linear", "comfort"])
@pytest.mark.parametrize("disparity", [0, 4])
@pytest.mark.parametrize("frame,generation", [(8, 3), (7, 4)])
def test_stale_depth_still_rejected_before_mapping(monkeypatch, profile, disparity, frame, generation):
    monkeypatch.setattr(stereo_module, "map_disparity_depth", lambda *a: pytest.fail("stale frame reached mapper"))
    synth = StereoSynthesizer(128, 72, disparity_px=disparity, disparity_profile=profile)
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        synth.synthesize(image(), depth(), frame_id=frame, generation=generation)


@pytest.mark.parametrize("method", ["forward", "forward-cuda"])
def test_profile_can_change_each_job_and_projects_one_shared_mapped_depth(monkeypatch, method):
    projections, mappings = [], []
    actual_project = forward_warp.synthesize_forward
    not_passed = object()
    def project(image, z, d, c, *, projection_depth_range=not_passed):
        if method == "forward-cuda" and mappings[-1][1] == "comfort":
            assert projection_depth_range == (0.375, 1.0)
        else:
            assert projection_depth_range is not_passed
        projections.append((z.clone(), d, c))
        return actual_project(image, z, d, c)
    def mapping(z, c, profile):
        mappings.append((z.clone(), profile))
        return map_disparity_depth(z, c, profile)
    monkeypatch.setattr(stereo_module, "map_disparity_depth", mapping)
    if method == "forward": monkeypatch.setattr(forward_warp, "synthesize_forward", project)
    else: monkeypatch.setitem(sys.modules, "quest3d.forward_warp_cuda", SimpleNamespace(synthesize_forward_cuda=project))
    synth = StereoSynthesizer(128, 72, disparity_px=4, stereo_method=method)
    raw = depth().tensor
    original = raw.clone()
    outputs = []
    for frame, profile in enumerate(("linear", "comfort", "linear"), 7):
        synth.disparity_profile = profile
        result = synth.synthesize(image(), DepthResult(frame, 3, raw, (7, 11), 0., 0.), frame_id=frame, generation=3)
        outputs.append(result)
        assert result.disparity_profile == profile and result.disparity_profile_reason is None
        assert result.effective_convergence == (.625 if profile == "comfort" else .5)
        assert result.depth_range == (2., 6.)
    assert len(projections) == len(mappings) == 3
    z = projections[0][0]
    torch.testing.assert_close(projections[1][0], torch.where(z < .75, .75+.5*(z-.75), z), rtol=0, atol=0)
    assert projections[1][2] == .625 and all(p[1] == 4 for p in projections)
    assert z.shape == (72, 127)  # Existing aspect-fit rounding remains unchanged.
    torch.testing.assert_close(raw, original, rtol=0, atol=0)
    np.testing.assert_array_equal(outputs[0].bgra, outputs[2].bgra)
    assert not np.array_equal(outputs[0].bgra, outputs[1].bgra)


def test_mapping_runs_after_refinement_once_without_renormalization(monkeypatch):
    calls = []
    refined = torch.linspace(0, 1, 72*127).reshape(72, 127)
    before = refined.clone()
    def guided(rgb, z, **options):
        assert rgb.shape[-2:] == refined.shape and z.shape == (1, 7, 11)
        calls.append("refine")
        return refined
    def project(rgb, z, d, c):
        calls.append("project")
        torch.testing.assert_close(z, torch.where(refined < .75, .75+.5*(refined-.75), refined), rtol=0, atol=0)
        assert z.min() == .375 and z.max() == 1 and c == .625
        return SimpleNamespace(eyes=rgb.expand(2, -1, -1, -1))
    monkeypatch.setitem(sys.modules, "quest3d.depth_refine", SimpleNamespace(guided_upsample_depth=guided))
    monkeypatch.setattr(forward_warp, "synthesize_forward", project)
    synth = StereoSynthesizer(128, 72, disparity_px=4, stereo_method="forward", depth_refinement="guided", disparity_profile="comfort")
    result = synth.synthesize(image(), depth(), frame_id=7, generation=3)
    assert calls == ["refine", "project"] and result.depth_refinement == "guided"
    torch.testing.assert_close(refined, before, rtol=0, atol=0)


def test_backward_grid_uses_same_mapped_depth_and_convergence(monkeypatch):
    grids = []
    original_grid_sample = F.grid_sample
    def sample(image, grid, **options):
        grids.append(grid.clone())
        return original_grid_sample(image, grid, **options)
    monkeypatch.setattr(stereo_module.F, "grid_sample", sample)
    synth = StereoSynthesizer(128, 72, disparity_px=4, disparity_profile="comfort")
    result = synth.synthesize(image(), depth(), frame_id=7, generation=3)
    width, height = result.content_rect[2:]
    z = F.interpolate(((depth().tensor-2)/4)[:, None], size=(height, width), mode="bilinear", align_corners=True)[0, 0]
    expected_z = torch.where(z < .75, .75+.5*(z-.75), z)
    gx = torch.linspace(-1, 1, width)[None].expand(height, width)
    displacement = (expected_z - .625) * 4 / (width-1)
    assert len(grids) == 1 and grids[0].shape == (2, height, width, 2)
    torch.testing.assert_close(grids[0][0, :, :, 0], gx - displacement)
    torch.testing.assert_close(grids[0][1, :, :, 0], gx + displacement)
    assert result.disparity_profile == "comfort" and result.effective_convergence == .625


def test_stereo_frame_old_positional_construction_retains_defaults():
    frame = StereoFrame(np.zeros((2, 4, 4), np.uint8), 7, 3, "2d", False, None)
    assert frame.disparity_profile == "linear" and frame.disparity_profile_reason is None
    assert frame.effective_convergence is None

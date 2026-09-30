"""CPU-only independent synthetic controls for optional depth interpolation.

Passing these does not select the candidate: displaced colour inside a low cell
and colour texture over nonlinear depth noise remain explicit negative controls.
No capture, model, CUDA initialization, timing claim, or live settings are used.
"""

import math

import pytest
import torch
import torch.nn.functional as F

from quest3d.depth_edges import edge_aware_upsample_depth


@pytest.fixture(autouse=True)
def _cpu_threads():
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(previous)


def _baseline(rgb, depth):
    return F.interpolate(depth[:, None], size=rgb.shape[-2:], mode="bilinear",
                         align_corners=True)[0, 0]


def _step(edge=68):
    depth = torch.full((1, 5, 17), .2)
    depth[:, :, 9:] = .8
    rgb = torch.zeros(1, 3, 33, 129)
    rgb[:, 0, :, :edge] = 1
    rgb[:, 1, :, edge:] = 1
    truth = torch.full((33, 129), .2)
    truth[:, 68:] = .8
    return rgb, depth, truth


def test_step_matches_independent_four_donor_gaussian_oracle():
    rgb, depth, _ = _step()
    output = edge_aware_upsample_depth(rgb, depth)
    # Depth curvature/range, donor colour range, and actual RGB edge all exceed
    # their upper gate bounds here; blend=.75. Compute scalar donor weights
    # independently rather than deriving expected output from implementation.
    for x in range(65, 72):
        fraction = (x - 64) / 8
        left_distance2 = 0 if x < 68 else 2 / 3
        right_distance2 = 2 / 3 if x < 68 else 0
        left = (1 - fraction) * math.exp(-left_distance2 / (2 * .1**2))
        right = fraction * math.exp(-right_distance2 / (2 * .1**2))
        joint = (.2 * left + .8 * right) / (left + right)
        linear = .2 * (1 - fraction) + .8 * fraction
        expected = linear + .04 * math.tanh(.75 * (joint - linear) / .04)
        assert output[16, x].item() == pytest.approx(expected, abs=1e-7)


def test_aligned_step_reduces_error_locally_without_changing_plane_depths():
    rgb, depth, truth = _step()
    before = _baseline(rgb, depth)
    output = edge_aware_upsample_depth(rgb, depth)
    # This bound intentionally prevents the first candidate's large edge snap.
    # Compare against the independent bounded oracle, not its old 75% snap.
    assert (output - truth).abs().mean() < (before - truth).abs().mean()
    torch.testing.assert_close(output[:, :65], before[:, :65], rtol=0, atol=0)
    torch.testing.assert_close(output[:, 72:], before[:, 72:], rtol=0, atol=0)
    assert output.min() >= .2 and output.max() <= .8
    assert torch.diff(output[16]).min() >= 0
    # Corner-aligned original low samples retain both near and far depths.
    torch.testing.assert_close(output[::8, ::8], depth[0], rtol=0, atol=0)


@pytest.mark.parametrize("value", [0., .37, 1.])
def test_constant_depth_ignores_arbitrary_rgb_texture(value):
    rgb = torch.rand(1, 3, 39, 75, generator=torch.Generator().manual_seed(128))
    depth = torch.full((1, 7, 13), value)
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


def test_linear_slanted_plane_ignores_sharp_rgb_texture():
    rgb = torch.rand(1, 3, 41, 129, generator=torch.Generator().manual_seed(28))
    depth = (.1 + torch.arange(6)[:, None] * .03 + torch.arange(17)[None, :] * .035)[None]
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


def test_low_noise_below_depth_gate_does_not_transfer_texture():
    rgb, _, _ = _step()
    generator = torch.Generator().manual_seed(51)
    depth = .4 + (torch.rand((1, 5, 17), generator=generator) - .5) * .01
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


def test_flat_rgb_preserves_existing_depth_edge_and_narrow_peak():
    rgb = torch.full((1, 3, 33, 129), .5)
    depth = torch.full((1, 5, 17), .2)
    depth[:, :, 8] = .8
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


def test_missing_depth_structure_is_not_invented_from_thin_rgb_line():
    rgb = torch.zeros(1, 3, 33, 129)
    rgb[:, :, :, 63:66] = 1
    depth = torch.full((1, 5, 17), .4)
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


@pytest.mark.parametrize("transpose", [False, True])
@pytest.mark.parametrize("phase", [-2, 0, 2])
def test_narrow_pole_and_rail_preserve_sample_peak_and_support(phase, transpose):
    rgb = torch.zeros(1, 3, 33, 129)
    rgb[:, :, :, 61 + phase:68 + phase] = 1
    depth = torch.full((1, 5, 17), .2)
    depth[:, :, 8] = .8
    if transpose:
        rgb, depth = rgb.transpose(-1, -2), depth.transpose(-1, -2)
    output = edge_aware_upsample_depth(rgb, depth)
    before = _baseline(rgb, depth)
    if transpose:
        output, before = output.T, before.T
    assert output[16, 64] == pytest.approx(.8, abs=0)
    assert output.min() >= .2 and output.max() <= .8
    assert torch.equal(output[:, :57], before[:, :57])
    assert torch.equal(output[:, 72:], before[:, 72:])
    if phase == 0:
        outside = torch.ones(129, dtype=torch.bool)
        outside[61:68] = False
        assert (output[:, outside] - .2).mean() < (before[:, outside] - .2).mean()


def test_far_displaced_rgb_edge_does_not_move_the_depth_edge():
    rgb, depth, _ = _step(edge=84)
    assert torch.equal(edge_aware_upsample_depth(rgb, depth), _baseline(rgb, depth))


def test_wrong_rgb_edge_still_hurts_wrong_side_despite_improved_global_average():
    rgb, depth, truth = _step(edge=65)
    output = edge_aware_upsample_depth(rgb, depth)
    before = _baseline(rgb, depth)
    # The correction cap improves this original fixture's overall average but
    # cannot identify the incorrectly guided side. Preserve both observations.
    assert (output - truth).abs().mean() < (before - truth).abs().mean()
    assert torch.all((output[:, 65:68] - truth[:, 65:68]).abs()
                     > (before[:, 65:68] - truth[:, 65:68]).abs())
    # The failure remains local and within measured donor depths; neither fact
    # justifies calling its inferred silhouette correct.
    assert torch.equal(output[:, :65], before[:, :65])
    assert torch.equal(output[:, 72:], before[:, 72:])


def test_limitation_wrong_rgb_edge_inside_cell_can_still_worsen_global_error():
    rgb, depth, _ = _step(edge=65)
    # Both true edges at 68 and 71 are consistent with low samples at 64/72.
    # This second admissible geometry places six interpolated pixels on the
    # wrongly guided side. The bounded change still worsens its global error.
    truth = torch.full((33, 129), .2)
    truth[:, 71:] = .8
    before = _baseline(rgb, depth)
    output = edge_aware_upsample_depth(rgb, depth)
    assert (output - truth).abs().mean() > (before - truth).abs().mean()


def test_limitation_nonlinear_depth_noise_can_accept_unrelated_rgb_texture():
    depth = (.47 + (torch.arange(17) % 2).float() * .06)[None, None].expand(1, 5, 17)
    stripe_x = ((torch.arange(129) // 8) % 2).bool()[None, :]
    stripe_y = ((torch.arange(33) % 8) >= 4).bool()[:, None]
    rgb = (stripe_x ^ stripe_y).float()[None, None].expand(1, 3, 33, 129)
    before = _baseline(rgb, depth)
    output = edge_aware_upsample_depth(rgb, depth)
    assert torch.equal(before[0], before[4])
    assert (output[0, 4:-4] - output[4, 4:-4]).abs().max() > .001
    changed = output != before
    assert output[changed].min() >= depth.min() and output[changed].max() <= depth.max()
    # The untouched F.interpolate baseline can differ from a repeated source
    # minimum by one float32 ULP. Do not change baseline pixels to hide that.
    assert output.min() >= torch.minimum(before.min(), depth.min())
    assert output.max() <= torch.maximum(before.max(), depth.max())


def test_strength_zero_and_same_size_preserve_values_in_independent_storage():
    rgb, depth, _ = _step()
    output = edge_aware_upsample_depth(rgb, depth, strength=0)
    assert torch.equal(output, _baseline(rgb, depth))
    assert torch.equal(edge_aware_upsample_depth(rgb, depth, max_correction=0), output)
    same_rgb = torch.zeros(1, 3, *depth.shape[-2:])
    same = edge_aware_upsample_depth(same_rgb, depth)
    assert torch.equal(same, depth[0]) and same.data_ptr() != depth.data_ptr()


def test_smooth_gate_crossing_does_not_create_a_threshold_jump():
    rgb, base, _ = _step()
    results = []
    for amplitude in (.1199, .12, .1201):
        depth = base.clone()
        depth[:, :, 9:] = .2 + amplitude
        results.append(edge_aware_upsample_depth(rgb, depth))
    assert (results[2] - results[0]).abs().max() < .0003


@pytest.mark.parametrize("limit", [.005, .04, .1])
def test_final_depth_change_is_smoothly_bounded_without_rescaling_planes(limit):
    rgb, depth, _ = _step()
    output = edge_aware_upsample_depth(rgb, depth, strength=1, max_correction=limit)
    before = _baseline(rgb, depth)
    # Subtracting two float32 values can add <=1 ULP; compare in float64 against
    # the independently rounded scalar bound, instead of widening a tolerance.
    for x in range(65, 72):
        linear = float(before[16, x])
        joint = .2 if x < 68 else .8
        expected = torch.tensor(linear + limit * math.tanh((joint - linear) / limit), dtype=torch.float32)
        torch.testing.assert_close(output[16, x], expected, rtol=0, atol=6e-8)
    assert torch.equal(output[:, :65], before[:, :65])
    assert torch.equal(output[:, 72:], before[:, 72:])


@pytest.mark.parametrize("correction", [True, -.01, 1.01, float("nan"), "0.04", 1e-300, 1e-40])
def test_invalid_correction_is_rejected(correction):
    rgb, depth, _ = _step()
    with pytest.raises(ValueError, match="Max correction"):
        edge_aware_upsample_depth(rgb, depth, max_correction=correction)


def test_channels_symmetry_strides_autocast_and_input_immutability():
    rgb, depth, _ = _step()
    rgb, depth = rgb.transpose(-1, -2), depth.transpose(-1, -2)
    original_rgb, original_depth = rgb.clone(), depth.clone()
    output = edge_aware_upsample_depth(rgb, depth)
    permuted = edge_aware_upsample_depth(rgb[:, [2, 0, 1]], depth)
    with torch.autocast("cpu", dtype=torch.bfloat16):
        under_autocast = edge_aware_upsample_depth(rgb, depth)
    assert output.dtype == torch.float32 and output.device.type == "cpu"
    torch.testing.assert_close(output, permuted, rtol=0, atol=1e-7)
    assert torch.equal(output, under_autocast)
    assert torch.equal(rgb, original_rgb) and torch.equal(depth, original_depth)


@pytest.mark.parametrize("shape,low_shape", [((1, 1), (1, 1)), ((1, 17), (1, 5)),
                                             ((17, 1), (5, 1)), ((9, 17), (1, 5))])
def test_singleton_axes_and_small_shapes(shape, low_shape):
    rgb = torch.rand((1, 3, *shape), generator=torch.Generator().manual_seed(77))
    depth = torch.rand((1, *low_shape), generator=torch.Generator().manual_seed(88))
    output = edge_aware_upsample_depth(rgb, depth)
    assert output.shape == shape and output.dtype == torch.float32
    assert torch.isfinite(output).all() and output.min() >= 0 and output.max() <= 1


@pytest.mark.parametrize("target", ["rgb", "depth"])
@pytest.mark.parametrize("bad", [-.01, 1.01, float("nan"), float("inf")])
def test_invalid_content_is_rejected_even_with_strength_zero(target, bad):
    rgb, depth, _ = _step()
    (rgb if target == "rgb" else depth).flatten()[0] = bad
    with pytest.raises(ValueError, match="finite and in"):
        edge_aware_upsample_depth(rgb, depth, strength=0)


@pytest.mark.parametrize("strength", [True, -.01, 1.01, float("nan"), "0.75"])
def test_invalid_strength_is_rejected(strength):
    rgb, depth, _ = _step()
    with pytest.raises(ValueError, match="Strength"):
        edge_aware_upsample_depth(rgb, depth, strength=strength)


@pytest.mark.parametrize("rgb,depth", [
    (torch.zeros(3, 9, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 4, 9, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(2, 3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(1, 0, 3)),
    (torch.zeros(1, 3, 2, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9, dtype=torch.float64), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(1, 3, 3, dtype=torch.float16)),
])
def test_invalid_layout_or_precision_is_rejected(rgb, depth):
    with pytest.raises(ValueError):
        edge_aware_upsample_depth(rgb, depth)

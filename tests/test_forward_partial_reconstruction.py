"""Synthetic CPU regressions for explicitly estimated partial-pixel recovery.

These checks are separate from the original projection oracle. A reconstructed
pixel is an approximation from observed samples, never known hidden background.
"""

import pytest
import torch

import quest3d.forward_warp as forward_warp
from quest3d.forward_warp import synthesize_forward


def _assert_masks(result):
    assert result.reconstructed_mask.dtype == torch.bool
    assert result.reconstructed_mask.shape == result.hole_mask.shape == result.filled_mask.shape
    assert result.reconstructed_mask.device == result.eyes.device
    assert not torch.any(result.reconstructed_mask & ~result.hole_mask)
    assert not torch.any(result.reconstructed_mask & ~result.filled_mask)
    assert not torch.any(result.filled_mask & ~result.hole_mask)
    assert result.estimated_donor_mask.dtype == torch.bool
    assert result.estimated_donor_mask.shape == result.hole_mask.shape
    assert result.estimated_donor_mask.device == result.eyes.device
    assert not torch.any(result.estimated_donor_mask & ~result.hole_mask)
    assert not torch.any(result.estimated_donor_mask & ~result.filled_mask)
    assert not torch.any(result.estimated_donor_mask & result.reconstructed_mask)
    assert torch.isfinite(result.eyes).all()


def test_smooth_expanding_plane_keeps_colour_and_records_estimated_partial_reconstruction():
    # Adjacent projected unit footprints are .008px apart. The centre has no
    # fully covered background donor in the bounded neighbourhood. This is a
    # sampling gap on a synthetic sloping plane, not an occlusion truth fixture.
    image = torch.tensor([.2, .4, .6]).view(1, 3, 1, 1).expand(1, 3, 3, 128).clone()
    depth = (.3 + torch.arange(128, dtype=torch.float32) * .002).repeat(3, 1)
    before_image, before_depth = image.clone(), depth.clone()
    result = synthesize_forward(image, depth, 8, .5)
    _assert_masks(result)
    assert result.hole_mask[0, :, 64].all()
    assert result.filled_mask[0, :, 64].all()
    assert result.reconstructed_mask[0, :, 64].all()
    expected = image[0, :, :, 64]
    torch.testing.assert_close(result.eyes[0, :, :, 64], expected, rtol=0, atol=1e-6)
    assert not result.hole_mask[1, :, 64].any()
    assert not result.reconstructed_mask[1, :, 64].any()
    torch.testing.assert_close(result.eyes[1, :, :, 64], expected, rtol=0, atol=1e-6)
    assert torch.equal(image, before_image) and torch.equal(depth, before_depth)


def test_bounded_background_fill_precedes_own_foreground_normalization():
    image = torch.zeros((1, 3, 3, 16), dtype=torch.float32)
    image[:, 2] = 1
    image[:, :, :, 7] = torch.tensor([1., 0., 0.])[None, :, None]
    depth = torch.zeros((3, 16), dtype=torch.float32)
    depth[:, 7] = 1
    result = synthesize_forward(image, depth, 1, .5)
    _assert_masks(result)
    for eye, fully_observed_boundary in ((0, 8), (1, 6)):
        # At destination7, red covers .75 and a bounded observed blue donor
        # fills .25. Normalizing red to1 would erase background antialiasing.
        assert result.hole_mask[eye, :, 7].all()
        assert result.filled_mask[eye, :, 7].all()
        assert not result.reconstructed_mask[eye, :, 7].any()
        expected = torch.tensor([.75, 0., .25])[:, None].expand(3, 3)
        torch.testing.assert_close(result.eyes[eye, :, :, 7], expected, rtol=0, atol=1e-6)
        # This boundary is already fully covered and must stay .25red/.75blue.
        assert not result.hole_mask[eye, :, fully_observed_boundary].any()
        expected = torch.tensor([.25, 0., .75])[:, None].expand(3, 3)
        torch.testing.assert_close(result.eyes[eye, :, :, fully_observed_boundary], expected, rtol=0, atol=1e-6)


def test_complete_unobserved_pixels_cannot_use_partial_normalization():
    image = torch.ones((1, 3, 2, 8), dtype=torch.float32)
    depth = torch.ones((2, 8), dtype=torch.float32)
    result = synthesize_forward(image, depth, 20, 0)  # Every source footprint leaves both frames.
    _assert_masks(result)
    assert result.hole_mask.all()
    assert not result.filled_mask.any() and not result.reconstructed_mask.any()
    assert torch.count_nonzero(result.eyes) == 0


def test_sub_epsilon_observed_fragment_is_not_amplified_into_a_full_pixel():
    image = torch.ones((1, 3, 2, 8), dtype=torch.float32)
    depth = torch.ones((2, 8), dtype=torch.float32)
    # Float32 shift is 7.999999523..., leaving only 4.768e-7 of the border tap.
    result = synthesize_forward(image, depth, 2 * (8 - 5e-7), 0)
    _assert_masks(result)
    assert result.hole_mask.all()
    assert not result.filled_mask.any() and not result.reconstructed_mask.any()
    assert torch.count_nonzero(result.eyes) == 0


def test_above_epsilon_fragment_own_reconstruction_is_separate_from_bounded_donor_fill():
    image = torch.zeros((1, 3, 2, 8), dtype=torch.float32)
    image[:, :, :, 0] = torch.tensor([.2, .4, .6])[None, :, None]
    image[:, :, :, -1] = torch.tensor([.7, .3, .1])[None, :, None]
    depth = torch.ones((2, 8), dtype=torch.float32)
    # Just above the declared numerical gate, not a physical quality claim.
    result = synthesize_forward(image, depth, 2 * (8 - 2e-6), 0)
    _assert_masks(result)
    expected_reconstructed = torch.zeros_like(result.hole_mask)
    expected_reconstructed[0, :, -1] = True
    expected_reconstructed[1, :, 0] = True
    assert torch.equal(result.reconstructed_mask, expected_reconstructed)
    # Original observed fragments are now eligible donors for nearby complete
    # holes. They remain distinct from a hole normalizing its nonexistent RGB.
    assert torch.equal(result.estimated_donor_mask, ~expected_reconstructed)
    assert result.filled_mask.all()
    assert result.hole_mask.all()
    torch.testing.assert_close(result.eyes[0, :, :, -1], image[0, :, :, 0], rtol=0, atol=1e-6)
    torch.testing.assert_close(result.eyes[1, :, :, 0], image[0, :, :, -1], rtol=0, atol=1e-6)
    torch.testing.assert_close(result.eyes[0], image[0, :, :, :1].expand(3, 2, 8), rtol=0, atol=1e-6)
    torch.testing.assert_close(result.eyes[1], image[0, :, :, -1:].expand(3, 2, 8), rtol=0, atol=1e-6)


@pytest.mark.parametrize("disparity,depth_value", [(0., .9), (8., .5)])
def test_original_full_coverage_samples_and_masks_remain_exact(disparity, depth_value):
    image = torch.arange(3 * 4 * 24, dtype=torch.float32).reshape(1, 3, 4, 24) / (3 * 4 * 24)
    depth = torch.full((4, 24), depth_value)
    result = synthesize_forward(image, depth, disparity, .5)
    _assert_masks(result)
    assert torch.equal(result.eyes, image.expand(2, -1, -1, -1))
    assert not result.hole_mask.any() and not result.filled_mask.any() and not result.reconstructed_mask.any()


def test_complete_holes_borrow_farther_original_partial_samples_without_recursive_donors(monkeypatch):
    # Left source centres project to .5, 3.5, 6.5, ... . Every observed
    # destination is half-covered; destinations2/5/8/11 are completely empty.
    # No fully covered primary donor exists. Both bounded neighbours exist at2,
    # where the farther red source0 must win over nearer green source1.
    image = torch.zeros((1, 3, 2, 12), dtype=torch.float32)
    image[:, 0] = 1
    image[:, :, :, 1] = torch.tensor([0., 1., 0.])[None, :, None]
    image[:, :, :, 2] = torch.tensor([0., 0., 1.])[None, :, None]
    depth = ((2 * torch.arange(12, dtype=torch.float32) + .5) / 32).repeat(2, 1)
    before_image, before_depth = image.clone(), depth.clone()
    calls = []
    original = forward_warp._background_fill

    def observe(*args, **kwargs):
        calls.append((tuple(x.clone() if isinstance(x, torch.Tensor) else x for x in args),
                      {key: value.clone() for key, value in kwargs.items()}))
        return original(*args, **kwargs)

    monkeypatch.setattr(forward_warp, "_background_fill", observe)
    result = synthesize_forward(image, depth, 64, 0)
    _assert_masks(result)
    assert len(calls) == 2
    projected, holes, remaining, *_ = calls[0][0]
    coverage = 1 - remaining
    original_observed = coverage > 1e-6
    assert torch.equal(calls[1][1]["donor_mask"], original_observed)
    torch.testing.assert_close(calls[1][1]["donor_colour"],
                               projected / coverage.clamp_min(1e-6)[..., None], rtol=0, atol=0)
    expected_complete = ~original_observed
    assert torch.equal(result.estimated_donor_mask, expected_complete)
    assert torch.equal(result.reconstructed_mask, original_observed)
    assert holes.all() and result.hole_mask.all() and result.filled_mask.all()
    torch.testing.assert_close(result.eyes[0, :, :, 2], image[0, :, :, 0], rtol=0, atol=0)
    torch.testing.assert_close(result.eyes[0, :, :, 5], image[0, :, :, 1], rtol=0, atol=0)
    assert not result.reconstructed_mask[0, :, 2].any()
    assert torch.equal(image, before_image) and torch.equal(depth, before_depth)


def _secondary_fill_fixture(width, donor_columns, *, bound, depths=None, mixed_column=None):
    # Exercise the actual production donor-selection operation with explicit
    # original projection facts. Half-covered red donors are distinguished from
    # unobserved black pixels; output colours never become additional donors.
    colour = torch.zeros((2, 1, width, 3), dtype=torch.float32)
    remaining = torch.ones((2, 1, width), dtype=torch.float32)
    nearest = torch.full_like(remaining, -torch.inf)
    farthest = torch.full_like(remaining, torch.inf)
    for index, column in enumerate(donor_columns):
        colour[:, :, column] = torch.tensor([.5, 0., 0.])
        remaining[:, :, column] = .5
        nearest[:, :, column] = farthest[:, :, column] = .2 if depths is None else depths[index]
    if mixed_column is not None:
        nearest[:, :, mixed_column], farthest[:, :, mixed_column] = .9, .1
    coverage = 1 - remaining
    return forward_warp._background_fill(
        colour, coverage == 0, remaining, nearest, farthest, .05, bound,
        donor_mask=coverage > 1e-6,
        donor_colour=colour / coverage.clamp_min(1e-6)[..., None])


def test_secondary_donor_bound_does_not_expand_through_previously_filled_pixels():
    colour, filled = _secondary_fill_fixture(16, [2], bound=3)
    expected = torch.zeros_like(filled)
    expected[:, :, [0, 1, 3, 4, 5]] = True
    assert torch.equal(filled, expected)
    torch.testing.assert_close(colour[:, :, 5], torch.tensor([1., 0., 0.]).expand(2, 1, 3))
    assert torch.count_nonzero(colour[:, :, 6:]) == 0


def test_secondary_internal_hole_refuses_one_donor_when_other_is_outside_bound():
    _, filled = _secondary_fill_fixture(16, [2, 12], bound=3)
    assert not filled[:, :, 4].any()  # Left2 is near, but right12 exists beyond bound.
    assert filled[:, :, 0].all()     # Exposed edge with only one side is permitted.


def test_secondary_mixed_surface_and_zero_coverage_are_not_donors():
    colour, filled = _secondary_fill_fixture(16, [2], bound=16, mixed_column=2)
    assert not filled.any()
    assert torch.count_nonzero(colour[:, :, :2]) == 0
    assert torch.count_nonzero(colour[:, :, 3:]) == 0
    colour, filled = _secondary_fill_fixture(16, [], bound=16)
    assert not filled.any() and torch.count_nonzero(colour) == 0

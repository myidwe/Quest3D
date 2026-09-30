"""CPU projection/occlusion regressions with synthetic depth, not AI or Quest evidence."""

import math

import numpy as np
import pytest
import torch


@pytest.fixture
def warp():
    from quest3d.forward_warp import synthesize_forward
    return synthesize_forward


def _pattern(height=9, width=40):
    y, x = np.indices((height, width))
    rgb = np.stack(((x + 1) / (width + 3), (y + 1) / (height + 3),
                    ((x * 3 + y * 5) % 17) / 17), axis=0).astype(np.float32)
    return torch.from_numpy(rgb[None])


def _integer_projection_oracle(image, depth, disparity, convergence):
    """Scalar source-point projection; no production helper, scatter, grid or fill."""
    source = image.numpy()[0]
    planes = depth.numpy()
    height, width = planes.shape
    expected = np.zeros((2, 3, height, width), dtype=np.float32)
    winner = np.full((2, height, width), -np.inf, dtype=np.float64)
    for eye, sign in enumerate((1, -1)):
        for y in range(height):
            for source_x in range(width):
                projected = source_x + sign * (float(planes[y, source_x]) - convergence) * disparity / 2
                destination = round(projected)
                assert abs(projected - destination) < 1e-6, "integer oracle only"
                if 0 <= destination < width and planes[y, source_x] > winner[eye, y, destination]:
                    winner[eye, y, destination] = planes[y, source_x]
                    expected[eye, :, y, destination] = source[:, y, source_x]
    return torch.from_numpy(expected), torch.from_numpy(np.isneginf(winner))


def _assert_layout_and_masks(result, height, width):
    assert result.eyes.shape == (2, 3, height, width)
    assert result.eyes.dtype == torch.float32 and result.eyes.device.type == "cpu"
    assert result.hole_mask.shape == result.filled_mask.shape == (2, height, width)
    assert result.hole_mask.dtype == result.filled_mask.dtype == torch.bool
    assert result.hole_mask.device == result.filled_mask.device == result.eyes.device
    assert torch.isfinite(result.eyes).all()
    assert torch.all((result.eyes >= 0) & (result.eyes <= 1))
    assert not torch.any(result.filled_mask & ~result.hole_mask), "fill cannot create a covered sample"


def _assert_observed_integer_pixels(result, image, depth, disparity, convergence):
    expected, holes = _integer_projection_oracle(image, depth, disparity, convergence)
    _assert_layout_and_masks(result, *depth.shape)
    assert torch.equal(result.hole_mask, holes), "raw disocclusion mask must survive filling"
    observed = (~holes)[:, None].expand_as(expected)
    torch.testing.assert_close(result.eyes[observed], expected[observed], rtol=0, atol=1e-6)
    # A fill may use a known donor, but unsupported holes have no observed truth.
    unresolved = (holes & ~result.filled_mask)[:, None].expand_as(expected)
    assert torch.count_nonzero(result.eyes[unresolved]) == 0


def test_scalar_integer_oracle_pole_has_correct_sign_collision_and_hole():
    image = torch.zeros((1, 3, 3, 16), dtype=torch.float32)
    image[:, 2] = 1
    image[:, :, :, 7] = torch.tensor([1., 0., 0.])[None, :, None]
    depth = torch.zeros((3, 16), dtype=torch.float32)
    depth[:, 7] = 1
    expected, holes = _integer_projection_oracle(image, depth, 4, .5)
    # Near pole moves to x8 left/x6 right. Its old occluded background is absent.
    assert torch.equal(expected[0, :, 1, 8], torch.tensor([1., 0., 0.]))
    assert torch.equal(expected[1, :, 1, 6], torch.tensor([1., 0., 0.]))
    assert holes[0, 1, 6] and holes[1, 1, 8]
    assert holes[0, :, -1].all() and holes[1, :, 0].all()


@pytest.mark.parametrize("depth_kind", ["varying", "flat"])
def test_zero_disparity_preserves_every_source_sample_and_inputs(warp, depth_kind):
    image = _pattern()
    depth = torch.linspace(0, 1, 40).repeat(9, 1) if depth_kind == "varying" else torch.full((9, 40), .7)
    image_before, depth_before = image.clone(), depth.clone()
    result = warp(image, depth, 0, .5)
    _assert_layout_and_masks(result, 9, 40)
    assert torch.equal(result.eyes, image.expand(2, -1, -1, -1))
    assert not result.hole_mask.any() and not result.filled_mask.any()
    assert torch.equal(image, image_before) and torch.equal(depth, depth_before)


def test_convergence_plane_is_unchanged_even_with_nonzero_disparity(warp):
    image = _pattern()
    depth = torch.full((9, 40), .375)
    result = warp(image, depth, 8, .375)
    assert torch.equal(result.eyes, image.expand(2, -1, -1, -1))
    assert not result.hole_mask.any() and not result.filled_mask.any()


@pytest.mark.parametrize("depth_value,convergence", [(1., .5), (0., .5), (.75, .25)])
def test_integer_flat_plane_projects_correct_eye_source_and_exposes_frame_edges(warp, depth_value, convergence):
    image = _pattern()
    depth = torch.full((9, 40), depth_value)
    result = warp(image, depth, 8, convergence)
    _assert_observed_integer_pixels(result, image, depth, 8, convergence)
    assert result.hole_mask.sum().item() == 2 * 9 * 2


@pytest.mark.parametrize("shape", ["one_pixel_pole", "one_pixel_rail", "crossing_layers", "foreground_block"])
def test_integer_thin_structures_and_depth_order_match_independent_projection(warp, shape):
    image = _pattern()
    depth = torch.zeros((9, 40), dtype=torch.float32)
    if shape == "one_pixel_pole":
        depth[:, 18] = 1
    elif shape == "one_pixel_rail":
        depth[4, 9:29] = 1
    elif shape == "crossing_layers":
        depth[4, 8:30] = .5
        depth[1:8, 18] = 1
    else:
        depth[2:7, 14:23] = 1
    image_before, depth_before = image.clone(), depth.clone()
    result = warp(image, depth, 8, .5)
    _assert_observed_integer_pixels(result, image, depth, 8, .5)
    assert torch.equal(image, image_before) and torch.equal(depth, depth_before)


def test_visible_pole_retains_shape_without_background_occlusion_overwrite(warp):
    image = torch.zeros((1, 3, 7, 32), dtype=torch.float32)
    image[:, 2] = 1
    image[:, :, 1:6, 15] = torch.tensor([1., 0., 0.])[None, :, None]
    depth = torch.zeros((7, 32), dtype=torch.float32)
    depth[1:6, 15] = 1
    result = warp(image, depth, 4, .5)
    _assert_observed_integer_pixels(result, image, depth, 4, .5)
    for eye, x in ((0, 16), (1, 14)):
        assert torch.equal(result.eyes[eye, :, 1:6, x], torch.tensor([1., 0., 0.])[:, None].expand(3, 5))
        assert not result.hole_mask[eye, 1:6, x].any()
        assert not result.filled_mask[eye, 1:6, x].any()
    assert result.hole_mask[0, 1:6, 14].all()
    assert result.hole_mask[1, 1:6, 16].all()


@pytest.mark.parametrize("shift", [.5, 1.5])
def test_subpixel_constant_plane_averages_visible_neighbors_and_reports_partial_edge(warp, shift):
    image = _pattern(height=5, width=32)
    depth = torch.ones((5, 32), dtype=torch.float32)
    result = warp(image, depth, 2 * shift, 0)
    _assert_layout_and_masks(result, 5, 32)
    for eye, sign in enumerate((1, -1)):
        for destination in range(2, 30):
            source_position = destination - sign * shift
            lo = math.floor(source_position)
            fraction = source_position - lo
            expected = image[0, :, :, lo] * (1 - fraction) + image[0, :, :, lo + 1] * fraction
            torch.testing.assert_close(result.eyes[eye, :, :, destination], expected, rtol=0, atol=1e-6)
            assert not result.hole_mask[eye, :, destination].any()
        edge = math.floor(shift) if eye == 0 else 31 - math.floor(shift)
        assert result.hole_mask[eye, :, edge].all(), "partial coverage must not be reported as observed-full"


def test_mirrored_scene_swaps_eyes_without_changing_observed_source_correspondence(warp):
    image = _pattern()
    depth = torch.zeros((9, 40), dtype=torch.float32)
    depth[1:8, 12:16] = 1
    result = warp(image, depth, 8, .5)
    mirrored = warp(image.flip(-1), depth.flip(-1), 8, .5)
    assert torch.equal(mirrored.hole_mask, result.hole_mask.flip(0).flip(-1))
    observed = (~mirrored.hole_mask)[:, None].expand_as(result.eyes)
    torch.testing.assert_close(mirrored.eyes[observed], result.eyes.flip(0).flip(-1)[observed], rtol=0, atol=1e-6)


def test_subpixel_foreground_only_occludes_its_footprint_and_overlap_is_not_extra_coverage(warp):
    image = torch.zeros((1, 3, 3, 16), dtype=torch.float32)
    image[:, 2] = 1  # Far blue plane; only observed source background is available.
    image[:, :, :, 7] = torch.tensor([1., 0., 0.])[None, :, None]
    depth = torch.zeros((3, 16), dtype=torch.float32)
    depth[:, 7] = 1
    result = warp(image, depth, 1, .5)
    _assert_layout_and_masks(result, 3, 16)
    # Left near footprint [6.75,7.75]; destination8 covers [7.5,8.5].
    # Red covers .25, visible blue .75. The near source must not paint all of8.
    # The corresponding mirrored right edge is destination6.
    for eye, boundary in ((0, 8), (1, 6)):
        expected = torch.tensor([.25, 0., .75])[:, None].expand(3, 3)
        torch.testing.assert_close(result.eyes[eye, :, :, boundary], expected, rtol=0, atol=1e-6)
        assert not result.hole_mask[eye, :, boundary].any()
        # At destination7, the overlapping blue tap is behind the red footprint;
        # adding its weight would falsely hide the uncovered .25 interval.
        assert result.hole_mask[eye, :, 7].all()


@pytest.mark.parametrize("invalid", ["image_nan", "depth_nan", "depth_inf", "negative_depth", "large_depth",
                                    "negative_disparity", "infinite_disparity", "nan_convergence", "large_convergence",
                                    "wrong_image_channels", "wrong_depth_shape"])
def test_invalid_input_cannot_publish_nonfinite_or_misbound_projection(warp, invalid):
    image, depth = _pattern(), torch.zeros((9, 40), dtype=torch.float32)
    disparity, convergence = 4., .5
    if invalid == "image_nan": image[0, 0, 0, 0] = float("nan")
    elif invalid == "depth_nan": depth[0, 0] = float("nan")
    elif invalid == "depth_inf": depth[0, 0] = float("inf")
    elif invalid == "negative_depth": depth[0, 0] = -.01
    elif invalid == "large_depth": depth[0, 0] = 1.01
    elif invalid == "negative_disparity": disparity = -1.
    elif invalid == "infinite_disparity": disparity = float("inf")
    elif invalid == "nan_convergence": convergence = float("nan")
    elif invalid == "large_convergence": convergence = 1.01
    elif invalid == "wrong_image_channels": image = image[:, :2]
    elif invalid == "wrong_depth_shape": depth = depth[:, :-1]
    with pytest.raises((ValueError, TypeError)):
        warp(image, depth, disparity, convergence)


def test_invalid_depth_is_not_hidden_by_zero_disparity_fast_path(warp):
    image = _pattern()
    depth = torch.zeros((9, 40), dtype=torch.float32)
    depth[2, 3] = float("nan")
    with pytest.raises((ValueError, TypeError)):
        warp(image, depth, 0, .5)

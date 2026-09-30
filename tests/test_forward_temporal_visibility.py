"""Independent visible-footprint phase oracles; synthetic CPU, not AI/Quest proof.

Case generators and oracle/assertion helpers are deliberately independent of the
CPU test entry points, so an explicit GPU harness can use identical known input.
No production helper supplies expected colours, visibility, or missing area.
"""

import math
from typing import NamedTuple

import numpy as np
import torch

from quest3d.forward_warp import synthesize_forward


class VisibilityCase(NamedTuple):
    name: str
    image: torch.Tensor
    depth: torch.Tensor
    disparity: float
    convergence: float
    phase: float


def flat_phase_cases():
    width = 1920
    rgb = np.zeros((3, width), np.float32)
    rgb[0, 1700] = 1
    rgb[1, 1680] = rgb[1, 1683] = 1
    rgb[2] = (np.arange(width) % 7) / 8
    for step in range(-16, 17):
        phase = step / 32
        yield VisibilityCase(f"flat-{step}", torch.from_numpy(rgb[None, :, None].copy()),
                             torch.full((1, width), .5), 8., .5-phase/4, phase)


def _pole_case(name, depth_background, depth_pole, disparity, convergence, phase):
    rgb = np.zeros((3, 64), np.float32)
    rgb[2] = 1
    rgb[:, 30] = (1., 0., 0.)
    depth = torch.full((1, 64), depth_background)
    depth[:, 30] = depth_pole
    return VisibilityCase(name, torch.from_numpy(rgb[None, :, None].copy()), depth,
                          disparity, convergence, phase)


def thin_phase_cases():
    for step in range(-16, 17):
        phase = step/32
        yield _pole_case(f"thin-{step}", 0., 1., 4., .5-phase/2, phase)


def threshold_cases():
    for separation in (.24, .249, .2499, .25, .2501, .251, .26):
        yield _pole_case(f"threshold-{separation}", .5, .5+separation/4,
                         8., .5, separation)


def small_separation_cases():
    # Binary fractions remain exactly representable at these source coordinates.
    # Small but nonzero depth differences still have an exact visibility order.
    for separation in (2**-18, 2**-12, 1/256, 1/128, 1/64, 1/32, 1/16, 1/8):
        yield _pole_case(f"small-{separation}", .5, .5+separation/4,
                         8., .5, separation)


def flat_plane_oracle(case):
    """Closed-form integral for translated source unit pixels on one plane."""
    rgb = case.image[0, :, 0].numpy().astype(np.float64)
    width = rgb.shape[1]
    colour = np.zeros((2, 3, width), np.float64)
    coverage = np.zeros((2, width), np.float64)
    shift = (float(case.depth[0, 0])-case.convergence)*case.disparity/2
    for eye, sign in enumerate((1, -1)):
        source_position = np.arange(width)-sign*shift
        lo = np.floor(source_position).astype(int)
        fraction = source_position-lo
        for indices, weights in ((lo, 1-fraction), (lo+1, fraction)):
            valid = (indices >= 0) & (indices < width)
            colour[eye, :, valid] += (rgb[:, indices[valid]]*weights[valid]).T
            coverage[eye, valid] += weights[valid]
    return colour, coverage


def visible_interval_oracle(case):
    """Scalar integration of known visible source footprints, no guessed fill.

    Split each destination interval at every source footprint boundary. On each
    nonempty subinterval choose the actual nearest observed source. Unseen
    intervals contribute zero premultiplied colour and zero observed coverage.
    """
    rgb = case.image[0, :, 0].numpy().astype(np.float64)
    depth = case.depth[0].numpy().astype(np.float64)
    width = len(depth)
    colour = np.zeros((2, 3, width), np.float64)
    coverage = np.zeros((2, width), np.float64)
    for eye, sign in enumerate((1, -1)):
        centers = np.arange(width)+sign*(depth-case.convergence)*case.disparity/2
        for x in range(width):
            left, right = x-.5, x+.5
            candidates = np.where((centers+.5 > left) & (centers-.5 < right))[0]
            points = sorted({left, right,
                             *[max(left, float(centers[j]-.5)) for j in candidates],
                             *[min(right, float(centers[j]+.5)) for j in candidates]})
            for a, b in zip(points, points[1:]):
                midpoint = (a+b)/2
                observed = [j for j in candidates if centers[j]-.5 <= midpoint < centers[j]+.5]
                if observed:
                    winner = max(observed, key=lambda j: depth[j])
                    colour[eye, :, x] += rgb[:, winner]*(b-a)
                    coverage[eye, x] += b-a
    return colour, coverage


def assert_visibility_result(result, expected, coverage, *, atol=8e-6):
    """Known RGB is exact; unknown RGB only has a defensible component interval.

    The 8e-6 bound accommodates two float32 coordinate ULPs at width64, including
    nonbinary threshold inputs. It is far below the previous .05 colour cliff.
    Raw coverage uses the public 1e-6 missing-area cutoff, never filled_mask.
    GPU harnesses may call this after their actual production dispatch completes.
    """
    actual = result.eyes.detach().cpu().numpy()[:, :, 0].astype(np.float64)
    holes = result.hole_mask.detach().cpu().numpy()[:, 0]
    assert np.isfinite(actual).all()
    np.testing.assert_array_equal(holes, coverage < 1-1e-6)
    known = np.broadcast_to((coverage >= 1-1e-12)[:, None], actual.shape)
    np.testing.assert_allclose(actual[known], expected[known], rtol=0, atol=atol)
    unknown = 1-coverage
    assert float(np.maximum(expected-actual, 0).max()) <= atol
    assert float(np.maximum(actual-(expected+unknown[:, None]), 0).max()) <= atol
    return actual


def _run_cpu(case):
    before_image, before_depth = case.image.clone(), case.depth.clone()
    result = synthesize_forward(case.image, case.depth, case.disparity, case.convergence)
    assert torch.equal(case.image, before_image) and torch.equal(case.depth, before_depth)
    return result


def test_known_flat_plane_phase_rgb_mass_centroid_and_variance():
    previous_error = None
    for case in flat_phase_cases():
        expected, coverage = flat_plane_oracle(case)
        actual = assert_visibility_result(_run_cpu(case), expected, coverage, atol=1e-6)
        residual = actual[:, :, 8:-8]-expected[:, :, 8:-8]
        if previous_error is not None:
            np.testing.assert_allclose(residual-previous_error, 0, rtol=0, atol=2e-6)
        previous_error = residual
        for eye, sign in enumerate((1, -1)):
            red = actual[eye, 0]
            mass = red.sum()
            center = float((red*np.arange(len(red))).sum()/mass)
            variance = float((red*(np.arange(len(red))-center)**2).sum()/mass)
            expected_center = 1700+sign*case.phase
            fraction = expected_center-math.floor(expected_center)
            assert abs(mass-1) <= 1e-6
            assert abs(center-expected_center) <= 1e-6
            assert abs(variance-fraction*(1-fraction)) <= 1e-6


def test_known_thin_two_plane_visibility_and_unknown_ranges_across_33_phases():
    for case in thin_phase_cases():
        expected, coverage = visible_interval_oracle(case)
        assert_visibility_result(_run_cpu(case), expected, coverage, atol=1e-6)


def test_small_nonzero_depth_differences_preserve_exact_observed_visibility():
    for case in small_separation_cases():
        expected, coverage = visible_interval_oracle(case)
        assert_visibility_result(_run_cpu(case), expected, coverage)


def test_quarter_pixel_threshold_has_no_unexplained_visible_colour_jump():
    # Collect every threshold sample before comparing: the complete sequence
    # tests continuity of error against an independent per-frame visibility
    # oracle, rather than demanding a stationary output for a moving surface.
    actual_red, expected_red = [], []
    results = []
    for case in threshold_cases():
        expected, coverage = visible_interval_oracle(case)
        result = _run_cpu(case)
        assert coverage[0, 31] == 1 and not result.hole_mask[0, 0, 31]
        actual_red.append(float(result.eyes[0, 0, 0, 31]))
        expected_red.append(float(expected[0, 0, 31]))
        results.append((result, expected, coverage))
    residual = np.asarray(actual_red)-np.asarray(expected_red)
    np.testing.assert_allclose(np.diff(residual), 0, rtol=0, atol=16e-6)
    for result, expected, coverage in results:
        assert_visibility_result(result, expected, coverage)

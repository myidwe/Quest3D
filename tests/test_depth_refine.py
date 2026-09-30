"""CPU synthetic comparisons, not an actual model/Quest quality benchmark.

The negative controls deliberately retain two guided-filter limitations: texture
transfer with an aliased guide, and loss of a thin depth feature without an RGB
edge. Passing these tests does not justify enabling the candidate by default.
"""

import pytest
import torch

from quest3d.depth_refine import bilinear_upsample_depth, guided_upsample_depth


@pytest.fixture(autouse=True)
def _bounded_cpu_threads():
    old = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(old)


def _step():
    # Rectangular, deliberately odd dimensions; a step halfway between low
    # sample centers tests interpolation instead of a coincident-grid shortcut.
    low = torch.full((1, 9, 17), 0.2)
    low[:, :, 9:] = 0.8
    rgb = torch.zeros((1, 3, 49, 129))
    rgb[:, 0, :, :68] = 1
    rgb[:, 1, :, 68:] = 1
    truth = torch.full((49, 129), 0.2)
    truth[:, 68:] = 0.8
    return rgb, low, truth


def test_bilinear_matches_existing_corner_aligned_depth_coordinates():
    low = torch.tensor([[[0.0, 0.2], [0.8, 1.0]]])
    actual = bilinear_upsample_depth(low, (5, 9))
    y = torch.linspace(0, 1, 5)[:, None]
    x = torch.linspace(0, 1, 9)[None, :]
    torch.testing.assert_close(actual, 0.8 * y + 0.2 * x)
    assert actual[0, 0] == 0 and actual[-1, -1] == 1


def test_aligned_color_edge_reduces_depth_transition_error_without_step_halo():
    rgb, low, truth = _step()
    baseline = bilinear_upsample_depth(low, truth.shape)
    actual = guided_upsample_depth(rgb, low)
    edge = slice(60, 76)
    assert (actual[:, edge] - truth[:, edge]).abs().mean() < (
        baseline[:, edge] - truth[:, edge]).abs().mean() * 0.4
    # Use 0.2/0.8, so the final 0..1 clamp cannot hide overshoot at this edge.
    assert actual.min() >= 0.2 - 2e-5
    assert actual.max() <= 0.8 + 2e-5
    assert torch.diff(actual[24]).min() >= -2e-5
    assert torch.count_nonzero((actual[24] > 0.3) & (actual[24] < 0.7)) < torch.count_nonzero(
        (baseline[24] > 0.3) & (baseline[24] < 0.7))


@pytest.mark.parametrize("value", [0.0, 0.37, 1.0])
def test_rgb_texture_does_not_create_structure_when_depth_is_constant(value):
    generator = torch.Generator(device="cpu").manual_seed(81)
    rgb = torch.rand((1, 3, 47, 83), generator=generator)
    low = torch.full((1, 11, 19), value)
    actual = guided_upsample_depth(rgb, low, epsilon=1e-4)
    torch.testing.assert_close(actual, torch.full((47, 83), value), atol=2e-5, rtol=0)


def test_missing_thin_depth_structure_is_not_invented_from_rgb_line():
    rgb = torch.zeros((1, 3, 49, 129))
    rgb[:, :, :, 63:66] = 1  # high-frequency rail absent from model output
    low = torch.full((1, 9, 17), 0.4)
    actual = guided_upsample_depth(rgb, low)
    torch.testing.assert_close(actual, bilinear_upsample_depth(low, (49, 129)), atol=2e-5, rtol=0)


def test_thin_structure_with_matching_guide_is_localized_not_widened_to_bilinear_ramp():
    low = torch.full((1, 9, 17), 0.2)
    low[:, :, 8] = 0.8
    rgb = torch.zeros((1, 3, 49, 129))
    rgb[:, :, :, 61:68] = 1
    actual = guided_upsample_depth(rgb, low)
    baseline = bilinear_upsample_depth(low, (49, 129))
    assert actual[24, 64] > 0.7
    outside = torch.ones(129, dtype=torch.bool)
    outside[61:68] = False
    assert (actual[24, outside] - 0.2).abs().mean() < (baseline[24, outside] - 0.2).abs().mean()
    assert actual.min() >= 0.2 - 2e-5 and actual.max() <= 0.8 + 2e-5


def test_limitation_thin_depth_feature_without_color_edge_loses_contrast():
    low = torch.full((1, 9, 17), 0.2)
    low[:, :, 8] = 0.8
    rgb = torch.full((1, 3, 49, 129), 0.5)
    baseline = bilinear_upsample_depth(low, (49, 129))
    actual = guided_upsample_depth(rgb, low)
    # Negative control: the method must not be sold as a general thin-object
    # recovery. With a flat guide the two box averages erase peak contrast.
    assert baseline[24, 64] == pytest.approx(0.8)
    assert actual[24, 64] < 0.5
    assert actual[24, 64] > 0.2


def test_limitation_aliased_rgb_texture_can_transfer_into_nonconstant_depth():
    low = torch.linspace(0.2, 0.8, 17)[None, None, :].expand(1, 17, 17)
    ramp = torch.linspace(0.2, 0.8, 129)[None, :].expand(129, 129)
    # Low-grid y centers are multiples of 8 and all miss the bright stripes.
    stripe = (torch.arange(129) % 8 >= 4).float()[:, None] * 0.1
    rgb = (ramp + stripe)[None, None].expand(1, 3, 129, 129)
    baseline = bilinear_upsample_depth(low, (129, 129))
    actual = guided_upsample_depth(rgb, low)
    assert (baseline[64] - baseline[68]).abs().max() == 0
    ripple = (actual[64, 24:104] - actual[68, 24:104]).abs().mean()
    assert 0.015 < ripple < 0.1, "retain this failure case before any production opt-in"


def test_displaced_rgb_edge_is_not_evidence_of_correct_object_geometry():
    rgb, low, truth = _step()
    rgb = torch.roll(rgb, shifts=16, dims=-1)
    actual = guided_upsample_depth(rgb, low)
    baseline = bilinear_upsample_depth(low, truth.shape)
    # A wrong guide can make the actual depth boundary worse. This test exposes
    # that limitation; it must not be hidden by evaluating aligned examples only.
    assert (actual[:, 52:100] - truth[:, 52:100]).abs().mean() > (
        baseline[:, 52:100] - truth[:, 52:100]).abs().mean()


def test_rgb_channels_are_used_symmetrically_and_inputs_are_not_mutated():
    rgb, low, _ = _step()
    before_rgb, before_depth = rgb.clone(), low.clone()
    actual = guided_upsample_depth(rgb, low)
    permuted = guided_upsample_depth(rgb[:, [2, 0, 1]], low)
    torch.testing.assert_close(actual, permuted, atol=2e-5, rtol=0)
    assert torch.equal(rgb, before_rgb) and torch.equal(low, before_depth)


@pytest.mark.parametrize("dtype", [torch.float16, torch.bfloat16, torch.float32, torch.float64])
def test_tiny_rectangular_noncontiguous_inputs_and_precision(dtype):
    rgb = torch.full((1, 3, 13, 9), 0.5, dtype=dtype).transpose(-1, -2)
    low = torch.tensor([[[0.1, 0.5, 0.9]]], dtype=dtype)
    actual = guided_upsample_depth(rgb, low, radius=4)
    assert actual.shape == (9, 13) and actual.device.type == "cpu"
    assert actual.dtype == (torch.float64 if dtype == torch.float64 else torch.float32)
    assert bool(torch.isfinite(actual).all())
    assert 0 <= actual.min() <= actual.max() <= 1


@pytest.mark.parametrize("target", ["rgb", "depth"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -0.01, 1.01])
def test_invalid_content_is_rejected_not_sanitized(target, bad):
    rgb, low, _ = _step()
    value = rgb if target == "rgb" else low
    value.flatten()[0] = bad
    with pytest.raises(ValueError, match="finite and normalized"):
        guided_upsample_depth(rgb, low)


@pytest.mark.parametrize("kwargs", [{"radius": 0}, {"radius": 5}, {"radius": True},
                                   {"radius": 1.5}, {"epsilon": 0}, {"epsilon": 1.01},
                                   {"epsilon": True}, {"epsilon": float("nan")}])
def test_parameter_bounds_are_explicit(kwargs):
    rgb, low, _ = _step()
    with pytest.raises(ValueError):
        guided_upsample_depth(rgb, low, **kwargs)


@pytest.mark.parametrize("rgb,low", [
    (torch.zeros(3, 9, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 4, 9, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(2, 3, 3)),
    (torch.zeros(1, 3, 2, 9), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9, dtype=torch.uint8), torch.zeros(1, 3, 3)),
    (torch.zeros(1, 3, 9, 9), torch.zeros(1, 0, 3)),
])
def test_incompatible_shapes_and_types_are_rejected(rgb, low):
    with pytest.raises(ValueError):
        guided_upsample_depth(rgb, low)


def test_callers_autocast_does_not_reduce_covariance_solver_precision():
    rgb, low, _ = _step()
    expected = guided_upsample_depth(rgb, low, epsilon=1e-4)
    with torch.autocast(device_type="cpu", dtype=torch.bfloat16):
        actual = guided_upsample_depth(rgb, low, epsilon=1e-4)
    assert actual.dtype == torch.float32
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)

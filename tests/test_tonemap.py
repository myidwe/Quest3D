"""Numeric tone-map contracts; generated ramps are not display calibration."""
import pytest
import torch

from quest3d.tonemap import scrgb_to_bgra8


def test_linear_srgb_reference_values_and_hdr_highlight_separation():
    levels = torch.tensor([0, .0031308, .18, .5, .75, 1, 2, 4], dtype=torch.float32)
    rgba = torch.ones((1, 8, 4), dtype=torch.float32)
    rgba[..., :3] = levels[None, :, None] * 4.8
    result = scrgb_to_bgra8(rgba, sdr_white_scale=4.8)
    # Standard sRGB lower-range reference values; declared shoulder at >.75.
    assert result[0, :, 0].tolist() == [0, 10, 118, 188, 225, 240, 250, 253]
    assert torch.equal(result[..., 0], result[..., 1])
    assert torch.equal(result[..., 1], result[..., 2])
    assert bool((result[..., 3] == 255).all())


def test_rgba_to_bgra_swizzle_and_alpha_are_explicit():
    rgba = torch.tensor([[[1., 0, 0, 0], [0, 1., 0, .5], [0, 0, 1., 1]]])
    result = scrgb_to_bgra8(rgba, sdr_white_scale=1)
    assert result.tolist() == [[[0, 0, 240, 255], [0, 240, 0, 255], [240, 0, 0, 255]]]
    assert result.is_contiguous() and result.dtype == torch.uint8


def test_white_normalization_is_stable_across_actual_display_white_levels():
    values = torch.linspace(0, 4, 256).repeat(4, 1).T.reshape(1, 256, 4)
    first = scrgb_to_bgra8(values * 4.8, sdr_white_scale=4.8)
    second = scrgb_to_bgra8(values * 3, sdr_white_scale=3)
    assert torch.equal(first, second)


def test_extended_negative_and_nonfinite_values_have_bounded_defined_output():
    rgba = torch.tensor([[[-.5, 0, 0, 1], [float("nan"), float("inf"), float("-inf"), 1]]])
    result = scrgb_to_bgra8(rgba, sdr_white_scale=1)
    assert result[0, 0].tolist() == [0, 0, 0, 255]
    assert result[0, 1].tolist() == [0, 255, 0, 255]


@pytest.mark.parametrize("scale", [None, True, 0, -1, float("nan"), float("inf"), 10 ** 400])
def test_unmeasured_or_invalid_white_is_rejected(scale):
    with pytest.raises(ValueError):
        scrgb_to_bgra8(torch.zeros(2, 2, 4), sdr_white_scale=scale)


@pytest.mark.gpu
def test_fp16_cuda_mapping_matches_cpu_reference_with_one_code_tolerance():
    if not torch.cuda.is_available():
        pytest.skip("CUDA is unavailable")
    generator = torch.Generator().manual_seed(2049)
    rgba = (torch.rand((65, 127, 4), generator=generator) * 12.5).half()
    expected = scrgb_to_bgra8(rgba, sdr_white_scale=4.8)
    actual = scrgb_to_bgra8(rgba.cuda(), sdr_white_scale=4.8).cpu()
    assert (actual.int() - expected.int()).abs().max().item() <= 1

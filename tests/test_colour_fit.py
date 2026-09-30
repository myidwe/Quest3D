"""CPU-only fit/projection quantization and original-frame ownership checks."""

import pytest
import torch
import torch.nn.functional as F

from quest3d.colour_fit import fit_bgra_float
from quest3d.forward_warp import synthesize_forward
from quest3d.stereo import StereoSynthesizer


@pytest.fixture(autouse=True)
def _cpu_threads():
    previous = torch.get_num_threads()
    torch.set_num_threads(2)
    yield
    torch.set_num_threads(previous)


def _frame(width, height):
    y, x = torch.meshgrid(torch.arange(height), torch.arange(width), indexing="ij")
    return torch.stack(((3 * x + y) % 256, (x + 5 * y) % 256,
                        (7 * x + 11 * y) % 256, (19 * x + y) % 256), -1).to(torch.uint8)


@pytest.mark.parametrize("source,eye,rect", [((64, 48), (48, 36), (0, 0, 48, 36)),
    ((31, 31), (48, 36), (6, 0, 36, 36)), ((25, 50), (48, 36), (15, 0, 18, 36)),
    ((121, 79), (200, 100), (23, 0, 153, 100)), ((1, 2), (1, 1), (0, 0, 1, 1))])
def test_fit_geometry_matches_existing_letterbox_math(source, eye, rect):
    result = fit_bgra_float(_frame(*source), *eye)
    assert result.content_rect == rect
    assert tuple(result.image.shape) == (1, 3, rect[3], rect[2])
    assert result.image.dtype == torch.float32 and result.image.device.type == "cpu"
    assert result.image.is_contiguous()
    assert result.image.min() >= 0 and result.image.max() <= 1


def test_no_resize_preserves_all_original_colour_bytes_after_final_quantization():
    source = _frame(64, 48)
    result = fit_bgra_float(source, 64, 48)
    quantized = (result.image[0].permute(1, 2, 0) * 255).round().to(torch.uint8)
    assert torch.equal(quantized, source[:, :, :3])
    assert result.image.untyped_storage().data_ptr() != source.untyped_storage().data_ptr()


def test_three_channel_fit_matches_same_bicubic_float_reference_before_rounding():
    source = _frame(64, 48)
    actual = fit_bgra_float(source, 48, 36).image
    # Reference retains all original RGBA channels, as the current _fit does;
    # discarding alpha must not change the interpolation of any colour channel.
    expected = F.interpolate(source.permute(2, 0, 1)[None].float(), (36, 48),
                             mode="bicubic", align_corners=False, antialias=True)
    expected = expected[:, :3].clamp(0, 255) / 255
    torch.testing.assert_close(actual, expected, rtol=0, atol=0)
    assert torch.count_nonzero(actual * 255 != (actual * 255).round()) > 0


def test_original_2d_is_byte_identical_before_and_after_candidate_and_later_frames():
    source = _frame(64, 48)
    before = source.clone()
    synth = StereoSynthesizer(48, 36, disparity_px=0, resize_filter="bicubic-aa")
    original = synth.original_2d(source, frame_id=71, generation=2)
    original_bytes = original.bgra.copy()
    first = fit_bgra_float(source, 48, 36)
    first_bytes = first.image.clone()
    second = fit_bgra_float(torch.flip(source, dims=(0, 1)), 48, 36)
    with torch.inference_mode():
        second.image.fill_(.99)
    assert torch.equal(source, before) and torch.equal(first.image, first_bytes)
    assert (original.bgra == original_bytes).all()
    assert (synth.original_2d(source, frame_id=71, generation=2).bgra == original_bytes).all()
    # Even a consumer modifying the first candidate cannot affect its source or
    # independently retained original frame. There is no shared output cache.
    with torch.inference_mode():
        first.image.zero_()
    assert torch.equal(source, before) and (original.bgra == original_bytes).all()


def test_noncontiguous_capture_and_autocast_do_not_change_precision_or_source():
    source = _frame(64, 48).transpose(0, 1)
    before = source.clone()
    reference = fit_bgra_float(source.contiguous(), 36, 48)
    with torch.autocast("cpu", dtype=torch.bfloat16):
        actual = fit_bgra_float(source, 36, 48)
    assert torch.equal(actual.image, reference.image)
    assert torch.equal(source, before)


def test_alpha_does_not_change_synthesized_colour_and_output_clamps_cubic_overshoot():
    source = torch.zeros(32, 64, 4, dtype=torch.uint8)
    source[:, 31:, :3] = 255
    source[:, :, 3] = 17
    actual = fit_bgra_float(source, 96, 48).image
    source[:, :, 3] = 233
    assert torch.equal(actual, fit_bgra_float(source, 96, 48).image)
    assert actual.min() == 0 and actual.max() == 1


def test_one_quantization_improves_known_visible_subpixel_projection_error():
    # Low-contrast deterministic structure makes one code-value rounding error
    # visible to the metric without inventing higher-resolution source detail.
    y, x = torch.meshgrid(torch.arange(48), torch.arange(64), indexing="ij")
    source = torch.stack((124 + (x + 2 * y) % 7, 126 + (3 * x + y) % 5,
                          125 + (x * y) % 6, torch.full_like(x, 255)), -1).to(torch.uint8)
    candidate = fit_bgra_float(source, 48, 36).image
    previous, _ = StereoSynthesizer(48, 36, disparity_px=0, resize_filter="bicubic-aa")._fit(source)
    previous = previous[:, :, :3].permute(2, 0, 1)[None].float() / 255
    depth = torch.full((36, 48), .75)
    new_eyes = synthesize_forward(candidate, depth, 2., .5).eyes
    old_eyes = synthesize_forward(previous, depth, 2., .5).eyes
    # Independent constant-plane visibility oracle: each eye projects by .25px.
    # Use float64 resampling and combine original samples without intermediate
    # quantization. Exclude the genuinely disoccluded outer image columns.
    fitted = F.interpolate(source[:, :, :3].permute(2, 0, 1)[None].double(), (36, 48),
                           mode="bicubic", align_corners=False, antialias=True)[0].clamp(0, 255)
    left = .25 * fitted[:, :, :-2] + .75 * fitted[:, :, 1:-1]
    right = .75 * fitted[:, :, 1:-1] + .25 * fitted[:, :, 2:]
    oracle = torch.stack((left, right)).round().to(torch.uint8)
    quantize = lambda eyes: (eyes[:, :, :, 1:-1] * 255).round().to(torch.uint8)
    new_error = (quantize(new_eyes).int() - oracle.int()).abs()
    old_error = (quantize(old_eyes).int() - oracle.int()).abs()
    assert new_error.float().mean() < old_error.float().mean()
    assert torch.count_nonzero(new_error) < torch.count_nonzero(old_error)
    # Geometry/depth and the original input are identical; this removes only
    # an additional rounding. At most one final code value separates candidates.
    assert (quantize(new_eyes).int() - quantize(old_eyes).int()).abs().max() <= 1


@pytest.mark.parametrize("source,width,height", [
    (torch.zeros(8, 8, 3, dtype=torch.uint8), 16, 16),
    (torch.zeros(8, 8, 4), 16, 16), (torch.zeros(0, 8, 4, dtype=torch.uint8), 16, 16),
    (torch.zeros(8, 8, 4, dtype=torch.uint8), 0, 16),
    (torch.zeros(8, 8, 4, dtype=torch.uint8), 16, True),
    (torch.zeros(8, 8, 4, dtype=torch.uint8), 16., 16),
])
def test_invalid_inputs_are_rejected_without_implicit_conversion(source, width, height):
    with pytest.raises(ValueError):
        fit_bgra_float(source, width, height)

"""Stereo invariants using explicitly synthetic depth, not an AI quality benchmark."""

import cv2
import numpy as np
import pytest
import torch
from PIL import Image

from quest3d.depth import DepthResult
from quest3d.stereo import StereoSynthesizer


def _synthetic_depth(values, *, frame_id=1, generation=0):
    tensor = torch.as_tensor(values, dtype=torch.float32)
    if tensor.ndim == 2:
        tensor = tensor.unsqueeze(0)
    return DepthResult(frame_id, generation, tensor, tuple(tensor.shape[-2:]), 0.0, 0.0)


def _image(width=200, height=100):
    # Deterministic multichannel pattern exposes swaps, interpolation, and cropping.
    y, x = np.indices((height, width))
    return np.stack(((x * 3 + y) % 256, (x + y * 2) % 256,
                     (x * 7 + y * 5) % 256, np.full_like(x, 255)), axis=-1).astype(np.uint8)


@pytest.mark.parametrize("disparity", [0, 6])
@pytest.mark.parametrize("depth_id,depth_generation,rgb_id,rgb_generation", [(1, 0, 2, 0), (1, 0, 1, 1)])
def test_different_frame_or_generation_is_rejected_even_in_zero_disparity_mode(
    disparity, depth_id, depth_generation, rgb_id, rgb_generation
):
    image = _image()
    depth = _synthetic_depth(np.ones((100, 200)), frame_id=depth_id, generation=depth_generation)
    synth = StereoSynthesizer(200, 100, disparity_px=disparity)
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        synth.synthesize(image, depth, frame_id=rgb_id, generation=rgb_generation)


@pytest.mark.parametrize("source_size,eye_size", [((200, 100), (200, 100)), ((100, 100), (200, 100)),
                                                  ((60, 120), (128, 72)), ((121, 79), (200, 100))])
def test_zero_disparity_is_exact_unwarped_letterboxed_source_in_both_eyes(source_size, eye_size):
    image = _image(*source_size)
    synth = StereoSynthesizer(*eye_size, disparity_px=0)
    # Deliberately varied test depth must have no effect in the original-2D mode.
    depth = _synthetic_depth(np.arange(image.shape[0] * image.shape[1]).reshape(image.shape[:2]))
    result = synth.synthesize(image, depth, frame_id=1, generation=0)
    ew, eh = eye_size
    expected = np.zeros((eh, ew, 4), dtype=np.uint8)
    expected[:, :, 3] = 255
    ratio = min(ew / source_size[0], eh / source_size[1])
    fw, fh = round(source_size[0] * ratio), round(source_size[1] * ratio)
    ox, oy = (ew - fw) // 2, (eh - fh) // 2
    expected[oy:oy + fh, ox:ox + fw] = cv2.resize(image, (fw, fh), interpolation=cv2.INTER_AREA)
    assert result.mode == "2d"
    assert result.bgra.shape == (eh, ew * 2, 4)
    assert np.array_equal(result.bgra[:, :ew], expected)
    assert np.array_equal(result.bgra[:, ew:], expected)


def test_flat_ui_pixels_are_unwarped_and_identical_in_both_eyes():
    image = _image()
    depth = _synthetic_depth(np.linspace(0, 1, 200)[None, :].repeat(100, axis=0))
    mask = np.zeros((100, 200), dtype=bool)
    mask[:12, :] = True  # address bar
    mask[40:60, 80:130] = True  # explicit flat control region
    result = StereoSynthesizer(200, 100, disparity_px=8).synthesize(
        image, depth, frame_id=1, generation=0, flat_mask=mask)
    assert np.array_equal(result.bgra[:, :200][mask], image[mask])
    assert np.array_equal(result.bgra[:, 200:][mask], image[mask])
    assert not np.array_equal(result.bgra[:, :200][~mask], result.bgra[:, 200:][~mask])


def test_flat_ui_is_not_duplicated_by_warp_into_surrounding_media():
    # A thin flat white UI stroke against black media must stay one stroke.
    image = np.zeros((100, 200, 4), dtype=np.uint8)
    image[:, :, 3] = 255
    image[20:80, 100:103, :3] = 255
    mask = np.zeros((100, 200), dtype=bool)
    mask[20:80, 100:103] = True
    depth = np.ones((100, 200), dtype=np.float32)
    depth[0, 0] = 0  # establishes a synthetic far reference outside the UI
    result = StereoSynthesizer(200, 100, disparity_px=8).synthesize(
        image, _synthetic_depth(depth), frame_id=1, generation=0, flat_mask=mask)
    for eye in (result.bgra[:, :200], result.bgra[:, 200:]):
        assert np.array_equal(eye[mask], image[mask])
        assert np.count_nonzero(eye[~mask, :3]) == 0, "flat UI must not leak through the media warp"


def test_flat_mask_shape_error_is_not_silently_resized_to_other_content():
    with pytest.raises(ValueError, match="Flat mask must match"):
        StereoSynthesizer(200, 100, disparity_px=8).synthesize(
            _image(), _synthetic_depth(np.ones((100, 200))), frame_id=1, generation=0,
            flat_mask=np.zeros((99, 200), dtype=bool))


def test_inverse_depth_near_feature_has_crossed_disparity_with_left_image_shifted_right():
    image = np.zeros((100, 200, 4), dtype=np.uint8)
    image[:, :, 3] = 255
    image[20:80, 98:103, :3] = 255
    inverse_depth = np.zeros((100, 200), dtype=np.float32)
    inverse_depth[10:90, 50:150] = 1.0
    result = StereoSynthesizer(200, 100, disparity_px=8, convergence=0.5).synthesize(
        image, _synthetic_depth(inverse_depth), frame_id=1, generation=0)
    left_x = np.flatnonzero(result.bgra[50, :200, 0] > 128).mean()
    right_x = np.flatnonzero(result.bgra[50, 200:, 0] > 128).mean()
    assert left_x > right_x
    assert left_x == pytest.approx(102.0, abs=0.1)
    assert right_x == pytest.approx(98.0, abs=0.1)
    assert left_x - right_x == pytest.approx(4.0, abs=0.1)


def test_full_sbs_keeps_eye_aspect_and_letterbox_outside_media_flat():
    image = _image(100, 100)
    depth = _synthetic_depth(np.linspace(0, 1, 100)[None, :].repeat(100, axis=0))
    result = StereoSynthesizer(200, 100, disparity_px=8).synthesize(image, depth, frame_id=1, generation=0)
    assert result.bgra.shape == (100, 400, 4)
    assert (result.bgra.shape[1] / 2) / result.bgra.shape[0] == 2
    for eye in (result.bgra[:, :200], result.bgra[:, 200:]):
        assert np.count_nonzero(eye[:, :50, :3]) == 0
        assert np.count_nonzero(eye[:, 150:, :3]) == 0
        assert np.all(eye[:, :, 3] == 255)


@pytest.mark.parametrize("bad_value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_depth_is_refused(bad_value):
    depth = np.zeros((100, 200), dtype=np.float32)
    depth[50, 100] = bad_value
    with pytest.raises(ValueError, match="non-finite"):
        StereoSynthesizer(200, 100, disparity_px=8).synthesize(
            _image(), _synthetic_depth(depth), frame_id=1, generation=0)


def test_scene_cut_and_generation_change_reset_depth_range():
    synth = StereoSynthesizer(200, 100, disparity_px=8)
    black = np.zeros((100, 200, 4), dtype=np.uint8)
    black[:, :, 3] = 255
    first = synth.synthesize(black, _synthetic_depth(np.tile(np.linspace(0, 1, 200), (100, 1))),
                             frame_id=1, generation=0)
    assert first.scene_reset
    same = synth.synthesize(black, _synthetic_depth(np.tile(np.linspace(2, 3, 200), (100, 1)), frame_id=2),
                            frame_id=2, generation=0)
    assert not same.scene_reset
    assert same.depth_range == pytest.approx((0.2, 1.2))
    white = np.full_like(black, 255)
    cut = synth.synthesize(white, _synthetic_depth(np.tile(np.linspace(10, 20, 200), (100, 1)), frame_id=3),
                           frame_id=3, generation=0)
    assert cut.scene_reset and cut.depth_range == (10, 20)
    changed = synth.synthesize(white, _synthetic_depth(np.zeros((100, 200)), frame_id=4, generation=1),
                               frame_id=4, generation=1)
    assert changed.scene_reset and changed.depth_range == (0, 0)


def _pillow_bicubic_float(source, size):
    # Independent Pillow resampling, channel by channel: mode F avoids an
    # intermediate uint8 quantization between horizontal and vertical passes.
    # Production also interpolates float BGRA before one final round/clamp.
    return np.stack([np.asarray(Image.fromarray(source[:, :, channel].astype(np.float32)).resize(
        size, resample=Image.Resampling.BICUBIC)) for channel in range(4)], axis=-1)


@pytest.mark.parametrize("source_size,eye_size", [((128, 96), (96, 72)),
                                                   ((119, 73), (72, 44)),
                                                   ((32, 24), (96, 72))])
def test_bicubic_aa_cpu_numpy_and_tensor_match_independent_pillow_oracle(source_size, eye_size):
    source = _image(*source_size)
    before = source.copy()
    tensor = torch.from_numpy(source.copy())
    synth = StereoSynthesizer(*eye_size, resize_filter="bicubic-aa", disparity_px=0)
    numpy_fit, numpy_rect = synth._fit(source)
    tensor_fit, tensor_rect = synth._fit(tensor)
    expected = np.rint(_pillow_bicubic_float(source, eye_size)).clip(0, 255).astype(np.uint8)
    assert isinstance(numpy_fit, np.ndarray) and numpy_fit.dtype == np.uint8
    assert tensor_fit.device == tensor.device and tensor_fit.dtype == torch.uint8
    assert numpy_rect == tensor_rect == (0, 0, *eye_size)
    assert np.max(np.abs(numpy_fit.astype(np.int16) - expected.astype(np.int16))) <= 1
    assert np.array_equal(numpy_fit, tensor_fit.numpy())
    assert np.array_equal(source, before) and np.array_equal(tensor.numpy(), before)
    assert np.all(numpy_fit[:, :, 3] == 255)


@pytest.mark.parametrize("tensor_input", [False, True])
def test_bicubic_aa_same_size_preserves_every_bgra_byte(tensor_input):
    source = _image(64, 32)
    # Nonconstant alpha exposes channel conversion or unnecessary resampling.
    source[:, :, 3] = np.arange(64, dtype=np.uint8)[None, :]
    value = torch.from_numpy(source.copy()) if tensor_input else source.copy()
    synth = StereoSynthesizer(64, 32, disparity_px=0, resize_filter="bicubic-aa")
    fitted, rect = synth._fit(value)
    actual = fitted.numpy() if tensor_input else fitted
    assert rect == (0, 0, 64, 32) and np.array_equal(actual, source)
    result = synth.original_2d(value, frame_id=17, generation=3)
    assert result.frame_id == 17 and result.generation == 3
    assert np.array_equal(result.bgra[:, :64], source)
    assert np.array_equal(result.bgra[:, 64:], source)


def test_bicubic_aa_clamps_real_high_contrast_overshoot_before_uint8():
    source = np.zeros((32, 64, 4), dtype=np.uint8)
    source[:, 32:, 0] = 255
    source[:, :32, 1] = 255
    source[16:, :, 2] = 255
    source[:, :, 3] = 255
    before = source.copy()
    oracle = _pillow_bicubic_float(source, (96, 48))
    below, above = oracle < -0.1, oracle > 255.1
    assert np.any(below) and np.any(above), "fixture must exercise genuine cubic overshoot"
    fitted, _ = StereoSynthesizer(96, 48, disparity_px=0, resize_filter="bicubic-aa")._fit(source)
    assert np.all(fitted[below] == 0) and np.all(fitted[above] == 255)
    expected = np.rint(oracle).clip(0, 255).astype(np.uint8)
    assert np.max(np.abs(fitted.astype(np.int16) - expected.astype(np.int16))) <= 1
    assert np.array_equal(source, before) and np.all(fitted[:, :, 3] == 255)


@pytest.mark.parametrize("tensor_input", [False, True])
@pytest.mark.parametrize("disparity", [0, 4])
def test_bicubic_aa_keeps_letterbox_opaque_eye_layout_and_frame_identity(tensor_input, disparity):
    source = _image(80, 80)
    before = source.copy()
    value = torch.from_numpy(source.copy()) if tensor_input else source.copy()
    depth = _synthetic_depth(np.tile(np.linspace(0, 1, 80), (80, 1)), frame_id=57, generation=9)
    depth_before = depth.tensor.clone()
    result = StereoSynthesizer(128, 72, disparity_px=disparity, resize_filter="bicubic-aa").synthesize(
        value, depth, frame_id=57, generation=9)
    assert result.bgra.shape == (72, 256, 4) and result.content_rect == (28, 0, 72, 72)
    assert result.frame_id == 57 and result.generation == 9
    for eye in (result.bgra[:, :128], result.bgra[:, 128:]):
        assert np.count_nonzero(eye[:, :28, :3]) == 0
        assert np.count_nonzero(eye[:, 100:, :3]) == 0
        assert np.all(eye[:, :, 3] == 255)
    if disparity == 0:
        expected = np.rint(_pillow_bicubic_float(source, (72, 72))).clip(0, 255).astype(np.uint8)
        assert result.mode == "2d" and np.array_equal(result.bgra[:, :128], result.bgra[:, 128:])
        assert np.max(np.abs(result.bgra[:, 28:100].astype(np.int16) - expected.astype(np.int16))) <= 1
    else:
        assert result.mode == "3d" and not np.array_equal(result.bgra[:, :128], result.bgra[:, 128:])
    assert np.array_equal(value.numpy() if tensor_input else value, before)
    assert torch.equal(depth.tensor, depth_before)


@pytest.mark.parametrize("bad_filter", [None, "", "bicubic", "BICUBIC-AA", 1])
def test_resize_filter_rejects_unknown_choices(bad_filter):
    with pytest.raises(ValueError, match="Resize filter"):
        StereoSynthesizer(resize_filter=bad_filter)


def test_resize_filter_is_keyword_only_and_bicubic_cannot_bypass_depth_identity():
    with pytest.raises(TypeError):
        StereoSynthesizer(128, 72, 0, 0.5, "bicubic-aa")
    source = _image(80, 80)
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        StereoSynthesizer(128, 72, disparity_px=0, resize_filter="bicubic-aa").synthesize(
            source, _synthetic_depth(np.ones((80, 80))), frame_id=2, generation=0)

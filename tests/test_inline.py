"""Inline compositor invariants, using synthetic depth rather than an AI claim."""

from dataclasses import replace

import numpy as np
import pytest
import torch

from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.inline import InlineGeometry, InlineSynthesizer
from quest3d.stereo import StereoSynthesizer


def image(width=200, height=100):
    y, x = np.indices((height, width))
    return np.stack(((3 * x + y) % 256, (x + 2 * y) % 256,
                     (7 * x + 5 * y) % 256, (x + 3 * y) % 256), -1).astype(np.uint8)


def geometry(width=200, height=100, *, origin=(-1920, -200), generation=4,
             roi=(35, 19, 131, 63), content=None):
    source = SourceGeometry(ScreenRect(*origin, width, height), generation)
    def absolute(rect):
        return ScreenRect(origin[0] + rect[0], origin[1] + rect[1], *rect[2:])
    return InlineGeometry(source, absolute(roi), absolute(content) if content else None)


def depth(region, *, frame_id=7, values=None, device="cpu"):
    if values is None:
        values = np.tile(np.linspace(0, 1, 21, dtype=np.float32), (13, 1))
    raw = torch.as_tensor(values, dtype=torch.float32, device=device)
    if raw.ndim == 2:
        raw = raw.unsqueeze(0)
    result = DepthResult(frame_id, region.source.generation, raw, (140, 280), 0, 0)
    return region.bind_depth(result)


def baseline(source, region, eye_size, frame_id=7):
    return StereoSynthesizer(*eye_size, disparity_px=0).original_2d(
        source, frame_id=frame_id, generation=region.source.generation)


def outside_mask(synth, region):
    active = synth.eye_active_rect(region)
    mask = np.ones((synth.height, synth.width), dtype=bool)
    if active:
        mask[active.top:active.bottom, active.left:active.right] = False
    return mask


@pytest.mark.parametrize("eye_size", [(200, 100), (128, 72), (300, 180)])
@pytest.mark.parametrize("as_tensor", [False, True])
def test_same_desktop_layout_and_every_pixel_outside_odd_roi_are_exact(eye_size, as_tensor):
    source = image()
    if as_tensor:
        source = torch.from_numpy(source)
    region = geometry()
    synth = InlineSynthesizer(*eye_size, disparity_px=eye_size[0] * .04)
    expected = baseline(source, region, eye_size)
    before = source.clone() if as_tensor else source.copy()
    result = synth.synthesize(source, depth(region), frame_id=7, geometry=region)
    assert result.frame_id == 7 and result.generation == 4 and result.mode == "3d"
    assert result.bgra.shape == expected.bgra.shape
    assert result.content_rect == expected.content_rect
    mask = outside_mask(synth, region)
    left, right = np.split(result.bgra, 2, axis=1)
    expected_eye = expected.bgra[:, :eye_size[0]]
    for eye in (left, right):
        assert np.array_equal(eye[mask], expected_eye[mask])
        assert np.array_equal(eye[:, :, 3], expected_eye[:, :, 3])
    assert not np.array_equal(left[~mask, :3], right[~mask, :3])
    assert torch.equal(source, before) if as_tensor else np.array_equal(source, before)


@pytest.mark.parametrize("eye_size", [(200, 100), (128, 72), (300, 180)])
@pytest.mark.parametrize("as_tensor", [False, True])
def test_zero_strength_and_explicit_2d_equal_the_entire_existing_2d_output(eye_size, as_tensor):
    source = torch.from_numpy(image()) if as_tensor else image()
    region = geometry(content=(41, 27, 119, 47))
    synth = InlineSynthesizer(*eye_size, disparity_px=0)
    expected = baseline(source, region, eye_size)
    supplied = depth(region)
    # Zero strength may bypass broken AI values, but never mismatched identity.
    supplied.result.tensor[:] = float("nan")
    result = synth.synthesize(source, supplied, frame_id=7, geometry=region)
    restored = synth.original_2d(source, frame_id=7, geometry=region)
    assert result.mode == restored.mode == "2d"
    assert result.content_rect == restored.content_rect == expected.content_rect
    assert np.array_equal(result.bgra, expected.bgra)
    assert np.array_equal(restored.bgra, expected.bgra)
    assert np.array_equal(*np.split(result.bgra, 2, axis=1))


def test_explicit_2d_return_does_not_require_or_reuse_depth_and_resets_temporal_state():
    source, region = image(), geometry()
    synth = InlineSynthesizer(200, 100, 8)
    first = synth.synthesize(source, depth(region), frame_id=7, geometry=region)
    assert first.scene_reset
    second = synth.synthesize(source, depth(region, frame_id=8), frame_id=8, geometry=region)
    assert not second.scene_reset
    returned = synth.original_2d(source, frame_id=9, geometry=region)
    assert np.array_equal(returned.bgra, baseline(source, region, (200, 100), 9).bgra)
    resumed = synth.synthesize(source, depth(region, frame_id=10), frame_id=10, geometry=region)
    assert resumed.scene_reset


@pytest.mark.parametrize("strength", [0, 8])
def test_depth_from_other_frame_is_rejected_even_when_flat(strength):
    region = geometry()
    with pytest.raises(ValueError, match="different RGB frame"):
        InlineSynthesizer(200, 100, strength).synthesize(
            image(), depth(region, frame_id=6), frame_id=7, geometry=region)


@pytest.mark.parametrize("change", ["generation", "source", "roi", "content"])
@pytest.mark.parametrize("strength", [0, 8])
def test_depth_crop_descriptor_mismatch_is_rejected_without_clamping(change, strength):
    region = geometry()
    changed = {
        "generation": replace(region, source=replace(region.source, generation=5)),
        "source": geometry(origin=(-1800, -200)),
        "roi": geometry(roi=(36, 19, 130, 63)),
        "content": geometry(content=(40, 20, 120, 50)),
    }[change]
    with pytest.raises(ValueError, match="different physical source, ROI or content"):
        InlineSynthesizer(200, 100, strength).synthesize(
            image(), depth(region), frame_id=7, geometry=changed)


def test_binding_catches_generation_mismatch_and_synthesis_catches_later_mutation():
    region = geometry()
    supplied = depth(region)
    with pytest.raises(ValueError, match="different geometry generation"):
        region.bind_depth(replace(supplied.result, generation=5))
    supplied.result.generation = 5
    with pytest.raises(ValueError, match="different RGB frame or geometry"):
        InlineSynthesizer(200, 100, 8).synthesize(image(), supplied, frame_id=7, geometry=region)


def test_depth_crop_is_exact_physical_pixels_with_negative_desktop_origin():
    source = image()
    region = geometry()
    selected = region.crop_for_depth(source)
    assert selected.shape == (63, 131, 4)
    assert np.array_equal(selected, source[19:82, 35:166])
    assert np.shares_memory(selected, source)
    assert np.array_equal(region.crop_for_depth(torch.from_numpy(source)).numpy(), selected)


@pytest.mark.parametrize("bad_roi", [(-1, 0, 60, 40), (0, -1, 60, 40),
                                    (180, 20, 21, 40), (20, 80, 60, 21)])
def test_out_of_bounds_physical_roi_is_refused_not_partially_clipped(bad_roi):
    with pytest.raises(ValueError, match="outside desktop bounds"):
        geometry(roi=bad_roi)


def test_content_outside_selected_roi_is_refused():
    with pytest.raises(ValueError, match="inside the inference ROI"):
        geometry(content=(34, 19, 100, 60))


@pytest.mark.parametrize("bad", [12.5, True, "12"])
def test_noninteger_physical_coordinates_are_refused(bad):
    with pytest.raises(TypeError, match="physical-pixel"):
        ScreenRect(bad, 0, 50, 50)


def test_dpi_scaled_bounds_do_not_silently_resize_the_desktop():
    logical = geometry(300, 150)
    with pytest.raises(ValueError, match="physical source bounds"):
        logical.crop_for_depth(image())
    with pytest.raises(ValueError, match="physical source bounds"):
        InlineSynthesizer(200, 100, 8).original_2d(image(), frame_id=7, geometry=logical)


def test_translating_the_whole_physical_desktop_does_not_change_its_pixels():
    source = image()
    original, translated = geometry(), geometry(origin=(500, 750))
    a = InlineSynthesizer(128, 72, 5).synthesize(source, depth(original), frame_id=7, geometry=original)
    b = InlineSynthesizer(128, 72, 5).synthesize(source, depth(translated), frame_id=7, geometry=translated)
    assert np.array_equal(a.bgra, b.bgra)
    assert a.content_rect == b.content_rect


def test_new_roi_resets_depth_limits_even_if_caller_forgot_to_bump_generation():
    source, original = image(), geometry()
    changed = geometry(roi=(40, 20, 120, 60))
    synth = InlineSynthesizer(200, 100, 8)
    synth.synthesize(source, depth(original), frame_id=7, geometry=original)
    values = np.ones((13, 21), np.float32) * 100
    result = synth.synthesize(source, depth(changed, frame_id=8, values=values), frame_id=8, geometry=changed)
    assert result.scene_reset and result.depth_range == (100.0, 100.0)


@pytest.mark.parametrize("bad", [True, -1, 1.5])
def test_invalid_depth_generation_is_rejected_when_binding(bad):
    region = geometry()
    with pytest.raises(ValueError, match="generation"):
        region.bind_depth(replace(depth(region).result, generation=bad))


@pytest.mark.parametrize("eye_size", [(200, 100), (128, 72), (300, 180)])
def test_player_letterbox_and_desktop_letterbox_remain_flat(eye_size):
    source = image()
    region = geometry(roi=(25, 9, 151, 83), content=(31, 25, 137, 49))
    synth = InlineSynthesizer(*eye_size, disparity_px=eye_size[0] * .04)
    expected = baseline(source, region, eye_size).bgra[:, :eye_size[0]]
    output = synth.synthesize(source, depth(region), frame_id=7, geometry=region)
    mask = outside_mask(synth, region)
    for eye in np.split(output.bgra, 2, axis=1):
        assert np.array_equal(eye[mask], expected[mask])


@pytest.mark.parametrize("eye_size", [(200, 100), (128, 72), (300, 180)])
@pytest.mark.parametrize("as_tensor", [False, True])
def test_roi_boundary_never_samples_white_neighboring_ui(eye_size, as_tensor):
    source = np.full((100, 200, 4), 255, np.uint8)
    source[19:82, 35:166, :3] = 0
    if as_tensor:
        source = torch.from_numpy(source)
    region = geometry()
    synth = InlineSynthesizer(*eye_size, disparity_px=eye_size[0] * .04)
    output = synth.synthesize(source, depth(region), frame_id=7, geometry=region)
    active = synth.eye_active_rect(region)
    for eye in np.split(output.bgra, 2, axis=1):
        assert not np.any(eye[active.top:active.bottom, active.left:active.right, :3])


@pytest.mark.parametrize("as_tensor", [False, True])
@pytest.mark.parametrize("eye_size", [(200, 100), (100, 50)])
def test_thin_flat_ui_stays_exact_and_cannot_be_warped_into_neighboring_media(as_tensor, eye_size):
    source = np.zeros((100, 200, 4), np.uint8)
    source[:, :, 3] = 255
    source[25:75, 100:101, :3] = 255
    mask = np.zeros((100, 200), bool)
    mask[25:75, 100:101] = True
    if as_tensor:
        source = torch.from_numpy(source)
        mask = torch.from_numpy(mask)
    region = geometry()
    expected = baseline(source, region, eye_size).bgra
    values = np.ones((30, 50), np.float32)
    values[0, 0] = 0
    output = InlineSynthesizer(*eye_size, disparity_px=eye_size[0] * .04).synthesize(
        source, depth(region, values=values), frame_id=7, geometry=region, flat_mask=mask)
    assert np.array_equal(output.bgra, expected), "a protected UI stroke must not clone into black video"


def test_actual_left_right_displacement_is_measured_in_full_eye_pixels():
    source = np.zeros((100, 200, 4), np.uint8)
    source[:, :, 3] = 255
    source[30:70, 98:103, :3] = 255
    region = geometry(roi=(40, 20, 120, 60))
    values = np.ones((60, 120), np.float32)
    values[:5, :] = 0
    output = InlineSynthesizer(200, 100, 8).synthesize(
        source, depth(region, values=values), frame_id=7, geometry=region)
    left_x = np.flatnonzero(output.bgra[50, :200, 0] > 128).mean()
    right_x = np.flatnonzero(output.bgra[50, 200:, 0] > 128).mean()
    assert left_x == pytest.approx(102.0, abs=.1)
    assert right_x == pytest.approx(98.0, abs=.1)
    assert left_x - right_x == pytest.approx(4.0, abs=.1)


def test_tiny_odd_roi_with_no_complete_output_pixel_has_explicit_error_but_2d_works():
    source = image()
    region = geometry(roi=(39, 19, 1, 1))
    synth = InlineSynthesizer(100, 50, 4)
    assert synth.eye_active_rect(region) is None
    with pytest.raises(ValueError, match="no complete output pixel"):
        synth.synthesize(source, depth(region), frame_id=7, geometry=region)
    assert np.array_equal(synth.original_2d(source, frame_id=7, geometry=region).bgra,
                          baseline(source, region, (100, 50)).bgra)


@pytest.mark.parametrize("size", [(1, 1), (1, 7), (9, 1)])
def test_single_pixel_wide_or_high_roi_at_native_size_is_defined(size):
    source = image()
    region = geometry(roi=(39, 19, *size))
    output = InlineSynthesizer(200, 100, 8).synthesize(
        source, depth(region), frame_id=7, geometry=region)
    assert np.isfinite(output.bgra).all()
    assert output.bgra.shape == (100, 400, 4)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_depth_is_refused_before_temporal_state_changes(bad):
    region = geometry()
    synth = InlineSynthesizer(200, 100, 8)
    supplied = depth(region)
    supplied.result.tensor[0, 0, 0] = bad
    with pytest.raises(ValueError, match="non-finite"):
        synth.synthesize(image(), supplied, frame_id=7, geometry=region)
    assert synth.synthesize(image(), depth(region), frame_id=7, geometry=region).scene_reset


@pytest.mark.parametrize("shape", [(10, 20), (2, 10, 20), (1, 0, 20), (1, 1, 10, 20)])
def test_invalid_depth_shape_is_rejected(shape):
    region = geometry()
    supplied = depth(region)
    supplied.result.tensor = torch.zeros(shape)
    with pytest.raises(ValueError, match="shape"):
        InlineSynthesizer(200, 100, 8).synthesize(image(), supplied, frame_id=7, geometry=region)


@pytest.mark.parametrize("mask", [np.zeros((99, 200), bool), np.zeros((100, 200, 1), bool),
                                 np.zeros((100, 200), np.float32)])
def test_invalid_flat_masks_are_refused(mask):
    region = geometry()
    with pytest.raises(ValueError, match="Flat mask"):
        InlineSynthesizer(200, 100, 8).synthesize(
            image(), depth(region), frame_id=7, geometry=region, flat_mask=mask)


@pytest.mark.parametrize("field,bad", [("disparity_px", float("nan")), ("disparity_px", 8.1),
                                       ("convergence", float("inf")), ("convergence", -.1)])
def test_changed_invalid_settings_are_revalidated(field, bad):
    region = geometry()
    synth = InlineSynthesizer(200, 100, 8)
    setattr(synth, field, bad)
    with pytest.raises(ValueError, match="finite and between"):
        synth.synthesize(image(), depth(region), frame_id=7, geometry=region)


@pytest.mark.gpu
@pytest.mark.skipif(not torch.cuda.is_available(), reason="Local CUDA GPU is required")
def test_cuda_depth_and_desktop_preserve_baseline_and_real_stereo_difference():
    source = torch.from_numpy(image()).cuda()
    region = geometry()
    synth = InlineSynthesizer(128, 72, 5)
    expected = baseline(source, region, (128, 72)).bgra[:, :128]
    output = synth.synthesize(source, depth(region, device="cuda"), frame_id=7, geometry=region)
    mask = outside_mask(synth, region)
    left, right = np.split(output.bgra, 2, axis=1)
    assert np.array_equal(left[mask], expected[mask])
    assert np.array_equal(right[mask], expected[mask])
    assert not np.array_equal(left[~mask], right[~mask])

from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d.cursor import CursorSample, DesktopCursorOverlay
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.stereo import StereoFrame


class Reader:
    def __init__(self, sample):
        self.current = sample
        self.calls = 0
        self.closed = False

    def sample(self):
        self.calls += 1
        if isinstance(self.current, Exception):
            raise self.current
        return self.current

    def close(self):
        self.closed = True


def context(bounds=ScreenRect(-100, 50, 8, 4), eye=(8, 4), rect=None):
    image = np.full((eye[1], eye[0] * 2, 4), [40, 80, 120, 255], dtype=np.uint8)
    output = StereoFrame(image, 5, 7, "3d", True, (0.1, 0.9), rect or (0, 0, *eye))
    source = SimpleNamespace(frame_id=5, geometry_generation=7,
        geometry=SourceGeometry(bounds, 7), captured_ns=123456,
        source_identity=SourceIdentity(SourceKind.MONITOR, 0, 91, 99, 0, bounds))
    return output, source


def opaque(position=(-98, 51), hotspot=(0, 0), color=(0, 0, 255, 255), size=(1, 1)):
    return CursorSample(True, position, hotspot,
        np.full((size[1], size[0], 4), color, dtype=np.uint8))


def test_moving_cursor_on_static_stereo_preserves_metadata_and_has_no_trails():
    output, source = context()
    original = output.bgra.copy()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader)
    first = overlay.sample_and_composite(output, source)
    state1 = overlay.snapshot()
    np.testing.assert_array_equal(first.bgra[1, 2], [0, 0, 255, 255])
    np.testing.assert_array_equal(first.bgra[:, :8], first.bgra[:, 8:])
    assert first.frame_id == 5 and first.generation == 7 and first.mode == "3d"
    assert first.scene_reset and first.depth_range == (0.1, 0.9)
    assert source.captured_ns == 123456
    reader.current = opaque((-96, 52))
    second = overlay.sample_and_composite(output, source)
    assert overlay.snapshot()["visual_signature"] != state1["visual_signature"]
    np.testing.assert_array_equal(second.bgra[1, 2], original[1, 2])
    np.testing.assert_array_equal(second.bgra[2, 4], [0, 0, 255, 255])
    np.testing.assert_array_equal(output.bgra, original)
    overlay.sample_and_composite(output, source)
    assert not overlay.snapshot()["changed"]
    reader.current = CursorSample(False)
    assert overlay.sample_and_composite(output, source) is output
    assert overlay.snapshot()["changed"] and not overlay.snapshot()["visible"]
    assert reader.calls == 4


def test_premultiplied_alpha_not_multiplied_twice_and_keeps_destination_alpha():
    output, source = context()
    cursor = opaque(color=(20, 40, 60, 128))
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    np.testing.assert_array_equal(result.bgra[1, 2], [40, 80, 120, 255])
    # Distinct premultiplied blue checks direction as well as neutral identity.
    cursor = opaque(color=(128, 0, 0, 128))
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    np.testing.assert_array_equal(result.bgra[1, 2], [148, 40, 60, 255])


def test_monochrome_and_xor_black_white_transparent_inverted():
    output, source = context()
    pixels = np.zeros((1, 4, 4), dtype=np.uint8)
    pixels[0, [1, 3], :3] = 255
    masks = np.zeros((1, 4, 3), dtype=np.uint8)
    masks[0, 2:] = 255
    cursor = CursorSample(True, (-100, 50), (0, 0), pixels, masks)
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    np.testing.assert_array_equal(result.bgra[0, :4, :3],
        [[0, 0, 0], [255, 255, 255], [40, 80, 120], [215, 175, 135]])
    np.testing.assert_array_equal(result.bgra[:, :8], result.bgra[:, 8:])


def test_colored_xor_keeps_bitwise_semantics():
    output, source = context()
    cursor = CursorSample(True, (-100, 50), (0, 0),
        np.array([[[7, 31, 127, 0]]], np.uint8), np.full((1, 1, 3), 255, np.uint8))
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    np.testing.assert_array_equal(result.bgra[0, 0, :3], [40 ^ 7, 80 ^ 31, 120 ^ 127])


def test_hotspot_outside_crop_still_draws_intersecting_shape_and_no_black_bar_bleed():
    bounds = ScreenRect(-100, 50, 8, 4)
    output, source = context(bounds, (8, 8), (0, 2, 8, 4))
    cursor = opaque((-101, 51), (0, 1), size=(3, 3))
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    np.testing.assert_array_equal(result.bgra[2:5, :2, :3], np.full((3, 2, 3), [0, 0, 255]))
    np.testing.assert_array_equal(result.bgra[:2], output.bgra[:2])
    np.testing.assert_array_equal(result.bgra[6:], output.bgra[6:])
    np.testing.assert_array_equal(result.bgra[:, 2:8], output.bgra[:, 2:8])


@pytest.mark.parametrize("position,hotspot", [((-110, 40), (0, 0)), ((-92, 54), (0, 0)),
                                                  ((0, 0), (0, 0))])
def test_cursor_outside_source_is_not_clamped_onto_edge(position, hotspot):
    output, source = context()
    overlay = DesktopCursorOverlay(reader=Reader(opaque(position, hotspot)))
    assert overlay.sample_and_composite(output, source) is output
    assert overlay.snapshot()["status"] == "outside_source"


def test_fractional_scaling_uses_source_and_actual_letterbox_rectangle():
    output, source = context(ScreenRect(200, -80, 12, 8), (8, 8), (0, 1, 8, 5))
    cursor = opaque((206, -76), (2, 2), size=(4, 4))
    result = DesktopCursorOverlay(reader=Reader(cursor)).sample_and_composite(output, source)
    # Inverse pixel-center mapping produces x 3..4, y 2..4.
    expected = output.bgra.copy()
    expected[2:5, 3:5] = [0, 0, 255, 255]
    expected[2:5, 11:13] = [0, 0, 255, 255]
    np.testing.assert_array_equal(result.bgra, expected)


@pytest.mark.parametrize("support", [False, None, 1, "yes"])
def test_unconfirmed_capture_does_not_sample_or_double_cursor(support):
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay.from_capture(SimpleNamespace(cursor_exclusion_supported=support))
    overlay.reader = reader
    assert overlay.sample_and_composite(output, source) is output
    assert reader.calls == 0
    assert overlay.snapshot()["status"] == "capture_cursor_exclusion_unconfirmed"


@pytest.mark.parametrize("kind", [SourceKind.PHOTO, SourceKind.VIDEO, SourceKind.WINDOW, None])
def test_file_or_window_never_uses_desktop_cursor(kind):
    output, source = context()
    if kind in (SourceKind.PHOTO, SourceKind.VIDEO):
        source.source_identity = SourceIdentity(kind, 0, 1, 0, 0, ScreenRect(0, 0, 8, 4))
    elif kind == SourceKind.WINDOW:
        source.source_identity = SourceIdentity(kind, 1, 1, 2, 3, source.geometry.bounds)
    else:
        source.source_identity = None
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader)
    assert overlay.sample_and_composite(output, source) is output
    assert reader.calls == 0 and overlay.snapshot()["status"] == "source_not_monitor"


def test_native_failure_removes_old_cursor_and_reports_error_without_stopping_video():
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader)
    overlay.sample_and_composite(output, source)
    reader.current = OSError("GetCursorInfo access failure")
    assert overlay.sample_and_composite(output, source) is output
    state = overlay.snapshot()
    assert state["status"] == "error" and state["changed"] and not state["visible"]
    assert state["errors"] == 1 and "GetCursorInfo" in state["last_error"]
    reader.current = opaque()
    overlay.sample_and_composite(output, source)
    assert overlay.snapshot()["last_error"] is None


def test_stale_geometry_rejected_without_sampling_and_timestamps_unchanged():
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader)
    stale = replace(output, generation=6)
    assert overlay.sample_and_composite(stale, source) is stale
    assert reader.calls == 0 and overlay.snapshot()["status"] == "error"
    assert source.captured_ns == 123456


def test_hide_option_and_close_do_not_sample():
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(enabled=False, reader=reader)
    assert overlay.sample_and_composite(output, source) is output
    assert reader.calls == 0 and overlay.snapshot()["status"] == "disabled"
    overlay.close()
    assert reader.closed and overlay.sample_and_composite(output, source) is output


def test_snapshot_is_detached_from_internal_state():
    overlay = DesktopCursorOverlay()
    state = overlay.snapshot()
    state["errors"] = 100
    assert overlay.snapshot()["errors"] == 0


def test_default_mutable_base_never_returns_stale_cached_pixels():
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader)
    first = overlay.sample_and_composite(output, source)
    output.bgra[0, 0] = [11, 22, 33, 255]
    second = overlay.sample_and_composite(output, source)
    assert first is not second
    np.testing.assert_array_equal(second.bgra[0, 0], output.bgra[0, 0])
    assert not overlay.snapshot()["cursor_cache_hit"]
    assert overlay.snapshot()["composites"] == 2


def test_explicit_immutable_cache_skips_full_copy_and_returns_owned_read_only_result():
    output, source = context()
    before = output.bgra.copy()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    first = overlay.sample_and_composite(output, source)
    assert not first.bgra.flags.writeable
    assert first.bgra.flags.owndata
    assert not np.shares_memory(first.bgra, output.bgra)
    with pytest.raises(ValueError, match="read-only"):
        first.bgra[0, 0] = 0
    # The final result remains an ordinary contiguous publication buffer.
    assert memoryview(np.ascontiguousarray(first.bgra)).cast("B").readonly
    assert overlay.snapshot()["cursor_copied_bytes"] == output.bgra.nbytes
    second = overlay.sample_and_composite(output, source)
    assert second is first and reader.calls == 2
    state = overlay.snapshot()
    assert state["cursor_cache_hit"] and not state["changed"]
    assert state["composites"] == 1 and state["cache_hits"] == 1
    assert state["cursor_copied_bytes"] == 0 and state["cursor_copy_ms"] == 0
    assert state["cursor_blend_ms"] == 0
    assert state["copied_bytes_total"] == output.bgra.nbytes
    np.testing.assert_array_equal(output.bgra, before)
    assert source.captured_ns == 123456


@pytest.mark.parametrize("change", ["movement", "hotspot", "raster", "dpi_shape", "mask"])
def test_cursor_changes_invalidate_cache_and_preserve_uncached_pixel_parity(change):
    output, source = context()
    reader = Reader(opaque(size=(2, 2)))
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    first = overlay.sample_and_composite(output, source)
    if change == "movement":
        reader.current = replace(reader.current, position=(-97, 52))
    elif change == "hotspot":
        reader.current = replace(reader.current, hotspot=(1, 1))
    elif change == "raster":
        # Reusing a native cursor handle cannot conceal changed resource pixels.
        reader.current.bgra[0, 0] = [255, 0, 0, 255]
    elif change == "dpi_shape":
        reader.current = opaque(size=(3, 3))
    else:
        reader.current = replace(reader.current, and_mask=np.full((2, 2, 3), 255, np.uint8))
    result = overlay.sample_and_composite(output, source)
    reference = DesktopCursorOverlay(reader=reader).sample_and_composite(output, source)
    assert result is not first and not overlay.snapshot()["cursor_cache_hit"]
    np.testing.assert_array_equal(result.bgra, reference.bgra)
    np.testing.assert_array_equal(result.bgra[:, :8], result.bgra[:, 8:])


@pytest.mark.parametrize("change", ["new_frame_object", "new_pixels", "mode", "revision",
                                    "source_id", "selection", "capture_receipt", "generation",
                                    "clipping"])
def test_base_and_source_scope_changes_do_not_reuse_old_overlay(change):
    output, source = context()
    reader = Reader(opaque(size=(3, 2)))
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    first = overlay.sample_and_composite(output, source)
    if change == "new_frame_object":
        output = replace(output)
    elif change == "new_pixels":
        output = replace(output, bgra=np.full_like(output.bgra, 50))
    elif change == "mode":
        output = replace(output, mode="2d")
    elif change == "revision":
        # Config revisions produce a new immutable StereoFrame, even when the
        # source frame ID/generation have not changed during static capture.
        output = replace(output, effective_convergence=.75, disparity_profile="comfort")
    elif change == "source_id":
        source.source_id = "new-source"
    elif change == "selection":
        source.source_identity = replace(source.source_identity, selection_id=92)
    elif change == "capture_receipt":
        source.captured_ns += 1
    elif change == "generation":
        source.geometry_generation += 1
        source.geometry = replace(source.geometry, generation=source.geometry_generation)
        output = replace(output, generation=source.geometry_generation)
    else:
        # Valid physical crop is smaller but remains inside the parent monitor.
        source.geometry = SourceGeometry(ScreenRect(-99, 50, 7, 4), 7)
    result = overlay.sample_and_composite(output, source)
    reference = DesktopCursorOverlay(reader=reader).sample_and_composite(output, source)
    assert result is not first and not overlay.snapshot()["cursor_cache_hit"]
    np.testing.assert_array_equal(result.bgra, reference.bgra)
    assert result.mode == output.mode


@pytest.mark.parametrize("boundary", ["hidden", "outside", "error", "disabled", "unsupported",
                                      "non_monitor", "geometry_error", "close"])
def test_unavailable_boundaries_retire_cache_instead_of_reviving_old_cursor(boundary):
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    first = overlay.sample_and_composite(output, source)
    original_identity = source.source_identity
    if boundary == "hidden":
        reader.current = CursorSample(False)
    elif boundary == "outside":
        reader.current = opaque((200, 200))
    elif boundary == "error":
        reader.current = OSError("cursor query failed")
    elif boundary == "disabled":
        overlay.enabled = False
    elif boundary == "unsupported":
        overlay.supported = False
    elif boundary == "non_monitor":
        source.source_identity = None
    elif boundary == "geometry_error":
        source.geometry_generation += 1
    else:
        overlay.close()
    assert overlay.sample_and_composite(output, source) is output
    assert overlay._cache is None
    assert not overlay.snapshot()["cursor_cache_hit"]
    reader.current = opaque()
    overlay.enabled = overlay.supported = True
    source.source_identity = original_identity
    source.geometry_generation = 7
    if boundary == "close":
        assert overlay.sample_and_composite(output, source) is output
        # Reconnect has a fresh overlay lifetime, with no inherited result.
        overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    result = overlay.sample_and_composite(output, source)
    assert result is not first and not overlay.snapshot()["cursor_cache_hit"]
    np.testing.assert_array_equal(result.bgra, first.bgra)


def test_cache_keeps_only_one_result_and_old_returned_result_is_unchanged():
    output, source = context()
    reader = Reader(opaque())
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    first = overlay.sample_and_composite(output, source)
    first_bytes = first.bgra.tobytes()
    reader.current = opaque((-95, 52))
    second = overlay.sample_and_composite(output, source)
    assert second is not first
    assert first.bgra.tobytes() == first_bytes
    reader.current = opaque()
    third = overlay.sample_and_composite(output, source)
    assert third is not first  # Old signatures are not stored in an unbounded map.
    np.testing.assert_array_equal(third.bgra, first.bgra)
    assert overlay.snapshot()["composites"] == 3


def test_cache_opt_in_is_explicit_boolean_and_factory_preserves_default():
    capture = SimpleNamespace(cursor_exclusion_supported=True)
    assert not DesktopCursorOverlay.from_capture(capture).immutable_base
    assert DesktopCursorOverlay.from_capture(capture, immutable_base=True).immutable_base
    with pytest.raises(TypeError):
        DesktopCursorOverlay(immutable_base="yes")


def test_timing_and_byte_metrics_reset_when_disabled_and_snapshot_is_detached():
    output, source = context()
    overlay = DesktopCursorOverlay(reader=Reader(opaque()), immutable_base=True)
    overlay.sample_and_composite(output, source)
    assert overlay.last_metrics["cursor_copied_bytes"] == output.bgra.nbytes
    for key in ("cursor_read_ms", "cursor_plan_ms", "cursor_copy_ms", "cursor_blend_ms"):
        assert overlay.last_metrics[key] >= 0
    state = overlay.snapshot()
    state["cursor_copied_bytes"] = 0
    assert overlay.last_metrics["cursor_copied_bytes"] == output.bgra.nbytes
    overlay.enabled = False
    overlay.sample_and_composite(output, source)
    assert overlay.snapshot()["sample_ms"] == 0
    assert overlay.last_metrics["cursor_copied_bytes"] == 0

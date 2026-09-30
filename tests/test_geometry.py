"""Geometry boundary tests and capture ownership/state tests without taking screenshots."""

import math
import os
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d.geometry import (
    LetterboxTransform, ScreenRect, parse_rect, pixel_to_windows_absolute,
    require_generation, validate_roi,
)


@pytest.mark.parametrize("rect", [(-1920, -200, 1920, 1080), (0, 0, 1, 1), (10, 20, 960, 540)])
def test_rect_accepts_negative_origins_and_uses_exclusive_edges(rect):
    result = ScreenRect(*rect)
    assert result.right == rect[0] + rect[2]
    assert result.bottom == rect[1] + rect[3]
    assert result.contains(ScreenRect(result.right - 1, result.bottom - 1, 1, 1))
    assert not result.contains(ScreenRect(result.right, result.bottom - 1, 1, 1))


@pytest.mark.parametrize("args", [(0, 0, 0, 1), (0, 0, 1, -2), (True, 0, 1, 1), (0.5, 0, 1, 1)])
def test_invalid_rectangles_are_rejected(args):
    with pytest.raises((TypeError, ValueError)):
        ScreenRect(*args)


def test_roi_does_not_silently_clip_or_shift():
    desktop = ScreenRect(-1920, -100, 3840, 1180)
    assert validate_roi(ScreenRect(-1920, -100, 3840, 1180), desktop) == desktop
    for roi in (ScreenRect(-1921, 0, 2, 1), ScreenRect(1919, 0, 2, 1), ScreenRect(0, 1079, 1, 2)):
        with pytest.raises(ValueError, match="outside desktop"):
            validate_roi(roi, desktop)


def test_parse_rect_supports_negative_desktop_coordinates():
    assert parse_rect("-1920, -100, 1920,1080") == ScreenRect(-1920, -100, 1920, 1080)
    for text in ("0,0,1", "0,0,1,1,1", "0,0,0,1", "0,0,1.5,1", "a,0,1,1"):
        with pytest.raises(ValueError):
            parse_rect(text)


def test_letterbox_black_bars_reject_clicks_and_edges_are_exclusive():
    transform = LetterboxTransform(ScreenRect(-1920, 100, 1920, 1080), 1000, 1000)
    assert transform.offset == (0, 218.75)
    assert transform.display_to_pixel(500, 100) is None
    assert transform.display_to_pixel(500, 781.25) is None
    assert transform.display_to_pixel(1000, 500) is None
    assert transform.display_to_pixel(0, 218.75) == (-1920, 100)
    assert transform.uv_to_pixel(0.5, 0.5) == (-960, 640)
    assert transform.uv_to_pixel(1, 0.5) is None


@pytest.mark.parametrize("source, viewport", [
    (ScreenRect(-1920, -200, 1920, 1080), (1280, 720)),
    (ScreenRect(123, 44, 801, 601), (720, 1280)),
    (ScreenRect(-300, -200, 300, 1200), (1280, 720)),
    (ScreenRect(0, 0, 1, 1), (777, 513)),
])
def test_pixel_center_round_trip_has_no_off_by_one_at_any_dpi_scale(source, viewport):
    transform = LetterboxTransform(source, *viewport)
    for local_x in sorted({0, source.width // 2, source.width - 1}):
        for local_y in sorted({0, source.height // 2, source.height - 1}):
            x, y = source.left + local_x, source.top + local_y
            displayed = transform.source_to_display(x + 0.5, y + 0.5)
            assert transform.display_to_pixel(*displayed) == (x, y)
            recovered = transform.display_to_source(*displayed)
            assert recovered == pytest.approx((x + 0.5, y + 0.5), abs=1e-9)


def test_nonfinite_coordinates_are_not_injected():
    transform = LetterboxTransform(ScreenRect(0, 0, 100, 100), 100, 100)
    for value in (math.nan, math.inf, -math.inf):
        assert transform.display_to_pixel(value, 50) is None
        assert transform.uv_to_pixel(value, 0.5) is None
        with pytest.raises(ValueError):
            transform.source_to_display(value, 50)


def test_negative_origin_windows_absolute_mapping_selects_pixel_centers():
    desktop = ScreenRect(-1920, -100, 3840, 1180)
    for x, y in ((desktop.left, desktop.top), (-1, -1), (0, 0), (desktop.right - 1, desktop.bottom - 1)):
        ax, ay = pixel_to_windows_absolute(x, y, desktop)
        assert 0 <= ax <= 65535 and 0 <= ay <= 65535
        # Windows absolute input uses the virtual desktop when VIRTUALDESK is set.
        assert desktop.left + math.floor(ax * desktop.width / 65536) == x
        assert desktop.top + math.floor(ay * desktop.height / 65536) == y
    with pytest.raises(ValueError):
        pixel_to_windows_absolute(desktop.right, 0, desktop)


def test_stale_geometry_is_rejected():
    require_generation(4, 4)
    with pytest.raises(ValueError, match="stale geometry"):
        require_generation(3, 4)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 monitor enumeration")
def test_repeated_monitor_enumeration_does_not_retain_a_type_per_video_frame():
    import ctypes
    import gc
    from quest3d.capture import list_monitors

    assert list_monitors()
    gc.collect()
    before = len(ctypes._pointer_type_cache)
    for _ in range(300):
        assert list_monitors()
    gc.collect()
    # This cache owns its keys permanently. The regression leaked 300 type
    # pairs here even after collection, and ~96MiB during a five-minute run.
    assert len(ctypes._pointer_type_cache) == before


def _fake_capture(monkeypatch):
    from quest3d import capture

    topology = [capture.MonitorInfo(0, ScreenRect(-4, 0, 8, 4), "all", False),
                capture.MonitorInfo(1, ScreenRect(-4, 0, 4, 4), "left", True),
                capture.MonitorInfo(2, ScreenRect(0, 0, 4, 4), "right", False)]
    monkeypatch.setattr(capture, "list_monitors", lambda: list(topology))
    monkeypatch.setattr(capture, "_require_physical_pixels",
                        lambda: capture.DpiAwarenessStatus("per_monitor_v2", True))

    class FakeMss:
        def __init__(self):
            self.raw = np.zeros((4, 4, 4), dtype=np.uint8)
            self.closed = False

        def grab(self, rect):
            self.raw = np.zeros((rect["height"], rect["width"], 4), dtype=np.uint8)
            self.raw[:, :, 0] = 7
            return self.raw

        def close(self):
            self.closed = True

    backend = FakeMss()
    monkeypatch.setitem(sys.modules, "mss", SimpleNamespace(mss=lambda: backend))
    return capture, topology, backend


def test_capture_owns_bgra_and_keeps_frame_geometry_snapshot(monkeypatch):
    capture, _, backend = _fake_capture(monkeypatch)
    monkeypatch.setattr(capture, "perf_counter_ns", lambda: 123456)
    with capture.DesktopCapture(monitor=1) as source:
        first = source.grab()
        assert first.captured_ns == 123456
        assert first.bgra.flags.c_contiguous and first.bgra.dtype == np.uint8
        backend.raw[:] = 99
        assert first.bgra[0, 0, 0] == 7
        source.set_source(rect=ScreenRect(-2, 1, 2, 2))
        second = source.grab()
        assert (first.frame_id, second.frame_id) == (1, 2)
        assert (first.geometry_generation, second.geometry_generation) == (0, 1)
        assert first.geometry.bounds == ScreenRect(-4, 0, 4, 4)
        assert second.geometry.bounds == ScreenRect(-2, 1, 2, 2)
        assert source.source_geometry == second.geometry
    assert backend.closed


def test_invalid_source_change_preserves_previous_source(monkeypatch):
    capture, _, _ = _fake_capture(monkeypatch)
    with capture.DesktopCapture(monitor=1) as source:
        original = (source.source_id, source.source_geometry)
        with pytest.raises(ValueError):
            source.set_source(rect=ScreenRect(3, 0, 2, 2))
        assert (source.source_id, source.source_geometry) == original
        source.set_source(monitor=1)
        assert source.grab().geometry_generation == 0


def test_capture_frame_source_identity_survives_change_and_reopen(monkeypatch):
    from dataclasses import replace
    capture, topology, _ = _fake_capture(monkeypatch)
    topology[1] = replace(topology[1], native_handle=100)
    topology[2] = replace(topology[2], native_handle=200)
    source = capture.DesktopCapture(monitor=1)
    with source:
        first = source.grab()
        identity = first.source_identity
        assert identity.native_handle == 100
        assert source.grab().source_identity == identity
        source.set_source(monitor=2)
        second = source.grab()
        assert second.source_identity.native_handle == 200
        assert second.source_identity.selection_id != identity.selection_id
        assert first.source_identity == identity
        with pytest.raises(ValueError):
            source.set_source(monitor=99)
        assert source.grab().source_identity == second.source_identity
    with source:
        reopened = source.grab()
        assert reopened.source_identity.native_handle == 200
        assert reopened.source_identity.selection_id != second.source_identity.selection_id
        assert first.source_identity == identity


def test_monitor_reconfiguration_invalidates_geometry_before_next_frame(monkeypatch):
    capture, topology, _ = _fake_capture(monkeypatch)
    with capture.DesktopCapture(monitor=1) as source:
        first = source.grab()
        topology[1] = capture.MonitorInfo(1, ScreenRect(-4, 0, 3, 4), "left", True)
        next_frame = source.grab()
        assert next_frame.bgra.shape == (4, 3, 4)
        assert next_frame.geometry_generation == first.geometry_generation + 1
        with pytest.raises(ValueError):
            require_generation(first.geometry_generation, source.geometry.generation)


@pytest.mark.parametrize("options", [{"monitor": 0}, {"monitor": -1}, {"device": -1},
                                      {"timeout_seconds": 0}, {"timeout_seconds": 31}])
def test_gpu_capture_refuses_unsupported_source_and_timeout_configuration(options):
    from quest3d.capture import GPUDesktopCapture
    with pytest.raises(ValueError):
        GPUDesktopCapture(**options)


@pytest.mark.gpu
@pytest.mark.capture
@pytest.mark.skipif(os.environ.get("QUEST3D_TEST_GPU_CAPTURE") != "1",
                    reason="opt in explicitly to real Windows GPU desktop capture")
def test_gpu_capture_actual_cuda_frame_and_clean_shutdown():
    import torch
    from quest3d.capture import GPUDesktopCapture, list_monitors

    pytest.importorskip("wc_cuda", reason="pinned GPU capture extra not installed")
    assert torch.cuda.is_available()
    monitor = next(item for item in list_monitors() if item.is_primary)
    # This is real desktop data. No synthetic image or depth enters this test.
    with GPUDesktopCapture(monitor=monitor.index, timeout_seconds=10) as source:
        frame = source.grab()
        assert frame.bgra.is_cuda and frame.bgra.dtype == torch.uint8
        assert frame.bgra.is_contiguous()
        assert tuple(frame.bgra.shape) == (monitor.bounds.height, monitor.bounds.width, 4)
        assert frame.geometry.bounds == monitor.bounds
        assert frame.frame_id > 0 and frame.captured_ns > 0
        assert source.timestamp_kind == "host_receive_perf_counter_ns"
        from importlib.metadata import version
        patched = version("wc_cuda") == "0.1.2+quest1"
        assert source.cursor_exclusion_supported is patched
        assert source.native_texture_lease_verified is patched
        assert source.cursor_exclusion_verified is False
        # Only aggregate metadata is evaluated; no screenshot is written.
        assert int(torch.count_nonzero(frame.bgra[:, :, :3]).item()) > 0
        retained = frame.bgra.clone()
    # The returned allocation remains usable after the native session ends.
    assert torch.equal(frame.bgra, retained)

"""Candidate-only real HDR capture checks; no display settings are changed."""
from dataclasses import replace
from importlib.metadata import version
import os
import time

import pytest
import torch

from quest3d.capture import GPUDesktopCapture, list_monitors
from quest3d.geometry import ScreenRect
from quest3d.stereo import StereoSynthesizer


candidate = os.environ.get("QUEST3D_TEST_HDR_CAPTURE") == "1"
pytestmark = [pytest.mark.gpu, pytest.mark.capture,
              pytest.mark.skipif(not candidate, reason="requires explicitly selected +quest2 HDR candidate")]


def test_candidate_cannot_silently_replace_the_production_backend():
    assert version("wc_cuda") == "0.1.2+quest2"
    with pytest.raises(RuntimeError, match="explicit opt-in"):
        with GPUDesktopCapture():
            pytest.fail("Candidate started without opt-in")


@pytest.mark.parametrize("implementation", ["torch", "fused"])
def test_real_hdr_roi_becomes_owned_opaque_bgra_and_original_equal_eyes(implementation):
    monitor = next(m for m in list_monitors() if m.is_primary)
    bounds = monitor.bounds
    # Nonaligned width verifies crop/pitch handling on actual native frames.
    roi = ScreenRect(bounds.left + 13, bounds.top + 17, 641, 359)
    with GPUDesktopCapture(monitor=monitor.index, rect=roi, experimental_hdr=True,
                           hdr_tonemap=implementation) as capture:
        frame = capture.grab()
        assert frame.bgra.shape == (359, 641, 4)
        assert frame.bgra.dtype == torch.uint8 and frame.bgra.is_cuda and frame.bgra.is_contiguous()
        assert bool((frame.bgra[..., 3] == 255).all())
        assert frame.geometry.bounds == roi
        assert frame.color_processing_ms > 0
        assert capture.color_profile["display"]["hdr_enabled"] is True
        retained = frame.bgra.clone()
    assert torch.equal(retained, frame.bgra)
    mono = StereoSynthesizer(320, 180, disparity_px=0).original_2d(
        frame.bgra, frame_id=frame.frame_id, generation=frame.geometry_generation)
    import numpy as np
    np.testing.assert_array_equal(mono.bgra[:, :320], mono.bgra[:, 320:])


def test_color_metadata_change_stops_capture_instead_of_reusing_wrong_white(monkeypatch):
    # Only the metadata response is altered; this never changes Windows HDR.
    from quest3d import display_color
    original_query = display_color.read_display_colors
    with GPUDesktopCapture(experimental_hdr=True) as capture:
        capture.grab()
        monkeypatch.setattr(display_color, "read_display_colors", lambda: [
            replace(value, sc_rgb_sdr_white_scale=value.sc_rgb_sdr_white_scale + .5)
            for value in original_query()])
        capture._last_color_check_ns = 0
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            try:
                capture.grab(timeout_seconds=.5)
            except TimeoutError:
                continue
            except RuntimeError as exc:
                assert "Display color/SDR white changed" in str(exc)
                break
        else:
            pytest.fail("Changed color metadata was not rejected")

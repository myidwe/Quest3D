"""Reject incorrect monitor/profile association without altering display settings."""
from dataclasses import replace

import pytest

from quest3d.display_color import DisplayColor, require_hdr_color


def color():
    return DisplayColor("DISPLAY2", "hdr", True, 8, 4800, 384, 4.8, {})


def test_selected_monitor_has_its_own_measured_white():
    second = replace(color(), device_name="DISPLAY1", sdr_white_raw=3000,
                     sdr_white_nits=240, sc_rgb_sdr_white_scale=3)
    assert require_hdr_color("display2", [second, color()]).sc_rgb_sdr_white_scale == 4.8
    assert require_hdr_color("DISPLAY1", [second, color()]).sc_rgb_sdr_white_scale == 3


@pytest.mark.parametrize("profiles", [[], [color(), color()], [replace(color(), device_name="DISPLAY3")]])
def test_unidentified_or_cloned_display_is_not_guessed(profiles):
    with pytest.raises(RuntimeError):
        require_hdr_color("DISPLAY2", profiles)


@pytest.mark.parametrize("change", [dict(hdr_enabled=None), dict(hdr_enabled=False),
    dict(active_mode="advanced_unknown"), dict(sc_rgb_sdr_white_scale=None),
    dict(sc_rgb_sdr_white_scale=float("nan")), dict(sc_rgb_sdr_white_scale=True),
    dict(sc_rgb_sdr_white_scale=0), dict(sc_rgb_sdr_white_scale=1000)])
def test_missing_white_or_unknown_hdr_cannot_enable_tone_mapping(change):
    with pytest.raises(RuntimeError):
        require_hdr_color("DISPLAY2", [replace(color(), **change)])

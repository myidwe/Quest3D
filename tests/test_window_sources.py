"""Read-only policy regressions; fake Win32 states are explicit fault injection."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from quest3d.geometry import ScreenRect
from quest3d.window_sources import (
    SourceExclusions, WindowIdentity, WindowObservation, WindowTracker, Win32WindowProvider, overlaps,
)


def observation(**changes):
    base = WindowObservation(123, 99, WindowIdentity(123, 10, 1234567890, 11, "known_class"),
        title="Video", window_bounds=ScreenRect(-1010, 10, 820, 640), frame_bounds=ScreenRect(-1002, 18, 804, 624),
        client_bounds=ScreenRect(-1000, 40, 800, 600), dpi=144, monitor_device="DISPLAY2",
        monitor_bounds=ScreenRect(-1920, 0, 1920, 1080), virtual_desktop=ScreenRect(-1920, 0, 3840, 1080),
        visible=True, minimized=False, cloaked=False, foreground=True, top_level=True,
        occlusion="no_overlapping_top_level_window")
    return replace(base, **changes)


class Events:
    def __init__(self):
        self.destroyed, self.revision, self.error = False, 0, None
    def __enter__(self):
        return self
    def __exit__(self, *_):
        pass
    def snapshot(self):
        return self.destroyed, self.revision, self.error


class Provider:
    def __init__(self, value):
        self.value = value
        self.during_query = None
    def observe(self, *_):
        if self.during_query:
            self.during_query()
        return self.value


def tracker(value=None, **kwargs):
    value = value or observation()
    provider, events = Provider(value), Events()
    return WindowTracker(value.identity, provider=provider, events=events, **kwargs), provider, events


def test_negative_physical_client_bounds_stable_title_and_no_input_authority():
    selected, provider, _ = tracker()
    with selected:
        first = selected.poll()
        assert first.desktop_crop_eligible
        assert first.crop_bounds == ScreenRect(-1000, 40, 800, 600)
        assert first.input_authorized is False
        provider.value = replace(provider.value, title="Title changed", observed_ns=100)
        assert selected.poll().generation == first.generation


@pytest.mark.parametrize("changes", [
    {"client_bounds": ScreenRect(-900, 40, 800, 600)},
    {"client_bounds": ScreenRect(-1000, 40, 900, 600)},
    {"frame_bounds": ScreenRect(-998, 20, 800, 620)},
    {"dpi": 192}, {"monitor_device": "DISPLAY1"},
    {"monitor_bounds": ScreenRect(0, 0, 2560, 1440)},
    {"foreground": False},
    {"occlusion": "possible", "occluders": (44,)},
])
def test_geometry_dpi_monitor_focus_and_occlusion_invalidate_generation(changes):
    selected, provider, _ = tracker()
    with selected:
        original = selected.poll().generation
        provider.value = replace(provider.value, **changes)
        assert selected.poll().generation > original


@pytest.mark.parametrize("changes,reason", [
    ({"minimized": True}, "minimized"), ({"cloaked": True}, "cloaked_or_unknown"),
    ({"cloaked": None}, "cloaked_or_unknown"), ({"visible": False}, "not_visible"),
    ({"occlusion": "unknown"}, "occlusion_unknown"), ({"dpi": None}, "dpi_or_monitor_unknown"),
    ({"client_bounds": None}, "bounds_unavailable"),
    ({"client_bounds": ScreenRect(-3000, 40, 800, 600)}, "outside_virtual_desktop"),
    ({"client_bounds": ScreenRect(-100, 40, 800, 600)}, "cross_monitor_or_outside_monitor"),
    ({"top_level": False}, "not_top_level"),
])
def test_uncertain_or_unavailable_target_never_claims_safe_desktop_crop(changes, reason):
    selected, provider, _ = tracker()
    with selected:
        selected.poll()
        provider.value = replace(provider.value, **changes)
        result = selected.poll()
        assert not result.desktop_crop_eligible and reason in result.reasons


@pytest.mark.parametrize("identity", [None,
    WindowIdentity(123, 20, 1234567890, 11, "known_class"),
    WindowIdentity(123, 10, 9999999999, 11, "known_class"),
    WindowIdentity(123, 10, 1234567890, 21, "known_class"),
    WindowIdentity(123, 10, 1234567890, 11, "new_class"),
])
def test_missing_window_and_reused_hwnd_or_pid_are_terminal(identity):
    selected, provider, _ = tracker()
    with selected:
        original = provider.value
        selected.poll()
        provider.value = replace(provider.value, identity=identity)
        assert selected.poll().invalid_reason == "window_identity_lost_or_reused"
        provider.value = original
        assert not selected.poll().valid_identity


def test_same_handle_same_process_destroy_create_and_geometry_round_trip():
    selected, _, events = tracker()
    with selected:
        generation = selected.poll().generation
        events.revision += 2  # Move away/back between snapshots: same final rectangle.
        assert selected.poll().generation > generation
        events.destroyed = True  # Same HWND/class/PID may already exist again.
        assert selected.poll().invalid_reason == "window_lifecycle_changed"


def test_event_race_or_hook_failure_does_not_claim_current_geometry():
    selected, provider, events = tracker()
    with selected:
        selected.poll()
        provider.during_query = lambda: setattr(events, "revision", events.revision + 1)
        result = selected.poll()
        assert not result.desktop_crop_eligible and "geometry_changed_during_query" in result.reasons
        events.error = "hook failed"
        assert selected.poll().invalid_reason == "lifecycle_monitor_failed"


def test_output_exclusions_block_selection_and_output_overlap_is_not_ignored():
    rule = SourceExclusions(hwnds={123}, process_ids={500})
    assert rule.matches(123, 10) and rule.matches(99, 500) and not rule.matches(99, 10)
    selected, provider, _ = tracker(exclusions=rule)
    with selected:
        provider.value = replace(provider.value, excluded=True)
        assert selected.poll().invalid_reason == "source_is_excluded_output"
    selected, provider, _ = tracker()
    with selected:
        provider.value = replace(provider.value, occlusion="possible", occluders=(44,), output_overlap=True)
        result = selected.poll()
        assert "output_overlap_recursion_risk" in result.reasons and not result.desktop_crop_eligible


def test_native_occlusion_walk_includes_untitled_transparent_output_and_unknown_bounds():
    provider = object.__new__(Win32WindowProvider)
    provider.user = SimpleNamespace(GetWindow=lambda hwnd, _: {123: 44, 44: 45, 45: 0}[hwnd],
                                   IsWindowVisible=lambda _: True, IsIconic=lambda _: False)
    provider._cloaked = lambda _: False
    provider._bounds = lambda hwnd, *_: ScreenRect(-900, 50, 30, 30) if hwnd == 44 else None
    provider._pid = lambda _: (500, 2)
    result = provider._occlusion(123, ScreenRect(-1000, 40, 800, 600), SourceExclusions(process_ids={500}))
    assert result == ("possible", (44,), True)
    provider._bounds = lambda *_: None
    assert provider._occlusion(123, ScreenRect(0, 0, 10, 10), SourceExclusions())[0] == "unknown"


def test_edge_touch_is_not_overlap_and_frame_selection_keeps_dwm_bounds():
    assert not overlaps(ScreenRect(-10, 0, 10, 10), ScreenRect(0, 0, 10, 10))
    assert overlaps(ScreenRect(-10, 0, 11, 10), ScreenRect(0, 0, 10, 10))
    selected, _, _ = tracker(bounds_kind="frame")
    with selected:
        result = selected.poll()
        assert result.crop_bounds == result.observation.frame_bounds


@pytest.mark.parametrize("bad", [True, 0, -1, 1.5, 2**64])
def test_exclusion_handles_reject_invalid_values(bad):
    with pytest.raises(ValueError):
        SourceExclusions(hwnds={bad})

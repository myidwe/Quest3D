"""Permission regressions; synthetic frames here are not OS-input evidence."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d.capture import CapturedFrame
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.input_policy import frame_input_decision, validate_input_options
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.stereo import StereoSynthesizer


def fixture():
    bounds = ScreenRect(-32, 5, 32, 18)
    identity = SourceIdentity(SourceKind.MONITOR, 0, 10, 44, 0, bounds)
    frame = CapturedFrame(np.zeros((18, 32, 4), dtype=np.uint8), 1_000_000_000,
        "fixture:monitor", 2, 3, SourceGeometry(bounds, 3), identity)
    stereo = StereoSynthesizer(64, 36, disparity_px=0).original_2d(frame.bgra, frame_id=2, generation=3)
    return dict(opt_in=True, protocol=3, requested_mode="2d", output=stereo,
                source=frame, current_frame=frame, now_ns=1_100_000_000)


def test_only_explicit_fresh_original_monitor_is_eligible():
    assert frame_input_decision(**fixture()).enabled


@pytest.mark.parametrize("changes,reason", [
    ({"opt_in": False}, "producer_opt_out"),
    ({"protocol": 2}, "source_protocol_required"),
    ({"requested_mode": "3d"}, "explicit_original_2d_required"),
    ({"error": "capture failed"}, "producer_error"),
    ({"now_ns": 999_999_999}, "capture_not_fresh"),
    ({"now_ns": 1_500_000_001}, "capture_not_fresh"),
])
def test_fallback_future_expired_or_disabled_never_grants_input(changes, reason):
    data = fixture()
    data.update(changes)
    decision = frame_input_decision(**data)
    assert not decision.enabled and decision.reason == reason


def test_capture_age_boundary_does_not_use_new_publication_time():
    data = fixture()
    data["now_ns"] = 1_500_000_000
    assert frame_input_decision(**data).enabled
    data["now_ns"] += 1
    assert not frame_input_decision(**data).enabled


@pytest.mark.parametrize("field,value", [("selection_id", 11), ("native_handle", 45),
    ("parent_bounds", ScreenRect(-32, 5, 33, 18))])
def test_current_selection_or_display_change_invalidates_old_frame(field, value):
    data = fixture()
    old = data["source"]
    data["current_frame"] = replace(old, source_identity=replace(old.source_identity, **{field: value}))
    assert frame_input_decision(**data).reason == "source_frame_mismatch"


@pytest.mark.parametrize("changes", [{"frame_id": 3}, {"source_id": "other"}, {"captured_ns": 1_100_000_000},
    {"geometry_generation": 4}, {"geometry": SourceGeometry(ScreenRect(0, 0, 32, 18), 3)}])
def test_previous_frame_cannot_be_relabelled_as_current(changes):
    data = fixture()
    data["current_frame"] = replace(data["source"], **changes)
    assert not frame_input_decision(**data).enabled


@pytest.mark.parametrize("kind", [SourceKind.WINDOW, SourceKind.VIDEO, SourceKind.PHOTO])
def test_files_and_windows_remain_ineligible_until_their_input_path_is_implemented(kind):
    data = fixture()
    identity = (SourceIdentity(kind, 20, 10, 44, 300, ScreenRect(-32, 5, 32, 18))
                if kind == SourceKind.WINDOW else
                SourceIdentity(kind, 0, 10, 0, 0, ScreenRect(0, 0, 32, 18)))
    data["source"] = data["current_frame"] = replace(data["source"], source_identity=identity)
    assert frame_input_decision(**data).reason == "monitor_identity_required"


@pytest.mark.parametrize("changes", [{"bridge_protocol": 2}, {"file": "movie.mp4"},
    {"file_av_clock": True}, {"monitor": 0}, {"command": "run", "publish": False}])
def test_unsupported_opt_in_is_rejected_before_starting_resources(changes):
    args = dict(enable_input=True, bridge_protocol=3, monitor=1, command="serve")
    args.update(changes)
    with pytest.raises(ValueError, match="--enable-input"):
        validate_input_options(SimpleNamespace(**args))


def test_default_options_remain_compatible_and_no_permission_is_inferred():
    validate_input_options(SimpleNamespace())
    validate_input_options(SimpleNamespace(enable_input=True, bridge_protocol=3,
        monitor=1, command="run", publish=True))

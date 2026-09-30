"""Desktop pair continuity; synthetic inputs do not claim GPU/Quest quality."""
from dataclasses import replace
import json

import numpy as np
import pytest

from quest3d.bridge import ORIGINAL_2D
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.input_policy import frame_input_decision
from quest3d.media import MediaFrame
from quest3d.session import _DesktopProcessedSelector, select_processed
from quest3d.source_identity import SourceIdentity, SourceKind
from test_session import _frame, _processed, _SessionHarness, _wait


SECOND = 1_000_000_000
MS = 1_000_000


def choose(selector, pair, current, now, **changes):
    options = dict(requested_mode="3d", revision=3, current_frame=current,
                   now_ns=now, max_age_ms=250)
    options.update(changes)
    return selector.select(pair, **options)


def static_pair():
    source = _frame(captured_ns=SECOND)
    return source, _processed(source), _DesktopProcessedSelector()


def test_first_new_frame_after_static_scene_retains_exact_old_pair_then_uses_real_new_result():
    old, pair, selector = static_pair()
    old_rgb, old_stereo = old.bgra.copy(), pair.stereo.bgra.copy()
    assert choose(selector, pair, old, 20 * SECOND) == (pair, None)
    current = _frame(2, captured_ns=20 * SECOND + 10 * MS)
    new_rgb = current.bgra.copy()
    # Existing callers keep the same stateless expiration semantics.
    assert select_processed(pair, requested_mode="3d", revision=3, current_frame=current,
        now_ns=20 * SECOND + 30 * MS, max_age_ms=250) == (None, "stale_depth_frame")
    selected, reason = choose(selector, pair, current, 20 * SECOND + 30 * MS)
    assert selected is pair and reason is None
    assert selected.source is old and selected.stereo is pair.stereo
    assert selector.snapshot() == {"decision": "held_matching_pair", "hold_age_ms": 30.0,
                                  "last_same_frame_observed_ns": 20 * SECOND}
    assert selected.source.captured_ns == SECOND
    assert selected.stereo.frame_id == selected.source.frame_id == 1
    assert current.frame_id == 2
    replacement = _processed(current)
    assert choose(selector, replacement, current, 20 * SECOND + 45 * MS) == (replacement, None)
    assert selector.snapshot()["decision"] == "observed_current_pair"
    np.testing.assert_array_equal(old.bgra, old_rgb)
    np.testing.assert_array_equal(pair.stereo.bgra, old_stereo)
    np.testing.assert_array_equal(current.bgra, new_rgb)


def test_continuous_new_rgb_never_renews_observation_deadline():
    old, pair, selector = static_pair()
    observed = 10 * SECOND
    choose(selector, pair, old, observed)
    for number, elapsed in enumerate((20, 80, 160, 249, 250), start=2):
        current = _frame(number, captured_ns=observed + elapsed * MS)
        selected, _ = choose(selector, pair, current, current.captured_ns)
        assert selected is pair
        assert selector.snapshot()["last_same_frame_observed_ns"] == observed
        assert selector.snapshot()["hold_age_ms"] == elapsed
    current = _frame(7, captured_ns=observed + 250 * MS + 1)
    assert choose(selector, pair, current, current.captured_ns) == (None, "stale_depth_frame")
    assert selector.snapshot()["last_same_frame_observed_ns"] is None
    later = _frame(8, captured_ns=observed + 260 * MS)
    assert choose(selector, pair, later, later.captured_ns) == (None, "stale_depth_frame")


def test_long_static_idle_is_valid_but_unobserved_time_does_not_create_grace():
    old, pair, selector = static_pair()
    choose(selector, pair, old, 2 * SECOND)
    # Presentation really observed the same image after an hour of static RGB.
    assert choose(selector, pair, old, 3600 * SECOND) == (pair, None)
    current = _frame(2, captured_ns=3600 * SECOND + 20 * MS)
    assert choose(selector, pair, current, current.captured_ns) == (pair, None)
    # Separate instance: no recent same-frame observation actually happened.
    absent = _DesktopProcessedSelector()
    choose(absent, pair, old, 2 * SECOND)
    assert choose(absent, pair, current, current.captured_ns) == (None, "stale_depth_frame")


@pytest.mark.parametrize("failure", ["2d", "ai_error", "source_error", "missing", "mismatched_depth"])
def test_mode_errors_and_missing_or_invalid_ai_invalidate_prior_continuity(failure):
    old, pair, selector = static_pair()
    choose(selector, pair, old, 10 * SECOND)
    current = _frame(2, captured_ns=10 * SECOND + 20 * MS)
    changes = {}
    candidate = pair
    if failure == "2d":
        changes["requested_mode"] = "2d"
    elif failure in ("ai_error", "source_error"):
        changes[failure] = "injected failure"
    elif failure == "missing":
        candidate = None
    else:
        candidate = replace(pair, stereo=replace(pair.stereo, frame_id=99))
    assert choose(selector, candidate, current, current.captured_ns, **changes)[0] is None
    assert selector.snapshot()["last_same_frame_observed_ns"] is None
    assert choose(selector, pair, current, current.captured_ns + MS) == (None, "stale_depth_frame")


@pytest.mark.parametrize("change", ["source", "revision", "generation", "geometry", "selection", "timeline"])
def test_scope_changes_cannot_reuse_or_resurrect_old_observation(change):
    old, pair, selector = static_pair()
    if change == "selection":
        identity = SourceIdentity(SourceKind.MONITOR, 0, 10, 44, 0, old.geometry.bounds)
        old = replace(old, source_identity=identity)
        pair = _processed(old)
    if change == "timeline":
        old = MediaFrame(old.bgra, old.captured_ns, old.source_id, old.frame_id,
                         old.geometry_generation, old.geometry, 0, 7)
        pair = _processed(old)
    choose(selector, pair, old, 10 * SECOND)
    current = replace(old, bgra=_frame(2).bgra, frame_id=2, captured_ns=10 * SECOND + 20 * MS)
    base_current = current
    options = {}
    if change == "source":
        current = replace(current, source_id="replacement:source")
    elif change == "revision":
        options["revision"] = 4
    elif change == "generation":
        current = replace(current, geometry_generation=1)
    elif change == "geometry":
        current = replace(current, geometry=SourceGeometry(ScreenRect(-300, 25, 32, 18), 0))
    elif change == "selection":
        current = replace(current, source_identity=replace(current.source_identity, selection_id=11))
    elif change == "timeline":
        current = replace(current, timeline_epoch=8)
    assert choose(selector, pair, current, current.captured_ns, **options)[0] is None
    # Returning to old scope without a new exact matching observation is not a renewal.
    assert choose(selector, pair, base_current, base_current.captured_ns + MS) == (None, "stale_depth_frame")


def test_clock_reversal_and_capture_regression_clear_continuity():
    old, pair, selector = static_pair()
    choose(selector, pair, old, 10 * SECOND)
    assert choose(selector, pair, old, 10 * SECOND - MS) == (None, "non_monotonic_presentation_time")
    assert selector.snapshot()["last_same_frame_observed_ns"] is None
    current = _frame(2, captured_ns=10 * SECOND + MS)
    assert choose(selector, pair, current, current.captured_ns) == (None, "stale_depth_frame")
    replacement = _processed(current)
    choose(selector, replacement, current, current.captured_ns)
    reverted = replace(old, captured_ns=current.captured_ns + MS)
    assert choose(selector, replacement, reverted, reverted.captured_ns) == (None, "non_monotonic_source_frame")


def test_future_capture_or_reused_identity_is_not_a_static_observation():
    old, pair, selector = static_pair()
    choose(selector, pair, old, 10 * SECOND)
    future = _frame(2, captured_ns=11 * SECOND)
    assert choose(selector, pair, future, 10 * SECOND + MS) == (None, "future_capture_timestamp")
    selector = _DesktopProcessedSelector()
    replacement_pixels = replace(old, bgra=_frame(2).bgra)
    assert choose(selector, pair, replacement_pixels, 10 * SECOND) == (None, "mismatched_rgb_depth")
    assert selector.snapshot()["last_same_frame_observed_ns"] is None


@pytest.mark.parametrize("change", ["capture_regression", "timestamp_refresh", "pixel_shape"])
def test_changed_capture_identity_invalidates_observation(change):
    old, pair, selector = static_pair()
    choose(selector, pair, old, 10 * SECOND)
    if change == "capture_regression":
        current = replace(old, frame_id=2, captured_ns=old.captured_ns - 1)
        reason = "non_monotonic_source_frame"
    elif change == "timestamp_refresh":
        current = replace(old, captured_ns=10 * SECOND)
        reason = "non_monotonic_source_frame"
    else:
        current = replace(old, frame_id=2, captured_ns=10 * SECOND, bgra=old.bgra[:9])
        reason = "stale_configuration"
    assert choose(selector, pair, current, 10 * SECOND + MS) == (None, reason)
    assert selector.snapshot()["last_same_frame_observed_ns"] is None


def test_new_ai_completion_without_exact_current_observation_cannot_extend_old_hold():
    old, pair, selector = static_pair()
    choose(selector, pair, old, 10 * SECOND)
    newer = _frame(2, captured_ns=10 * SECOND + 10 * MS)
    current = _frame(3, captured_ns=10 * SECOND + 20 * MS)
    next_pair = replace(_processed(newer), completion_sequence=2)
    assert choose(selector, next_pair, current, current.captured_ns) == (next_pair, None)
    assert selector.snapshot()["last_same_frame_observed_ns"] is None
    later = _frame(4, captured_ns=11 * SECOND)
    assert choose(selector, next_pair, later, later.captured_ns) == (None, "stale_depth_frame")


def test_hold_cannot_enable_input_or_refresh_its_timestamp():
    old, pair, selector = static_pair()
    identity = SourceIdentity(SourceKind.MONITOR, 0, 10, 44, 0, old.geometry.bounds)
    old = replace(old, source_identity=identity)
    pair = _processed(old)
    choose(selector, pair, old, 10 * SECOND)
    current = replace(old, frame_id=2, captured_ns=10 * SECOND + 20 * MS, bgra=_frame(2).bgra)
    held, _ = choose(selector, pair, current, current.captured_ns)
    decision = frame_input_decision(opt_in=True, protocol=3, requested_mode="3d", output=held.stereo,
        source=held.source, current_frame=current, now_ns=current.captured_ns)
    assert not decision.enabled and held.source.captured_ns == SECOND


def test_serve_holds_static_pair_without_counting_a_new_ai_completion(monkeypatch, tmp_path):
    # First capture is intentionally old but remains the actual current object
    # until a real initial stereo publication; second inference is then blocked.
    harness = _SessionHarness(monkeypatch, tmp_path, max_frame_age_ms=200, first_capture_age_ms=2000)
    try:
        assert harness.saw_3d.wait(3) and harness.ai_blocked.wait(3)
        # Let the real bounded hold expire while actual worker remains blocked.
        state = _wait(lambda: s if (s := harness.status()).get("fallback_reason") == "stale_depth_frame" else None)
        assert state["ai_frames"] == state["ai_completed_published"] == 1
        assert not harness.allow_ai.is_set()
        assert harness.published[-1][1]["flags"] & ORIGINAL_2D
    finally:
        harness.close()
    records = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    held = [row for row in records if row["stereo_continuity"]["decision"] == "held_matching_pair"]
    assert held and all(row["mode"] == "3d" and row["source_frame_id"] == 1 for row in held)
    assert all(row["current_source_frame_id"] == 2 and row["source_ns"] < row["current_source_ns"] for row in held)
    assert {row["ai_completion_sequence"] for row in held} == {1}
    assert len({row["source_ns"] for row in held}) == 1
    assert len({row["stereo_continuity"]["last_same_frame_observed_ns"] for row in held}) == 1
    assert all(0 <= row["stereo_continuity"]["hold_age_ms"] <= 200 for row in held)

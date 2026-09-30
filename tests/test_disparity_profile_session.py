"""Profile changes must travel with real session revisions and RGB/depth jobs.

Injected CPU depth isolates orchestration; it is not actual AI/Quest evidence.
"""
import json
import threading

import numpy as np

from quest3d.session import LatestAIWorker
from quest3d.session_control import atomic_json, send_control
from test_session import _frame, _synthetic_depth, _wait, _SessionHarness


def test_worker_seals_profile_with_each_queued_frame_and_can_restore_linear(tmp_path):
    entered, release = threading.Event(), threading.Event()

    class Engine:
        def __init__(self, _):
            pass

        def infer(self, bgra, *, frame_id, generation):
            if frame_id == 1:
                entered.set()
                assert release.wait(3)
            return _synthetic_depth(bgra, frame_id, generation)

    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                            directory=tmp_path, engine_factory=Engine)
    try:
        worker.submit(_frame(1), 0, 4)
        assert entered.wait(3)
        worker.submit(_frame(2), 1, 4, disparity_profile="comfort")
        release.set()
        result = _wait(lambda: p if (p := worker.snapshot()[0]) and p.revision == 1 else None)
        assert worker.error is None
        assert result.source.frame_id == result.stereo.frame_id == 2
        assert result.stereo.disparity_profile == "comfort"
        assert result.stereo.effective_convergence == .625
        old_pixels = result.stereo.bgra.copy()
        worker.submit(_frame(3), 2, 4)  # Legacy signature explicitly returns linear.
        restored = _wait(lambda: p if (p := worker.snapshot()[0]) and p.revision == 2 else None)
        assert restored.stereo.disparity_profile == "linear"
        assert restored.stereo.effective_convergence == .5
        np.testing.assert_array_equal(result.stereo.bgra, old_pixels)
    finally:
        release.set()
        worker.close()


def apply(harness, directory, **kwargs):
    request = send_control(directory, **kwargs)
    return _wait(lambda: s if (s := harness.status()).get("applied_request") == request["request_id"] else None)


def test_static_source_profile_toggle_preserves_strength_and_survives_2d_controls(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, second_action="static")
    try:
        assert harness.saw_3d.wait(3)
        first = harness.status()
        enabled = apply(harness, tmp_path, disparity_profile="comfort")
        assert enabled["disparity"] == first["disparity"] == 4
        assert enabled["effective_disparity_profile"] == "comfort"
        assert enabled["effective_convergence"] == .625
        assert enabled["revision"] == first["revision"] + 1
        two = apply(harness, tmp_path, mode="2d")
        assert two["disparity_profile"] == "comfort"
        assert two["effective_disparity_profile"] == "linear"
        assert two["disparity_profile_reason"] == "original_2d"
        pixels = harness.published[-1][0]
        np.testing.assert_array_equal(pixels[:, :320], pixels[:, 320:])
        restored = apply(harness, tmp_path, mode="3d", disparity=5)
        assert restored["disparity_profile"] == restored["effective_disparity_profile"] == "comfort"
        assert restored["disparity"] == 5
        disabled = apply(harness, tmp_path, disparity_profile="linear")
        assert disabled["effective_disparity_profile"] == "linear"
        assert disabled["effective_convergence"] == .5 and disabled["disparity"] == 5
    finally:
        harness.close()
    records = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    stereo = [r for r in records if r["published"] and r["mode"] == "3d"]
    assert {r["disparity_profile"] for r in stereo} == {"linear", "comfort"}
    assert all(r["disparity_profile"] == r["effective_disparity_profile"] for r in stereo)


def test_profile_change_does_not_publish_old_profile_while_inference_is_blocked(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3) and harness.ai_blocked.wait(3)
        request = send_control(tmp_path, disparity_profile="comfort")
        waiting = _wait(lambda: s if (s := harness.status()).get("seen_request") == request["request_id"] else None)
        assert waiting["disparity_profile"] == "comfort"
        assert waiting["applied_request"] != request["request_id"]
        assert waiting["effective_mode"] == "2d"
        harness.allow_ai.set()
        enabled = _wait(lambda: s if (s := harness.status()).get("applied_request") == request["request_id"] else None)
        assert enabled["effective_disparity_profile"] == "comfort"
    finally:
        harness.close()


def test_stale_profile_request_cannot_overwrite_newer_user_selection(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, second_action="static")
    try:
        assert harness.saw_3d.wait(3)
        before = harness.status()
        enabled = apply(harness, tmp_path, disparity_profile="comfort")
        stale = dict(session_id=enabled["session_id"], request_id="stale-profile",
                     mode="3d", disparity=1, disparity_profile="linear", expected_revision=before["revision"])
        atomic_json(tmp_path / "request.json", stale)
        rejected = _wait(lambda: s if (s := harness.status()).get("rejected_request") == "stale-profile" else None)
        assert rejected["revision"] == enabled["revision"]
        assert rejected["disparity_profile"] == "comfort" and rejected["disparity"] == 4
        recovered = apply(harness, tmp_path, mode="3d")
        assert recovered["effective_disparity_profile"] == "comfort"
    finally:
        harness.close()

"""P1 orchestration regressions; injected depth is not evidence of actual AI.

These tests use synthetic RGB and failure/blocking depth engines to isolate
control latency, queue bounds and frame identity. Stereo and session code are
real. Real capture/model/Quest validation belongs to the hardware run records.
"""

from dataclasses import replace
import json
import threading
import time
import traceback
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import session
from quest3d.bridge import ORIGINAL_2D
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session import LatestAIWorker, Processed, select_processed
from quest3d.session_control import atomic_json, read_json, send_control, validate_request
from quest3d.stereo import StereoSynthesizer


def _frame(number=1, generation=0, source_id="test:synthetic", captured_ns=None):
    bgra = np.full((18, 32, 4), (number * 17) % 256, dtype=np.uint8)
    bgra[:, :, 3] = 255
    return CapturedFrame(bgra, time.perf_counter_ns() if captured_ns is None else captured_ns,
                         source_id, number, generation,
                         SourceGeometry(ScreenRect(-320, 25, 32, 18), generation))


def _synthetic_depth(bgra, frame_id, generation):
    tensor = torch.linspace(0, 1, bgra.shape[0] * bgra.shape[1]).reshape(1, *bgra.shape[:2])
    return DepthResult(frame_id, generation, tensor, tuple(bgra.shape[:2]), 0.0, 0.0)


def _processed(frame, revision=3):
    output = StereoSynthesizer(320, 180, disparity_px=4).synthesize(
        frame.bgra, _synthetic_depth(frame.bgra, frame.frame_id, frame.geometry_generation),
        frame_id=frame.frame_id, generation=frame.geometry_generation)
    return Processed(frame, output, revision)


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(.005)
    raise AssertionError("Expected session state was not reached within the test timeout")


def test_requesting_2d_does_not_require_any_ai_result():
    current = _frame()
    assert select_processed(None, requested_mode="2d", revision=99, current_frame=current,
                            now_ns=current.captured_ns, max_age_ms=250) == (None, "requested_2d")


@pytest.mark.parametrize("change", [{"revision": 2}, {"source_id": "test:other"}, {"generation": 7}])
def test_stale_source_or_configuration_never_returns_previous_3d(change):
    source = _frame(captured_ns=1_000_000_000)
    processed = _processed(source, revision=change.get("revision", 3))
    current = _frame(2, generation=change.get("generation", 0),
                     source_id=change.get("source_id", source.source_id), captured_ns=1_050_000_000)
    selected, reason = select_processed(processed, requested_mode="3d", revision=3,
                                       current_frame=current, now_ns=1_100_000_000, max_age_ms=250)
    assert selected is None and reason == "stale_configuration"


def test_older_rgb_and_its_own_depth_are_selected_together_then_expire():
    source = _frame(captured_ns=1_000_000_000)
    processed = _processed(source)
    current = _frame(2, captured_ns=1_100_000_000)
    selected, reason = select_processed(processed, requested_mode="3d", revision=3,
                                       current_frame=current, now_ns=1_200_000_000, max_age_ms=250)
    assert selected is processed and selected.source is source and reason is None
    assert selected.stereo.frame_id == source.frame_id != current.frame_id
    selected, reason = select_processed(processed, requested_mode="3d", revision=3,
                                       current_frame=current, now_ns=1_250_000_001, max_age_ms=250)
    assert selected is None and reason == "stale_depth_frame"


@pytest.mark.parametrize("field", ["frame_id", "generation"])
def test_processed_boundary_rejects_stereo_from_another_rgb_or_geometry(field):
    source = _frame(captured_ns=1_000_000_000)
    processed = _processed(source)
    corrupted = replace(processed, stereo=replace(processed.stereo, **{field: 99}))
    selected, _ = select_processed(corrupted, requested_mode="3d", revision=3, current_frame=source,
                                   now_ns=1_100_000_000, max_age_ms=250)
    assert selected is None


def test_future_capture_time_is_not_treated_as_a_fresh_frame():
    source = _frame(captured_ns=2_000_000_000)
    selected, _ = select_processed(_processed(source), requested_mode="3d", revision=3,
                                   current_frame=source, now_ns=1_000_000_000, max_age_ms=250)
    assert selected is None


def test_static_source_exact_rgb_depth_pair_does_not_expire_without_new_rgb():
    source = _frame(captured_ns=1_000_000_000)
    processed = _processed(source)
    selected, reason = select_processed(processed, requested_mode="3d", revision=3,
                                       current_frame=source, now_ns=100_000_000_000, max_age_ms=250)
    assert selected is processed and reason is None


def test_slow_worker_has_only_one_pending_frame_and_keeps_matching_rgb_depth(tmp_path):
    entered, release = threading.Event(), threading.Event()
    observed = []

    class BlockingEngine:
        def __init__(self, size):
            pass

        def infer(self, bgra, *, frame_id, generation):
            observed.append(frame_id)
            if frame_id == 1:
                entered.set()
                assert release.wait(3)
            return _synthetic_depth(bgra, frame_id, generation)

    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                            directory=tmp_path, engine_factory=BlockingEngine)
    try:
        first = _frame(1)
        worker.submit(first, 0, 4)
        assert entered.wait(3)
        second, third = _frame(2), _frame(3)
        worker.submit(second, 1, 4)
        worker.submit(third, 2, 5)
        assert worker.overwritten == 1
        with worker.condition:
            assert worker.pending[0] is third
        release.set()
        latest = _wait(lambda: worker.snapshot()[0] if worker.frames == 2 else None)
        assert observed == [1, 3]
        assert latest.source is third and latest.revision == 2
        assert latest.stereo.frame_id == third.frame_id
        assert latest.stereo.generation == third.geometry_generation
        assert latest.stereo.bgra.shape == (180, 640, 4)
    finally:
        release.set()
        worker.close()
    worker.submit(_frame(4), 3, 4)
    assert worker.pending is None
    assert not worker.thread.is_alive()


def test_small_valid_eye_size_does_not_fail_worker_initialization(tmp_path):
    class Engine:
        def __init__(self, size):
            pass

        def infer(self, bgra, *, frame_id, generation):
            return _synthetic_depth(bgra, frame_id, generation)

    worker = LatestAIWorker(eye_width=64, eye_height=36, ai_size=280,
                            directory=tmp_path, engine_factory=Engine)
    try:
        worker.submit(_frame(), 0, 1)
        _wait(lambda: worker.snapshot()[0] is not None or worker.snapshot()[1] is not None)
        latest, error, ready = worker.snapshot()
        assert error is None and ready and latest is not None
    finally:
        worker.close()


def test_inline_worker_infers_only_bound_roi_and_preserves_surrounding_desktop(tmp_path):
    from quest3d.inline import InlineGeometry, InlineSynthesizer
    calls = []

    class Engine:
        def __init__(self, size):
            pass
        def infer(self, bgra, *, frame_id, generation):
            calls.append((bgra.copy(), frame_id, generation))
            return _synthetic_depth(bgra, frame_id, generation)

    source = _frame(3)
    source.bgra[..., :3] = np.arange(32, dtype=np.uint8)[None, :, None] * 7
    roi = ScreenRect(-316, 29, 20, 10)
    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280, directory=tmp_path,
                            engine_factory=Engine, inline_rect=roi)
    try:
        worker.submit(source, 9, 10)
        processed = _wait(lambda: worker.snapshot()[0] or worker.snapshot()[1])
        assert not worker.snapshot()[1]
        assert processed.source is source and processed.revision == 9
        np.testing.assert_array_equal(calls[0][0], source.bgra[4:14, 4:24])
        assert calls[0][1:] == (source.frame_id, source.geometry_generation)
        geom = InlineGeometry(source.geometry, roi)
        synth = InlineSynthesizer(320, 180)
        baseline = synth.original_2d(source.bgra, frame_id=3, geometry=geom).bgra
        active = synth.eye_active_rect(geom)
        outside = np.ones((180, 320), dtype=bool)
        outside[active.top:active.bottom, active.left:active.right] = False
        left, right = np.split(processed.stereo.bgra, 2, axis=1)
        np.testing.assert_array_equal(left[outside], baseline[:, :320][outside])
        np.testing.assert_array_equal(right[outside], baseline[:, 320:][outside])
        assert np.any(left[~outside] != right[~outside])
    finally:
        worker.close()


def test_worker_close_discards_pending_work_and_new_worker_starts_cleanly(tmp_path):
    entered, release = threading.Event(), threading.Event()
    observed = []

    class Engine:
        def __init__(self, size):
            pass

        def infer(self, bgra, *, frame_id, generation):
            observed.append(frame_id)
            if frame_id == 1:
                entered.set()
                assert release.wait(3)
            return _synthetic_depth(bgra, frame_id, generation)

    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                            directory=tmp_path / "first", engine_factory=Engine)
    worker.submit(_frame(1), 0, 4)
    assert entered.wait(3)
    worker.submit(_frame(2), 0, 4)
    errors = []

    def stop():
        try:
            worker.close()
        except Exception as exc:
            errors.append(exc)

    closer = threading.Thread(target=stop)
    closer.start()
    try:
        _wait(lambda: worker.closing)
        assert worker.pending is None
    finally:
        release.set()
        closer.join(timeout=3)
    assert not closer.is_alive() and not errors and not worker.thread.is_alive()
    assert observed == [1]
    restarted = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                               directory=tmp_path / "second", engine_factory=Engine)
    try:
        restarted.submit(_frame(3), 7, 4)
        result = _wait(lambda: restarted.snapshot()[0])
        assert result.source.frame_id == 3 and result.revision == 7
        assert observed == [1, 3]
    finally:
        restarted.close()


def _request(**changes):
    value = {"session_id": "live-session", "request_id": "request-1", "mode": "3d", "disparity": 4}
    value.update(changes)
    return value


@pytest.mark.parametrize("payload", [None, [], "invalid", _request(session_id="old-session"),
    _request(command="shell text"), _request(request_id=""), _request(request_id=True),
    _request(mode="stereo"), _request(mode=None), _request(disparity=True), _request(disparity="4"),
    _request(disparity=float("nan")), _request(disparity=float("inf")), _request(disparity=-1),
    _request(disparity=13), _request(disparity=10 ** 400), _request(stop="true")])
def test_invalid_or_foreign_control_is_rejected_as_a_validation_error(payload):
    with pytest.raises(ValueError):
        validate_request(payload, "live-session", 320)


def test_control_submission_is_not_an_applied_ack_and_stopped_session_is_rejected(tmp_path):
    status = {"session_id": "live-session", "running": True, "requested_mode": "3d",
              "disparity": 4, "eye_width": 320, "applied_request": None}
    atomic_json(tmp_path / "status.json", status)
    result = send_control(tmp_path, mode="2d")
    submitted = json.loads((tmp_path / "request.json").read_text("utf-8"))
    assert submitted["request_id"] == result["request_id"] and submitted["mode"] == "2d"
    assert json.loads((tmp_path / "status.json").read_text("utf-8"))["applied_request"] is None
    status["running"] = False
    atomic_json(tmp_path / "status.json", status)
    with pytest.raises(RuntimeError, match="not running"):
        send_control(tmp_path, stop=True)


class _SessionHarness:
    """Synthetic capture/input control harness for blocked/failing AI only."""

    def __init__(self, monkeypatch, directory, *, initial_mode="3d", second_action="block",
                 publisher_busy=False, enable_input=False, max_frame_age_ms=5000, first_capture_age_ms=0,
                 resize_filter="area", poll_capture=False, extra_args=None):
        self.directory = directory
        self.allow_ai = threading.Event()
        self.ai_blocked = threading.Event()
        self.saw_3d = threading.Event()
        self.published, self.errors, self.workers = [], [], []
        self.publisher_closed = False
        self.capture_waits = self.capture_polls = 0
        self.finished = threading.Event()
        self.publications_allowed = threading.Event()
        if not publisher_busy:
            self.publications_allowed.set()
        harness = self

        class Capture:
            def __init__(self, **kwargs):
                self.count = self.dropped_frames = 0

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                pass

            def grab(self, **kwargs):
                if self.count == 0 or (self.count == 1 and harness.saw_3d.is_set() and second_action != "static"):
                    self.count += 1
                    frame = _frame(self.count)
                    if self.count == 1 and first_capture_age_ms:
                        frame = replace(frame, captured_ns=frame.captured_ns - round(first_capture_age_ms * 1e6))
                    if enable_input:
                        from quest3d.source_identity import SourceIdentity, SourceKind
                        frame = replace(frame, source_identity=SourceIdentity(
                            SourceKind.MONITOR, 0, 10, 44, 0, frame.geometry.bounds))
                    return frame
                raise TimeoutError("Synthetic static capture has no new frame")

        if poll_capture:
            original_grab = Capture.grab

            def initial_grab(capture, **kwargs):
                harness.capture_waits += 1
                assert capture.count == 0, "Presentation waited for a subsequent capture"
                return original_grab(capture, **kwargs)

            def try_grab(capture):
                harness.capture_polls += 1
                try:
                    return original_grab(capture)
                except TimeoutError:
                    return None

            Capture.grab = initial_grab
            Capture.try_grab = try_grab

        class Publisher:
            def __init__(self, **kwargs):
                self.skipped = 0
                self.epoch = 1

            def publish(self, bgra, **metadata):
                if not harness.publications_allowed.is_set():
                    self.skipped += 1
                    return False
                harness.published.append((bgra.copy(), metadata))
                if not metadata["flags"] & ORIGINAL_2D:
                    harness.saw_3d.set()
                return True

            def close(self):
                harness.publisher_closed = True

        class InjectedEngine:
            def __init__(self, size):
                pass

            def infer(self, bgra, *, frame_id, generation):
                if frame_id == 2:
                    if second_action == "failure":
                        raise RuntimeError("injected depth failure")
                    harness.ai_blocked.set()
                    assert harness.allow_ai.wait(4)
                return _synthetic_depth(bgra, frame_id, generation)

        def worker_factory(**kwargs):
            worker = LatestAIWorker(**kwargs, engine_factory=InjectedEngine)
            harness.workers.append(worker)
            return worker

        monkeypatch.setattr(session, "GPUDesktopCapture", Capture)
        monkeypatch.setattr(session, "FramePublisher", Publisher)
        monkeypatch.setattr(session, "LatestAIWorker", worker_factory)
        monkeypatch.setattr(session, "ARTIFACT_DIR", directory / "routing")
        self.args = SimpleNamespace(output=str(directory), cpu_threads=1, mode=initial_mode,
            disparity=4, eye_width=320, eye_height=180, ai_size=280, monitor=1,
            rect=None, seconds=5, fps=60, max_frame_age_ms=max_frame_age_ms,
            enable_input=enable_input, bridge_protocol=3 if enable_input else 2, resize_filter=resize_filter)
        if extra_args:
            vars(self.args).update(extra_args)

        def run():
            try:
                session.serve(self.args)
            except BaseException as exc:
                self.errors.append(traceback.format_exc())
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def status(self):
        path = self.directory / "status.json"
        return read_json(path) if path.exists() else {}

    def close(self):
        self.allow_ai.set()
        if self.status().get("running"):
            send_control(self.directory, stop=True)
        assert self.finished.wait(4), "Session failed to stop after the injected engine was released"
        self.thread.join()
        assert not self.errors
        assert not self.status()["running"]
        assert all(not worker.thread.is_alive() for worker in self.workers)


@pytest.mark.parametrize("requested_mode,strength", [("2d", 4), ("3d", 0)])
def test_2d_control_is_applied_while_ai_is_blocked_on_a_newer_frame(monkeypatch, tmp_path, requested_mode, strength):
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3)
        assert harness.ai_blocked.wait(3)
        request = send_control(tmp_path, mode=requested_mode, disparity=strength)
        status = _wait(lambda: state if (state := harness.status()).get("applied_request") == request["request_id"] else None)
        assert not harness.allow_ai.is_set()
        assert status["effective_mode"] == "2d" and status["requested_mode"] == requested_mode
        bgra, metadata = harness.published[-1]
        assert metadata["flags"] & ORIGINAL_2D
        np.testing.assert_array_equal(bgra[:, :320], bgra[:, 320:])
        assert metadata["capture_ns"] > 0
    finally:
        harness.close()


def test_quality_resize_reaches_ai_and_independent_2d_while_ai_is_blocked(monkeypatch, tmp_path):
    constructed = []

    def synth_factory(*args, **kwargs):
        synth = StereoSynthesizer(*args, **kwargs)
        constructed.append(synth.resize_filter)
        return synth

    monkeypatch.setattr(session, "StereoSynthesizer", synth_factory)
    harness = _SessionHarness(monkeypatch, tmp_path, resize_filter="bicubic-aa")
    try:
        assert harness.saw_3d.wait(3)
        assert harness.ai_blocked.wait(3)
        assert constructed == ["bicubic-aa", "bicubic-aa"]
        assert harness.status()["rgb_resize_filter"] == "bicubic-aa"
        request = send_control(tmp_path, mode="2d")
        status = _wait(lambda: state if (state := harness.status()).get("applied_request") == request["request_id"] else None)
        assert status["effective_mode"] == "2d" and status["rgb_resize_filter"] == "bicubic-aa"
        assert not harness.allow_ai.is_set()
        bgra, metadata = harness.published[-1]
        assert metadata["flags"] & ORIGINAL_2D
        np.testing.assert_array_equal(bgra[:, :320], bgra[:, 320:])
    finally:
        harness.close()


def test_static_capture_current_frame_is_resubmitted_after_3d_request(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static")
    try:
        _wait(lambda: harness.published)
        request = send_control(tmp_path, mode="3d")
        _wait(lambda: harness.status().get("applied_request") == request["request_id"])
        assert harness.saw_3d.wait(2), "A static current RGB needs resubmission under the new configuration"
        status = _wait(lambda: state if (state := harness.status()).get("effective_mode") == "3d" else None)
        assert status["ai_frames"] >= 1 and status["revision"] == 1
    finally:
        harness.close()


def test_ai_failure_immediately_falls_back_even_with_a_recent_3d_result(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, second_action="failure")
    try:
        assert harness.saw_3d.wait(3)
        status = _wait(lambda: state if (state := harness.status()).get("ai_error") else None)
        assert "injected depth failure" in status["ai_error"]
        assert status["effective_mode"] == "2d" and status["fallback_reason"] == "ai_error"
        assert harness.published[-1][1]["flags"] & ORIGINAL_2D
    finally:
        harness.close()


def test_invalid_control_does_not_kill_the_session_and_valid_request_recovers(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static")
    try:
        status = _wait(lambda: state if (state := harness.status()).get("running") else None)
        atomic_json(tmp_path / "request.json", _request(session_id=status["session_id"], disparity=10 ** 400))
        rejected = _wait(lambda: state if "control rejected" in str((state := harness.status()).get("error")) else None)
        assert rejected["running"] and rejected["effective_mode"] == "2d"
        request = send_control(tmp_path, mode="2d")
        recovered = _wait(lambda: state if (state := harness.status()).get("applied_request") == request["request_id"] else None)
        assert recovered["running"] and recovered["error"] is None
    finally:
        harness.close()


def test_busy_bridge_is_not_counted_as_published_or_applied_control(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static",
                              publisher_busy=True)
    try:
        _wait(lambda: harness.status().get("publication_attempts", 0) > 0)
        request = send_control(tmp_path, mode="2d")
        status = _wait(lambda: state if (state := harness.status()).get("revision") == 1 else None)
        assert status["applied_request"] is None
        assert status["published_frames"] == 0
        assert status["bridge_skipped"] == status["publication_attempts"] > 0
        harness.publications_allowed.set()
        applied = _wait(lambda: state if (state := harness.status()).get("applied_request") == request["request_id"] else None)
        assert applied["published_frames"] > 0
    finally:
        harness.close()


def test_input_opt_in_is_not_enabled_before_successful_publication(monkeypatch, tmp_path):
    from quest3d.bridge import INPUT_ENABLED
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static",
                              publisher_busy=True, enable_input=True)
    try:
        state = _wait(lambda: state if (state := harness.status()).get("publication_attempts", 0) else None)
        assert state["input_opt_in"] and not state["input_enabled"]
        assert not state["input_last_published_flag"]
        harness.publications_allowed.set()
        _wait(lambda: harness.published)
        # Original capture timestamp is preserved even on repeat publications.
        assert harness.published[0][1]["flags"] & INPUT_ENABLED
        _wait(lambda: any(not row[1]["flags"] & INPUT_ENABLED for row in harness.published))
        stale = _wait(lambda: state if (state := harness.status()).get("input_reason") == "capture_not_fresh" else None)
        assert not stale["input_enabled"]
        assert len({row[1]["capture_ns"] for row in harness.published}) == 1
    finally:
        harness.close()
    assert not harness.status()["input_enabled"]


def test_switch_to_3d_revokes_input_even_while_original_2d_is_presented(monkeypatch, tmp_path):
    from quest3d.bridge import INPUT_ENABLED
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", enable_input=True)
    try:
        _wait(lambda: harness.published)
        assert harness.published[0][1]["flags"] & INPUT_ENABLED
        request = send_control(tmp_path, mode="3d")
        _wait(lambda: harness.status().get("seen_request") == request["request_id"])
        _wait(lambda: harness.ai_blocked.is_set())
        request = send_control(tmp_path, mode="3d", disparity=5)
        _wait(lambda: harness.status().get("seen_request") == request["request_id"])
        assert not harness.status()["input_enabled"]
        assert harness.status()["effective_mode"] == "2d"
        assert not harness.published[-1][1]["flags"] & INPUT_ENABLED
    finally:
        harness.close()


def test_rejected_playback_request_is_not_acknowledged_by_a_desktop_frame(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static")
    try:
        _wait(lambda: harness.published)
        request = send_control(tmp_path, paused=True)
        rejected = _wait(lambda: state if (state := harness.status()).get("rejected_request") == request["request_id"] else None)
        assert rejected["applied_request"] != request["request_id"]
        assert rejected["running"] and rejected["effective_mode"] == "2d"
        good = send_control(tmp_path, mode="2d")
        _wait(lambda: harness.status().get("applied_request") == good["request_id"])
    finally:
        harness.close()


def test_3d_request_is_not_acknowledged_by_a_waiting_original_2d_frame(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path)
    try:
        assert harness.saw_3d.wait(3)
        assert harness.ai_blocked.wait(3)
        request = send_control(tmp_path, mode="3d", disparity=5)
        state = _wait(lambda: state if (state := harness.status()).get("revision") == 1 else None)
        assert state["effective_mode"] == "2d"
        assert state["applied_request"] != request["request_id"]
        harness.allow_ai.set()
        _wait(lambda: harness.status().get("applied_request") == request["request_id"])
    finally:
        harness.close()


def test_stale_expected_revision_cannot_override_a_newer_control(monkeypatch, tmp_path):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d", second_action="static")
    try:
        _wait(lambda: harness.published)
        first = send_control(tmp_path, mode="2d")
        _wait(lambda: harness.status().get("applied_request") == first["request_id"])
        state = harness.status()
        stale = _request(session_id=state["session_id"], expected_revision=0)
        atomic_json(tmp_path / "request.json", stale)
        rejected = _wait(lambda: state if (state := harness.status()).get("rejected_request") == stale["request_id"] else None)
        assert rejected["seen_request"] == first["request_id"]
        assert rejected["applied_request"] == first["request_id"]
        assert rejected["revision"] == 1 and rejected["requested_mode"] == "2d"
    finally:
        harness.close()


def test_file_playout_uses_due_frames_instead_of_bursty_completion_order(tmp_path):
    class Engine:
        def __init__(self, size):
            pass
        def infer(self, bgra, *, frame_id, generation):
            return _synthetic_depth(bgra, frame_id, generation)
    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                            directory=tmp_path, engine_factory=Engine, buffered=True)
    try:
        frames = [_frame(i + 1, captured_ns=1_000_000_000 + i * 40_000_000) for i in range(3)]
        for i, frame in enumerate(frames):
            worker.submit(frame, 0, 4)
            _wait(lambda: worker.frames == i + 1)
        assert worker.snapshot()[0].source.frame_id == 3
        assert worker.due_for_presentation(1_119_999_999, 120_000_000, 0, frames[-1]) is None
        first = worker.due_for_presentation(1_120_000_000, 120_000_000, 0, frames[-1])
        second = worker.due_for_presentation(1_160_000_000, 120_000_000, 0, frames[-1])
        third = worker.due_for_presentation(1_200_000_000, 120_000_000, 0, frames[-1])
        assert [frame.source.frame_id for frame in (first, second, third)] == [1, 2, 3]
        assert all(frame.source.frame_id == frame.stereo.frame_id for frame in (first, second, third))
        assert worker.completed_due_skipped == worker.completed_queue_overwritten == 0
        assert worker.due_for_presentation(2_000_000_000, 0, 1, frames[-1]) is None
    finally:
        worker.close()


def test_file_output_queue_is_bounded_and_reports_late_consumer_loss(tmp_path):
    class Engine:
        def __init__(self, size):
            pass
        def infer(self, bgra, *, frame_id, generation):
            return _synthetic_depth(bgra, frame_id, generation)
    worker = LatestAIWorker(eye_width=320, eye_height=180, ai_size=280,
                            directory=tmp_path, engine_factory=Engine, buffered=True)
    try:
        for i in range(12):
            frame = _frame(i + 1, captured_ns=1_000_000_000 + i * 40_000_000)
            worker.submit(frame, 0, 4)
            _wait(lambda: worker.frames == i + 1)
        assert len(worker.completed_queue) == 8
        assert worker.completed_queue_overwritten == 4
        newest = worker.due_for_presentation(10_000_000_000, 120_000_000, 0, frame)
        assert newest.source.frame_id == 12
        assert worker.completed_due_skipped == 7
        assert len(worker.completed_queue) == 0
    finally:
        worker.close()

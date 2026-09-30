"""Window orchestration with injected WGC, depth and publisher boundaries.

The production selector/session/AI-worker/stereo code runs on synthetic pixels.
No real HWND, capture process, shared bridge, model or desktop cursor is opened.
"""
from dataclasses import replace
import queue
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest
import torch

from quest3d import session, window_session
from quest3d.bridge import CAPTURE_RECEIPT, FULL_SBS, INPUT_ENABLED, ORIGINAL_2D
from quest3d.capture import CapturedFrame
from quest3d.depth import DepthResult
from quest3d.geometry import ScreenRect, SourceGeometry
from quest3d.session import LatestAIWorker
from quest3d.session_control import read_json, send_control
from quest3d.source_identity import SourceIdentity, SourceKind
from quest3d.stereo import StereoSynthesizer
from quest3d.window_capture import WindowCaptureFailed, WindowCaptureUnavailable
from quest3d.window_sources import WindowIdentity, WindowObservation


IDENTITY = WindowIdentity(0x12345, 321, 987654321, 456, "InjectedWindow")


def _args(directory=None, **changes):
    values = dict(window=IDENTITY.hwnd, experimental_window=True, bridge_protocol=3,
                  file=None, rect=None, inline_rect=None, experimental_hdr=False,
                  hdr_tonemap=None, enable_input=False, output=str(directory),
                  cpu_threads=1, mode="3d", disparity=2, eye_width=80, eye_height=48,
                  ai_size=280, monitor=1, seconds=8, fps=40, max_frame_age_ms=5000,
                  hide_cursor=True)
    values.update(changes)
    return SimpleNamespace(**values)


def _observation(**changes):
    return replace(WindowObservation(IDENTITY.hwnd, time.perf_counter_ns(), IDENTITY,
                                    top_level=True, visible=True), **changes)


def _wait(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.005)
    raise AssertionError("Window session did not reach the expected state")


@pytest.mark.parametrize("changes", [
    {"window": None, "experimental_window": True},
    {"window": True}, {"window": "0x12345"}, {"window": 0}, {"window": -1},
    {"window": 1 << 63}, {"experimental_window": False}, {"experimental_window": 1},
    {"bridge_protocol": 2}, {"file": "movie.mp4"}, {"rect": "0,0,100,100"},
    {"inline_rect": "0,0,50,50"}, {"experimental_hdr": True}, {"hdr_tonemap": "fused"},
    {"enable_input": True},
])
def test_window_source_options_reject_ambiguous_or_unauthorized_selection(changes):
    with pytest.raises(ValueError):
        window_session.validate_window_options(_args(**changes))


def test_window_options_keep_monitor_default_and_explicit_candidate_distinct():
    window_session.validate_window_options(_args(window=None, experimental_window=False, bridge_protocol=2))
    window_session.validate_window_options(_args())
    window_session.validate_window_options(_args(window=0x7FFFFFFFFFFFFFFF))


@pytest.mark.parametrize("changes", [{"identity": None}, {"top_level": False}, {"excluded": True}])
def test_selected_window_rejects_ineligible_observation_without_starting_capture(monkeypatch, changes):
    calls = []
    monkeypatch.setattr(window_session, "Win32WindowProvider", lambda **kwargs:
                        SimpleNamespace(observe=lambda *args, **opts: _observation(**changes)))
    monkeypatch.setattr(window_session, "ProcessWindowCapture", lambda *args, **kwargs: calls.append(args))
    with pytest.raises(ValueError, match="eligible top-level"):
        window_session.selected_window_capture(_args())
    assert calls == []


def test_selected_window_preserves_exact_observed_lifetime_without_title_or_reselection(monkeypatch):
    calls = []
    observation = _observation(minimized=True)
    candidate = object()

    class Provider:
        def __init__(self, **kwargs):
            calls.append(("provider", kwargs))

        def observe(self, hwnd, **kwargs):
            calls.append(("observe", hwnd, kwargs))
            return observation

    def capture(identity, **kwargs):
        calls.append(("capture", identity, kwargs))
        assert identity is observation.identity
        return candidate

    monkeypatch.setattr(window_session, "Win32WindowProvider", Provider)
    monkeypatch.setattr(window_session, "ProcessWindowCapture", capture)
    assert window_session.selected_window_capture(_args()) is candidate
    assert calls == [("provider", {"include_titles": False}),
                     ("observe", IDENTITY.hwnd, {"include_occlusion": False}),
                     ("capture", IDENTITY, {"experimental_window": True})]


class _WindowHarness:
    def __init__(self, monkeypatch, directory):
        self.directory = directory
        self.published, self.errors, self.workers = [], [], []
        self.observations, self.selections, self.timeouts = [], [], []
        self.finished = threading.Event()
        self.ai_recovery_entered = threading.Event()
        self.allow_recovery = threading.Event()
        self.publisher_closed = self.cursor_closed = False
        self.capture_exits = []
        self.results = []
        harness = self

        class Capture:
            def __init__(self):
                self.steps = queue.Queue()
                self.dropped_frames = 0
                self.status = {"state": "awaiting_frame", "identity": IDENTITY.hwnd}
                self.unavailable = None

            def __enter__(self):
                return self

            def __exit__(self, kind, value, tb):
                harness.capture_exits.append((kind, value))
                self.status = {"state": "closed", "identity": IDENTITY.hwnd}

            def grab(self, *, timeout_seconds):
                harness.timeouts.append(timeout_seconds)
                try:
                    step = self.steps.get(timeout=0 if self.unavailable else timeout_seconds)
                except queue.Empty:
                    if self.unavailable:
                        raise WindowCaptureUnavailable(self.unavailable)
                    raise TimeoutError("No injected window frame")
                if isinstance(step, WindowCaptureUnavailable):
                    self.unavailable = str(step)
                    self.status = {"state": "unavailable", "reason": self.unavailable}
                    raise step
                if isinstance(step, BaseException):
                    raise step
                self.unavailable = None
                self.status = {"state": "ready", "geometry_generation": step.geometry_generation}
                return step

        self.capture = Capture()

        class Provider:
            def __init__(self, **kwargs):
                assert kwargs == {"include_titles": False}

            def observe(self, hwnd, **kwargs):
                harness.observations.append((hwnd, kwargs))
                return _observation()

        def selected_capture(identity, **kwargs):
            harness.selections.append((identity, kwargs))
            return harness.capture

        class Publisher:
            def __init__(self, **kwargs):
                assert kwargs == {"version": 3}
                self.epoch, self.skipped = 987, 0

            def publish(self, bgra, **metadata):
                harness.published.append((bgra.copy(), metadata))
                return True

            def close(self):
                harness.publisher_closed = True

        class InjectedDepth:
            def __init__(self, size):
                pass

            def infer(self, bgra, *, frame_id, generation):
                if generation == 2:
                    harness.ai_recovery_entered.set()
                    assert harness.allow_recovery.wait(4), "Test must release recovery inference"
                values = torch.linspace(0, 1, bgra.shape[0] * bgra.shape[1]).reshape(1, *bgra.shape[:2])
                return DepthResult(frame_id, generation, values, tuple(bgra.shape[:2]), 0., 0.)

        def worker_factory(**kwargs):
            worker = LatestAIWorker(**kwargs, engine_factory=InjectedDepth)
            harness.workers.append(worker)
            return worker

        class Cursor:
            @staticmethod
            def from_capture(capture, *, enabled, immutable_base=False):
                assert capture is harness.capture and enabled is False
                assert immutable_base is False
                return Cursor()

            def sample_and_composite(self, output, source):
                return output

            def snapshot(self):
                return {"visual_signature": "hidden"}

            def close(self):
                harness.cursor_closed = True

        def reject_desktop(**kwargs):
            raise AssertionError("An explicit HWND must not fall back to desktop capture")

        monkeypatch.setattr(window_session, "Win32WindowProvider", Provider)
        monkeypatch.setattr(window_session, "ProcessWindowCapture", selected_capture)
        monkeypatch.setattr(session, "GPUDesktopCapture", reject_desktop)
        monkeypatch.setattr(session, "FramePublisher", Publisher)
        monkeypatch.setattr(session, "LatestAIWorker", worker_factory)
        monkeypatch.setattr(session, "DesktopCursorOverlay", Cursor)
        monkeypatch.setattr(session, "ARTIFACT_DIR", directory / "routing")
        self.args = _args(directory)

        def run():
            try:
                self.results.append(session.serve(self.args))
            except BaseException as error:
                self.errors.append(error)
            finally:
                self.finished.set()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()

    def status(self):
        path = self.directory / "status.json"
        return read_json(path) if path.exists() else {}

    def frame(self, number, generation=1, bounds=ScreenRect(-200, 50, 32, 18)):
        pixels = np.zeros((bounds.height, bounds.width, 4), np.uint8)
        pixels[..., 0] = np.arange(bounds.width, dtype=np.uint8)[None, :] * 5
        pixels[..., 1] = number * 31
        pixels[..., 2] = np.arange(bounds.height, dtype=np.uint8)[:, None] * 7
        pixels[..., 3] = 255
        return CapturedFrame(pixels, time.perf_counter_ns(), "injected:window-lifetime", number,
                             generation, SourceGeometry(bounds, generation),
                             SourceIdentity(SourceKind.WINDOW, IDENTITY.process_id, 44, IDENTITY.hwnd,
                                            IDENTITY.process_created_filetime, bounds))

    def close(self, *, terminal=False):
        self.allow_recovery.set()
        if not self.finished.is_set() and self.status().get("running"):
            send_control(self.directory, stop=True)
        assert self.finished.wait(3), "Window session and AI worker must finish cleanup"
        self.thread.join()
        assert bool(self.errors) is terminal
        assert not self.status()["running"]
        assert self.publisher_closed and self.cursor_closed
        assert len(self.capture_exits) == 1
        assert all(not worker.thread.is_alive() for worker in self.workers)
        assert self.observations == [(IDENTITY.hwnd, {"include_occlusion": False})]
        assert self.selections == [(IDENTITY, {"experimental_window": True})]


def test_first_window_timeout_keeps_status_and_stop_control_alive(monkeypatch, tmp_path):
    harness = _WindowHarness(monkeypatch, tmp_path)
    try:
        waiting = _wait(lambda: state if (state := harness.status()).get("capture_unavailable") == "awaiting_first_window_frame" else None)
        assert waiting["running"] and waiting["fallback_reason"] == "source_unavailable"
        assert waiting["published_frames"] == 0 and not harness.published
        assert harness.timeouts and all(value <= .1 for value in harness.timeouts)
        request = send_control(tmp_path, stop=True)
        assert harness.finished.wait(1), "An initial WGC timeout must not impose the desktop 3-second wait"
        assert harness.status()["seen_request"] == request["request_id"]
        assert not harness.status()["input_enabled"]
    finally:
        harness.close()


def test_minimized_before_first_frame_is_paced_and_remains_stoppable(monkeypatch, tmp_path):
    harness = _WindowHarness(monkeypatch, tmp_path)
    harness.capture.steps.put(WindowCaptureUnavailable("minimized"))
    try:
        _wait(lambda: harness.status().get("capture_unavailable") == "minimized")
        before = len(harness.timeouts)
        time.sleep(.16)
        assert 1 <= len(harness.timeouts) - before <= 12, "Immediate unavailability needs frame-rate pacing"
        assert not harness.published
    finally:
        harness.close()


def test_unavailable_flattens_retained_rgb_and_recovers_only_new_geometry(monkeypatch, tmp_path):
    harness = _WindowHarness(monkeypatch, tmp_path)
    first = harness.frame(1)
    harness.capture.steps.put(first)
    try:
        _wait(lambda: any(not meta["flags"] & ORIGINAL_2D for _, meta in harness.published))
        start = len(harness.published)
        harness.capture.steps.put(WindowCaptureUnavailable("minimized"))
        retained = _wait(lambda: next(((pixels, meta) for pixels, meta in harness.published[start:]
                                      if not meta["flags"] & CAPTURE_RECEIPT), None))
        pixels, meta = retained
        assert meta["flags"] & ORIGINAL_2D and meta["flags"] & FULL_SBS
        assert not meta["flags"] & INPUT_ENABLED
        assert meta["capture_ns"] == first.captured_ns and meta["generation"] == 1
        assert meta["source_identity"] is first.source_identity
        expected = StereoSynthesizer(80, 48, disparity_px=0).original_2d(
            first.bgra, frame_id=first.frame_id, generation=first.geometry_generation)
        np.testing.assert_array_equal(pixels, expected.bgra)
        np.testing.assert_array_equal(pixels[:, :80], pixels[:, 80:])
        waiting = _wait(lambda: state if (state := harness.status()).get("capture_unavailable") == "minimized" else None)
        assert waiting["effective_mode"] == "2d" and not waiting["input_enabled"]
        # A timeout after minimization is not a successful new capture.
        before = len(harness.published)
        harness.capture.steps.put(TimeoutError("still no fresh frame"))
        _wait(lambda: len(harness.published) >= before + 2)
        assert all(not meta["flags"] & CAPTURE_RECEIPT for _, meta in harness.published[before:])

        recovered = harness.frame(2, generation=2, bounds=ScreenRect(-175, 75, 40, 24))
        harness.capture.steps.put(recovered)
        assert harness.ai_recovery_entered.wait(2)
        fresh = _wait(lambda: next(((pixels, meta) for pixels, meta in harness.published
                                   if meta["generation"] == 2), None))
        pixels, meta = fresh
        assert meta["flags"] & CAPTURE_RECEIPT and meta["flags"] & ORIGINAL_2D
        assert meta["capture_ns"] == recovered.captured_ns
        assert meta["source_rect"] == (-175, 75, 40, 24)
        assert meta["source_identity"] is recovered.source_identity
        expected = StereoSynthesizer(80, 48, disparity_px=0).original_2d(
            recovered.bgra, frame_id=recovered.frame_id, generation=2)
        np.testing.assert_array_equal(pixels, expected.bgra)
        assert all(meta["flags"] & ORIGINAL_2D for _, meta in harness.published if meta["generation"] == 2)
        harness.allow_recovery.set()
        _wait(lambda: any(meta["generation"] == 2 and not meta["flags"] & ORIGINAL_2D
                          for _, meta in harness.published))
        recovered_status = _wait(lambda: state if (state := harness.status()).get("effective_mode") == "3d" and not state.get("capture_unavailable") else None)
        assert recovered_status["window_capture"]["geometry_generation"] == 2
        assert recovered_status["quest_display_verified"] is False
    finally:
        harness.close()


@pytest.mark.parametrize("had_frame", [False, True])
def test_terminal_window_destruction_closes_capture_publisher_and_ai_without_reselection(monkeypatch, tmp_path, had_frame):
    harness = _WindowHarness(monkeypatch, tmp_path)
    try:
        if had_frame:
            harness.capture.steps.put(harness.frame(1))
            _wait(lambda: harness.published)
        else:
            _wait(lambda: harness.status().get("running"))
        harness.capture.steps.put(WindowCaptureFailed("selected HWND destroyed or reused"))
        assert harness.finished.wait(2)
        assert len(harness.errors) == 1 and isinstance(harness.errors[0], WindowCaptureFailed)
        state = harness.status()
        assert not state["running"] and "selected HWND destroyed or reused" in state["error"]
        assert not state["input_enabled"] and not state["quest_display_verified"]
        count = len(harness.published)
        time.sleep(.04)
        assert len(harness.published) == count
        assert harness.capture_exits[0][0] is WindowCaptureFailed
    finally:
        harness.close(terminal=True)

"""Real local A/V decode and session controls with injected AI/IPC boundaries.

Synthetic depth isolates scheduling; these tests are not model or Quest proof.
"""
import json
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from quest3d import file_session
from quest3d.bridge import ORIGINAL_2D
from quest3d.session_control import read_json, send_control
from test_media_audio import make_av
from test_session import _synthetic_depth


def wait_for(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(.005)
    raise AssertionError("File session did not reach the required state")


def setup(monkeypatch, tmp_path, engine, *, mode="3d", paused=False, seconds=.6):
    path = tmp_path / "av.nut"
    make_av(path, duration=2)
    seen = []

    class Publisher:
        skipped, epoch = 0, 101
        def publish(self, bgra, **metadata):
            # Copy: a real encoder must retain/consume its own immutable frame.
            seen.append((bgra.copy(), metadata))
            return True
        def close(self):
            pass

    original = file_session.FileAIWorker
    monkeypatch.setattr(file_session, "FileAIWorker", lambda *a, **kw: original(*a, **kw, engine_factory=engine, gpu_upload=False))
    monkeypatch.setattr(file_session, "FramePublisher", Publisher)
    monkeypatch.setattr(file_session, "ARTIFACT_DIR", tmp_path / "routing")
    args = SimpleNamespace(file=str(path), paused=paused, file_playout_ms=120,
        output=str(tmp_path / "session"), mode=mode, disparity=2, cpu_threads=1,
        eye_width=160, eye_height=90, ai_size=280, seconds=seconds, fps=60)
    return args, seen


@pytest.mark.parametrize("paused", [True, False])
def test_failed_model_releases_only_automatic_preroll_and_never_consumes_pcm(monkeypatch, tmp_path, paused):
    class Failed:
        def __init__(self, _):
            raise RuntimeError("injected missing model")

    args, seen = setup(monkeypatch, tmp_path, Failed, paused=paused)
    result = file_session.serve_file(args)
    assert result["error"] is None and "injected missing model" in result["ai_error"]
    assert not result["media_preroll"] and result["media"]["paused"] is paused
    assert seen and all(meta["flags"] & ORIGINAL_2D for _, meta in seen)
    assert result["media"]["audio_consumed_samples"] == 0
    assert result["media"]["issued_audio_tokens"] == 0
    assert result["media"]["audio_samples"] <= 24000
    positions = [json.loads(line)["pts_ns"] for line in (tmp_path / "session/presentation.jsonl").read_text().splitlines()]
    assert len(set(positions)) == 1 if paused else len(set(positions)) >= 3
    for pixels, _ in seen:
        np.testing.assert_array_equal(pixels[:, :160], pixels[:, 160:])


def test_future_ai_is_presented_on_common_clock_and_exact_source(monkeypatch, tmp_path):
    class Fast:
        def __init__(self, _):
            pass
        def infer(self, bgra, *, frame_id, generation):
            return _synthetic_depth(bgra, frame_id, generation)

    args, _ = setup(monkeypatch, tmp_path, Fast, seconds=.9)
    result = file_session.serve_file(args)
    assert result["error"] is None and result["ai_error"] is None
    rows = [json.loads(line) for line in (tmp_path / "session/presentation.jsonl").read_text().splitlines()]
    # make_av is VFR: only these four frames are due before .9s. Requiring
    # five would demand the future 1.0s frame be presented early.
    assert {row["pts_ns"] for row in rows if row["mode"] == "3d"} == {0, 100_000_000, 350_000_000, 700_000_000}
    assert result["ai_completed_published"] == 4
    assert all(row["pts_ns"] <= row["media_position_ns"] for row in rows)
    inferred = [json.loads(line) for line in (tmp_path / "session/ai/frames.jsonl").read_text().splitlines()]
    accepted = {(row["epoch"], row["frame_id"], row["revision"], row["pts_ns"]) for row in inferred if row["accepted_for_presentation"]}
    assert all((row["generation"], row["source_frame_id"], row["revision"], row["pts_ns"]) in accepted for row in rows if row["mode"] == "3d")
    assert result["media"]["max_video_buffer_bytes"] <= 128 * 1024 * 1024


def test_blocked_ai_does_not_block_2d_pause_seek_or_revision_rejection(monkeypatch, tmp_path):
    entered, release = threading.Event(), threading.Event()
    class Blocked:
        def __init__(self, _):
            pass
        def infer(self, bgra, *, frame_id, generation):
            if frame_id == 1:
                entered.set()
                assert release.wait(6)
            return _synthetic_depth(bgra, frame_id, generation)

    args, seen = setup(monkeypatch, tmp_path, Blocked, seconds=0)
    directory = tmp_path / "session"
    result, errors = [], []
    def run():
        try:
            result.append(file_session.serve_file(args))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    def applied(request):
        state = read_json(directory / "status.json")
        return state if state["applied_request"] == request["request_id"] else None
    try:
        assert entered.wait(4)
        flat = send_control(directory, mode="3d", disparity=0)
        state = wait_for(lambda: applied(flat))
        assert not release.is_set() and state["effective_mode"] == "2d" and not state["media_preroll"]
        assert state["requested_mode"] == "3d" and state["disparity"] == 0
        pause = send_control(directory, paused=True)
        paused = wait_for(lambda: applied(pause))
        assert paused["media"]["paused"]
        seek = send_control(directory, seek_seconds=.6)
        sought = wait_for(lambda: applied(seek))
        assert sought["media"]["epoch"] == 1 and sought["media"]["paused"]
        # There is no .6s frame in this VFR source. A seek resolves both media
        # clocks to the first actual video PTS at/after the requested position.
        assert sought["media"]["position_ns"] == 700_000_000
        release.set()
        # Old in-flight frame cannot be accepted into the new seek/configuration.
        wait_for(lambda: read_json(directory / "status.json")["ai_frames"] >= 1)
        assert read_json(directory / "status.json")["ai_accepted"] == 0
        resume = send_control(directory, mode="3d", disparity=2, paused=False)
        active = wait_for(lambda: applied(resume))
        assert active["media"]["epoch"] == 1 and active["effective_mode"] == "3d"
        assert active["media"]["audio_consumed_samples"] == 0
    finally:
        release.set()
        if thread.is_alive() and (directory / "status.json").exists():
            send_control(directory, stop=True)
        thread.join(7)
    assert not thread.is_alive() and not errors and result and seen
    assert result[0]["media"]["stale_ai_results"] >= 1

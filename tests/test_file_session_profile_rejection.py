"""Actual file decode/control rejection; injected CPU depth is not model/Quest proof."""

import threading

import pytest
import torch

from quest3d import file_session
from quest3d.session_control import atomic_json, read_json, send_control
from test_file_session import setup, wait_for
from test_session import _synthetic_depth


@pytest.mark.parametrize("profile,stop", [("linear", False), ("comfort", False), ("comfort", True)])
def test_direct_profile_json_rejects_whole_request_without_playback_or_ack(monkeypatch, tmp_path, profile, stop):
    old_threads = torch.get_num_threads()
    torch.set_num_threads(2)
    monkeypatch.setattr(torch.cuda, "_lazy_init", lambda: pytest.fail("File control CPU regression cannot initialize CUDA"))
    class CPUModel:
        def __init__(self, _): pass
        def infer(self, image, *, frame_id, generation):
            return _synthetic_depth(image, frame_id, generation)
    args, seen = setup(monkeypatch, tmp_path, CPUModel, mode="2d", paused=True, seconds=0)
    args.cpu_threads = 2
    directory = tmp_path / "session"
    results, errors = [], []
    def run():
        try:
            results.append(file_session.serve_file(args))
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=run)
    thread.start()
    def state():
        return read_json(directory / "status.json") if (directory / "status.json").exists() else {}
    def applied(request):
        current = state()
        return current if current.get("applied_request") == request["request_id"] else None
    try:
        baseline = wait_for(lambda: s if (s := state()).get("running") and seen and not s.get("media_preroll") else None)
        payload = dict(session_id=baseline["session_id"], request_id="unsupported-profile", mode="3d",
                       disparity=5, expected_revision=baseline["revision"], disparity_profile=profile,
                       paused=False, seek_seconds=.6, stop=stop)
        atomic_json(directory / "request.json", payload)
        rejected = wait_for(lambda: s if (s := state()).get("rejected_request") == payload["request_id"] else None)
        assert "disparity profiles are unsupported in file AV playback" in rejected["error"]
        assert rejected["applied_request"] == baseline["applied_request"]
        assert rejected["revision"] == baseline["revision"]
        assert rejected["requested_mode"] == "2d" and rejected["disparity"] == 2
        assert rejected["media"]["epoch"] == baseline["media"]["epoch"] == 0
        assert rejected["media"]["paused"] and rejected["running"] and thread.is_alive()
        assert rejected["media"]["audio_consumed_samples"] == 0
        assert rejected["media"]["issued_audio_tokens"] == 0
        assert "disparity_profile" not in rejected
        # Ordinary mode/strength and subsequent pause/seek remain usable.
        ordinary = send_control(directory, mode="2d", disparity=0, paused=True)
        updated = wait_for(lambda: applied(ordinary))
        assert updated["revision"] == baseline["revision"] + 1 and updated["error"] is None
        assert updated["disparity"] == 0 and updated["media"]["paused"]
        seek = send_control(directory, seek_seconds=.6)
        sought = wait_for(lambda: applied(seek))
        assert sought["revision"] == baseline["revision"] + 2
        assert sought["media"]["epoch"] == 1 and sought["media"]["paused"]
        assert sought["media"]["position_ns"] == 700_000_000
    finally:
        if thread.is_alive() and state().get("running"):
            send_control(directory, stop=True)
        thread.join(7)
        torch.set_num_threads(old_threads)
    assert not thread.is_alive() and not errors and results
    assert not results[0]["running"]

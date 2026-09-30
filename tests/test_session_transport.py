"""New transport options preserve real session transitions and source identity."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from quest3d import session
from quest3d.session_control import send_control
from test_session import _SessionHarness, _wait


@pytest.mark.parametrize("mode", ["2d", "3d"])
def test_immutable_transport_preserves_static_frame_and_control_updates(monkeypatch, tmp_path, mode):
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode=mode,
        second_action="static", poll_capture=True,
        extra_args={"reuse_immutable_payload": True, "trace_cadence": True})
    try:
        _wait(lambda: len(harness.published) >= 6)
        if mode == "3d":
            assert harness.saw_3d.wait(3)
        first_capture = harness.published[0][1]["capture_ns"]
        request = send_control(tmp_path, mode="3d" if mode == "2d" else "2d", disparity=4)
        _wait(lambda: harness.status().get("applied_request") == request["request_id"])
        assert all(meta["immutable_payload"] is True for _, meta in harness.published)
        assert {meta["capture_ns"] for _, meta in harness.published} == {first_capture}
        assert len({meta["frame_id"] for _, meta in harness.published}) == len(harness.published)
        assert harness.capture_waits == 1 and harness.capture_polls >= 5
        assert harness.status()["reuse_immutable_payload"] is True
    finally:
        harness.close()
    captured = [json.loads(line) for line in (tmp_path / "capture-timing.jsonl").read_text().splitlines()]
    assert sum(row["outcome"] == "frame" for row in captured) == 1
    assert any(row["outcome"] == "timeout" for row in captured)
    for row in captured:
        assert row["tick_started_ns"] <= row["capture_started_ns"] <= row["capture_completed_ns"]
    workers = [json.loads(line) for line in (tmp_path / "ai/worker-timing.jsonl").read_text().splitlines()]
    present = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    assert workers
    for row in workers:
        assert row["capture_ns"] <= row["submitted_ns"] <= row["started_ns"] <= row["completed_ns"] <= row["available_ns"]
        published = [p for p in present if p["ai_completion_sequence"] == row["completion_sequence"]]
        for shown in published:
            assert shown["source_frame_id"] == row["frame_id"] and shown["revision"] == row["revision"]
            assert shown["selected_available_ns"] == row["available_ns"] <= shown["snapshot_ns"]
            assert shown["snapshot_ns"] <= shown["publish_started_ns"] <= shown["host_ns"]
    assert any(row["pacing"] for row in present)


@pytest.mark.parametrize("options", [
    {"reuse_immutable_payload": 1}, {"trace_cadence": "on"},
    {"reuse_immutable_payload": True, "file": "example.mp4"},
    {"reuse_immutable_payload": True, "inline_rect": "0,0,20,20"},
])
def test_unverified_payload_contracts_fail_before_output_creation(tmp_path, options):
    output = tmp_path / "not-started"
    with pytest.raises(ValueError):
        session.serve(SimpleNamespace(output=str(output), **options))
    assert not output.exists()


@pytest.mark.parametrize("failed_logs", [
    ("capture-timing.jsonl",), ("presentation.jsonl",),
    ("presentation.jsonl", "capture-timing.jsonl"),
])
def test_trace_close_failures_close_other_resources_and_preserve_stopped_status(monkeypatch, tmp_path, failed_logs):
    real_open = Path.open
    logs = {}

    class CloseFailure:
        def __init__(self, file, name):
            self.file, self.name = file, name
            self.close_calls = 0

        def __getattr__(self, name):
            return getattr(self.file, name)

        def close(self):
            self.close_calls += 1
            self.file.close()
            if self.name in failed_logs:
                raise OSError(f"injected {self.name} close failure")

    def intercepted_open(path, *args, **kwargs):
        file = real_open(path, *args, **kwargs)
        if path.name in ("presentation.jsonl", "capture-timing.jsonl") and args and args[0] == "x":
            logs[path.name] = CloseFailure(file, path.name)
            return logs[path.name]
        return file

    # Preserve an error already collected earlier in shutdown as well as both
    # log-close failures; one failure must not prevent the next cleanup.
    original_cursor_close = session.DesktopCursorOverlay.close

    def cursor_close(cursor):
        original_cursor_close(cursor)
        raise OSError("injected earlier cursor cleanup")

    monkeypatch.setattr(Path, "open", intercepted_open)
    monkeypatch.setattr(session.DesktopCursorOverlay, "close", cursor_close)
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="3d",
        second_action="static", poll_capture=True,
        extra_args={"reuse_immutable_payload": True, "trace_cadence": True})
    try:
        assert harness.saw_3d.wait(3)
    finally:
        harness.close()
    assert harness.publisher_closed
    assert all(not worker.thread.is_alive() for worker in harness.workers)
    assert logs.keys() == {"presentation.jsonl", "capture-timing.jsonl"}
    assert all(log.close_calls == 1 and log.file.closed for log in logs.values())
    state = harness.status()
    assert state["running"] is False
    assert "injected earlier cursor cleanup" in state["error"]
    for name in failed_logs:
        assert f"injected {name} close failure" in state["error"]

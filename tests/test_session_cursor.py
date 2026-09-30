"""Actual session+stereo+cursor composition with a controlled cursor source.

Synthetic capture and cursor samples isolate presentation cadence; this is not
hardware cursor capture or Quest visibility evidence.
"""
from dataclasses import replace
import json
import threading

import numpy as np
import pytest

from quest3d import session
from quest3d.bridge import ORIGINAL_2D
from quest3d.cursor import CursorSample, DesktopCursorOverlay
from test_session import _SessionHarness, _wait


class ControlledCursor:
    def __init__(self):
        pixels = np.full((2, 2, 4), (230, 35, 190, 255), dtype=np.uint8)
        self.value = CursorSample(True, (-315, 30), (0, 0), pixels)
        self.lock = threading.Lock()
        self.closed = False

    def sample(self):
        with self.lock:
            return self.value

    def change(self, **changes):
        with self.lock:
            self.value = replace(self.value, **changes)

    def close(self):
        self.closed = True


@pytest.mark.parametrize("reuse_immutable_payload", [False, True])
def test_static_desktop_cursor_moves_and_hides_without_new_capture_or_depth(monkeypatch, tmp_path, reuse_immutable_payload):
    reader = ControlledCursor()
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    monkeypatch.setattr(session.DesktopCursorOverlay, "from_capture",
                        classmethod(lambda cls, capture, enabled=True, **kwargs: overlay))
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d",
                              second_action="static", enable_input=True,
                              extra_args={"reuse_immutable_payload": reuse_immutable_payload})
    color = np.array((230, 35, 190, 255), dtype=np.uint8)
    try:
        first = _wait(lambda: harness.published[-1] if harness.published else None)
        assert np.array_equal(first[0][50:70, 50:70], np.broadcast_to(color, (20, 20, 4)))
        first_copy = first[0].copy()
        reader.change(position=(-304, 30))
        moved = _wait(lambda: harness.published[-1] if
            np.array_equal(harness.published[-1][0][50, 160], color) else None)
        assert np.array_equal(moved[0][:, :320], moved[0][:, 320:])
        assert np.all(moved[0][50:70, 50:70, :3] == 17), "Old cursor left a trail"
        assert np.array_equal(first[0], first_copy), "A previously published frame was mutated"
        reader.change(visible=False)
        _wait(lambda: np.all(harness.published[-1][0][:, :, :3] == 17))
        state = _wait(lambda: s if (s := harness.status()).get("cursor_changed_published", 0) >= 3 else None)
        assert state["ai_frames"] == 0
        assert state["cursor"]["status"] == "cursor_hidden"
        assert len({metadata["capture_ns"] for _, metadata in harness.published}) == 1
        assert len({metadata["generation"] for _, metadata in harness.published}) == 1
        assert len({metadata["source_identity"] for _, metadata in harness.published}) == 1
        assert state["repeated_frames"] == state["published_frames"] - 1
    finally:
        harness.close()
    assert reader.closed
    assert all(metadata.get("immutable_payload", False) is reuse_immutable_payload
               for _, metadata in harness.published)


def test_cursor_cleanup_error_does_not_leave_session_publisher_or_ai_alive(monkeypatch, tmp_path):
    class FailingClose(ControlledCursor):
        def close(self):
            self.closed = True
            raise OSError("injected cursor cleanup failure")

    reader = FailingClose()
    overlay = DesktopCursorOverlay(reader=reader)
    monkeypatch.setattr(session.DesktopCursorOverlay, "from_capture",
                        classmethod(lambda cls, capture, enabled=True, **kwargs: overlay))
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="2d",
                              second_action="static", enable_input=True)
    _wait(lambda: harness.published)
    harness.close()
    assert reader.closed
    assert harness.publisher_closed
    assert "cursor cleanup failure" in harness.status()["error"]


@pytest.mark.parametrize("reuse_immutable_payload", [False, True])
def test_static_3d_cursor_updates_reuse_one_completed_rgb_depth_pair(monkeypatch, tmp_path, reuse_immutable_payload):
    reader = ControlledCursor()
    overlay = DesktopCursorOverlay(reader=reader, immutable_base=True)
    monkeypatch.setattr(session.DesktopCursorOverlay, "from_capture",
                        classmethod(lambda cls, capture, enabled=True, **kwargs: overlay))
    harness = _SessionHarness(monkeypatch, tmp_path, initial_mode="3d",
                              second_action="static", enable_input=True,
                              extra_args={"reuse_immutable_payload": reuse_immutable_payload})
    color = np.array((230, 35, 190, 255), dtype=np.uint8)
    try:
        assert harness.saw_3d.wait(3), "Real StereoSynthesizer never reached presentation"
        completed = harness.workers[0].snapshot()[0]
        baseline = completed.stereo.bgra.copy()
        source_pixels = completed.source.bgra.copy()
        first = next(row for row in harness.published if not row[1]["flags"] & ORIGINAL_2D)
        assert np.array_equal(first[0][50:70, 50:70], np.broadcast_to(color, (20, 20, 4)))
        first_pixels = first[0].copy()

        reader.change(position=(-304, 30))
        moved = _wait(lambda: harness.published[-1] if
            not harness.published[-1][1]["flags"] & ORIGINAL_2D and
            np.array_equal(harness.published[-1][0][50, 160], color) else None)
        np.testing.assert_array_equal(moved[0][50:70, 50:70], baseline[50:70, 50:70])
        np.testing.assert_array_equal(moved[0][50:70, 370:390], baseline[50:70, 370:390])
        np.testing.assert_array_equal(moved[0][50:70, 160:180], moved[0][50:70, 480:500])
        np.testing.assert_array_equal(first[0], first_pixels)

        reader.change(visible=False)
        _wait(lambda: np.array_equal(harness.published[-1][0], baseline))
        _wait(lambda: s if (s := harness.status()).get("cursor_changed_published", 0) >= 3 else None)
        assert harness.workers[0].snapshot()[0] is completed, "Cursor motion resubmitted AI"
        np.testing.assert_array_equal(completed.stereo.bgra, baseline)
        np.testing.assert_array_equal(completed.source.bgra, source_pixels)
    finally:
        harness.close()

    assert reader.closed and harness.publisher_closed
    assert all(metadata.get("immutable_payload", False) is reuse_immutable_payload
               for _, metadata in harness.published)
    state = harness.status()
    assert state["ai_frames"] == state["ai_completed_published"] == 1
    assert state["ai_completed_unpresented"] == state["ai_pending_overwritten"] == 0
    records = [json.loads(line) for line in (tmp_path / "presentation.jsonl").read_text().splitlines()]
    stereo_records = [row for row in records if row["mode"] == "3d"]
    assert len(stereo_records) >= 3
    assert {row["ai_completion_sequence"] for row in stereo_records} == {completed.completion_sequence}
    assert {row["source_ns"] for row in records} == {completed.source.captured_ns}
    assert {row["source_frame_id"] for row in records} == {completed.source.frame_id}
    assert len({row["generation"] for row in records}) == 1
    assert len({metadata["source_identity"] for _, metadata in harness.published}) == 1
    published = [row for row in records if row["published"]]
    identities = [(row["source_frame_id"], row["generation"], row["mode"], row["revision"])
                  for row in published]
    assert state["repeated_frames"] == sum(a == b for a, b in zip(identities, identities[1:]))
    assert state["cursor_changed_published"] == 3

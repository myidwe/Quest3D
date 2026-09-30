"""Real file decode/session/render EOF checks with an explicit media clock.

Only AI creation, IPC publication and the clock are injected. The unknown-end
case removes decoded duration metadata, retaining real FFV1 pixels and PTS.
These tests do not exercise a model, audio sink, encoder, network or Quest.
"""
from dataclasses import fields
from fractions import Fraction
import json
import os
from pathlib import Path
import threading
import time
from types import SimpleNamespace

import av
import numpy as np
import pytest

from quest3d import file_session, media, media_playout, session, subtitle_session
from quest3d.session_control import send_control
from quest3d.stereo import StereoFrame


LAST_PTS_NS = 250_000_000
VIDEO_END_NS = 350_000_000


class Clock:
    def __init__(self):
        self.value = time.perf_counter_ns()

    def __call__(self):
        return self.value


class UnusedAI:
    def __init__(self, _):
        pass

    def infer(self, *_args, **_kwargs):
        raise AssertionError("The Original2D EOF test must not request AI")


@pytest.fixture
def short_video(tmp_path):
    path = tmp_path / "subtitle-eof.nut"
    with av.open(str(path), "w", format="nut") as output:
        stream = output.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 32, "bgra"
        stream.time_base = stream.codec_context.time_base = Fraction(1, 1000)
        for index, pts in enumerate((0, 100, 250)):
            pixels = np.full((32, 64, 4), (20 + 30 * index, 45, 90, 255), np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="bgra")
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    # The container omits duration; the actual final decoded frame declares it.
    reader = media.MediaReader(path)
    try:
        assert reader.duration_ns is None
        decoded_pts = []
        while True:
            try:
                decoded_pts.append(reader.next()[1])
            except StopIteration:
                break
        assert decoded_pts == [0, 100_000_000, LAST_PTS_NS]
        assert reader.duration_ns == VIDEO_END_NS
    finally:
        reader.close()
    return path


def run_eof_case(monkeypatch, tmp_path, short_video, *, common_clock, known_end):
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts/malgun.ttf"
    if not font.is_file():
        pytest.skip("Requires the installed Windows Malgun Gothic font; no font is redistributed")
    caption = tmp_path / "long-last-cue.srt"
    caption.write_text("1\n00:00:00,000 --> 00:00:10,000\n마지막 자막\n", encoding="utf-8")
    clock, decoder_eof = Clock(), threading.Event()
    directory = tmp_path / "session"
    owners, readers, rendered, published, checkpoints = [], [], [], [], []
    stage, after_unknown_end = "first", 0

    class Reader(media.MediaReader):
        def __init__(self, path):
            super().__init__(path)
            readers.append(self)
            if not known_end:
                assert self.duration_ns is None
                decoded_frames = self.frames
                self.frames = (SimpleNamespace(pts=f.pts, time_base=f.time_base,
                    duration=None, colorspace=f.colorspace, color_range=f.color_range,
                    format=f.format, to_ndarray=f.to_ndarray) for f in decoded_frames)

        def next(self):
            try:
                return super().next()
            except StopIteration:
                decoder_eof.set()
                raise

    class Overlay(subtitle_session.FileSubtitleOverlay):
        def composite(self, frame, pts_ns, *, eof=False):
            original = frame.bgra.copy()
            output = super().composite(frame, pts_ns, eof=eof)
            assert self.error is None
            np.testing.assert_array_equal(frame.bgra, original)
            for field in fields(StereoFrame):
                if field.name != "bgra":
                    assert getattr(output, field.name) == getattr(frame, field.name)
            rendered.append({"pts_ns": pts_ns, "input": frame, "original": original,
                "output": output, "diagnostics": dict(self.renderer.last_diagnostics)})
            return output

    def position(value):
        owner = owners[0]
        with owner.condition:
            assert not owner.timeline.paused
            clock.value = owner.timeline.host_anchor_ns + value - owner.timeline.media_anchor_ns
            owner.condition.notify_all()

    class Publisher:
        skipped, epoch = 0, 701

        def publish(self, bgra, **metadata):
            nonlocal stage, after_unknown_end
            record = rendered[-1]
            diag = record["diagnostics"]
            assert bgra is record["output"].bgra
            assert metadata["generation"] == record["output"].generation
            assert metadata["content_rect"] == record["output"].content_rect
            published.append(record)
            if stage == "first":
                assert record["pts_ns"] == 0 and diag["cue_indices"] == [1]
                position(LAST_PTS_NS)
                stage = "last"
            elif stage == "last" and record["pts_ns"] == LAST_PTS_NS:
                assert diag["cue_indices"] == [1] and not diag["eof"]
                if known_end:
                    position(VIDEO_END_NS - 1)
                    stage = "before_end"
                else:
                    # Legacy pauses at unknown decoder EOF. Advance the host
                    # clock anyway; the caption must still use this RGB PTS.
                    clock.value += 3_000_000_000
                    stage = "unknown_end"
            elif stage == "before_end" and decoder_eof.is_set():
                assert record["pts_ns"] == LAST_PTS_NS
                assert not diag["eof"] and diag["cue_indices"] == [1]
                assert diag["overlay_rect"] is not None
                checkpoints.append(("before_end", record))
                position(VIDEO_END_NS)
                stage = "at_end"
            elif stage == "at_end" and diag["eof"]:
                assert record["pts_ns"] == LAST_PTS_NS and diag["cue_indices"] == []
                assert diag["overlay_rect"] is None
                np.testing.assert_array_equal(bgra, record["original"])
                checkpoints.append(("at_end", record))
                send_control(directory, stop=True)
                stage = "stopped"
            elif stage == "unknown_end" and decoder_eof.is_set():
                assert readers[0].duration_ns is None
                assert record["pts_ns"] == LAST_PTS_NS
                assert not diag["eof"] and diag["cue_indices"] == [1]
                assert diag["overlay_rect"] is not None
                after_unknown_end += 1
                if after_unknown_end == 3:
                    checkpoints.append(("unknown_end", record))
                    send_control(directory, stop=True)
                    stage = "stopped"
            return True

        def close(self):
            pass

    monkeypatch.setattr(media, "MediaReader", Reader)
    monkeypatch.setattr(media_playout, "MediaReader", Reader)
    monkeypatch.setattr(subtitle_session, "FileSubtitleOverlay", Overlay)
    monkeypatch.setattr(session, "FileSubtitleOverlay", Overlay)
    if common_clock:
        player_type, worker_type = file_session.FileAVPlayback, file_session.FileAIWorker

        def player_factory(*args, **kwargs):
            owner = player_type(*args, **kwargs, now_ns=clock)
            owners.append(owner)
            return owner

        monkeypatch.setattr(file_session, "FileAVPlayback", player_factory)
        monkeypatch.setattr(file_session, "FileAIWorker", lambda *a, **kw:
            worker_type(*a, **kw, engine_factory=UnusedAI, gpu_upload=False))
        target = file_session
        serve = file_session.serve_file
    else:
        # Replace this module's clock object, never the shared stdlib time module.
        monkeypatch.setattr(media, "time", SimpleNamespace(perf_counter_ns=clock))
        source_type, worker_type = media.FileVideoSource, session.LatestAIWorker

        def source_factory(*args, **kwargs):
            owner = source_type(*args, **kwargs)
            owners.append(owner)
            return owner

        monkeypatch.setattr(media, "FileVideoSource", source_factory)
        monkeypatch.setattr(session, "LatestAIWorker", lambda **kw: worker_type(**kw, engine_factory=UnusedAI))
        target, serve = session, session.serve
    monkeypatch.setattr(target, "FramePublisher", Publisher)
    monkeypatch.setattr(target, "ARTIFACT_DIR", tmp_path / "routing")
    result = serve(SimpleNamespace(file=str(short_video), output=str(directory),
        file_av_clock=common_clock, file_native_pcm=False, file_playout_ms=120, paused=False,
        mode="2d", disparity=0, cpu_threads=1, eye_width=320, eye_height=180,
        ai_size=280, seconds=5, fps=60, max_frame_age_ms=200,
        subtitles=str(caption), subtitle_font=str(font), subtitle_size=16))
    assert stage == "stopped", f"EOF checkpoint was not reached: {stage}; {result}"
    assert result["error"] is None and result["ai_error"] is None
    assert result["subtitles"]["error"] is None and result["ai_frames"] == 0
    rows = [json.loads(line) for line in (directory / "presentation.jsonl").read_text().splitlines()]
    assert len(rows) == len(published)
    for row, record in zip(rows, published, strict=True):
        assert row["pts_ns"] == record["pts_ns"] == row["subtitles"]["last"]["pts_ns"]
        assert row["source_frame_id"] == record["input"].frame_id
        assert row["generation"] == record["input"].generation
        diag = record["diagnostics"]
        if rect := diag["overlay_rect"]:
            x, y, width, height = rect
            output = record["output"].bgra
            np.testing.assert_array_equal(output[y:y+height, x:x+width], output[y:y+height, 320+x:320+x+width])
            assert not np.array_equal(output, record["original"])
        np.testing.assert_array_equal(record["input"].bgra, record["original"])
    return result, dict(checkpoints)


@pytest.mark.parametrize("common_clock", [False, True], ids=["legacy", "common_av"])
def test_long_last_cue_clears_only_at_known_video_end_and_restores_cached_original(
        monkeypatch, tmp_path, short_video, common_clock):
    result, checkpoints = run_eof_case(monkeypatch, tmp_path, short_video,
                                     common_clock=common_clock, known_end=True)
    before, after = checkpoints["before_end"], checkpoints["at_end"]
    assert before["input"] is after["input"]  # Repeated publication used the same clean cache.
    np.testing.assert_array_equal(after["output"].bgra, before["original"])
    assert result["subtitles"]["last"]["eof"] is True


@pytest.mark.parametrize("common_clock", [False, True], ids=["legacy", "common_av"])
def test_unknown_final_duration_does_not_fabricate_eof_or_remove_long_last_cue(
        monkeypatch, tmp_path, short_video, common_clock):
    result, checkpoints = run_eof_case(monkeypatch, tmp_path, short_video,
                                     common_clock=common_clock, known_end=False)
    assert checkpoints["unknown_end"]["diagnostics"]["eof"] is False
    assert result["subtitles"]["last"]["cue_indices"] == [1]
    state = result["media"]
    if common_clock:
        assert state["video_eof"] and not state["video_presentation_finished"]
        assert state["video_duration_ns"] is None
    else:
        assert state["eof"] and state["duration_seconds"] is None

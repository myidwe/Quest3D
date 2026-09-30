"""Real local codec/PTS and bounded playback tests; no AI/Quest claims."""
from fractions import Fraction
import time
import threading

import av
import numpy as np
from PIL import Image
import pytest

from quest3d.media import FileVideoSource, MediaReader, PlaybackTimeline, seconds_ns


@pytest.fixture
def video(tmp_path):
    path = tmp_path / "variable.mkv"
    with av.open(str(path), "w") as output:
        stream = output.add_stream("ffv1", rate=10)
        stream.width, stream.height, stream.pix_fmt = 64, 32, "bgra"
        stream.time_base = Fraction(1, 1000)
        stream.codec_context.time_base = Fraction(1, 1000)
        for i, pts in enumerate([0, 100, 300, 500, 900, 1200, 1600, 1900]):
            pixels = np.full((32, 64, 4), (i * 25, 40, 80, 255), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(pixels, format="bgra")
            frame.pts, frame.time_base = pts, Fraction(1, 1000)
            for packet in stream.encode(frame):
                output.mux(packet)
        for packet in stream.encode():
            output.mux(packet)
    return path


@pytest.mark.parametrize("paused", [False, True])
def test_file_ai_startup_failure_resumes_only_automatic_preroll(monkeypatch, tmp_path, video, paused):
    from types import SimpleNamespace
    from quest3d import session
    from quest3d.bridge import ORIGINAL_2D
    seen = []

    class FailedModel:
        def __init__(self, size):
            raise RuntimeError("injected unavailable model")

    class Publisher:
        skipped, epoch = 0, 1
        def publish(self, bgra, **metadata):
            seen.append((int(bgra[0, 0, 0]), metadata["flags"]))
            return True
        def close(self):
            pass

    worker_type = session.LatestAIWorker
    monkeypatch.setattr(session, "LatestAIWorker", lambda **kw: worker_type(**kw, engine_factory=FailedModel))
    monkeypatch.setattr(session, "FramePublisher", Publisher)
    monkeypatch.setattr(session, "ARTIFACT_DIR", tmp_path / "routing")
    args = SimpleNamespace(file=str(video), paused=paused, file_playout_ms=120,
        output=str(tmp_path / "session"), mode="3d", disparity=4, cpu_threads=1,
        eye_width=320, eye_height=160, ai_size=280, seconds=.65, fps=30, max_frame_age_ms=200)
    result = session.serve(args)
    assert "injected unavailable model" in result["ai_error"]
    assert not result["media_preroll"]
    assert all(flags & ORIGINAL_2D for _, flags in seen)
    assert len({pixel for pixel, _ in seen}) == 1 if paused else len({pixel for pixel, _ in seen}) >= 3
    assert result["media"]["paused"] is paused


def test_clock_pause_resume_seek_preserves_position_without_wall_time_drift():
    clock = PlaybackTimeline(1_000_000_000)
    assert clock.position(1_500_000_000) == 500_000_000
    clock.set_paused(True, 1_500_000_000)
    assert clock.position(90_000_000_000) == 500_000_000
    clock.set_paused(False, 90_000_000_000)
    assert clock.position(90_100_000_000) == 600_000_000
    clock.seek(250_000_000, 90_100_000_000)
    assert clock.epoch == 1
    assert clock.deadline(350_000_000) == 90_200_000_000


@pytest.mark.parametrize("value", [True, -1, float("inf"), float("nan"), "2", 10 ** 400])
def test_invalid_seek_is_rejected(value):
    with pytest.raises(ValueError):
        seconds_ns(value)


def test_real_vfr_decoder_preserves_pts_and_pixels(video):
    reader = MediaReader(video)
    try:
        actual = [reader.next() for _ in range(8)]
        assert [pts for _, pts in actual] == [n * 1_000_000 for n in [0, 100, 300, 500, 900, 1200, 1600, 1900]]
        for i, (image, _) in enumerate(actual):
            assert image.shape == (32, 64, 4)
            assert np.all(image == (i * 25, 40, 80, 255))
        with pytest.raises(StopIteration):
            reader.next()
        reader.seek(900_000_000)
        seek_frames = []
        while True:
            image, pts = reader.next()
            seek_frames.append(pts)
            if pts >= 900_000_000:
                break
        assert seek_frames[-1] == 900_000_000
    finally:
        reader.close()


def test_missing_stream_duration_uses_only_the_final_decoded_duration(video):
    reader = MediaReader(video)
    try:
        assert reader.duration_ns is None
        frames = [reader.next() for _ in range(8)]
        assert frames[-1][1] == 1_900_000_000
        assert reader.duration_ns is None  # Decoder EOF is not known yet.
        with pytest.raises(StopIteration):
            reader.next()
        assert reader.duration_ns == 2_000_000_000
    finally:
        reader.close()


def test_paused_player_seek_publishes_new_epoch_and_no_unbounded_queue(video):
    with FileVideoSource(video, paused=True) as source:
        first = source.grab()
        assert first.pts_ns == 0
        with pytest.raises(TimeoutError):
            source.grab(.05)
        source.control(seek_seconds=.9)
        selected = source.grab()
        assert selected.pts_ns == 900_000_000
        assert selected.timeline_epoch == selected.geometry_generation == 1
        assert selected.frame_id > first.frame_id
        assert np.all(selected.bgra[:, :, 0] == 100)
        with pytest.raises(TimeoutError):
            source.grab(.05)
        source.control(paused=False)
        next_frame = source.grab(.8)
        assert next_frame.pts_ns == 1_200_000_000
        assert source.playback_status()["audio_playback_integrated"] is False


def test_slow_consumer_gets_latest_due_frame_and_reports_drops(video):
    with FileVideoSource(video) as source:
        source.grab()
        time.sleep(.55)
        current = source.grab()
        assert current.pts_ns >= 300_000_000
        assert source.dropped_frames >= 1
        assert source.latest is current
        assert current.captured_ns <= time.perf_counter_ns()


def test_photo_orientation_alpha_and_seek_epoch(tmp_path):
    path = tmp_path / "alpha.png"
    Image.new("RGBA", (20, 10), (200, 100, 40, 128)).save(path)
    with FileVideoSource(path) as source:
        first = source.grab()
        assert first.bgra.shape == (10, 20, 4)
        assert np.all(first.bgra == (20, 50, 100, 255))
        source.control(seek_seconds=0)
        assert source.grab().timeline_epoch == 1
        with pytest.raises(ValueError):
            source.control(seek_seconds=1)


def test_missing_and_invalid_files_fail_without_stalling_presentation(tmp_path):
    path = tmp_path / "invalid.mkv"
    path.write_bytes(b"not a video")
    with FileVideoSource(path) as source:
        with pytest.raises(RuntimeError):
            source.grab()
    with FileVideoSource(tmp_path / "missing.mp4") as source:
        with pytest.raises(RuntimeError):
            source.grab()


def test_eof_freezes_at_declared_duration_and_play_restarts_epoch(video):
    with FileVideoSource(video) as source:
        source.grab()
        deadline = time.monotonic() + 3
        while not source.playback_status()["eof"] and time.monotonic() < deadline:
            time.sleep(.01)
        state = source.playback_status()
        assert state["eof"] and state["paused"]
        if state["duration_seconds"] is not None:
            assert state["position_seconds"] == state["duration_seconds"]
        source.grab(.1)  # Consume the last old-epoch frame before replay.
        source.control(paused=False)
        restarted = source.grab()
        assert restarted.timeline_epoch == 1 and restarted.pts_ns == 0


def test_paused_vfr_seek_never_publishes_a_future_timestamp(video):
    with FileVideoSource(video, paused=True) as source:
        source.grab()
        source.control(seek_seconds=.6)
        selected = source.grab()
        assert selected.pts_ns == 900_000_000
        assert source.host_presentation_ns(selected) <= time.perf_counter_ns()
        assert source.playback_status()["position_seconds"] == .9
        source.control(paused=False)
        assert source.host_presentation_ns(selected) <= time.perf_counter_ns()


def test_blocked_libav_seek_does_not_block_status_pause_or_stop_control(video, monkeypatch):
    entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
    original_seek = MediaReader.seek
    def slow_seek(reader, position_ns):
        entered.set()
        assert release.wait(3)
        original_seek(reader, position_ns)
    monkeypatch.setattr(MediaReader, "seek", slow_seek)
    with FileVideoSource(video, paused=True) as source:
        source.grab()
        source.control(seek_seconds=.6)
        assert entered.wait(1)
        def control():
            source.playback_status()
            source.control(paused=True, seek_seconds=1.2)
            controlled.set()
        thread = threading.Thread(target=control)
        thread.start()
        try:
            assert controlled.wait(.5), "File IO held the control lock"
        finally:
            release.set()
            thread.join(1)
        selected = source.grab()
        assert selected.timeline_epoch == 2
        assert selected.pts_ns == 1_200_000_000

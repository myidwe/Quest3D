"""Generated A/V files exercise real demux/decode/resampling; no playback claims."""
from fractions import Fraction
import threading
import time
from types import SimpleNamespace

import av
import numpy as np
import pytest

from quest3d.media import MediaReader
from quest3d.media_audio import FileAudioSource, MediaAudioReader, RATE


def make_av(path, *, rate=48_000, duration=2, audio_start=Fraction(7, 8), video_start=Fraction(1),
            mono=False, gap_after=None, gap=Fraction(0), container="nut", codec="pcm_f32le"):
    """Known stereo signal and VFR video with explicit rational source timestamps."""
    total = round(duration * rate)
    indices = np.arange(total)
    left = (np.sin(2 * np.pi * 440 * indices / rate) * .25).astype(np.float32)
    right = (np.cos(2 * np.pi * 997 * indices / rate) * .125).astype(np.float32)
    expected = np.column_stack((left, right)).astype(np.float32)
    packets = []
    with av.open(str(path), "w", format=container) as output:
        video = output.add_stream("ffv1", rate=10)
        video.width, video.height, video.pix_fmt = 32, 16, "bgra"
        video.time_base = video.codec_context.time_base = Fraction(1, 1000)
        audio = output.add_stream(codec, rate=rate)
        audio.layout = "mono" if mono else "stereo"
        audio.time_base = audio.codec_context.time_base = Fraction(1, rate)
        for count, offset in enumerate((0, 100, 350, 700, 1000, 1500, 2000)):
            image = np.full((16, 32, 4), (count * 25, 20, 40, 255), dtype=np.uint8)
            frame = av.VideoFrame.from_ndarray(image, format="bgra")
            frame.pts = round(video_start * 1000) + offset
            frame.time_base = Fraction(1, 1000)
            packets.extend(video.encode(frame))
        packets.extend(video.encode())
        for offset in range(0, total, 441 if rate == 44_100 else 480):
            samples = expected[offset:offset + (441 if rate == 44_100 else 480)]
            data = samples[:, :1] if mono else samples
            frame = av.AudioFrame.from_ndarray(data.reshape(1, -1).copy(), format="flt", layout=audio.layout.name)
            frame.sample_rate, frame.time_base = rate, Fraction(1, rate)
            frame.pts = round(audio_start * rate) + offset
            if gap_after is not None and offset >= gap_after:
                frame.pts += round(gap * rate)
            packets.extend(audio.encode(frame))
        packets.extend(audio.encode())
        packets.sort(key=lambda packet: (packet.dts * packet.time_base, packet.stream.index))
        for packet in packets:
            output.mux(packet)
    return expected


def collect(reader):
    result = []
    while True:
        try:
            result.append(reader.next())
        except StopIteration:
            return result


def await_state(source, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = source.status()
        if state["error"]:
            raise RuntimeError(state["error"])
        if predicate(state):
            return state
        time.sleep(.005)
    raise AssertionError(f"State did not converge: {source.status()}")


@pytest.fixture
def av_file(tmp_path):
    path = tmp_path / "known-av.nut"
    expected = make_av(path)
    return path, expected


def test_real_stereo_pcm_and_negative_audio_lead_keep_video_timebase(av_file):
    path, expected = av_file
    video = MediaReader(path)
    try:
        _, video_pts = video.next()
        assert video_pts == 0
        with MediaAudioReader(path) as reader:
            blocks = collect(reader)
            assert reader.video_origin == video.origin == 1
            assert reader.audio_start == Fraction(7, 8)
            assert blocks[0].absolute_pts == Fraction(7, 8)
            assert blocks[0].pts_ns == -125_000_000
            assert blocks[0].discontinuity == "start"
            assert blocks[-1].end_absolute_pts == Fraction(23, 8)
            assert sum(len(block.samples) for block in blocks) == len(expected)
            np.testing.assert_array_equal(np.concatenate([block.samples for block in blocks]), expected)
            assert all(block.samples.dtype == np.float32 and block.samples.flags.c_contiguous
                       and not block.samples.flags.writeable and 0 < len(block.samples) <= 480 for block in blocks)
            assert all(b.absolute_pts == a.end_absolute_pts for a, b in zip(blocks, blocks[1:]))
    finally:
        video.close()


def test_explicit_shared_video_origin_is_not_rebased_to_first_decoded_video_pts(av_file):
    path, _ = av_file
    # This explicitly supplied shared origin differs from the file's first video
    # frame (1s). It tests the caller contract, not fabricated container metadata.
    with MediaAudioReader(path, video_origin=Fraction(3, 4)) as reader:
        first = reader.next()
        assert reader.video_origin == Fraction(3, 4)
        assert first.absolute_pts == Fraction(7, 8)
        assert first.pts_ns == 125_000_000


@pytest.mark.parametrize("metadata_start,expected_origin", [(500, Fraction(1, 2)), (None, Fraction(1))])
def test_origin_policy_prefers_video_start_and_only_falls_back_to_first_pts(av_file, monkeypatch, metadata_start, expected_origin):
    # Deliberate stream metadata injection covers start_time != first decoded
    # video PTS and absent metadata. Pixel/PCM decode still uses the real file.
    real_open = av.open
    class Container:
        def __init__(self, *args, **kwargs):
            self.actual = real_open(*args, **kwargs)
            self.video = SimpleNamespace(start_time=metadata_start, time_base=Fraction(1, 1000))
            self.streams = SimpleNamespace(video=[self.video], audio=self.actual.streams.audio)
        def decode(self, stream):
            return self.actual.decode(self.actual.streams.video[0] if stream is self.video else stream)
        def close(self):
            self.actual.close()
        def __enter__(self):
            return self
        def __exit__(self, *_):
            self.close()
    monkeypatch.setattr(av, "open", Container)
    with MediaAudioReader(av_file[0]) as reader:
        assert reader.video_origin == expected_origin
        first = reader.next()
        assert first.pts_ns == round((first.absolute_pts - expected_origin) * 1e9)


def test_actual_44100_to_48000_resampler_flush_preserves_total_duration(tmp_path):
    path = tmp_path / "rate-conversion.nut"
    make_av(path, rate=44_100, duration=1)
    with MediaAudioReader(path) as reader:
        blocks = collect(reader)
        assert reader.resampler_flush_samples > 0
        assert sum(len(block.samples) for block in blocks) == 48_000
        assert blocks[-1].end_absolute_pts - blocks[0].absolute_pts == 1
        assert all(b.absolute_pts == a.end_absolute_pts for a, b in zip(blocks, blocks[1:]))
        assert reader.eof
        with pytest.raises(StopIteration):
            reader.next()
        values = np.concatenate([block.samples for block in blocks])
        assert np.isfinite(values).all()
        assert np.sqrt(np.mean(values ** 2)) > .05


def test_mono_resamples_to_equal_left_right_without_output_device(tmp_path):
    path = tmp_path / "mono.nut"
    make_av(path, mono=True, duration=1)
    with MediaAudioReader(path) as reader:
        samples = np.concatenate([block.samples for block in collect(reader)])
        assert samples.shape == (48_000, 2)
        np.testing.assert_array_equal(samples[:, 0], samples[:, 1])
        assert np.max(np.abs(samples)) > .1


def test_actual_gap_is_reported_without_silence_fill_or_old_filter_tail_leak(tmp_path):
    path = tmp_path / "gap.nut"
    make_av(path, rate=44_100, duration=1, gap_after=22_050, gap=Fraction(1, 10))
    with MediaAudioReader(path) as reader:
        blocks = collect(reader)
        gaps = [block for block in blocks if block.discontinuity == "gap"]
        assert len(gaps) == 1
        assert gaps[0].discontinuity_ns == 100_000_000
        # 7/8 second is halfway between two 44.1k input samples. The encoder
        # rounded the declared input PTS, then resampling rounds to a 48k grid.
        input_start = Fraction(round(Fraction(7, 8) * 44_100), 44_100)
        expected_gap_pts = Fraction(round((input_start + Fraction(3, 5)) * RATE), RATE)
        assert gaps[0].absolute_pts == expected_gap_pts
        assert sum(len(block.samples) for block in blocks) == 48_000
        position = next(index for index, block in enumerate(blocks) if block is gaps[0])
        assert gaps[0].absolute_pts - blocks[position - 1].end_absolute_pts == Fraction(1, 10)
        assert reader.discontinuities == 1


@pytest.mark.parametrize("rate", [48_000, 44_100])
def test_seek_trims_before_target_and_resets_epoch_and_eof(tmp_path, rate):
    path = tmp_path / f"seek-{rate}.nut"
    make_av(path, rate=rate, duration=2)
    with MediaAudioReader(path) as reader:
        original = collect(reader)
        assert reader.eof
        target_ns = 1_333_333_333
        reader.seek(target_ns, 4)
        selected = reader.next()
        assert selected.epoch == 4 and selected.discontinuity == "seek"
        assert selected.pts_ns >= target_ns
        assert selected.pts_ns - target_ns <= 1e9 / RATE + 1
        assert selected.absolute_pts == selected.video_origin + Fraction(selected.pts_ns, 1_000_000_000) or abs(
            selected.absolute_pts - selected.video_origin - Fraction(selected.pts_ns, 1_000_000_000)) <= Fraction(1, 2_000_000_000)
        assert reader.trimmed_samples > 0 and not reader.eof
        baseline = np.concatenate([block.samples for block in original])
        index = round((selected.absolute_pts - original[0].absolute_pts) * RATE)
        np.testing.assert_allclose(selected.samples, baseline[index:index + len(selected.samples)], atol=1e-6)
        with pytest.raises(ValueError, match="newer"):
            reader.seek(0, 4)
        reader.seek(0, 9)
        assert reader.next().epoch == 9


def test_quantized_matroska_pts_do_not_generate_spurious_discontinuities(tmp_path):
    path = tmp_path / "quantized.mkv"
    make_av(path, rate=44_100, duration=1, container="matroska")
    with MediaAudioReader(path) as reader:
        blocks = collect(reader)
        assert reader.discontinuities == 0
        assert sum(len(block.samples) for block in blocks) == 48_000


def test_actual_aac_audio_keeps_decoder_pts_and_padding_visible(tmp_path):
    path = tmp_path / "aac.mkv"
    make_av(path, duration=1, container="matroska", codec="aac", audio_start=Fraction(1))
    with av.open(str(path)) as reference:
        decoded = list(reference.decode(reference.streams.audio[0]))
    with MediaAudioReader(path) as reader:
        blocks = collect(reader)
        assert sum(len(block.samples) for block in blocks) == sum(frame.samples for frame in decoded)
        # Container precision is1ms and AAC encoder priming may precede the
        # requested start. Preserve decoder truth, not a fabricated one-second clip.
        expected_start = Fraction(decoded[0].pts) * decoded[0].time_base
        assert abs(blocks[0].absolute_pts - expected_start) <= Fraction(1, RATE)
        assert reader.discontinuities == 0
        assert all(b.absolute_pts == a.end_absolute_pts for a, b in zip(blocks, blocks[1:]))
        assert max(np.max(np.abs(block.samples)) for block in blocks) > .1
        duration = Fraction(sum(len(block.samples) for block in blocks), RATE)
        assert blocks[-1].end_absolute_pts - blocks[0].absolute_pts == duration
        assert reader.max_timestamp_adjustment_ns > 0
        assert reader.max_timestamp_adjustment_ns <= 1_000_000 + round(1e9 / RATE)
        for block in blocks:
            assert round((block.absolute_pts - block.resampler_pts) * 1e9) == block.timestamp_adjustment_ns


def test_paused_worker_queue_bound_and_seek_discards_old_epoch(av_file):
    path, _ = av_file
    with FileAudioSource(path, paused=True) as source:
        await_state(source, lambda state: state["ready"])
        assert source.status()["queued_samples"] == 0
        with pytest.raises(TimeoutError):
            source.read(.02)
        source.control(paused=False)
        state = await_state(source, lambda state: state["queued_samples"] == 24_000)
        assert state["high_water_samples"] <= 24_000 and state["buffer_ms"] == 500
        source.control(paused=True)
        frozen = source.status()["queued_samples"]
        time.sleep(.03)
        assert source.status()["queued_samples"] == frozen
        source.control(seek_ns=750_000_000, epoch=3)
        assert source.status()["queued_samples"] == 0
        with pytest.raises(TimeoutError):
            source.read(.02)
        source.control(paused=False)
        block = source.read()
        assert block.epoch == 3 and block.pts_ns >= 750_000_000
        assert source.status()["audio_playback_integrated"] is False


def test_blocked_seek_does_not_hold_control_lock_and_latest_epoch_wins(av_file, monkeypatch):
    path, _ = av_file
    entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
    real_seek = MediaAudioReader.seek
    def slow_seek(reader, target, epoch):
        entered.set()
        assert release.wait(3)
        return real_seek(reader, target, epoch)
    monkeypatch.setattr(MediaAudioReader, "seek", slow_seek)
    with FileAudioSource(path, paused=True) as source:
        await_state(source, lambda state: state["ready"])
        source.control(seek_ns=200_000_000, epoch=1)
        assert entered.wait(1)
        def control():
            source.status()
            source.control(paused=True, seek_ns=900_000_000, epoch=2)
            controlled.set()
        thread = threading.Thread(target=control)
        thread.start()
        try:
            assert controlled.wait(.5), "libav seek held the PCM control lock"
        finally:
            release.set()
            thread.join(1)
        source.control(paused=False)
        block = source.read()
        assert block.epoch == 2 and block.pts_ns >= 900_000_000


def test_worker_drains_partial_eof_without_padding_then_seek_restarts(tmp_path):
    path = tmp_path / "short.nut"
    make_av(path, duration=Fraction(1001, RATE))
    with FileAudioSource(path) as source:
        blocks = []
        while True:
            try:
                blocks.append(source.read())
            except StopIteration:
                break
        assert sum(len(block.samples) for block in blocks) == 1001
        assert len(blocks[-1].samples) == 41
        source.control(seek_ns=0, epoch=2)
        # This file's audio ends before video origin; seek0 truthfully reaches EOF.
        with pytest.raises(StopIteration):
            source.read()


def test_worker_eof_keeps_queued_audio_then_replay_uses_new_epoch(tmp_path):
    path = tmp_path / "eof-replay.nut"
    make_av(path, duration=Fraction(1001, RATE), audio_start=Fraction(1))
    with FileAudioSource(path) as source:
        state = await_state(source, lambda state: state["decoder_eof"])
        assert state["queued_samples"] == 1001 and not state["drained"]
        assert sum(len(source.read().samples) for _ in range(3)) == 1001
        with pytest.raises(StopIteration):
            source.read()
        assert source.status()["drained"]
        source.control(seek_ns=0, epoch=5)
        restarted = source.read()
        assert restarted.epoch == 5 and restarted.pts_ns == 0 and restarted.discontinuity == "seek"


def test_blocked_decode_and_pause_do_not_leak_old_samples_after_seek(av_file, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_next = MediaAudioReader.next
    calls = [0]
    def stalled(reader):
        calls[0] += 1
        if calls[0] == 1:
            entered.set()
            assert release.wait(3)
        return real_next(reader)
    monkeypatch.setattr(MediaAudioReader, "next", stalled)
    with FileAudioSource(av_file[0]) as source:
        assert entered.wait(1)
        try:
            source.control(paused=True, seek_ns=500_000_000, epoch=7)
            assert source.status()["queued_samples"] == 0
        finally:
            release.set()
        with pytest.raises(TimeoutError):
            source.read(.03)
        source.control(paused=False)
        block = source.read()
        assert block.epoch == 7 and block.pts_ns >= 500_000_000


def test_overlapping_and_missing_pts_are_explicit_policy_failures(av_file):
    # Fault injection only: invalid timestamp sequences are not claimed to be
    # ordinary encoded media. Resampling still executes actual libav code.
    def frame(pts):
        result = av.AudioFrame.from_ndarray(np.full((1, 960), .125, dtype=np.float32), format="flt", layout="stereo")
        result.pts, result.time_base, result.sample_rate = pts, Fraction(1, RATE), RATE
        return result
    with MediaAudioReader(av_file[0]) as reader:
        reader.frames = iter([frame(48000), frame(48240)])  # 5ms overlap of two 10ms blocks.
        reader.blocks = reader._decode_blocks("start", None)
        blocks = collect(reader)
        overlaps = [block for block in blocks if block.discontinuity == "overlap"]
        assert len(overlaps) == 1 and overlaps[0].discontinuity_ns == -5_000_000
        assert overlaps[0].absolute_pts == Fraction(201, 200)
        assert sum(len(block.samples) for block in blocks) == 960
    with MediaAudioReader(av_file[0]) as reader:
        reader.frames = iter([frame(None)])
        reader.blocks = reader._decode_blocks("start", None)
        with pytest.raises(ValueError, match="no presentation timestamp"):
            reader.next()


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), 10 ** 400, "1"])
def test_invalid_read_timeout_is_value_error(av_file, value):
    source = FileAudioSource(av_file[0])
    with pytest.raises(ValueError):
        source.read(value)


@pytest.mark.parametrize("control", [{"paused": 1}, {"seek_ns": 1}, {"epoch": 1},
                                     {"seek_ns": -1, "epoch": 1}, {"seek_ns": 0, "epoch": True}])
def test_invalid_controls_do_not_change_state(av_file, control):
    source = FileAudioSource(av_file[0], paused=True)
    before = source.status()
    with pytest.raises(ValueError):
        source.control(**control)
    assert source.status() == before


def test_missing_file_and_audio_stream_are_explicit_errors(tmp_path):
    with pytest.raises(FileNotFoundError):
        MediaAudioReader(tmp_path / "missing.nut")
    path = tmp_path / "empty.wav"
    path.write_bytes(b"invalid media")
    with FileAudioSource(path) as source:
        with pytest.raises(RuntimeError):
            source.read()
    photo = tmp_path / "silent.png"
    from PIL import Image
    Image.new("RGB", (16, 16), "black").save(photo)
    with pytest.raises(ValueError, match="audio stream"):
        MediaAudioReader(photo)


def test_stop_during_blocked_decode_does_not_enqueue_after_close(av_file, monkeypatch):
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()
    original = MediaAudioReader.next
    def blocked(reader):
        entered.set()
        assert release.wait(3)
        return original(reader)
    monkeypatch.setattr(MediaAudioReader, "next", blocked)
    source = FileAudioSource(av_file[0]).__enter__()
    assert entered.wait(1)
    thread = threading.Thread(target=lambda: (source.__exit__(), stopped.set()))
    thread.start()
    try:
        deadline = time.monotonic() + 1
        while not source.closing and time.monotonic() < deadline:
            time.sleep(.005)
        assert source.closing
    finally:
        release.set()
        thread.join(2)
    assert stopped.is_set()
    assert source.status()["queued_samples"] == 0
    assert not source.thread.is_alive()

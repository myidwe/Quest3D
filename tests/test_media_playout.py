"""Actual generated A/V decode with a controlled presentation clock; no output."""
from fractions import Fraction
import threading
import time

import numpy as np
import pytest

from quest3d.media import MediaReader
from quest3d.media_audio import MediaAudioReader, RATE
from quest3d.media_playout import FileAIRequest, FileAVPlayback, FileRGB
from quest3d.stereo import StereoFrame
from test_media_audio import make_av


class Clock:
    def __init__(self):
        self.value = 10_000_000_000
    def __call__(self):
        return self.value
    def advance(self, nanoseconds):
        self.value += nanoseconds


def test_known_video_end_waits_for_pcm_consumption_then_freezes_exact_end(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        # NUT omits stream duration; the real decoded final frame explicitly
        # lasts100ms after its2s PTS. No nominal-FPS end is substituted.
        player.start()
        clock.advance(3_000_000_000)
        wait_for(player, lambda state: state["video_eof"])
        player.tick()
        assert not player.finish_if_drained()  # PCM cannot be assumed consumed.
        assert player.status()["video_presentation_finished"]  # Captions can end while PCM is still pending.
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            for packet in player.audio_window():
                player.ack_audio(packet.token, packet.total_samples)
            if player.status()["audio_eof"] and player.status()["audio_samples"] == 0:
                break
            time.sleep(.005)
        assert player.finish_if_drained()
        state = player.status()
        assert state["finished"] and state["paused"] and state["position_ns"] == 2_100_000_000
        clock.advance(100_000_000_000)
        assert player.status()["position_ns"] == 2_100_000_000
        player.set_paused(False)  # Play at the confirmed end is a new replay epoch.
        assert not player.status()["finished"] and player.status()["epoch"] == 1
        assert player.status()["preroll"] and player.status()["position_ns"] == 0
        assert not player.status()["video_presentation_finished"]


def test_unknown_last_video_duration_is_not_fabricated_at_eof(source, monkeypatch):
    from types import SimpleNamespace
    from quest3d import media_playout

    class MissingDurationReader(MediaReader):
        def __init__(self, path):
            super().__init__(path)
            original_frames = self.frames
            # Preserve real decoding/PTS/pixels but simulate a decoder that
            # does not supply per-frame durations either.
            self.frames = (SimpleNamespace(pts=f.pts, time_base=f.time_base, duration=None,
                                           colorspace=f.colorspace, color_range=f.color_range,
                                           format=f.format, to_ndarray=f.to_ndarray) for f in original_frames)

    monkeypatch.setattr(media_playout, "MediaReader", MissingDurationReader)
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        player.start()
        clock.advance(3_000_000_000)
        wait_for(player, lambda state: state["video_eof"])
        player.tick()
        assert player.duration_ns is None
        assert not player.finish_if_drained() and not player.status()["finished"]
        assert not player.status()["video_presentation_finished"]


def typed_result(request, *, frame_id=None, generation=None):
    """Typed synthetic transport fixture; no depth/model inference is claimed."""
    frame = request.frame
    return StereoFrame(np.concatenate((frame.bgra, frame.bgra), axis=1),
                       frame.frame_id if frame_id is None else frame_id,
                       frame.epoch if generation is None else generation, "3d", False, None)


def wait_for(player, predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        state = player.status()
        if state["error"]:
            raise RuntimeError(state["error"])
        if predicate(state):
            return state
        time.sleep(.005)
    raise AssertionError(f"Player state did not converge: {player.status()}")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "av.nut"
    make_av(path, duration=2)
    return path


def test_final_pcm_batch_is_atomic_and_respects_packet_limit(tmp_path):
    path = tmp_path / "short-audio.nut"
    make_av(path, duration=.03, audio_start=Fraction(1))
    with FileAVPlayback(path) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_eof"])
        player.start()
        packets, final = player.audio_batch(max_packets=1)
        assert len(packets) == 1 and not final
        assert player.ack_audio(packets[0].token, packets[0].total_samples)
        packets, final = player.audio_batch()
        assert len(packets) == 2 and final
        assert sum(packet.total_samples for packet in packets) == 960


def test_preroll_holds_both_media_clocks_until_exact_first_ai_is_ready(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        clock.advance(20_000_000_000)
        assert player.status()["position_ns"] == 0
        assert player.audio_window() == []
        with pytest.raises(RuntimeError, match="preroll"):
            player.start(require_ai=True)
        first = player.next_ai_frame()
        assert first.frame.pts_ns == 0
        payload = typed_result(first)
        assert player.submit_ai(first, payload)
        player.start(require_ai=True)
        shown = player.tick(mode="3d")
        assert shown.source is first.frame and shown.processed is payload
        assert shown.clock.video_origin == Fraction(1)
        assert shown.clock.position(clock()) == 0
        packets = player.audio_window(max_packets=1)
        assert packets and packets[0].pts_ns == 0 and packets[0].due_ns == clock()
        assert player.status()["audio_trimmed_before_start"] == 6000


def test_future_ai_rgb_is_not_presented_early_and_2d_3d_share_same_clock(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: state["video_frames"] >= 2 and player.ready_for_start())
        first, future = player.next_ai_frame(), player.next_ai_frame()
        assert future.frame.pts_ns == 100_000_000
        first_result, future_result = typed_result(first), typed_result(future)
        assert player.submit_ai(first, first_result)
        assert player.submit_ai(future, future_result)
        player.start()
        clock.advance(50_000_000)
        assert player.tick(mode="2d").source is first.frame
        assert player.tick(mode="3d").source is first.frame
        clock.advance(60_000_000)
        stereo = player.tick(mode="3d")
        flat = player.tick(mode="2d")
        assert stereo.source is flat.source is future.frame
        assert stereo.processed is future_result and flat.processed is None
        assert stereo.clock == flat.clock
        assert stereo.clock.deadline(future.frame.pts_ns) == 10_100_000_000


def test_pause_remaps_partially_consumed_pcm_without_missing_or_duplicate_samples(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        packet = player.audio_window(max_packets=1)[0]
        assert len(packet.samples) == 240  # Trimmed initial 5ms of the source packet.
        original = packet.samples.copy()
        assert player.ack_audio(packet.token, 120)
        clock.advance(2_500_000)
        transition = player.begin_transition(paused=True)
        player.commit_transition(transition, [], flush_confirmed=True)
        position = player.clock_snapshot().position(clock())
        assert position == 2_500_000
        assert not player.ack_audio(packet.token, 240)
        clock.advance(20_000_000_000)
        assert player.audio_window() == []
        assert player.clock_snapshot().position(clock()) == position
        player.set_paused(False)
        resumed = player.audio_window(max_packets=1)[0]
        assert resumed.token != packet.token
        assert resumed.offset_samples == 120 and resumed.pts_ns == 2_500_000
        assert resumed.due_ns == clock()
        np.testing.assert_array_equal(resumed.samples, original[120:])
        assert player.ack_audio(resumed.token, resumed.total_samples)
        # Decode runs on another thread. Consuming the first block does not
        # synchronously decode the second; await real data without advancing
        # the media clock or fabricating/acknowledging any samples.
        wait_for(player, lambda state: state["audio_samples"] > 0)
        next_packet = player.audio_window(max_packets=1)[0]
        assert next_packet.absolute_pts == resumed.absolute_pts + Fraction(len(resumed.samples), 48_000)


def test_vfr_seek_resolves_one_common_pts_and_rejects_old_ai_audio(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        old = player.next_ai_frame()
        player.start()
        old_audio = player.audio_window(max_packets=1)[0]
        transition = player.begin_transition(seek_ns=600_000_000, paused=True)
        epoch = player.commit_transition(transition, [], flush_confirmed=True).epoch
        assert epoch == 1
        assert not player.submit_ai(old, typed_result(old))
        assert not player.ack_audio(old_audio.token, len(old_audio.samples))
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        assert player.clock_snapshot().media_anchor_ns == 700_000_000
        selected = player.tick(mode="2d").source
        assert selected.epoch == 1 and selected.pts_ns == 700_000_000
        player.start()
        assert player.audio_window() == []
        player.set_paused(False)
        audio = player.audio_window(max_packets=1)[0]
        assert audio.token[1] == 1 and audio.pts_ns == 700_000_000
        assert audio.due_ns == clock()


def test_slow_ai_reports_fallback_for_due_rgb_instead_of_showing_future_or_old_ai(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: state["video_frames"] >= 2 and player.ready_for_start())
        first = player.next_ai_frame()
        old_result = typed_result(first)
        player.submit_ai(first, old_result)
        player.start()
        assert player.tick(mode="3d").processed is old_result
        clock.advance(110_000_000)
        shown = player.tick(mode="3d")
        assert shown.source.pts_ns == 100_000_000
        assert shown.mode == "2d" and shown.processed is None and shown.fallback_reason == "ai_underflow"
        assert player.status()["ai_underflow_ticks"] == 1
        assert not player.submit_ai(first, typed_result(first))


def test_decode_ahead_and_unacknowledged_pcm_remain_bounded(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock, lookahead_ms=500, max_audio_samples=960,
                        max_video_frames=3, max_video_bytes=32 * 16 * 4 * 3) as player:
        wait_for(player, lambda state: state["audio_samples"] >= 720 and state["video_frames"] == 2)
        player.start()
        first = player.audio_window()
        clock.advance(10_000_000_000)
        for _ in range(3):
            player.audio_window()
        time.sleep(.05)
        state = player.status()
        assert state["audio_samples"] <= 960 and state["max_audio_buffer_samples"] <= 960
        assert state["video_bytes"] <= 32 * 16 * 4 * 3
        assert state["video_frames"] <= 3
        assert player.audio_window()[0].token == first[0].token
        assert state["max_audio_lateness_ns"] >= 10_000_000_000


def test_blocked_video_seek_keeps_pause_and_superseding_global_seek_responsive(source, monkeypatch):
    clock = Clock()
    entered, release, controlled = threading.Event(), threading.Event(), threading.Event()
    real_seek = MediaReader.seek
    def blocked(reader, target):
        entered.set()
        assert release.wait(3)
        return real_seek(reader, target)
    monkeypatch.setattr(MediaReader, "seek", blocked)
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        player.seek(100_000_000)
        assert entered.wait(1)
        def control():
            player.set_paused(True)
            player.seek(350_000_000)
            player.status()
            controlled.set()
        thread = threading.Thread(target=control)
        thread.start()
        try:
            assert controlled.wait(.5), "libav held the common clock lock"
        finally:
            release.set()
            thread.join(1)
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        assert player.tick().source.epoch == 2
        assert player.clock_snapshot().media_anchor_ns == 350_000_000


def test_slow_video_io_exposes_decode_underflow_and_retains_last_matching_frame(source, monkeypatch):
    clock = Clock()
    entered, release = threading.Event(), threading.Event()
    real_next = MediaReader.next
    calls = [0]
    def blocked(reader):
        calls[0] += 1
        if calls[0] == 2:
            entered.set()
            assert release.wait(3)
        return real_next(reader)
    monkeypatch.setattr(MediaReader, "next", blocked)
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        assert entered.wait(1)
        player.start()
        first = player.tick().source
        clock.advance(500_000_000)
        try:
            shown = player.tick()
            assert shown.source is first and shown.fallback_reason == "video_decode_wait"
            assert player.status()["video_underflow_ticks"] == 1
        finally:
            release.set()


def test_forged_rgb_identity_and_pcm_ack_cannot_advance_playout(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        real = player.next_ai_frame()
        frame = real.frame
        forged = FileAIRequest(FileRGB(frame.bgra.copy(), frame.pts_ns, frame.epoch, frame.frame_id), real.ai_revision)
        assert not player.submit_ai(forged, typed_result(forged))
        player.start()
        packet = player.audio_window(max_packets=1)[0]
        with pytest.raises(ValueError):
            player.ack_audio(packet.token, packet.total_samples + 1)
        with pytest.raises(ValueError):
            player.ack_audio([0, 0, 1], 1)
        assert not player.ack_audio((player.session_id, 0, 0, 999999), 1)
        assert player.status()["audio_consumed_samples"] == 0


def test_replay_restarts_both_readers_with_new_epoch(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        original = player.audio_window(max_packets=1)[0].samples.copy()
        transition = player.begin_transition(seek_ns=1_000_000_000)
        player.commit_transition(transition, [], flush_confirmed=True)
        wait_for(player, lambda state: player.ready_for_start())
        assert player.replay() == 2
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        assert player.tick().source.pts_ns == 0
        packet = player.audio_window(max_packets=1)[0]
        assert packet.token[1] == 2 and packet.pts_ns == 0
        np.testing.assert_array_equal(packet.samples, original)


def test_pcm_ack_history_is_bounded_while_consumer_drains_real_file(tmp_path):
    path = tmp_path / "longer.nut"
    make_av(path, duration=3, audio_start=Fraction(1))
    clock = Clock()
    with FileAVPlayback(path, now_ns=clock, lookahead_ms=500) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        count = 0
        for _ in range(8):
            clock.advance(400_000_000)
            player.tick()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                packets = player.audio_window()
                for packet in packets:
                    assert player.ack_audio(packet.token, packet.total_samples)
                    count += len(packet.samples)
                state = player.status()
                if state["audio_eof"] and state["audio_samples"] == 0:
                    break
                if not packets:
                    time.sleep(.005)
                if state["audio_samples"] == 0 and count / RATE >= state["position_ns"] / 1e9 + .45:
                    break
            if player.status()["audio_eof"] and player.status()["audio_samples"] == 0:
                break
        state = player.status()
        assert count == 3 * RATE
        assert state["retired_ack_tokens"] <= 128 and state["issued_audio_tokens"] == 0
        epoch = player.replay()
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        assert player.audio_window(max_packets=1)[0].token[1] == epoch


def test_pending_audio_decode_after_vfr_resolution_is_trimmed_to_resolved_pts(source, monkeypatch):
    clock = Clock()
    blocked, release = threading.Event(), threading.Event()
    real_next = MediaAudioReader.next
    def slow_after_seek(reader):
        if reader.epoch == 1 and not blocked.is_set():
            blocked.set()
            assert release.wait(3)
        return real_next(reader)
    monkeypatch.setattr(MediaAudioReader, "next", slow_after_seek)
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        player.seek(600_000_000)
        assert blocked.wait(1)
        wait_for(player, lambda state: state["position_ns"] == 700_000_000 and state["video_frames"] > 0)
        release.set()
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        assert player.audio_window(max_packets=1)[0].pts_ns == 700_000_000


def test_seek_beyond_unknown_duration_reports_exhausted_preroll_and_replay_recovers(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start())
        assert player.duration_ns is None  # NUT fixture has no video stream duration.
        player.seek(50_000_000_000)
        wait_for(player, lambda state: state["preroll_exhausted"])
        assert not player.ready_for_start()
        with pytest.raises(RuntimeError, match="preroll"):
            player.start()
        player.replay()
        wait_for(player, lambda state: player.ready_for_start())
        assert player.tick().source.pts_ns == 0


@pytest.mark.parametrize("value", [True, -1, float("nan"), 10 ** 400, "1"])
def test_invalid_ai_timeout_does_not_wait_or_mutate_clock(source, value):
    player = FileAVPlayback(source)
    original = player.clock_snapshot()
    with pytest.raises(ValueError):
        player.next_ai_frame(value)
    assert player.clock_snapshot() == original


def test_missing_file_worker_reports_error_without_consuming_pcm(tmp_path):
    with FileAVPlayback(tmp_path / "missing.mp4") as player:
        deadline = time.monotonic() + 2
        while player.status()["error"] is None and time.monotonic() < deadline:
            time.sleep(.005)
        with pytest.raises(RuntimeError, match="FileNotFoundError"):
            player.tick()
        assert player.status()["audio_consumed_samples"] == 0


def test_ack_from_another_file_session_cannot_consume_identical_new_block_ids(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as first, FileAVPlayback(source, now_ns=clock) as second:
        for player in (first, second):
            wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
            player.start()
        a, b = first.audio_window(max_packets=1)[0], second.audio_window(max_packets=1)[0]
        assert a.token[1:] == b.token[1:] and a.token[0] != b.token[0]
        assert not second.ack_audio(a.token, a.total_samples)
        assert second.status()["audio_consumed_samples"] == 0


def test_ai_result_must_match_rgb_id_epoch_and_current_configuration_revision(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: state["video_frames"] >= 2 and player.ready_for_start())
        a, b = player.next_ai_frame(), player.next_ai_frame()
        with pytest.raises(ValueError, match="different RGB"):
            player.submit_ai(a, typed_result(b))
        with pytest.raises(ValueError, match="different RGB"):
            player.submit_ai(a, typed_result(a, generation=1))
        with pytest.raises(ValueError, match="StereoFrame"):
            player.submit_ai(a, "not a typed result")
        previous_frames = player.status()["video_frames"]
        player.set_ai_revision(1)
        assert player.status()["video_frames"] == previous_frames
        assert not player.submit_ai(a, typed_result(a))
        new = player.next_ai_frame()
        assert new.frame is a.frame and new.ai_revision == 1
        result = typed_result(new)
        assert player.submit_ai(new, result)
        assert player.tick(mode="3d").processed is result
        player.set_ai_revision(2)
        assert player.tick(mode="3d").processed is None
        with pytest.raises(ValueError):
            player.set_ai_revision(1)


def test_late_consumed_ack_during_pause_barrier_is_applied_before_generation_changes(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        packet = player.audio_window(max_packets=1)[0]
        with pytest.raises(RuntimeError, match="flush barrier"):
            player.set_paused(True)
        clock.advance(2_500_000)
        transition = player.begin_transition(paused=True)
        assert player.clock_snapshot().generation == packet.token[2]
        assert player.audio_window() == []
        # The native consumer already consumed120 samples, but its ack arrives
        # after the pause request. The old generation is still valid here.
        assert player.ack_audio(packet.token, 120)
        player.commit_transition(transition, [(packet.token, 120)], flush_confirmed=True)
        assert player.clock_snapshot().generation > packet.token[2]
        assert not player.ack_audio(packet.token, 240)
        player.set_paused(False)
        remaining = player.audio_window(max_packets=1)[0]
        assert remaining.offset_samples == 120
        np.testing.assert_array_equal(remaining.samples, packet.samples[120:])


def test_unconfirmed_or_invalid_flush_keeps_clock_frozen_without_partial_ack_mutation(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        packet = player.audio_window(max_packets=1)[0]
        transition = player.begin_transition(paused=True)
        before = player.clock_snapshot()
        clock.advance(100_000_000_000)
        with pytest.raises(ValueError, match="unconfirmed"):
            player.commit_transition(transition, [(packet.token, 120)], flush_confirmed=False)
        with pytest.raises(ValueError, match="exceeds"):
            player.commit_transition(transition, [(packet.token, 120), (packet.token, 480)], flush_confirmed=True)
        assert player.status()["audio_consumed_samples"] == 0
        assert player.clock_snapshot() == before
        assert player.audio_window() == []
        with pytest.raises(ValueError, match="confirmed"):
            player.restart_after_transport_reset(transition, reset_confirmed=False)
        epoch = player.restart_after_transport_reset(transition, reset_confirmed=True)
        assert epoch == 1 and player.status()["transport_reset_restarts"] == 1
        assert not player.ack_audio(packet.token, 120)
        wait_for(player, lambda state: player.ready_for_start())
        assert player.tick().source.epoch == epoch


def test_final_ack_flush_barrier_is_bound_to_exact_transition_and_cannot_commit_twice(source):
    clock = Clock()
    with FileAVPlayback(source, now_ns=clock) as player:
        wait_for(player, lambda state: player.ready_for_start() and state["audio_samples"] > 0)
        player.start()
        packet = player.audio_window(max_packets=1)[0]
        transition = player.begin_transition(seek_ns=350_000_000)
        with pytest.raises(RuntimeError, match="already pending"):
            player.begin_transition(paused=True)
        player.commit_transition(transition, [(packet.token, packet.total_samples)], flush_confirmed=True)
        with pytest.raises(ValueError, match="already committed"):
            player.commit_transition(transition, [], flush_confirmed=True)
        wait_for(player, lambda state: player.ready_for_start())
        assert player.clock_snapshot().epoch == 1 and player.tick().source.pts_ns == 350_000_000

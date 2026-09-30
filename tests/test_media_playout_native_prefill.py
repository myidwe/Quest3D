"""Real file decoding with a held clock; no native/device READY or output ACK."""
from fractions import Fraction

import numpy as np
import pytest

from quest3d.media_audio import RATE
from quest3d.media_playout import FileAVPlayback
from test_media_audio import make_av
from test_media_playout import Clock, typed_result, wait_for


def scope(clock):
    return clock.session_id, clock.epoch, clock.generation


@pytest.fixture
def short_file(tmp_path):
    path = tmp_path / "native-prefill.nut"
    expected = make_av(path, duration=5003 / RATE, audio_start=Fraction(1))
    return path, expected


def ready(player):
    wait_for(player, lambda state: player.ready_for_start() and state["audio_eof"])


def test_real_pcm_prefill_holds_clock_and_releases_identical_tokens_once(short_file):
    path, expected = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        before = player.clock_snapshot()
        prepared = player.prepare_native_epoch()
        assert prepared.generation == before.generation + 1 and prepared.epoch == before.epoch
        assert prepared.paused and prepared.preroll
        assert prepared.host_anchor_ns == before.host_anchor_ns
        first_rgb = player.tick().source
        now.advance(20_000_000_000)
        packets, final = player.native_audio_batch()
        assert final and len(packets) == 11 and len(packets[-1].samples) == 203
        assert all(p.due_ns is None and p.token[:3] == scope(prepared) for p in packets)
        assert [p.pts_ns for p in packets] == list(range(0, 100_000_001, 10_000_000))
        assert all(p.offset_samples == 0 and not p.samples.flags.writeable for p in packets)
        np.testing.assert_array_equal(np.concatenate([p.samples for p in packets]), expected)
        assert player.tick().source is first_rgb and player.status()["position_ns"] == 0
        assert player.audio_batch() == ((), False)
        repeated, final_repeat = player.native_audio_batch(max_packets=1)
        assert repeated[0].token == packets[0].token and not final_repeat
        np.testing.assert_array_equal(repeated[0].samples, packets[0].samples)
        assert player.status()["audio_consumed_samples"] == 0

        anchor = now() + 200_000_000
        released = player.release_native_epoch(scope(prepared), anchor)
        assert scope(released) == scope(prepared) and released.host_anchor_ns == anchor
        assert not released.paused and not released.preroll
        assert released.media_anchor_ns == prepared.media_anchor_ns == 0
        native_after, _ = player.native_audio_batch()
        scheduled, _ = player.audio_batch()
        assert [p.token for p in scheduled] == [p.token for p in native_after] == [p.token for p in packets]
        assert all(p.due_ns is None for p in native_after)
        assert all(p.due_ns == anchor + p.pts_ns for p in scheduled)
        assert player.tick().source is first_rgb
        now.advance(199_999_999)
        assert player.status()["position_ns"] == 0
        now.advance(1)
        assert player.status()["position_ns"] == 0
        now.advance(110_000_000)
        assert player.tick().source.pts_ns == 100_000_000
        assert player.status()["audio_consumed_samples"] == 0
        assert not player.finish_if_drained()
        assert not player.status()["native_output_integrated"]
        assert not player.status()["av_sync_verified"]


def test_unready_and_repeated_prepare_or_legacy_start_do_not_change_scope(short_file):
    path, _ = short_file
    player = FileAVPlayback(path)
    with pytest.raises(RuntimeError, match="preroll"):
        player.prepare_native_epoch()
    assert player.clock_snapshot().generation == 0
    with player:
        ready(player)
        prepared = player.prepare_native_epoch()
        with pytest.raises(RuntimeError, match="already prepared"):
            player.prepare_native_epoch()
        with pytest.raises(RuntimeError, match="release_native_epoch"):
            player.start()
        assert player.clock_snapshot() == prepared


@pytest.mark.parametrize("wrong", [None, [], ("wrong", 0, 1), ("same", False, 1), ("same", 0, 99)])
def test_wrong_release_scope_never_advances_reserved_clock(short_file, wrong):
    path, _ = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        prepared = player.prepare_native_epoch()
        if type(wrong) is tuple and wrong[0] == "same":
            wrong = (prepared.session_id, *wrong[1:])
        with pytest.raises(ValueError, match="scope"):
            player.release_native_epoch(wrong, now())
        assert player.clock_snapshot() == prepared


@pytest.mark.parametrize("offset", [True, -1, 1.5, "10", 2**63])
def test_invalid_start_time_keeps_clock_and_generation(short_file, offset):
    path, _ = short_file
    with FileAVPlayback(path) as player:
        ready(player)
        prepared = player.prepare_native_epoch()
        with pytest.raises(ValueError, match="start_host_ns"):
            player.release_native_epoch(scope(prepared), offset)
        assert player.clock_snapshot() == prepared


def test_past_start_and_double_release_rejected_without_retiming(short_file):
    path, _ = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        prepared = player.prepare_native_epoch()
        with pytest.raises(ValueError, match="past"):
            player.release_native_epoch(scope(prepared), now() - 1)
        released = player.release_native_epoch(scope(prepared), now())
        with pytest.raises(RuntimeError, match="already been released"):
            player.release_native_epoch(scope(prepared), now() + 100)
        with pytest.raises(RuntimeError, match="already prepared"):
            player.prepare_native_epoch()
        assert player.clock_snapshot() == released


def test_issued_prefill_keeps_pause_seek_and_pending_transition_guards(short_file):
    path, _ = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        prepared = player.prepare_native_epoch()
        packets, _ = player.native_audio_batch(max_packets=1)
        assert packets
        for change in (lambda: player.set_paused(True), lambda: player.seek(0), player.replay):
            with pytest.raises(RuntimeError, match="confirmed native flush"):
                change()
        transition = player.begin_transition(paused=True, expected_scope=scope(prepared))
        with pytest.raises(RuntimeError, match="transition"):
            player.release_native_epoch(scope(prepared), now())
        with pytest.raises(RuntimeError, match="transition"):
            player.native_audio_batch()
        with pytest.raises(ValueError, match="unconfirmed"):
            player.commit_transition(transition, [], flush_confirmed=False)
        assert player.status()["audio_consumed_samples"] == 0
        assert player.clock_snapshot().generation == prepared.generation


def test_unissued_seek_and_pause_invalidate_old_reservations(short_file):
    path, _ = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        old = player.prepare_native_epoch()
        player.seek(0)
        with pytest.raises(ValueError, match="scope"):
            player.release_native_epoch(scope(old), now())
        with pytest.raises(RuntimeError, match="prepared"):
            player.native_audio_batch()
        ready(player)
        current = player.prepare_native_epoch()
        assert current.epoch != old.epoch
        player.set_paused(True)
        with pytest.raises(ValueError, match="scope"):
            player.release_native_epoch(scope(current), now())
        assert not player.status()["native_epoch_prepared"]


def test_ai_requirement_is_rechecked_and_errors_revoke_release(short_file):
    path, _ = short_file
    now = Clock()
    with FileAVPlayback(path, now_ns=now) as player:
        ready(player)
        with pytest.raises(RuntimeError, match="preroll"):
            player.prepare_native_epoch(require_ai=True)
        request = player.next_ai_frame()
        # Typed identity fixture only; this test makes no model inference claim.
        assert player.submit_ai(request, typed_result(request))
        prepared = player.prepare_native_epoch(require_ai=True)
        player.set_ai_revision(1)
        with pytest.raises(RuntimeError, match="preroll"):
            player.release_native_epoch(scope(prepared), now())
        player._fail(RuntimeError("controlled decode failure"))
        with pytest.raises(RuntimeError, match="controlled decode failure"):
            player.release_native_epoch(scope(prepared), now())
        with pytest.raises(RuntimeError, match="controlled decode failure"):
            player.native_audio_batch()
        assert player.clock_snapshot() == prepared


def test_partial_ack_contract_keeps_original_suffix_without_release(short_file):
    path, expected = short_file
    with FileAVPlayback(path) as player:
        ready(player)
        prepared = player.prepare_native_epoch()
        packet = player.native_audio_batch(max_packets=1)[0][0]
        # Explicit unit input to the existing consumed-cursor API, not a real
        # device observation. Prefill itself must never issue this acknowledgement.
        assert player.ack_audio(packet.token, 123)
        suffix = player.native_audio_batch(max_packets=1)[0][0]
        assert suffix.token == packet.token and suffix.total_samples == 480
        assert suffix.offset_samples == 123 and suffix.pts_ns == 2_562_500
        assert suffix.absolute_pts == Fraction(1) + Fraction(123, RATE)
        assert suffix.due_ns is None and suffix.discontinuity is None
        np.testing.assert_array_equal(suffix.samples, expected[123:480])
        assert not suffix.samples.flags.writeable and packet.offset_samples == 0
        assert player.clock_snapshot() == prepared


def test_paused_user_empty_future_audio_and_closed_player_are_not_ready(tmp_path):
    path = tmp_path / "delayed-audio.nut"
    make_av(path, duration=.03, audio_start=Fraction(2))
    now = Clock()
    with FileAVPlayback(path, now_ns=now, paused=True) as player:
        wait_for(player, lambda state: player.ready_for_start())
        prepared = player.prepare_native_epoch()
        assert player.native_audio_batch() == ((), False)
        released = player.release_native_epoch(scope(prepared), now())
        assert released.paused and not released.preroll
        assert player.status()["audio_consumed_samples"] == 0
    with pytest.raises(RuntimeError, match="closing"):
        player.native_audio_batch()
    with pytest.raises(ValueError, match="scope"):
        player.release_native_epoch(scope(prepared), now())


@pytest.mark.parametrize("value", [True, 0, 51, 1.5])
def test_native_batch_bounds_reject_without_issuing(short_file, value):
    path, _ = short_file
    with FileAVPlayback(path) as player:
        ready(player)
        player.prepare_native_epoch()
        with pytest.raises(ValueError, match="max_packets"):
            player.native_audio_batch(max_packets=value)
        assert player.status()["issued_audio_tokens"] == 0

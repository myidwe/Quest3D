"""Native link failure boundaries; injected consumers are not sound proof."""
from types import SimpleNamespace

import pytest

from quest3d.audio_bridge import EncoderAck
from quest3d.file_audio import FileAudioLink


def link_fixture(*, acknowledgement_ok=True, backpressure=False, final_batch=True):
    calls = []
    clock = SimpleNamespace(session_id="a" * 32, epoch=0, generation=1, paused=False, preroll=False)
    token = (clock.session_id, 0, 1, 2)
    packet = SimpleNamespace(token=token)
    player = SimpleNamespace(clock_snapshot=lambda: clock,
        status=lambda: {"finished": False, "audio_eof": True},
        ack_audio=lambda token, count: acknowledgement_ok,
        audio_batch=lambda: ((packet,), final_batch))

    class Publisher:
        def __init__(self, *args): pass
        def snapshot(self): return {"consumer_ready": True, "fault": None}
        def poll_acks(self): return (EncoderAck(token, 240),)
        def confirm_acks(self, acks): calls.append(("confirmed", acks))
        def offer(self, packet):
            calls.append(("offer", packet.token))
            return not backpressure
        def mark_decoder_eof(self): calls.append("eof")
        def begin_flush(self): calls.append("flush_requested"); return 7
        def close(self): calls.append("closed")

    return FileAudioLink(player, publisher_factory=Publisher), calls


def test_rejected_file_ack_does_not_reclaim_native_cursor():
    link, calls = link_fixture(acknowledgement_ok=False)
    with pytest.raises(RuntimeError, match="stale file identity"):
        link.pump()
    assert calls == []


@pytest.mark.parametrize("backpressure,final", [(True, True), (False, False)])
def test_eof_waits_for_entire_atomic_batch_despite_later_decoder_eof(backpressure, final):
    link, calls = link_fixture(backpressure=backpressure, final_batch=final)
    link.pump()
    # player.status reports EOF, as if the decoder finished after audio_batch.
    assert "eof" not in calls and not link.eof_sent


def test_successful_final_batch_forwards_eof_once_and_requires_reset_for_controls():
    link, calls = link_fixture()
    link.pump()
    link.pump()
    assert calls.count("eof") == 1
    with pytest.raises(RuntimeError, match="confirmed transport reset"):
        link.require_direct_control_safe()


def test_failure_requests_native_stop_without_claiming_confirmed_flush():
    link, calls = link_fixture()
    link.fail(RuntimeError("encoder lost"))
    state = link.snapshot()
    assert calls == ["flush_requested"] and state["failure_flush_sequence"] == 7
    assert not state["failure_flush_confirmed"] and not state["audio_integrated"]
    assert "encoder lost" in state["error"]

"""Real FileAV decode/clock; injected encoder receipts are protocol fixtures only."""
import threading
import uuid

import pytest

from quest3d.audio_bridge import EncoderAck, FlushResult
from quest3d.file_audio import FileAudioLink
from quest3d.file_transport_reset import FileResetBinding, FileTransportReset
from quest3d.media_playout import FileAVPlayback
from test_media_audio import make_av
from test_media_playout import Clock, wait_for


class DiagnosticPublisher:
    """No native encoder/output: explicit fault/ACK injection for state-machine tests."""
    def __init__(self, session_id, epoch, generation):
        self.state = {"file_session": session_id, "epoch": epoch, "generation": generation,
                      "channel": "b" * 32, "producer_pid": 101, "producer_creation_filetime": 1001,
                      "consumer_pid": 202, "consumer_creation_filetime": 2002,
                      "consumer_ready": True, "fault": None}
        self.packets, self.confirmed = [], []
        self.flush_result = None
        self.sequence = 0
        self.closed = False
        self.before_offer = self.before_finish = lambda: None
        self.begin_error = None
    def snapshot(self): return dict(self.state)
    def poll_acks(self): return ()
    def confirm_acks(self, acks): self.confirmed.extend(acks)
    def offer(self, packet):
        self.before_offer()
        self.packets.append(packet)
        return True
    def mark_decoder_eof(self): pass
    def begin_flush(self):
        if self.begin_error: raise self.begin_error
        self.sequence += 1
        return self.sequence
    def finish_flush(self, sequence, timeout):
        assert timeout == 0 and sequence == self.sequence
        self.before_finish()
        if self.flush_result is None: raise TimeoutError("Fixture has no native completion")
        return self.flush_result
    def close(self): self.closed = True


@pytest.fixture
def active(tmp_path):
    path = tmp_path / "av.nut"
    make_av(path)
    clock = Clock()
    with FileAVPlayback(path, now_ns=clock) as player:
        wait_for(player, lambda _: player.ready_for_start())
        player.start()
        link = FileAudioLink(player, publisher_factory=DiagnosticPublisher)
        state = player.clock_snapshot()
        binding = FileResetBinding(player.session_id, "b"*32, "c"*32, "d"*64,
                                   101, 1001, 202, 2002, state.epoch, state.generation)
        reset = FileTransportReset(link, binding, now_ns=clock)
        yield player, link, reset, clock
        link.close()


def request(reset, *, operation="pause", paused=True, seek_ns=None):
    result = {"version": 1, "request_id": uuid.uuid4().hex, "expected_revision": "0",
              "file_session_id": reset.binding.file_session_id, "old_transport_epoch": reset.binding.transport_epoch,
              "operation": operation, "paused": paused}
    if seek_ns is not None: result["seek_ns"] = seek_ns
    return result


def diagnostic(ticket, role):
    steps = (["packet_gate_closed", "audio_worker_joined", "video_worker_joined", "pcm_reader_retired"] if role == "host" else
             ["moonlight_unwound", "audio_backend_shutdown", "decoder_retired", "video_queue_cleared", "xr_leases_retired"])
    return {**ticket.wire_identity(), "role": role, "scope": "diagnostic_only", "completed_steps": steps}


def test_real_clock_freezes_with_same_epoch_and_late_encoder_ack_never_grants_reset(active):
    player, link, reset, clock = active
    link.pump()
    assert link.publisher.packets and link.offered
    packet = link.publisher.packets[0]
    old = player.clock_snapshot()
    ticket = reset.request(request(reset))
    clock.advance(2_000_000_000)
    assert player.clock_snapshot().position(clock()) == ticket.frozen_position_ns
    assert (player.clock_snapshot().epoch, player.clock_snapshot().generation) == (old.epoch, old.generation)
    ack = EncoderAck(packet.token, min(240, packet.total_samples))
    link.publisher.flush_result = FlushResult(1, (ack,), ack.consumed_samples, 1, None)
    assert reset.poll_local_flush().acks == (ack,)
    assert link.publisher.confirmed == [ack]
    state = reset.snapshot()
    assert state["local_encoder_flush_confirmed"] and state["phase"] == "waiting_transport_teardown"
    assert not state["reset_confirmed"] and not state["quest_native_closed"]
    with pytest.raises(RuntimeError, match="confirmed transport reset"): link.require_direct_control_safe()
    with pytest.raises(RuntimeError, match="not connected"): reset.commit()
    assert player.clock_snapshot().epoch == old.epoch


@pytest.mark.parametrize("role_order", [("host", "quest"), ("quest", "host")])
def test_complete_diagnostic_pair_never_becomes_real_permit(active, role_order):
    player, link, reset, clock = active
    ticket = reset.request(request(reset, operation="seek", paused=False, seek_ns="900000000"))
    for role in role_order:
        reset.observe_diagnostic_receipt(diagnostic(ticket, role), role=role)
        reset.observe_diagnostic_receipt(diagnostic(ticket, role), role=role)  # Idempotent only.
    state = reset.snapshot()
    assert state["diagnostic_host_receipt"] and state["diagnostic_quest_receipt"]
    assert not state["host_transport_closed"] and not state["reset_confirmed"] and not state["commit_available"]
    with pytest.raises(RuntimeError): reset.commit()
    assert player.clock_snapshot().epoch == ticket.binding.media_epoch
    assert player.clock_snapshot().position(clock()) == ticket.frozen_position_ns


def test_timeout_is_unconfirmed_and_never_consumption_or_new_epoch(active):
    player, link, reset, clock = active
    ticket = reset.request(request(reset))
    assert reset.poll_local_flush() is None
    clock.advance(60_000_000_000)
    assert reset.snapshot()["phase"] == "unconfirmed_timeout"
    with pytest.raises(ValueError, match="Expired"): reset.observe_diagnostic_receipt(diagnostic(ticket, "host"), role="host")
    assert player.clock_snapshot().epoch == ticket.binding.media_epoch and not link.publisher.confirmed


def test_flush_request_failure_keeps_actual_clock_frozen(active):
    player, link, reset, clock = active
    link.publisher.begin_error = OSError("injected IPC failure")
    ticket = reset.request(request(reset))
    assert reset.snapshot()["phase"] == "local_flush_error"
    clock.advance(100_000_000)
    assert player.clock_snapshot().position(clock()) == ticket.frozen_position_ns
    assert reset.poll_local_flush() is None
    with pytest.raises(RuntimeError): link.require_direct_control_safe()


def test_request_retries_do_not_freeze_twice_and_replay_is_explicit_zero(active):
    player, link, reset, clock = active
    value = request(reset, operation="replay", paused=False)
    first = reset.request(value)
    assert first.request.seek_ns == 0 and reset.request(value) is first
    assert reset.revision == 1 and link.publisher.sequence == 1
    with pytest.raises(ValueError, match="different body"):
        reset.request({**value, "operation": "pause", "paused": True})
    with pytest.raises(RuntimeError, match="pending"): reset.request(request(reset))


@pytest.mark.parametrize("field,value", [("consumer_pid", 203), ("consumer_creation_filetime", 2003),
    ("producer_pid", 102), ("producer_creation_filetime", 1002), ("channel", "f"*32)])
def test_wrong_actual_pcm_lifetime_is_rejected_before_freeze(active, field, value):
    player, link, reset, _ = active
    before = player.clock_snapshot()
    link.publisher.state[field] = value
    with pytest.raises(ValueError, match="lifetime"): reset.request(request(reset))
    assert player.clock_snapshot() == before and not link.snapshot()["transport_reset_pending"]


@pytest.mark.parametrize("field,value", [("version", True), ("expected_revision", 0),
    ("expected_revision", "00"), ("expected_revision", "1"), ("request_id", "0"*32),
    ("old_transport_epoch", "f"*32), ("file_session_id", "f"*32),
    ("paused", 1), ("seek_ns", "-1"), ("seek_ns", "9"*30),
    ("operation", "cancel"), ("password", "not a protocol field")])
def test_invalid_request_never_changes_clock(active, field, value):
    player, link, reset, _ = active
    before = player.clock_snapshot()
    with pytest.raises(ValueError): reset.request({**request(reset), field: value})
    assert player.clock_snapshot() == before and reset.snapshot()["phase"] == "idle"


@pytest.mark.parametrize("field,value", [("host_creation_filetime", "2003"), ("producer_creation_filetime", "1002"),
    ("client_certificate_sha256", "e"*64),
    ("reset_id", "f"*32), ("coordinator_instance", "f"*32), ("old_media_generation", "8"),
    ("old_pcm_channel", "f"*32), ("scope", "production"), ("version", True),
    ("host_pid", 202), ("completed_steps", ["packet_gate_closed"]), ("extra", True)])
def test_stale_or_forged_diagnostic_receipt_is_not_recorded(active, field, value):
    _, _, reset, _ = active
    ticket = reset.request(request(reset))
    with pytest.raises(ValueError): reset.observe_diagnostic_receipt({**diagnostic(ticket, "host"), field: value}, role="host")
    assert not reset.snapshot()["diagnostic_host_receipt"]


def test_freeze_serializes_against_already_started_offer_batch(active):
    player, link, reset, _ = active
    entered, release, frozen = threading.Event(), threading.Event(), threading.Event()
    def before_offer():
        entered.set()
        assert release.wait(3)
    link.publisher.before_offer = before_offer
    pumping = threading.Thread(target=link.pump)
    freezing = threading.Thread(target=lambda: (reset.request(request(reset)), frozen.set()))
    pumping.start()
    try:
        assert entered.wait(2)
        freezing.start()
        assert not frozen.wait(.05)
    finally:
        release.set()
        pumping.join(3)
        if freezing.ident is not None: freezing.join(3)
    assert frozen.is_set() and not pumping.is_alive() and not freezing.is_alive()
    offered = len(link.publisher.packets)
    link.pump()
    assert offered > 0 and len(link.publisher.packets) == offered


def test_status_stays_available_while_local_flush_ipc_is_blocked(active):
    _, link, reset, _ = active
    reset.request(request(reset))
    entered, release, status_read = threading.Event(), threading.Event(), threading.Event()
    def wait_finish():
        entered.set()
        assert release.wait(3)
    link.publisher.before_finish = wait_finish
    polling = threading.Thread(target=reset.poll_local_flush)
    polling.start()
    assert entered.wait(2)
    reader = threading.Thread(target=lambda: (reset.snapshot(), status_read.set()))
    reader.start()
    try:
        assert status_read.wait(.5)
    finally:
        release.set()
        polling.join(3)
        reader.join(3)


@pytest.mark.parametrize("field,value", [("file_session", "e"*32), ("epoch", 100), ("generation", 100)])
def test_pcm_scope_must_match_the_real_file_clock_before_freeze(active, field, value):
    player, link, reset, _ = active
    before = player.clock_snapshot()
    link.publisher.state[field] = value
    with pytest.raises(ValueError, match="PCM media scope"): reset.request(request(reset))
    assert player.clock_snapshot() == before


def test_wrong_scope_final_ack_never_confirms_flush_or_commits(active):
    player, link, reset, _ = active
    link.pump()
    packet = link.publisher.packets[0]
    ticket = reset.request(request(reset))
    wrong = EncoderAck(("e"*32, *packet.token[1:]), 240)
    link.publisher.flush_result = FlushResult(1, (wrong,), 240, 1, None)
    with pytest.raises(RuntimeError, match="stale file identity"): reset.poll_local_flush()
    assert not link.publisher.confirmed
    assert not reset.snapshot()["local_encoder_flush_confirmed"]
    assert player.clock_snapshot().epoch == ticket.binding.media_epoch


def test_clock_change_between_inspection_and_freeze_rejected_atomically(active):
    player, link, reset, _ = active
    original = link.freeze_for_transport_reset
    def raced(**kwargs):
        # Real clock mutation after binding inspection but before transition lock.
        player.set_paused(True)
        return original(**kwargs)
    link.freeze_for_transport_reset = raced
    with pytest.raises(ValueError, match="scope changed"): reset.request(request(reset))
    assert reset.snapshot()["phase"] == "idle" and not link.snapshot()["transport_reset_pending"]


def test_concurrent_final_poll_retains_first_immutable_ack_result(active):
    _, link, reset, _ = active
    link.pump()
    packet = link.publisher.packets[0]
    reset.request(request(reset))
    ack = EncoderAck(packet.token, 240)
    first = FlushResult(1, (ack,), 240, 1, None)
    link.publisher.flush_result = first
    entered, release = threading.Event(), threading.Event()
    finishes, results = [], []
    def before_finish():
        finishes.append(True)
        entered.set()
        assert release.wait(3)
    link.publisher.before_finish = before_finish
    workers = [threading.Thread(target=lambda: results.append(reset.poll_local_flush())) for _ in range(2)]
    workers[0].start()
    assert entered.wait(2)
    workers[1].start()
    release.set()
    for worker in workers: worker.join(3)
    assert all(not worker.is_alive() for worker in workers)
    assert len(finishes) == 1 and len(results) == 2 and all(result is first for result in results)
    assert link.publisher.confirmed == [ack]

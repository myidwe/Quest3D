"""File clock to native PCM consumption link; no Windows sound device changes."""
from .audio_bridge import AudioPublisher
import threading


class FileAudioLink:
    def __init__(self, player, *, publisher_factory=AudioPublisher):
        self.player = player
        self._lock = threading.RLock()
        self._reset_transition = None
        self._reset_flush_result = None
        clock = player.clock_snapshot()
        self.publisher = publisher_factory(clock.session_id, clock.epoch, clock.generation)
        self.scope = (clock.epoch, clock.generation)
        self.offered = False
        self.eof_sent = False
        self.error = None
        self.flush_sequence = None
        self.closed = False
        self.latest = self.publisher.snapshot()

    def refresh(self):
        self.latest = self.publisher.snapshot()
        if self.latest["fault"]:
            raise RuntimeError(f"Native PCM fault: {self.latest['fault']}")
        return self.latest

    def ready(self):
        return bool(self.refresh()["consumer_ready"])

    def adopt_started_clock(self):
        clock = self.player.clock_snapshot()
        self.publisher.prepare_scope(clock.epoch, clock.generation)
        self.scope = (clock.epoch, clock.generation)

    def pump(self):
        # Serialize freeze with the entire offer batch. A batch obtained before
        # begin_transition must not be published after the freeze returns.
        with self._lock:
            return self._pump_locked()

    def _pump_locked(self):
        state = self.refresh()
        # EOF freezes the player in a final generation after all original PCM
        # was consumed. It does not rewrite the native stream's old tokens.
        if self.player.status()["finished"]:
            return ()
        acks = self.publisher.poll_acks()
        for ack in acks:
            if not self.player.ack_audio(ack.token, ack.consumed_samples):
                raise RuntimeError("Native PCM acknowledgement has a stale file identity")
        self.publisher.confirm_acks(acks)
        clock = self.player.clock_snapshot()
        if self._reset_transition is not None or clock.paused or clock.preroll or not state["consumer_ready"]:
            return acks
        if (clock.epoch, clock.generation) != self.scope:
            raise RuntimeError("File clock changed without a native PCM transition")
        all_offered = True
        packets, final_batch = self.player.audio_batch()
        for packet in packets:
            if not self.publisher.offer(packet):
                all_offered = False
                break
            self.offered = True
        if all_offered and final_batch and not self.eof_sent:
            self.publisher.mark_decoder_eof()
            self.eof_sent = True
        return acks

    def freeze_for_transport_reset(self, *, paused, seek_ns=None, expected_scope=None):
        """Freeze RGB/PCM atomically against pump; this grants no restart permit."""
        with self._lock:
            if self.closed:
                raise RuntimeError("The file audio link is closed")
            if self._reset_transition is not None:
                raise RuntimeError("A file transport reset is already pending")
            transition = self.player.begin_transition(paused=paused, seek_ns=seek_ns, expected_scope=expected_scope)
            self._reset_transition = transition
            return transition

    def request_transport_flush(self, transition):
        """Ask the existing native reader to stop its local gate; never infer success."""
        with self._lock:
            if transition is not self._reset_transition or self.closed:
                raise ValueError("Unknown or closed file transport transition")
            self.flush_sequence = self.publisher.begin_flush()
            return self.flush_sequence

    def poll_transport_flush(self, transition, sequence):
        """Observe at most one bounded mutex attempt and preserve real late ACKs.

        A result is only the PC encoder/gate boundary. It is neither reader
        retirement nor a Quest receive/decode/playout queue flush.
        """
        with self._lock:
            if transition is not self._reset_transition or self.closed:
                raise ValueError("Unknown or closed file transport transition")
            if sequence != self.flush_sequence:
                raise ValueError("Unknown file transport flush sequence")
            if self._reset_flush_result is not None:
                return self._reset_flush_result
            try:
                result = self.publisher.finish_flush(sequence, timeout=0)
            except TimeoutError:
                return None
            for ack in result.acks:
                if not self.player.ack_audio(ack.token, ack.consumed_samples):
                    raise RuntimeError("Final native PCM acknowledgement has a stale file identity")
            self.publisher.confirm_acks(result.acks)
            self._reset_flush_result = result
            return result

    def require_direct_control_safe(self):
        # Ring-empty and encoder ACKs cannot establish that Quest has cleared
        # already encoded/sent audio. Keep the old timeline intact until the
        # forthcoming stream-reset handshake can perform a common seek.
        if self.offered or self.error or self._reset_transition is not None:
            raise RuntimeError("Active native PCM requires a confirmed transport reset for pause/seek/replay")

    def fail(self, error):
        self.error = f"{type(error).__name__}: {error}"
        try:
            self.flush_sequence = self.publisher.begin_flush()
        except Exception as flush_error:
            self.error += f"; native stop unconfirmed: {flush_error}"

    def snapshot(self):
        return {**self.latest, "requested": True, "error": self.error,
                "audio_integrated": bool(self.latest["consumer_ready"] and not self.error and not self.closed),
                "consumption_scope": "successful native Opus encode/handoff; not Quest playback",
                "playback_transition_requires_transport_reset": self.offered,
                "failure_flush_sequence": self.flush_sequence, "failure_flush_confirmed": False,
                "transport_reset_pending": self._reset_transition is not None,
                "quest_audio_verified": False}

    def close(self):
        with self._lock:
            self.publisher.close()
            self.closed = True

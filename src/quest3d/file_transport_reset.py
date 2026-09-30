"""Bounded FilePCM reset protocol and real local freeze/encoder-flush boundary.

No production host/Quest lifecycle receipt issuer is connected yet. Diagnostic
receipts validate the proposed wire identity but CANNOT authorize a new epoch.
In particular no elapsed time, encoder cursor, or caller-supplied boolean is a
reset permit. Existing file-session direct-control guards remain in force.
"""
from dataclasses import dataclass
import json
import threading
import time
import uuid


def _hex(value, name, length=32):
    if type(value) is not str or len(value) != length or any(c not in "0123456789abcdef" for c in value) or not int(value, 16):
        raise ValueError(f"Invalid {name}")
    return value


def _integer(value, name, *, nonzero=False, maximum=(1 << 63) - 1):
    if type(value) is not int or not int(nonzero) <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _decimal(value, name, *, nonzero=False):
    if type(value) is not str or not value or len(value) > 19 or not value.isascii() or not value.isdecimal() or (len(value) > 1 and value[0] == "0"):
        raise ValueError(f"Invalid decimal {name}")
    return _integer(int(value), name, nonzero=nonzero)


def _object(value, maximum=4096):
    if type(value) is not dict:
        raise ValueError("A protocol object is required")
    try:
        data = json.dumps(value, ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("Invalid protocol JSON") from None
    if len(data) > maximum:
        raise ValueError("Protocol object exceeds its byte limit")


@dataclass(frozen=True, slots=True)
class FileResetBinding:
    """Trusted local snapshot of the active file, PCM owner and paired host."""
    file_session_id: str
    pcm_channel: str
    transport_epoch: str
    client_certificate_sha256: str
    producer_pid: int
    producer_creation_filetime: int
    host_pid: int
    host_creation_filetime: int
    media_epoch: int
    media_generation: int

    def __post_init__(self):
        for name in ("file_session_id", "pcm_channel", "transport_epoch"):
            _hex(getattr(self, name), name)
        _hex(self.client_certificate_sha256, "client certificate SHA256", 64)
        for name in ("producer_pid", "host_pid"):
            _integer(getattr(self, name), name, nonzero=True, maximum=(1 << 32) - 1)
        for name in ("producer_creation_filetime", "host_creation_filetime"):
            _integer(getattr(self, name), name, nonzero=True)
        for name in ("media_epoch", "media_generation"):
            _integer(getattr(self, name), name)


@dataclass(frozen=True, slots=True)
class FileResetRequest:
    request_id: str
    expected_revision: int
    operation: str
    paused: bool
    seek_ns: int | None


def parse_reset_request(value, binding):
    """Validate a future paired-host body; this is not authentication itself."""
    _object(value)
    required = {"version", "request_id", "expected_revision", "file_session_id", "old_transport_epoch", "operation", "paused"}
    if set(value) - required - {"seek_ns"} or not required <= set(value):
        raise ValueError("Unexpected or missing file reset fields")
    if type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("Unsupported file reset version")
    if value["file_session_id"] != binding.file_session_id or value["old_transport_epoch"] != binding.transport_epoch:
        raise ValueError("File reset belongs to a different session or transport")
    request_id = _hex(value["request_id"], "request id")
    revision = _decimal(value["expected_revision"], "expected revision")
    operation, paused = value["operation"], value["paused"]
    if type(operation) is not str or operation not in ("pause", "resume", "seek", "replay") or type(paused) is not bool:
        raise ValueError("Invalid file reset operation or pause state")
    seek = _decimal(value["seek_ns"], "seek_ns") if "seek_ns" in value else None
    if operation == "pause" and (not paused or seek is not None):
        raise ValueError("Pause requires paused=true and no seek")
    if operation == "resume" and (paused or seek is not None):
        raise ValueError("Resume requires paused=false and no seek")
    if operation == "seek" and seek is None:
        raise ValueError("Seek requires an explicit target")
    if operation == "replay":
        if paused or seek not in (None, 0):
            raise ValueError("Replay requires target zero and paused=false")
        seek = 0  # Never default replay to frozen EOF.
    return FileResetRequest(request_id, revision, operation, paused, seek)


@dataclass(frozen=True, slots=True)
class FileResetTicket:
    coordinator_instance: str
    reset_id: str
    request: FileResetRequest
    binding: FileResetBinding
    transition_id: str
    frozen_position_ns: int
    expires_ns: int

    def wire_identity(self):
        """Exact matching scope for diagnostics/future authenticated receipts."""
        b = self.binding
        return {"version": 1, "coordinator_instance": self.coordinator_instance, "reset_id": self.reset_id,
                "request_id": self.request.request_id, "transition_id": self.transition_id,
                "file_session_id": b.file_session_id, "old_transport_epoch": b.transport_epoch,
                "client_certificate_sha256": b.client_certificate_sha256,
                "old_pcm_channel": b.pcm_channel, "old_media_epoch": str(b.media_epoch),
                "old_media_generation": str(b.media_generation), "producer_pid": str(b.producer_pid),
                "producer_creation_filetime": str(b.producer_creation_filetime), "host_pid": str(b.host_pid),
                "host_creation_filetime": str(b.host_creation_filetime)}


_HOST_STEPS = ("packet_gate_closed", "audio_worker_joined", "video_worker_joined", "pcm_reader_retired")
_QUEST_STEPS = ("moonlight_unwound", "audio_backend_shutdown", "decoder_retired", "video_queue_cleared", "xr_leases_retired")


def validate_diagnostic_receipt(value, ticket, role):
    """Validate identity/order only. A parsed receipt has NO authority to reset."""
    _object(value)
    expected = ticket.wire_identity()
    steps = _HOST_STEPS if role == "host" else _QUEST_STEPS if role == "quest" else None
    if steps is None or set(value) != set(expected) | {"role", "completed_steps", "scope"}:
        raise ValueError("Invalid teardown receipt fields")
    if value["scope"] != "diagnostic_only" or value["role"] != role:
        raise ValueError("Only explicitly diagnostic receipts are accepted here")
    for key, expected_value in expected.items():
        if type(value[key]) is not type(expected_value) or value[key] != expected_value:
            raise ValueError(f"Mismatched teardown receipt: {key}")
    if type(value["completed_steps"]) is not list or value["completed_steps"] != list(steps):
        raise ValueError("Incomplete or out-of-order teardown receipt")
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class FileTransportReset:
    """One pending reset; default and diagnostic modes always fail closed.

    No public receipt parser creates a capability. Production callback issuers,
    authenticated Quest cleanup and new-session READY are intentionally not
    connected; commit() rejects even a complete pair of diagnostic receipts.
    """
    def __init__(self, audio_link, binding, *, revision=0, timeout_ns=15_000_000_000, now_ns=time.perf_counter_ns):
        _integer(revision, "revision")
        _integer(timeout_ns, "timeout_ns", nonzero=True, maximum=60_000_000_000)
        if type(binding) is not FileResetBinding:
            raise ValueError("An immutable file reset binding is required")
        self.audio, self.binding, self.revision = audio_link, binding, revision
        self.now_ns, self.timeout_ns = now_ns, timeout_ns
        self.instance = uuid.uuid4().hex
        self._lock = threading.RLock()
        self._ticket = self._transition = self._flush = None
        self._flush_sequence = None
        self._diagnostics = {}
        self._error = None
        self._expired = False

    def _check_current_binding(self):
        clock = self.audio.player.clock_snapshot()
        state = self.audio.refresh()
        b = self.binding
        if (clock.session_id, clock.epoch, clock.generation) != (b.file_session_id, b.media_epoch, b.media_generation):
            raise ValueError("File clock changed before reset freeze")
        if (state.get("file_session"), state.get("epoch"), state.get("generation")) != (b.file_session_id, b.media_epoch, b.media_generation):
            raise ValueError("PCM media scope changed before reset freeze")
        if (state.get("channel"), state.get("producer_pid"), state.get("producer_creation_filetime")) != (b.pcm_channel, b.producer_pid, b.producer_creation_filetime):
            raise ValueError("PCM producer lifetime changed before reset freeze")
        if (state.get("consumer_pid"), state.get("consumer_creation_filetime")) != (b.host_pid, b.host_creation_filetime):
            raise ValueError("Native PCM reader does not belong to the expected host lifetime")

    def request(self, value):
        """Freeze the real clock and ask for local flush; do not block on teardown."""
        request = parse_reset_request(value, self.binding)
        with self._lock:
            if self._ticket:
                if request == self._ticket.request:
                    return self._ticket
                if request.request_id == self._ticket.request.request_id:
                    raise ValueError("A request id cannot be reused with a different body")
                raise RuntimeError("Another file reset is pending")
            if request.expected_revision != self.revision:
                raise ValueError("Stale file reset revision")
            if self.revision == (1 << 63) - 1:
                raise RuntimeError("File reset revision exhausted")
            self._check_current_binding()
            transition = self.audio.freeze_for_transport_reset(paused=request.paused, seek_ns=request.seek_ns,
                expected_scope=(self.binding.file_session_id, self.binding.media_epoch, self.binding.media_generation))
            ticket = FileResetTicket(self.instance, uuid.uuid4().hex, request, self.binding,
                                    transition.transition_id, transition.frozen_position_ns, self.now_ns() + self.timeout_ns)
            self._ticket, self._transition = ticket, transition
            self.revision += 1
            try:
                self._flush_sequence = self.audio.request_transport_flush(transition)
            except Exception as exc:
                self._error = f"local_flush_request_failed: {type(exc).__name__}"
            return ticket

    def poll_local_flush(self):
        """Observe real final encoder ACKs without holding the status mutex during IPC."""
        with self._lock:
            if self._ticket is None:
                raise RuntimeError("No file reset has been requested")
            if self.now_ns() >= self._ticket.expires_ns:
                self._expired = True
            if self._flush is not None or self._flush_sequence is None:
                return self._flush
            transition, sequence = self._transition, self._flush_sequence
        try:
            result = self.audio.poll_transport_flush(transition, sequence)
        except Exception as exc:
            with self._lock:
                self._error = f"local_flush_observation_failed: {type(exc).__name__}"
            raise
        with self._lock:
            if result is not None:
                self._flush = result
            return result

    def observe_diagnostic_receipt(self, value, *, role):
        """Test/protocol ledger only; never feed this result to FileAV commit."""
        with self._lock:
            if self._ticket is None:
                raise RuntimeError("No pending file reset")
            if self.now_ns() >= self._ticket.expires_ns:
                self._expired = True
            if self._expired:
                raise ValueError("Expired reset diagnostic receipt")
            canonical = validate_diagnostic_receipt(value, self._ticket, role)
            previous = self._diagnostics.get(role)
            if previous is not None and previous != canonical:
                raise ValueError("Conflicting reset diagnostic receipt")
            self._diagnostics[role] = canonical

    def commit(self):
        """Reject until real production host and authenticated Quest issuers exist."""
        raise RuntimeError("Production host/Quest teardown receipts are not connected; file epoch remains frozen")

    def snapshot(self):
        with self._lock:
            if self._ticket and self.now_ns() >= self._ticket.expires_ns:
                self._expired = True
            phase = ("idle" if self._ticket is None else "unconfirmed_timeout" if self._expired else
                     "local_flush_error" if self._error else "waiting_transport_teardown" if self._flush else "waiting_local_flush")
            return {"phase": phase, "revision": self.revision,
                    "reset_id": self._ticket.reset_id if self._ticket else None,
                    "transition_id": self._ticket.transition_id if self._ticket else None,
                    "frozen_position_ns": self._ticket.frozen_position_ns if self._ticket else None,
                    "flush_sequence": self._flush_sequence, "local_encoder_flush_confirmed": self._flush is not None,
                    "local_encoded_samples": self._flush.encoded_samples if self._flush else None,
                    "local_encoded_packets": self._flush.encoded_packets if self._flush else None,
                    "local_encoder_fault": self._flush.fault if self._flush else None,
                    "diagnostic_host_receipt": "host" in self._diagnostics,
                    "diagnostic_quest_receipt": "quest" in self._diagnostics,
                    "host_transport_closed": False, "quest_native_closed": False,
                    "reset_confirmed": False, "commit_available": False,
                    "software_reset_completed": False, "quest_audio_verified": False, "error": self._error}

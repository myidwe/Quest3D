"""Private Windows PCM ring. ACK means successful native Opus consumption only.

No output device, loopback, remote playback or elapsed-time ACK is used here.
The caller must apply poll_acks() to FileAVPlayback before confirm_acks().
"""
from contextlib import contextmanager
from dataclasses import dataclass
import ctypes as C
from ctypes import wintypes as W
import hashlib
import math
import os
import secrets
import struct
import time

import numpy as np

MAGIC = b"Q3DAUD1\0"
HEADER_BYTES, SLOT_BYTES, SLOT_COUNT = 256, 4096, 50
CAPACITY = HEADER_BYTES + SLOT_BYTES * SLOT_COUNT
RATE, MAX_SAMPLES = 48000, 24000
EOF, PRODUCER_CLOSED, CONSUMER_STOPPED = 1, 2, 4
FAULTS = {1: "invalid_protocol", 2: "producer_lost", 3: "consumer_lost",
          4: "discontinuity", 5: "late_pcm", 6: "encode_failed", 7: "abandoned_mutex"}
U32, U64, I64 = struct.Struct("<I"), struct.Struct("<Q"), struct.Struct("<q")


def _integer(value, name, maximum=(1 << 63) - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {name}")
    return value


def _identifier(value):
    if type(value) is not str or len(value) != 32 or any(c not in "0123456789abcdef" for c in value):
        raise ValueError("Identifier must be 32 lowercase hexadecimal characters")
    result = bytes.fromhex(value)
    if not any(result): raise ValueError("Identifier cannot be all zero")
    return result


def pack_offer(packet):
    """Serialize one immutable original-block suffix; consumed starts at offset."""
    if type(packet.token) is not tuple or len(packet.token) != 4:
        raise ValueError("Invalid PCM token")
    session, epoch, generation, block_id = packet.token
    _identifier(session)
    for value in (epoch, generation, block_id):
        _integer(value, "token integer")
    total = _integer(packet.total_samples, "total_samples", 480)
    offset = _integer(packet.offset_samples, "offset_samples", 479)
    if total < 1 or offset >= total:
        raise ValueError("Invalid PCM suffix bounds")
    samples = np.asarray(packet.samples)
    if samples.dtype != np.float32 or samples.shape != (total - offset, 2) or not np.isfinite(samples).all():
        raise ValueError("Expected finite float32 stereo PCM matching original block bounds")
    due = _integer(packet.due_ns, "due_ns") - round(offset * 1_000_000_000 / RATE)
    pts = packet.pts_ns - round(offset * 1_000_000_000 / RATE)
    if due < 0 or type(packet.pts_ns) is not int or not -(1 << 63) <= pts < (1 << 63):
        raise ValueError("Invalid original PCM timestamp")
    reasons = {None: 0, "start": 1, "seek": 2, "gap": 3, "overlap": 4, "format_change": 5}
    if packet.discontinuity not in reasons:
        raise ValueError("Unknown PCM discontinuity")
    result = bytearray(SLOT_BYTES)
    struct.pack_into("<QQqIIIIII", result, 0, block_id, due, pts, total, offset,
                     total - offset, offset, reasons[packet.discontinuity], 0)
    U64.pack_into(result, 88, epoch); U64.pack_into(result, 96, generation)
    result[128:128 + samples.nbytes] = samples.astype("<f4", copy=False).tobytes(order="C")
    result[56:88] = hashlib.sha256(result[:36] + result[40:44] + result[88:104] + result[128:128 + samples.nbytes]).digest()
    return bytes(result)


@dataclass(frozen=True)
class EncoderAck:
    """Cumulative original-block samples consumed by a successful Opus encode."""
    token: tuple[str, int, int, int]
    consumed_samples: int


@dataclass(frozen=True)
class FlushResult:
    """Native pull has stopped; these are final local encoder cursors."""
    sequence: int
    acks: tuple[EncoderAck, ...]
    encoded_samples: int
    encoded_packets: int
    fault: str | None
    quest_queue_flushed: bool = False

    @property
    def transport_reset_required(self):
        """Encoded/remote queues are not reversible by the local PCM barrier."""
        return bool(self.encoded_packets or self.fault)


class _Windows:
    """Small explicit Win32 boundary; objects are private to this user and SYSTEM."""
    def __init__(self):
        if os.name != "nt":
            raise OSError("The PCM bridge requires Windows")
        self.k = C.WinDLL("kernel32", use_last_error=True)
        self.a = C.WinDLL("advapi32", use_last_error=True)
        signatures = {
            "GetCurrentProcess": ([], W.HANDLE), "GetCurrentProcessId": ([], W.DWORD),
            "GetProcessTimes": ([W.HANDLE] + [C.c_void_p] * 4, W.BOOL),
            "OpenProcess": ([W.DWORD, W.BOOL, W.DWORD], W.HANDLE),
            "CloseHandle": ([W.HANDLE], W.BOOL),
            "WaitForSingleObject": ([W.HANDLE, W.DWORD], W.DWORD),
            "ReleaseMutex": ([W.HANDLE], W.BOOL),
            "CreateMutexW": ([C.c_void_p, W.BOOL, W.LPCWSTR], W.HANDLE),
            "OpenMutexW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            "OpenFileMappingW": ([W.DWORD, W.BOOL, W.LPCWSTR], W.HANDLE),
            "CreateFileMappingW": ([W.HANDLE, C.c_void_p, W.DWORD, W.DWORD, W.DWORD, W.LPCWSTR], W.HANDLE),
            "MapViewOfFile": ([W.HANDLE, W.DWORD, W.DWORD, W.DWORD, C.c_size_t], C.c_void_p),
            "UnmapViewOfFile": ([C.c_void_p], W.BOOL), "LocalFree": ([C.c_void_p], C.c_void_p),
        }
        for name, (args, restype) in signatures.items():
            getattr(self.k, name).argtypes, getattr(self.k, name).restype = args, restype
        for name, args in {
            "OpenProcessToken": [W.HANDLE, W.DWORD, C.POINTER(W.HANDLE)],
            "GetTokenInformation": [W.HANDLE, C.c_int, C.c_void_p, W.DWORD, C.POINTER(W.DWORD)],
            "ConvertSidToStringSidW": [C.c_void_p, C.POINTER(C.c_void_p)],
            "ConvertStringSecurityDescriptorToSecurityDescriptorW": [W.LPCWSTR, W.DWORD, C.POINTER(C.c_void_p), C.c_void_p],
        }.items():
            getattr(self.a, name).argtypes, getattr(self.a, name).restype = args, W.BOOL

    def creation(self, process):
        values = [C.c_uint64() for _ in range(4)]
        if not self.k.GetProcessTimes(process, *(C.byref(v) for v in values)):
            raise C.WinError(C.get_last_error())
        return values[0].value

    @contextmanager
    def security(self):
        token, sid_text, descriptor = W.HANDLE(), C.c_void_p(), C.c_void_p()
        class Attributes(C.Structure):
            _fields_ = [("length", W.DWORD), ("descriptor", C.c_void_p), ("inherit", W.BOOL)]
        try:
            if not self.a.OpenProcessToken(self.k.GetCurrentProcess(), 8, C.byref(token)):
                raise C.WinError(C.get_last_error())
            size = W.DWORD()
            self.a.GetTokenInformation(token, 1, None, 0, C.byref(size))
            data = C.create_string_buffer(size.value)
            if not self.a.GetTokenInformation(token, 1, data, size, C.byref(size)):
                raise C.WinError(C.get_last_error())
            if not self.a.ConvertSidToStringSidW(C.cast(data, C.POINTER(C.c_void_p))[0], C.byref(sid_text)):
                raise C.WinError(C.get_last_error())
            sid = C.wstring_at(sid_text)
            if not self.a.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                    f"D:P(A;;GA;;;SY)(A;;GA;;;{sid})", 1, C.byref(descriptor), None):
                raise C.WinError(C.get_last_error())
            attributes = Attributes(C.sizeof(Attributes), descriptor, False)
            yield C.byref(attributes)
        finally:
            if token: self.k.CloseHandle(token)
            if sid_text: self.k.LocalFree(sid_text)
            if descriptor: self.k.LocalFree(descriptor)


def inspect_channel(channel):
    """Read a channel descriptor without registering a consumer or writing memory.

    Only the producer's immutable descriptor is authoritative here. A native
    consumer must still acquire the mutex and validate the complete ring. A
    read-only observer MUST NOT wait on this mutex: consuming WAIT_ABANDONED
    then releasing it would silently erase the native consumer's fault signal.
    """
    expected = _identifier(channel)
    win = _Windows()
    prefix = "Local\\Quest3D.Audio.v1." + channel
    mutex = mapping = pointer = process = None
    try:
        mutex = win.k.OpenMutexW(0x100001, False, prefix + ".Mutex")
        mapping = win.k.OpenFileMappingW(4, False, prefix)
        if not mutex or not mapping:
            raise RuntimeError("PCM channel does not exist or is inaccessible")
        pointer = win.k.MapViewOfFile(mapping, 4, 0, 0, CAPACITY)
        if not pointer:
            raise RuntimeError("PCM channel mapping is truncated or inaccessible")
        header = C.string_at(pointer, HEADER_BYTES)
        if (header[:8] != MAGIC
                or struct.unpack_from("<7I", header, 8) != (1, HEADER_BYTES, SLOT_COUNT, SLOT_BYTES, RATE, 2, 1)
                or header[80:96] != expected or not any(header[64:80]) or any(header[196:])):
            raise ValueError("Invalid PCM channel header")
        flags = U32.unpack_from(header, 36)[0]
        if flags & ~7 or flags & (EOF | PRODUCER_CLOSED | CONSUMER_STOPPED) or U32.unpack_from(header, 148)[0]:
            raise RuntimeError("PCM channel is closed, stopped, at EOF or faulted")
        pid, created = U32.unpack_from(header, 40)[0], U64.unpack_from(header, 48)[0]
        if not pid or not created: raise ValueError("PCM channel has no producer identity")
        process = win.k.OpenProcess(0x100000 | 0x1000, False, pid)
        if (not process or win.creation(process) != created
                or win.k.WaitForSingleObject(process, 0) != 0x102):
            raise RuntimeError("PCM producer process is absent or its birth time changed")
        second = C.string_at(pointer, HEADER_BYTES)
        # Exclude heartbeat/cursor counters, which an existing producer updates.
        # Identity, close/fault/READY changes require another preflight instead.
        if header[:112] + header[148:152] + header[192:196] != second[:112] + second[148:152] + second[192:196]:
            raise RuntimeError("PCM descriptor changed during preflight")
        return {"channel": channel, "file_session_id": header[64:80].hex(),
                "producer_pid": pid, "producer_creation_filetime": created,
                "consumer_pid": U32.unpack_from(header, 44)[0],
                "consumer_ready": bool(U32.unpack_from(header, 192)[0])}
    finally:
        if process: win.k.CloseHandle(process)
        if pointer: win.k.UnmapViewOfFile(pointer)
        if mapping: win.k.CloseHandle(mapping)
        if mutex: win.k.CloseHandle(mutex)


class AudioPublisher:
    """One producer and one native encoder per random private channel.

    poll_acks is repeatable. confirm_acks must follow successful FileAVPlayback
    ack application; only then can completed slots be reclaimed. A full ring
    backpressures without losing unobserved consumption information.
    """
    def __init__(self, session_id, epoch=0, generation=0, *, channel=None):
        self.session_id = session_id
        self.channel = channel or secrets.token_hex(16)
        session_bytes, channel_bytes = _identifier(session_id), _identifier(self.channel)
        _integer(epoch, "epoch"); _integer(generation, "generation")
        self.win = _Windows()
        self.mapping = self.mutex = self.pointer = None
        self.consumer_handle = None
        self.closed = False
        self.confirmed, self.offered, self.encoded = {}, {}, {}
        self.last_block = -1
        prefix = "Local\\Quest3D.Audio.v1." + self.channel
        try:
            with self.win.security() as security:
                C.set_last_error(0)
                self.mutex = self.win.k.CreateMutexW(security, False, prefix + ".Mutex")
                if not self.mutex or C.get_last_error() == 183:
                    raise RuntimeError("Audio channel mutex already exists or cannot be created")
                C.set_last_error(0)
                self.mapping = self.win.k.CreateFileMappingW(W.HANDLE(-1), security, 4, 0, CAPACITY, prefix)
                if not self.mapping or C.get_last_error() == 183:
                    raise RuntimeError("Audio mapping already exists or cannot be created")
            self.pointer = self.win.k.MapViewOfFile(self.mapping, 0xF001F, 0, 0, CAPACITY)
            if not self.pointer:
                raise C.WinError(C.get_last_error())
            self.memory = (C.c_ubyte * CAPACITY).from_address(self.pointer)
            header = bytearray(HEADER_BYTES)
            struct.pack_into("<8s8I", header, 0, MAGIC, 1, HEADER_BYTES, SLOT_COUNT, SLOT_BYTES, RATE, 2, 1, 0)
            U32.pack_into(header, 40, os.getpid())
            U64.pack_into(header, 48, self.win.creation(self.win.k.GetCurrentProcess()))
            header[64:80], header[80:96] = session_bytes, channel_bytes
            U64.pack_into(header, 96, epoch); U64.pack_into(header, 104, generation)
            C.memmove(self.pointer, bytes(header), HEADER_BYTES)
        except BaseException:
            self._release()
            raise

    def _get(self, offset, word=U64): return word.unpack_from(self.memory, offset)[0]
    def _put(self, offset, value, word=U64): word.pack_into(self.memory, offset, value)

    @contextmanager
    def _lock(self, timeout_ms=50):
        if self.closed: raise RuntimeError("PCM publisher is closed")
        result = self.win.k.WaitForSingleObject(self.mutex, timeout_ms)
        if result == 0x102: raise TimeoutError("PCM ring mutex busy")
        if result not in (0, 0x80): raise C.WinError(C.get_last_error())
        try:
            if result == 0x80: self._put(148, 7, U32)
            self._put(160, time.perf_counter_ns())
            yield
        finally:
            self.win.k.ReleaseMutex(self.mutex)

    def _fault(self):
        fault = self._get(148, U32)
        pid = self._get(44, U32)
        if pid and not self._get(36, U32) & CONSUMER_STOPPED:
            if self.consumer_handle is None:
                process = self.win.k.OpenProcess(0x100000 | 0x1000, False, pid)
                if process and self.win.creation(process) == self._get(56):
                    self.consumer_handle = process
                else:
                    if process: self.win.k.CloseHandle(process)
                    self._put(148, 3, U32); fault = 3
            if self.consumer_handle and self.win.k.WaitForSingleObject(self.consumer_handle, 0) != 0x102:
                self._put(148, 3, U32); fault = 3
        return FAULTS.get(fault, "unknown_fault" if fault else None)

    def _slots(self):
        head, tail = self._get(112), self._get(120)
        if head > tail or tail - head > SLOT_COUNT:
            raise RuntimeError("Malformed PCM ring cursors")
        return [(seq, HEADER_BYTES + seq % SLOT_COUNT * SLOT_BYTES) for seq in range(head, tail)]

    def _acks(self):
        scope = (self.session_id, self._get(96), self._get(104))
        result = tuple(EncoderAck((*scope, self._get(base)), self._get(base + 36, U32))
                     for _, base in self._slots() if self._get(base + 36, U32) > self._get(base + 28, U32))
        for ack in result: self.encoded[ack.token[3]] = ack.consumed_samples
        return result

    def offer(self, packet):
        packed = pack_offer(packet)
        digest = packed[56:88]
        block_id = packet.token[3]
        with self._lock():
            if fault := self._fault(): raise RuntimeError(f"PCM bridge fault: {fault}")
            if packet.token[:3] != (self.session_id, self._get(96), self._get(104)):
                raise ValueError("Stale PCM scope")
            if self._get(128) != self._get(136) or self._get(36, U32) & (PRODUCER_CLOSED | CONSUMER_STOPPED):
                return False
            previous = self.offered.get(block_id)
            if previous is not None:
                if previous[56:88] != digest:
                    self._acks()
                    old_offset = U32.unpack_from(previous, 28)[0]
                    offset = packet.offset_samples
                    start = 128 + (offset - old_offset) * 8
                    if (offset <= old_offset or offset > self.encoded.get(block_id, -1)
                            or packed[:28] != previous[:28] or U32.unpack_from(packed, 40)[0] != 0
                            or packed[128:128 + packet.samples.nbytes] != previous[start:start + packet.samples.nbytes]):
                        raise ValueError("Same PCM token has different content or timing")
                return True
            if block_id <= self.last_block: raise ValueError("Retired or out-of-order PCM token")
            if self._get(36, U32) & EOF: raise ValueError("Cannot append PCM after decoder EOF")
            self._reap()
            head, tail = self._get(112), self._get(120)
            samples = packet.total_samples - packet.offset_samples
            if tail - head == SLOT_COUNT or self._get(144, U32) + samples > MAX_SAMPLES:
                return False
            if tail >= (1 << 63) - 1: raise RuntimeError("PCM ring sequence exhausted")
            slot = bytearray(packed)
            U64.pack_into(slot, 48, tail)
            C.memmove(self.pointer + HEADER_BYTES + tail % SLOT_COUNT * SLOT_BYTES, bytes(slot), SLOT_BYTES)
            self._put(144, self._get(144, U32) + samples, U32)
            self._put(120, tail + 1)
            self._put(184, block_id)
            self.offered[block_id] = packed
            self.last_block = block_id
            # Keep a bounded retry fingerprint window even after ring reclamation.
            while len(self.offered) > 128:
                retired = next(iter(self.offered))
                self.offered.pop(retired); self.encoded.pop(retired, None)
            return True

    def _reap(self):
        for seq, base in self._slots():
            block_id, total = self._get(base), self._get(base + 24, U32)
            if self._get(base + 36, U32) != total or self.confirmed.get(block_id, -1) < total: break
            self._put(144, self._get(144, U32) - self._get(base + 32, U32), U32)
            self._put(112, seq + 1)
            self.confirmed.pop(block_id, None)

    def poll_acks(self):
        with self._lock(): return self._acks()

    def confirm_acks(self, acks):
        acks = tuple(acks)
        if len(acks) > SLOT_COUNT: raise ValueError("ACK confirmation batch exceeds ring capacity")
        with self._lock():
            offered = {ack.token: ack.consumed_samples for ack in self._acks()}
            for ack in acks:
                if not isinstance(ack, EncoderAck) or ack.token not in offered or not 0 <= ack.consumed_samples <= offered[ack.token]:
                    raise ValueError("Cannot confirm an unobserved encoder cursor")
            for ack in acks: self.confirmed[ack.token[3]] = max(self.confirmed.get(ack.token[3], 0), ack.consumed_samples)
            self._reap()

    def mark_decoder_eof(self):
        with self._lock(): self._put(36, self._get(36, U32) | EOF, U32)

    def begin_flush(self):
        with self._lock():
            if self._get(128) != self._get(136): return self._get(128)
            sequence = self._get(128) + 1
            self._put(128, sequence)
            return sequence

    def finish_flush(self, sequence, timeout=2):
        _integer(sequence, "flush sequence")
        if sequence == 0: raise ValueError("A flush must first be requested")
        if not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not 0 <= timeout <= 5:
            raise ValueError("Flush timeout must be bounded by five seconds")
        deadline = time.monotonic() + timeout
        while True:
            with self._lock():
                fault = self._fault()
                if sequence != self._get(128): raise ValueError("Unknown flush sequence")
                if self._get(136) == sequence and self._get(36, U32) & CONSUMER_STOPPED:
                    return FlushResult(sequence, self._acks(), self._get(168), self._get(176), fault)
                if fault == "consumer_lost":
                    raise RuntimeError(f"PCM flush unconfirmed: {fault}; transport reset required")
            if time.monotonic() >= deadline: raise TimeoutError("Native PCM flush is unconfirmed")
            time.sleep(.002)

    def resume(self, epoch, generation):
        _integer(epoch, "epoch"); _integer(generation, "generation")
        with self._lock():
            if self._fault() or not self._get(192, U32) or self._get(128) != self._get(136) or not self._get(36, U32) & CONSUMER_STOPPED:
                raise RuntimeError("Resume requires a completed healthy native flush")
            if (epoch, generation) <= (self._get(96), self._get(104)):
                raise ValueError("Resume must establish a new PCM scope")
            self._put(96, epoch); self._put(104, generation)
            self._put(112, 0); self._put(120, 0); self._put(144, 0, U32); self._put(184, 0)
            self._put(36, 0, U32)
            self.confirmed.clear(); self.offered.clear(); self.encoded.clear(); self.last_block = -1

    def prepare_scope(self, epoch, generation):
        """Bind the post-preroll clock before this empty scope's very first offer.

        READY must precede player.start(); this method then adopts its actual
        epoch/generation. It cannot rewrite any offered or encoded PCM identity.
        """
        _integer(epoch, "epoch"); _integer(generation, "generation")
        with self._lock():
            if (self._fault() or not self._get(192, U32) or self._get(128) != self._get(136)
                    or self._get(36, U32) & (PRODUCER_CLOSED | CONSUMER_STOPPED | EOF)
                    or self.last_block != -1 or self.offered or self._get(112) != self._get(120)):
                raise RuntimeError("Scope preparation requires READY and no PCM offers in this scope")
            current = (self._get(96), self._get(104))
            if (epoch, generation) < current: raise ValueError("PCM scope cannot move backwards")
            self._put(96, epoch); self._put(104, generation)

    def snapshot(self):
        with self._lock():
            fault = self._fault()
            return {"channel": self.channel, "file_session": self.session_id, "epoch": self._get(96),
                    "producer_pid": self._get(40, U32), "producer_creation_filetime": self._get(48),
                    "consumer_pid": self._get(44, U32), "consumer_creation_filetime": self._get(56),
                    "generation": self._get(104), "slots": self._get(120) - self._get(112),
                    "buffered_samples": self._get(144, U32), "encoder_consumed_samples": self._get(168),
                    "encoded_packets": self._get(176), "decoder_eof": bool(self._get(36, U32) & EOF),
                    "encoder_drained": bool(self._get(36, U32) & EOF) and all(
                        self._get(base + 36, U32) == self._get(base + 24, U32) for _, base in self._slots()),
                    "consumer_ready": bool(self._get(192, U32)), "fault": fault,
                    "quest_playback_verified": False}

    def _release(self):
        if self.consumer_handle: self.win.k.CloseHandle(self.consumer_handle); self.consumer_handle = None
        if self.pointer: self.win.k.UnmapViewOfFile(self.pointer); self.pointer = None
        for field in ("mapping", "mutex"):
            handle = getattr(self, field, None)
            if handle: self.win.k.CloseHandle(handle); setattr(self, field, None)

    def close(self):
        if self.closed: return
        try:
            with self._lock(): self._put(36, self._get(36, U32) | PRODUCER_CLOSED, U32)
        finally:
            self.closed = True
            self._release()

    def __enter__(self): return self
    def __exit__(self, *_): self.close()

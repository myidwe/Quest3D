"""Bounded Windows file-wire handoff; no encoder or device-consumption ACK.

The new magic/object namespace cannot be opened by the legacy Opus reader.
Use a new immutable Scope after the actual authenticated launch nonce is known.
"""
from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
import hashlib
import os
import struct
import threading
import time

from .audio_bridge import _Windows
from .file_audio_protocol import Block, Scope, decode, encode

MAGIC = b"Q3DFAI1\0"
PREFIX = "Local\\Quest3D.FileAudio.v1."
HEADER_BYTES, SLOT_COUNT, SLOT_BYTES = 256, 8, 4096
CAPACITY = HEADER_BYTES + SLOT_COUNT * SLOT_BYTES
MAX_WIRE_BYTES = 3968
U32, U64 = struct.Struct("<I"), struct.Struct("<Q")
FAULTS = {1: "invalid_protocol", 2: "producer_lost", 3: "consumer_lost", 4: "abandoned_mutex"}


def pack_header(scope: Scope, producer_pid: int, producer_creation: int) -> bytes:
    if not isinstance(scope, Scope):
        raise ValueError("Expected immutable file scope")
    if type(producer_pid) is not int or not 0 < producer_pid < 2**32:
        raise ValueError("Invalid producer PID")
    if type(producer_creation) is not int or not 0 < producer_creation < 2**64:
        raise ValueError("Invalid producer creation FILETIME")
    header = bytearray(HEADER_BYTES)
    struct.pack_into("<8s4I", header, 0, MAGIC, 1, HEADER_BYTES, SLOT_COUNT, SLOT_BYTES)
    U32.pack_into(header, 24, producer_pid)
    U64.pack_into(header, 32, producer_creation)
    for at, value in ((48, scope.file_session), (64, scope.transport), (80, scope.channel)):
        header[at:at+16] = bytes.fromhex(value)
    U64.pack_into(header, 96, scope.epoch)
    U64.pack_into(header, 104, scope.generation)
    return bytes(header)


def pack_slot(block: Block) -> bytes:
    wire = encode(block)
    slot = bytearray(SLOT_BYTES)
    U64.pack_into(slot, 0, block.sequence)
    U32.pack_into(slot, 8, len(wire))
    slot[16:48] = hashlib.sha256(wire).digest()
    slot[48:48+len(wire)] = wire
    return bytes(slot)


def unpack_slot(slot: bytes, scope: Scope, sequence: int) -> Block:
    if len(slot) != SLOT_BYTES:
        raise ValueError("Invalid slot size")
    length = U32.unpack_from(slot, 8)[0]
    if not 128 <= length <= MAX_WIRE_BYTES or U32.unpack_from(slot, 12)[0] or any(slot[48+length:]):
        raise ValueError("Invalid slot length/reserved bytes")
    wire = slot[48:48+length]
    if hashlib.sha256(wire).digest() != slot[16:48]:
        raise ValueError("Changed file PCM bytes")
    block = decode(wire, scope)
    if block.sequence != sequence or U64.unpack_from(slot, 0)[0] != sequence:
        raise ValueError("Stale file IPC sequence")
    return block


class FileAudioIPCPublisher:
    """One immutable epoch, eight records, exactly one actual host consumer.

    offer(False) changes no queue cursor. A True result is local publication;
    handed_off_* reports only host queue ownership. Neither is audio playback.
    """
    def __init__(self, scope: Scope):
        self._lifetime_lock = threading.RLock()
        self.scope = scope
        self.win = _Windows()
        self.mapping = self.mutex = self.pointer = self.consumer_handle = None
        self.closed = False
        self._close_published = False
        self._tail = self._frames = self._head = self._handed_frames = 0
        self._eof = False
        self._consumer = None
        header = pack_header(scope, os.getpid(), self.win.creation(self.win.k.GetCurrentProcess()))
        self._identity = header
        self.name = PREFIX + scope.channel
        try:
            with self.win.security() as security:
                C.set_last_error(0)
                self.mutex = self.win.k.CreateMutexW(security, False, self.name + ".Mutex")
                if not self.mutex or C.get_last_error() == 183:
                    raise RuntimeError("File audio mutex exists or cannot be created")
                C.set_last_error(0)
                self.mapping = self.win.k.CreateFileMappingW(W.HANDLE(-1), security, 4, 0, CAPACITY, self.name)
                if not self.mapping or C.get_last_error() == 183:
                    raise RuntimeError("File audio mapping exists or cannot be created")
            self.pointer = self.win.k.MapViewOfFile(self.mapping, 0xF001F, 0, 0, CAPACITY)
            if not self.pointer:
                raise C.WinError(C.get_last_error())
            self.memory = (C.c_ubyte * CAPACITY).from_address(self.pointer)
            C.memmove(self.pointer, header, HEADER_BYTES)
        except BaseException:
            self._release()
            raise

    def _get(self, at, word=U64):
        return word.unpack_from(self.memory, at)[0]

    def _put(self, at, value, word=U64):
        word.pack_into(self.memory, at, value)

    @contextmanager
    def _lock(self, timeout_ms=50, *, allow_closed=False):
        with self._lifetime_lock:
            if self.closed and not allow_closed:
                raise RuntimeError("File audio publisher closed")
            wait = self.win.k.WaitForSingleObject(self.mutex, timeout_ms)
            if wait == 0x102:
                raise TimeoutError("File audio mutex busy")
            if wait not in (0, 0x80):
                raise C.WinError(C.get_last_error())
            try:
                if wait == 0x80:
                    self._put(152, 4, U32)
                self._put(160, time.perf_counter_ns())
                yield
            finally:
                if not self.win.k.ReleaseMutex(self.mutex):
                    raise C.WinError(C.get_last_error())

    def _validate(self):
        raw = C.string_at(self.pointer, HEADER_BYTES)
        if (raw[:28] != self._identity[:28] or raw[32:40] != self._identity[32:40]
                or raw[48:112] != self._identity[48:112] or any(raw[200:])):
            raise RuntimeError("File audio immutable identity changed")
        head, tail = self._get(112), self._get(120)
        handed = self._get(136)
        if (not self._head <= head <= tail == self._tail or tail-head > SLOT_COUNT
                or self._get(128) != self._frames or not self._handed_frames <= handed <= self._frames
                or self._get(176) != tail or self._get(184) != head
                or any(self._get(at, U32) > 1 for at in (144, 148, 156, 192, 196))
                or bool(self._get(192, U32)) != self._eof
                or (self._get(196, U32) and (not self._eof or head != tail))):
            raise RuntimeError("Invalid file audio handoff counters")
        pending_frames = 0
        for sequence in range(head, tail):
            at = HEADER_BYTES + sequence % SLOT_COUNT * SLOT_BYTES
            block = unpack_slot(C.string_at(self.pointer + at, SLOT_BYTES), self.scope, sequence)
            pending_frames += block.count
        if pending_frames != self._frames - handed:
            raise RuntimeError("File audio handoff samples disagree with retained queue")
        consumer = (self._get(28, U32), self._get(40))
        attached, stopped = self._get(156, U32), self._get(144, U32)
        if (bool(consumer[0]) != bool(consumer[1])
                or ((attached or stopped or head or handed or self._get(168) or self._get(196, U32)) and not all(consumer))
                or (all(consumer) and not attached and not stopped)):
            raise RuntimeError("Incomplete native consumer identity")
        if self._consumer is not None and consumer != self._consumer:
            raise RuntimeError("Native consumer identity changed")
        if all(consumer):
            if self._consumer is None:
                process = self.win.k.OpenProcess(0x100000 | 0x1000, False, consumer[0])
                matched = False
                try:
                    matched = bool(process and self.win.creation(process) == consumer[1])
                finally:
                    if process and not matched:
                        self.win.k.CloseHandle(process)
                if matched:
                    self.consumer_handle = process
                    self._consumer = consumer
                else:
                    self._put(152, 3, U32)
            if not stopped and (not self.consumer_handle or self.win.k.WaitForSingleObject(self.consumer_handle, 0) != 0x102):
                self._put(152, 3, U32)
        fault = self._get(152, U32)
        if fault:
            raise RuntimeError("File audio IPC fault: " + FAULTS.get(fault, "unknown"))
        self._head, self._handed_frames = head, handed

    def offer(self, block: Block) -> bool:
        if not isinstance(block, Block) or block.scope != self.scope:
            raise ValueError("Wrong file audio scope")
        packed = pack_slot(block)
        with self._lock():
            self._validate()
            if self._eof or block.sequence != self._tail:
                raise ValueError("File audio sequence already accepted, skipped or after EOF")
            if self._tail == 2**64 - 1 or self._frames + block.count >= 2**64:
                raise OverflowError("File audio IPC sequence/sample counter exhausted")
            if self._get(144, U32) or self._get(148, U32):
                return False
            if self._tail - self._head == SLOT_COUNT:
                return False
            at = HEADER_BYTES + self._tail % SLOT_COUNT * SLOT_BYTES
            C.memmove(self.pointer + at, packed, SLOT_BYTES)
            self._tail += 1
            self._frames += block.count
            self._eof = bool(block.flags)
            self._put(120, self._tail)
            self._put(128, self._frames)
            self._put(176, self._tail)
            self._put(192, int(self._eof), U32)
            return True

    def snapshot(self):
        with self._lock():
            self._validate()
            return {"scope": self.scope.request(), "producer_pid": self._get(24, U32),
                    "producer_creation_filetime": self._get(32), "consumer_pid": self._get(28, U32),
                    "consumer_creation_filetime": self._get(40), "consumer_attached": bool(self._get(156, U32)),
                    "consumer_closed": bool(self._get(144, U32)), "queued_records": self._tail-self._head,
                    "produced_records": self._tail, "handed_off_records": self._head,
                    "produced_frames": self._frames, "handed_off_frames": self._handed_frames,
                    "eof_produced": self._eof, "eof_handed_off": bool(self._get(196, U32)),
                    "observation": "host queue handoff only", "device_consumption_verified": False}

    def _release(self):
        if self.consumer_handle:
            if not self.win.k.CloseHandle(self.consumer_handle):
                raise C.WinError(C.get_last_error())
            self.consumer_handle = None
        if self.pointer:
            if not self.win.k.UnmapViewOfFile(self.pointer):
                raise C.WinError(C.get_last_error())
            self.pointer = None
        for name in ("mapping", "mutex"):
            handle = getattr(self, name)
            if handle:
                if not self.win.k.CloseHandle(handle):
                    raise C.WinError(C.get_last_error())
                setattr(self, name, None)

    def close(self):
        with self._lifetime_lock:
            self.closed = True  # Revoke local operations even if notification/cleanup must be retried.
            if not self._close_published:
                with self._lock(allow_closed=True):
                    self._put(148, 1, U32)
                self._close_published = True
            self._release()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

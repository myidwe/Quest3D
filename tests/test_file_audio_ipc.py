"""Wire/layout and real Windows producer tests; native peer covered separately."""
import ctypes as C
import os
import struct
import threading
import uuid

import numpy as np
import pytest

from quest3d.file_audio_protocol import Block, Scope
from quest3d.file_audio_ipc import (
    FileAudioIPCPublisher, HEADER_BYTES, SLOT_COUNT, SLOT_BYTES,
    pack_header, pack_slot, unpack_slot,
)


def scope():
    return Scope(uuid.uuid4().hex, uuid.uuid4().hex, uuid.uuid4().hex, 7, 9)


def block(identity, sequence=0, count=480):
    return Block(identity, sequence, sequence, sequence*10_000_000, count, 0,
                 np.full((count, 2), .25, np.float32))


def test_layout_preserves_exact_full_partial_and_eof():
    identity = scope()
    header = pack_header(identity, 123, 456)
    assert len(header) == 256 and header[:8] == b"Q3DFAI1\0"
    assert struct.unpack_from("<4I", header, 8) == (1, 256, 8, 4096)
    assert struct.unpack_from("<I", header, 24)[0] == 123
    assert struct.unpack_from("<Q", header, 32)[0] == 456
    assert header[48:96] == bytes.fromhex(identity.file_session+identity.transport+identity.channel)
    assert struct.unpack_from("<QQ", header, 96) == (7, 9)
    for value in (block(identity), block(identity, 1, 203),
                  Block(identity, 2, 2, 14229167, 0, 0, np.empty((0, 2), np.float32), flags=1)):
        packed = pack_slot(value)
        assert len(packed) == 4096
        actual = unpack_slot(packed, identity, value.sequence)
        assert actual.pts_ns == value.pts_ns and actual.count == value.count and actual.flags == value.flags
        assert actual.samples.tobytes() == value.samples.tobytes()


@pytest.mark.parametrize("at", [0, 8, 12, 16, 40, 48, 160, 4016, 4095])
def test_slot_mutation_rejected(at):
    identity = scope()
    wire = bytearray(pack_slot(block(identity)))
    wire[at] ^= 1
    with pytest.raises(ValueError):
        unpack_slot(bytes(wire), identity, 0)


def test_slot_old_scope_and_sequence_rejected():
    identity = scope()
    slot = pack_slot(block(identity))
    with pytest.raises(ValueError):
        unpack_slot(slot, scope(), 0)
    with pytest.raises(ValueError):
        unpack_slot(slot, identity, 1)


@pytest.mark.parametrize("pid,birth", [(0, 1), (True, 1), (2**32, 1), (1, 0), (1, True), (1, 2**64)])
def test_invalid_native_producer_identity(pid, birth):
    with pytest.raises(ValueError):
        pack_header(scope(), pid, birth)


windows = pytest.mark.skipif(os.name != "nt", reason="Actual private Win32 mapping")


@windows
def test_real_windows_bounded_publication_without_fake_consumer():
    identity = scope()
    with FileAudioIPCPublisher(identity) as publisher:
        for sequence in range(SLOT_COUNT):
            assert publisher.offer(block(identity, sequence))
        before = publisher.snapshot()
        slot_bytes = C.string_at(publisher.pointer+HEADER_BYTES, SLOT_COUNT*SLOT_BYTES)
        assert not publisher.offer(block(identity, SLOT_COUNT))
        assert publisher.snapshot() == before
        assert C.string_at(publisher.pointer+HEADER_BYTES, SLOT_COUNT*SLOT_BYTES) == slot_bytes
        assert before["produced_frames"] == 3840 and before["handed_off_frames"] == 0
        assert not before["consumer_attached"] and not before["device_consumption_verified"]
        with pytest.raises(ValueError):
            publisher.offer(block(identity, 0))
        with pytest.raises(ValueError):
            publisher.offer(block(scope(), 8))


@windows
def test_duplicate_named_mapping_preserves_original():
    identity = scope()
    with FileAudioIPCPublisher(identity) as original:
        assert original.offer(block(identity))
        before = original.snapshot()
        with pytest.raises(RuntimeError, match="exists"):
            FileAudioIPCPublisher(identity)
        assert original.snapshot() == before


@windows
def test_explicit_eof_is_neither_handoff_nor_device_consumption():
    identity = scope()
    with FileAudioIPCPublisher(identity) as publisher:
        assert publisher.offer(block(identity, 0, 203))
        eof = Block(identity, 1, 1, 4229167, 0, 0, np.empty((0, 2), np.float32), flags=1)
        assert publisher.offer(eof)
        state = publisher.snapshot()
        assert state["eof_produced"] and not state["eof_handed_off"]
        assert state["produced_frames"] == 203 and state["handed_off_frames"] == 0
        with pytest.raises(ValueError):
            publisher.offer(block(identity, 2))


@windows
@pytest.mark.parametrize("offset,value", [(48, 0), (120, 3), (184, 1), (200, 1), (156, 2)])
def test_peer_mutated_identity_cursors_reserved_or_flags_rejected(offset, value):
    identity = scope()
    with FileAudioIPCPublisher(identity) as publisher:
        assert publisher.offer(block(identity))
        publisher.memory[offset] = (publisher.memory[offset] ^ 1) if offset == 48 else value
        with pytest.raises((ValueError, RuntimeError)):
            publisher.snapshot()


@windows
def test_closed_publisher_cannot_reuse_mapping_memory():
    publisher = FileAudioIPCPublisher(scope())
    publisher.close()
    assert publisher.closed and publisher.pointer is None and publisher.mapping is None and publisher.mutex is None
    publisher.close()
    with pytest.raises(RuntimeError, match="closed"):
        publisher.snapshot()


@windows
def test_close_waits_for_actual_local_mapping_user():
    publisher = FileAudioIPCPublisher(scope())
    entered, release, returned = threading.Event(), threading.Event(), threading.Event()
    def held_operation():
        with publisher._lock():
            entered.set()
            assert release.wait(5)
            assert publisher.pointer is not None
    thread = threading.Thread(target=held_operation)
    thread.start()
    assert entered.wait(5)
    closer = threading.Thread(target=lambda: (publisher.close(), returned.set()))
    closer.start()
    try:
        assert not returned.wait(.1)
    finally:
        release.set()
        thread.join(5)
        closer.join(5)
    assert not thread.is_alive() and not closer.is_alive() and returned.is_set()
    assert publisher.closed and publisher.pointer is None


@windows
def test_busy_native_mutex_preserves_close_notification_for_retry():
    publisher = FileAudioIPCPublisher(scope())
    held, release = threading.Event(), threading.Event()
    def external_style_holder():
        assert publisher.win.k.WaitForSingleObject(publisher.mutex, 5000) == 0
        held.set()
        assert release.wait(5)
        assert publisher.win.k.ReleaseMutex(publisher.mutex)
    thread = threading.Thread(target=external_style_holder)
    thread.start()
    assert held.wait(5)
    try:
        with pytest.raises(TimeoutError):
            publisher.close()
        assert publisher.closed and not publisher._close_published
        assert publisher.pointer and publisher.mapping and publisher.mutex
        assert publisher._get(148, struct.Struct("<I")) == 0
        with pytest.raises(RuntimeError, match="closed"):
            publisher.snapshot()
    finally:
        release.set()
        thread.join(5)
        publisher.close()
    assert publisher._close_published and publisher.pointer is None


@windows
def test_partial_cleanup_retries_without_accessing_unmapped_view(monkeypatch):
    publisher = FileAudioIPCPublisher(scope())
    actual_close = publisher.win.k.CloseHandle
    mapping = publisher.mapping
    failed = False
    def fail_mapping_once(handle):
        nonlocal failed
        if handle == mapping and not failed:
            failed = True
            C.set_last_error(6)
            return False
        return actual_close(handle)
    monkeypatch.setattr(publisher.win.k, "CloseHandle", fail_mapping_once)
    with pytest.raises(OSError):
        publisher.close()
    assert publisher.closed and publisher._close_published and publisher.pointer is None
    assert publisher.mapping == mapping
    with pytest.raises(RuntimeError, match="closed"):
        publisher.snapshot()
    publisher.close()
    assert publisher.mapping is None and publisher.mutex is None


@windows
def test_creation_query_exception_closes_actual_opened_process_handle(monkeypatch):
    publisher = FileAudioIPCPublisher(scope())
    actual_open, actual_close = publisher.win.k.OpenProcess, publisher.win.k.CloseHandle
    opened, closed = [], []
    def record_open(*args):
        handle = actual_open(*args)
        opened.append(handle)
        return handle
    def record_close(handle):
        closed.append(handle)
        return actual_close(handle)
    def fail_creation(_):
        raise OSError("injected GetProcessTimes failure")
    # Explicit fault fixture peer fields; not evidence of a native consumer.
    with publisher._lock():
        publisher._put(28, os.getpid(), struct.Struct("<I"))
        publisher._put(40, publisher._get(32))
        publisher._put(156, 1, struct.Struct("<I"))
    monkeypatch.setattr(publisher.win.k, "OpenProcess", record_open)
    monkeypatch.setattr(publisher.win.k, "CloseHandle", record_close)
    monkeypatch.setattr(publisher.win, "creation", fail_creation)
    try:
        with pytest.raises(OSError, match="GetProcessTimes"):
            publisher.snapshot()
        assert len(opened) == 1 and opened[0] and opened[0] in closed
        assert publisher.consumer_handle is None
    finally:
        publisher.close()


@windows
def test_handoff_without_registered_native_consumer_rejected():
    identity = scope()
    with FileAudioIPCPublisher(identity) as publisher:
        assert publisher.offer(block(identity))
        # All sample/queue arithmetic agrees; the missing owner must still fail.
        with publisher._lock():
            publisher._put(112, 1)
            publisher._put(136, 480)
            publisher._put(184, 1)
        with pytest.raises(RuntimeError, match="consumer identity"):
            publisher.snapshot()

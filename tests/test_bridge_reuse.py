"""Deterministic publisher fault injection without Windows IPC or GPU work."""

import gc
import mmap
import os
import sys
import threading
import uuid
import weakref
from contextlib import contextmanager

import numpy as np
import pytest

from quest3d import bridge
from quest3d.bridge import FrameHeader, FramePublisher, FULL_SBS, ORIGINAL_2D
from quest3d.bridge_v3 import SourceFrameHeader
from quest3d.geometry import ScreenRect
from quest3d.source_identity import SourceIdentity, SourceKind


class FakeKernel:
    def __init__(self):
        self.handle = 0
        self.waits = []
        self.events = 0
        self.releases = 0
        self.fail_event = False
        self.fail_release = False

    def CreateMutexW(self, *args):
        self.handle += 1
        return self.handle

    CreateEventW = CreateMutexW

    def WaitForSingleObject(self, *args):
        result = self.waits.pop(0) if self.waits else 0
        if isinstance(result, BaseException):
            raise result
        return result

    def ReleaseMutex(self, *args):
        self.releases += 1
        return not self.fail_release

    def SetEvent(self, *args):
        self.events += 1
        return not self.fail_event

    def CloseHandle(self, *args):
        return True


class FakeMemory:
    def __init__(self, storage):
        self.storage = storage
        self.position = 0
        self.writes = []
        self.failure = None
        self.closed = False

    def seek(self, position):
        self.position = position

    def write(self, data):
        data = bytes(data)
        self.writes.append((self.position, data))
        failure = self.failure
        if failure and failure[0] == len(self.writes):
            self.failure = None
            written = len(data) // 2
            self.storage[self.position:self.position + written] = data[:written]
            if failure[1] == "raise":
                raise OSError("injected partial mapping write")
            return written
        end = self.position + len(data)
        if len(self.storage) < end:
            self.storage.extend(bytes(end - len(self.storage)))
        self.storage[self.position:end] = data
        self.position = end
        return len(data)

    def __setitem__(self, key, value):
        self.storage[key] = value

    def close(self):
        self.closed = True


@pytest.fixture
def mapping(monkeypatch):
    kernel = FakeKernel()
    storage = {}
    monkeypatch.setattr(bridge, "kernel32", lambda: kernel)

    def open_mapping(*args, tagname, **kwargs):
        return FakeMemory(storage.setdefault(tagname, bytearray(192)))

    monkeypatch.setattr(bridge.mmap, "mmap", open_mapping)
    return kernel


def image():
    return np.arange(4 * 8 * 4, dtype=np.uint8).reshape(4, 8, 4)


def identity(selection=1):
    return SourceIdentity(SourceKind.MONITOR, 0, selection, 19, 0,
                          ScreenRect(-1920, 0, 1920, 1080))


def publish(owner, pixels, frame_id=1, **kwargs):
    values = dict(frame_id=frame_id, capture_ns=100 + frame_id,
                  generation=0, flags=FULL_SBS,
                  source_rect=(-1920, 0, 1920, 1080),
                  content_rect=(0, 0, pixels.shape[1] // 2, pixels.shape[0]),
                  immutable_payload=True)
    if owner.version == 3:
        values["source_identity"] = identity()
    values.update(kwargs)
    return owner.publish(pixels, **values)


def read(owner):
    data = owner.memory.storage
    header_type = FrameHeader if owner.version == 2 else SourceFrameHeader
    header = header_type.unpack(bytes(data[:owner.header_size]))
    return header, bytes(data[owner.header_size:owner.header_size + header.width * header.height * 4])


@pytest.mark.parametrize("version", [2, 3])
def test_reuse_preserves_pixels_but_updates_all_metadata_and_event(mapping, version):
    with FramePublisher(version=version) as owner:
        pixels = image()
        assert publish(owner, pixels)
        before, payload = read(owner)
        options = dict(capture_ns=999, generation=7, flags=FULL_SBS | ORIGINAL_2D,
                       source_rect=(-1910, 10, 100, 100), content_rect=(1, 1, 2, 2))
        if version == 3:
            options["source_identity"] = identity(9)
        assert publish(owner, pixels, 2, **options)
        after, new_payload = read(owner)
        assert payload == new_payload == pixels.tobytes()
        assert after.frame_id == 2 and after.capture_ns == 999 and after.generation == 7
        assert after.flags == FULL_SBS | ORIGINAL_2D
        assert (after.source_left, after.source_top, after.source_width, after.source_height) == (-1910, 10, 100, 100)
        assert (after.content_left, after.content_top, after.content_width, after.content_height) == (1, 1, 2, 2)
        assert after.publish_ns > before.publish_ns and after.stream_epoch == before.stream_epoch
        if version == 3:
            assert after.source_identity.selection_id == 9
        assert mapping.events == 2
        metrics = owner.snapshot()
        assert metrics["publish_count"] == 2 and metrics["copied_count"] == 1 and metrics["reused_count"] == 1
        assert metrics["copied_bytes"] == 0 and metrics["reused_bytes"] == pixels.nbytes
        assert metrics["copied_bytes_total"] == metrics["reused_bytes_total"] == pixels.nbytes
        assert metrics["mutex_wait_ms"] >= 0 and metrics["write_ms"] >= 0
        metrics["reused_count"] = 1000
        assert owner.snapshot()["reused_count"] == 1


def test_default_mutable_input_always_copies_and_invalidates_previous_optin(mapping):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        pixels[:] = 42
        assert publish(owner, pixels, 2, immutable_payload=False)
        assert read(owner)[1] == pixels.tobytes()
        assert not owner.snapshot()["reused_payload"]
        assert publish(owner, pixels, 3)
        assert not owner.snapshot()["reused_payload"]
        assert publish(owner, pixels, 4)
        assert owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("kind", ["alias", "copy", "noncontiguous"])
def test_only_same_object_is_reused_and_noncontiguous_conversion_is_skipped(mapping, monkeypatch, kind):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        other = pixels.view() if kind == "alias" else pixels.copy() if kind == "copy" else pixels[:, ::-1]
        assert publish(owner, other, 2)
        assert not owner.snapshot()["reused_payload"]
        assert read(owner)[1] == other.tobytes()
        monkeypatch.setattr(bridge.np, "ascontiguousarray", lambda value: pytest.fail("reuse converted pixels"))
        assert publish(owner, other, 3)
        assert owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("change", ["shape", "strides", "pointer"])
def test_same_object_changed_layout_or_address_requires_fresh_copy(mapping, change):
    with FramePublisher() as owner:
        pixels = image().copy()
        assert publish(owner, pixels)
        old_pointer = pixels.ctypes.data
        if change == "shape":
            pixels.shape = (2, 16, 4)
        elif change == "strides":
            pixels.strides = (0, 4, 1)
        else:
            # Force a new allocation while keeping the same ndarray identity.
            # A live adjacent allocation reduces in-place extension opportunity;
            # a bounded larger retry handles allocators that still extend it.
            blocker = image().copy()
            for width in (1024, 2048, 4096):
                pixels.resize((4, width, 4), refcheck=False)
                if pixels.ctypes.data != old_pointer:
                    break
            assert pixels.ctypes.data != old_pointer
            assert blocker.shape == (4, 8, 4)
        assert publish(owner, pixels, 2)
        assert not owner.snapshot()["reused_payload"]
        assert read(owner)[1] == pixels.tobytes()


def test_cached_array_has_strong_lifetime_and_close_releases_it(mapping):
    owner = FramePublisher()
    pixels = image()
    reference = weakref.ref(pixels)
    assert publish(owner, pixels)
    del pixels
    gc.collect()
    assert reference() is not None
    owner.close()
    gc.collect()
    assert reference() is None
    with pytest.raises(RuntimeError, match="closed"):
        publish(owner, image(), 2)


@pytest.mark.parametrize("result", [0x102, 0x80, 0xFFFFFFFF, OSError("injected wait")])
def test_busy_abandoned_and_failed_wait_cannot_certify_reuse(mapping, result):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        previous = read(owner)
        if result == 0x80:
            owner.memory.storage[owner.header_size:] = bytes(len(previous[1]))
        mapping.waits.append(result)
        if isinstance(result, BaseException) or result == 0xFFFFFFFF:
            with pytest.raises(OSError):
                publish(owner, pixels, 2)
            assert owner.snapshot()["error_count"] == 1
        elif result == 0x102:
            assert publish(owner, pixels, 2) is False
            assert read(owner) == previous
            assert owner.snapshot()["skipped_count"] == 1
        else:
            assert publish(owner, pixels, 2)
            assert not owner.snapshot()["reused_payload"]
            assert read(owner)[1] == pixels.tobytes()
        assert publish(owner, pixels, 3)
        assert not owner.snapshot()["reused_payload"]
        assert publish(owner, pixels, 4)
        assert owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("failure_step", [1, 2, 3])
@pytest.mark.parametrize("failure_kind", ["raise", "short"])
def test_partial_write_failure_invalidates_header_and_forces_full_retry(mapping, failure_step, failure_kind):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        newer = pixels.copy()
        newer[:] = 82
        owner.memory.failure = (len(owner.memory.writes) + failure_step, failure_kind)
        with pytest.raises(OSError):
            publish(owner, newer, 2)
        assert owner.memory.storage[:owner.header_size] == bytes(owner.header_size)
        assert owner.last_frame_id == 1 and owner.snapshot()["error_count"] == 1
        assert publish(owner, pixels, 2)
        assert not owner.snapshot()["reused_payload"]
        assert read(owner)[1] == pixels.tobytes()


def test_partial_reused_header_failure_cannot_retain_reuse_cache(mapping):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        owner.memory.failure = (len(owner.memory.writes) + 2, "short")
        with pytest.raises(OSError):
            publish(owner, pixels, 2)
        assert owner.memory.storage[:owner.header_size] == bytes(owner.header_size)
        assert publish(owner, pixels, 2)
        assert not owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("failure", ["fail_event", "fail_release"])
def test_committed_header_consumes_id_but_signal_error_does_not_cache(mapping, failure):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        setattr(mapping, failure, True)
        with pytest.raises(OSError):
            publish(owner, pixels, 2)
        setattr(mapping, failure, False)
        assert owner.last_frame_id == 2 and read(owner)[0].frame_id == 2
        assert owner.snapshot()["reused_count"] == 0
        assert publish(owner, pixels, 3)
        assert not owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("invalid", [dict(frame_id=1), dict(flags=16),
    dict(source_rect=(0, 0, 0, 0)), dict(content_rect=(3, 0, 4, 4)),
    dict(immutable_payload=1), dict(source_identity=identity())])
def test_validation_errors_preserve_frame_but_invalidate_cache(mapping, invalid):
    with FramePublisher() as owner:
        pixels = image()
        assert publish(owner, pixels)
        previous = read(owner)
        with pytest.raises(ValueError):
            publish(owner, pixels, **{"frame_id": 2, **invalid})
        assert read(owner) == previous
        assert publish(owner, pixels, 2)
        assert not owner.snapshot()["reused_payload"]


@pytest.mark.parametrize("version", [2, 3])
def test_restart_has_no_payload_cache_even_with_same_object_and_surviving_mapping(mapping, version):
    pixels = image()
    with FramePublisher(version=version) as first:
        assert publish(first, pixels)
        epoch = first.epoch
    with FramePublisher(version=version) as second:
        assert second.epoch != epoch
        assert publish(second, pixels)
        assert not second.snapshot()["reused_payload"]
        assert read(second)[1] == pixels.tobytes()


real_ipc = pytest.mark.skipif(
    sys.platform != "win32" or os.environ.get("Q3D_RUN_BRIDGE_IPC") != "1",
    reason="Explicit Q3D_RUN_BRIDGE_IPC=1 required for isolated real Windows IPC",
)


@contextmanager
def real_reader(prefix, version):
    kernel = bridge.kernel32()
    mutex = kernel.CreateMutexW(None, False, prefix + f".Mutex.v{version}")
    size = 128 if version == 2 else 192
    memory = mmap.mmap(-1, size + bridge.MAX_WIDTH * bridge.MAX_HEIGHT * 4,
                       tagname=prefix + f".v{version}", access=mmap.ACCESS_READ)
    try:
        def snapshot():
            assert kernel.WaitForSingleObject(mutex, 1000) == 0
            try:
                raw = bytes(memory[:size])
                header_type = FrameHeader if version == 2 else SourceFrameHeader
                header = header_type.unpack(raw)
                payload = bytes(memory[size:size + header.width * header.height * 4])
                return header, payload
            finally:
                assert kernel.ReleaseMutex(mutex)
        yield snapshot
    finally:
        memory.close()
        kernel.CloseHandle(mutex)


@real_ipc
@pytest.mark.parametrize("version", [2, 3])
def test_real_ipc_reuse_updates_header_and_cursor_replacement_writes_new_pixels(version):
    prefix = "Local\\Quest3D.Test.Reuse." + uuid.uuid4().hex
    pixels = image()
    with FramePublisher(prefix, version=version) as owner, real_reader(prefix, version) as snapshot:
        assert publish(owner, pixels)
        before, expected = snapshot()
        changes = dict(capture_ns=512, generation=4, flags=FULL_SBS | ORIGINAL_2D,
                       source_rect=(-1910, 10, 100, 100), content_rect=(1, 1, 2, 2))
        if version == 3:
            changes["source_identity"] = identity(8)
        assert publish(owner, pixels, 2, **changes)
        after, payload = snapshot()
        assert expected == payload == pixels.tobytes()
        assert after.frame_id == 2 and after.capture_ns == 512 and after.generation == 4
        assert after.publish_ns > before.publish_ns
        assert after.flags == FULL_SBS | ORIGINAL_2D
        assert (after.source_left, after.source_top, after.source_width, after.source_height) == (-1910, 10, 100, 100)
        assert (after.content_left, after.content_top, after.content_width, after.content_height) == (1, 1, 2, 2)
        if version == 3:
            assert after.source_identity.selection_id == 8
        assert owner.snapshot()["reused_payload"]
        # Cursor overlay changes create a new owned image, never mutate the old.
        with_cursor = pixels.copy()
        with_cursor[1:3, 2:4] = (10, 20, 30, 255)
        assert publish(owner, with_cursor, 3)
        assert snapshot()[1] == with_cursor.tobytes() != expected
        assert not owner.snapshot()["reused_payload"]
        assert publish(owner, with_cursor, 4)
        assert owner.snapshot()["reused_payload"]


@real_ipc
@pytest.mark.parametrize("version", [2, 3])
def test_real_ipc_busy_mutex_forces_next_copy_and_restart_is_fresh(version):
    prefix = "Local\\Quest3D.Test.Reuse." + uuid.uuid4().hex
    pixels = image()
    locked, release = threading.Event(), threading.Event()
    errors = []

    def hold_mutex():
        kernel = bridge.kernel32()
        mutex = kernel.CreateMutexW(None, False, prefix + f".Mutex.v{version}")
        try:
            assert kernel.WaitForSingleObject(mutex, 1000) == 0
            locked.set()
            assert release.wait(5)
            assert kernel.ReleaseMutex(mutex)
        except BaseException as exc:
            errors.append(exc)
            locked.set()
        finally:
            kernel.CloseHandle(mutex)

    first = FramePublisher(prefix, version=version)
    with real_reader(prefix, version) as snapshot:
        try:
            assert publish(first, pixels)
            before = snapshot()
            assert publish(first, pixels, 2)
            assert first.snapshot()["reused_payload"]
            holder = threading.Thread(target=hold_mutex)
            holder.start()
            try:
                assert locked.wait(5) and not errors
                assert publish(first, pixels, 3) is False
                assert first.snapshot()["status"] == "skipped"
            finally:
                release.set()
                holder.join(5)
            assert not holder.is_alive() and not errors
            assert snapshot()[0].frame_id == 2
            assert publish(first, pixels, 3)
            assert not first.snapshot()["reused_payload"]
            assert snapshot()[1] == pixels.tobytes()
        finally:
            first.close()
        with FramePublisher(prefix, version=version) as second:
            changed = pixels.copy()
            changed[:] = 123
            assert publish(second, changed)
            header, payload = snapshot()
            assert header.stream_epoch != before[0].stream_epoch and header.frame_id == 1
            assert payload == changed.tobytes()
            assert not second.snapshot()["reused_payload"]

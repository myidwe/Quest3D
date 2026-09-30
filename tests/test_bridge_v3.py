"""Immutable source identity at the producer/native boundary, including real IPC."""
from contextlib import contextmanager
from dataclasses import FrozenInstanceError, replace
import mmap
import struct
import sys
import threading
import uuid

import numpy as np
import pytest

from quest3d.bridge import (FrameHeader, FramePublisher, FULL_SBS, ORIGINAL_2D,
                            INPUT_ENABLED, MAX_WIDTH, MAX_HEIGHT, kernel32)
from quest3d.bridge_v3 import SourceFrameHeader
from quest3d.capture import DesktopCapture, MonitorInfo
from quest3d.geometry import ScreenRect
from quest3d.source_identity import SourceIdentity, SourceKind


def monitor(**changes):
    value = SourceIdentity(SourceKind.MONITOR, 0, (1 << 63) + 17, 0x800000000001, 0,
                           ScreenRect(-1920, -200, 1920, 1080))
    return replace(value, **changes)


def header(**changes):
    value = SourceFrameHeader(width=64, height=32, frame_id=19, capture_ns=400,
        publish_ns=500, generation=7, flags=FULL_SBS | ORIGINAL_2D,
        source_left=-1900, source_top=-100, source_width=1280, source_height=720,
        stream_epoch=(1 << 63) + 11, content_width=32, content_height=18,
        source_identity=monitor())
    return replace(value, **changes)


def test_exact_192_byte_wire_and_unsigned_lifetime_are_not_truncated():
    value = header()
    wire = value.pack()
    assert len(wire) == 192 and wire[:8] == b"Q3DFRM3\0"
    assert struct.unpack_from("<II", wire, 8) == (3, 192)
    assert struct.unpack_from("<Q", wire, 88)[0] == (1 << 63) + 11
    assert struct.unpack_from("<IIQQQ", wire, 128) == (1, 0, (1 << 63) + 17, 0x800000000001, 0)
    assert struct.unpack_from("<2i2I", wire, 160) == (-1920, -200, 1920, 1080)
    assert wire[112:128] == bytes(16) and wire[176:192] == bytes(16)
    assert SourceFrameHeader.unpack(wire) == value
    with pytest.raises(ValueError):
        FrameHeader.unpack(wire[:128])


@pytest.mark.parametrize("offset,value", [(8, 2), (12, 128), (112, 1), (127, 1), (176, 1), (191, 1)])
def test_v3_rejects_wrong_protocol_and_all_reserved_regions(offset, value):
    wire = bytearray(header().pack())
    wire[offset] = value
    with pytest.raises(ValueError):
        SourceFrameHeader.unpack(wire)


@pytest.mark.parametrize("field,value", [("kind", 1), ("process_id", True),
    ("process_id", 1 << 32), ("selection_id", 0), ("selection_id", -1),
    ("selection_id", 1 << 64), ("native_handle", 0), ("native_handle", -1),
    ("native_handle", 1 << 64), ("creation_filetime", 1)])
def test_monitor_identity_rejects_missing_handle_and_invalid_integer_domain(field, value):
    with pytest.raises(ValueError):
        monitor(**{field: value})


def test_window_requires_process_birth_and_carries_full_hwnd():
    window = SourceIdentity(SourceKind.WINDOW, 1234, 99, (1 << 63) + 33,
        134334378605162747, ScreenRect(-1920, -200, 1920, 1080))
    assert SourceFrameHeader.unpack(header(source_identity=window).pack()).source_identity == window
    for field in ("process_id", "native_handle", "creation_filetime"):
        with pytest.raises(ValueError):
            replace(window, **{field: 0})
    with pytest.raises(FrozenInstanceError):
        window.selection_id = 1


@pytest.mark.parametrize("kind", [SourceKind.VIDEO, SourceKind.PHOTO])
def test_files_display_but_cannot_claim_os_identity_or_enable_os_input(kind):
    source = SourceIdentity(kind, 0, 123, 0, 0, ScreenRect(0, 0, 1920, 1080))
    value = header(source_left=0, source_top=0, source_identity=source)
    assert SourceFrameHeader.unpack(value.pack()) == value
    with pytest.raises(ValueError, match="File source cannot enable"):
        replace(value, flags=FULL_SBS | ORIGINAL_2D | INPUT_ENABLED).pack()
    for changes in ({"process_id": 1}, {"native_handle": 1}, {"creation_filetime": 1},
                    {"parent_bounds": ScreenRect(1, 0, 1920, 1080)}):
        with pytest.raises(ValueError):
            replace(source, **changes)


@pytest.mark.parametrize("changes", [{"source_left": -1921}, {"source_top": -201},
    {"source_width": 3000}, {"source_height": 2000}, {"source_identity": None}])
def test_reject_missing_or_outside_source_identity(changes):
    with pytest.raises(ValueError):
        header(**changes).pack()


def test_input_requires_original_2d_even_when_stereo_content_geometry_is_valid():
    assert SourceFrameHeader.unpack(header(flags=FULL_SBS | ORIGINAL_2D | INPUT_ENABLED).pack())
    with pytest.raises(ValueError, match="unwarped original 2D"):
        header(flags=FULL_SBS | INPUT_ENABLED).pack()


def test_monitor_crop_tracks_parent_and_refuses_virtual_spanning_or_ambiguous_sources():
    monitors = [MonitorInfo(0, ScreenRect(-1920, 0, 3840, 1080), "virtual", False),
        MonitorInfo(1, ScreenRect(-1920, 0, 1920, 1080), "left", True, 123),
        MonitorInfo(2, ScreenRect(0, 0, 1920, 1080), "right", False, 456)]
    crop = ScreenRect(-500, 100, 400, 300)
    identity = DesktopCapture._identity(monitors, crop, 100)
    assert identity.parent_bounds == monitors[1].bounds and identity.native_handle == 123
    assert DesktopCapture._identity(monitors, ScreenRect(-100, 0, 200, 100), 100) is None
    assert DesktopCapture._identity(monitors, monitors[0].bounds, 100) is None
    assert DesktopCapture._identity(monitors + [monitors[1]], crop, 100) is None
    assert DesktopCapture._identity([monitors[0], replace(monitors[1], native_handle=0)], crop, 100) is None


@contextmanager
def reader(prefix, version):
    kernel = kernel32()
    mutex = kernel.CreateMutexW(None, False, prefix + f".Mutex.v{version}")
    size = 192 if version == 3 else 128
    memory = mmap.mmap(-1, size + MAX_WIDTH * MAX_HEIGHT * 4,
                       tagname=prefix + f".v{version}", access=mmap.ACCESS_READ)
    try:
        def snapshot():
            assert kernel.WaitForSingleObject(mutex, 1000) == 0
            try:
                raw = bytes(memory[:size])
                payload = bytes(memory[size:size + struct.unpack_from("<I", raw, 64)[0]])
                return raw, payload
            finally:
                kernel.ReleaseMutex(mutex)
        yield snapshot
    finally:
        memory.close()
        kernel.CloseHandle(mutex)


windows = pytest.mark.skipif(sys.platform != "win32", reason="real Windows named IPC required")


@windows
def test_real_named_mapping_v2_v3_are_isolated_and_v3_payload_starts_at_192():
    prefix = "Local\\Quest3D.Test.Source." + uuid.uuid4().hex
    image = np.arange(32 * 64 * 4, dtype=np.uint8).reshape(32, 64, 4)
    metadata = dict(frame_id=1, capture_ns=100, generation=0, flags=FULL_SBS | ORIGINAL_2D,
                    source_rect=(-1900, -100, 1280, 720), content_rect=(0, 0, 32, 18))
    with FramePublisher(prefix) as v2, FramePublisher(prefix, version=3) as v3, \
            reader(prefix, 2) as read2, reader(prefix, 3) as read3:
        assert v2.publish(image, **metadata)
        before = read2()
        assert v3.publish(image[:, ::-1], source_identity=monitor(), **metadata)
        wire, pixels = read3()
        received = SourceFrameHeader.unpack(wire)
        assert received.source_identity == monitor() and received.stream_epoch == v3.epoch
        assert pixels == image[:, ::-1].tobytes(order="C")
        assert read2() == before
        with pytest.raises(RuntimeError, match="already running"):
            FramePublisher(prefix, version=3)
        assert read3() == (wire, pixels)


@windows
def test_publisher_rejection_preserves_previous_complete_frame_and_can_recover():
    prefix = "Local\\Quest3D.Test.Source." + uuid.uuid4().hex
    image = np.ones((32, 64, 4), dtype=np.uint8)
    metadata = dict(frame_id=1, capture_ns=100, generation=0, flags=FULL_SBS,
                    source_rect=(-1900, -100, 1280, 720))
    with FramePublisher(prefix, version=3) as publisher, reader(prefix, 3) as read:
        assert publisher.publish(image, source_identity=monitor(), **metadata)
        before = read()
        metadata["frame_id"] = 2
        with pytest.raises(ValueError, match="immutable source identity"):
            publisher.publish(image * 2, **metadata)
        assert read() == before
        assert publisher.publish(image * 2, source_identity=monitor(), **metadata)
        assert read()[1] == (image * 2).tobytes()
    with FramePublisher(prefix) as v2:
        with pytest.raises(ValueError, match="v2 cannot silently"):
            v2.publish(image, source_identity=monitor(), **metadata)


@windows
def test_restarting_writer_invalidates_surviving_mapping_before_owner_is_ready(monkeypatch):
    from quest3d import bridge
    prefix = "Local\\Quest3D.Test.Source." + uuid.uuid4().hex
    image = np.ones((32, 64, 4), dtype=np.uint8)
    old = FramePublisher(prefix, version=3)
    metadata = dict(frame_id=42, capture_ns=100, generation=0, flags=FULL_SBS,
                    source_rect=(-1900, -100, 1280, 720), source_identity=monitor())

    def other_thread_owner_state():
        states = []
        def observe():
            kernel = kernel32()
            handle = kernel.CreateMutexW(None, False, prefix + ".Producer.v3")
            try:
                state = kernel.WaitForSingleObject(handle, 0)
                states.append(state)
                if state in (0, 0x80): kernel.ReleaseMutex(handle)
            finally:
                kernel.CloseHandle(handle)
        thread = threading.Thread(target=observe); thread.start(); thread.join(timeout=2)
        assert not thread.is_alive() and len(states) == 1
        return states[0]

    with reader(prefix, 3) as read:
        assert old.publish(image, **metadata)
        previous = read()
        old.close()
        real_mmap = mmap.mmap
        observed = []
        def during_open(*args, **kwargs):
            memory = real_mmap(*args, **kwargs)
            # Deliberately observe the previously valid header at the point
            # before the new producer is allowed to advertise a live owner.
            assert bytes(memory[:192]) == previous[0]
            observed.append(other_thread_owner_state())
            return memory
        monkeypatch.setattr(bridge.mmap, "mmap", during_open)
        with FramePublisher(prefix, version=3) as new:
            assert observed == [0], "Old header was exposed under a live new producer"
            assert read()[0] == bytes(192)
            assert other_thread_owner_state() == 0x102
            metadata["frame_id"] = 1
            assert new.publish(image * 2, **metadata)
            assert read()[1] == (image * 2).tobytes()
            assert SourceFrameHeader.unpack(read()[0]).stream_epoch == new.epoch


@windows
def test_failed_v3_mapping_open_releases_producer_and_named_handles(monkeypatch):
    from quest3d import bridge
    prefix = "Local\\Quest3D.Test.Source." + uuid.uuid4().hex
    real_mmap = mmap.mmap
    def fail(*args, **kwargs):
        raise OSError("simulated mapping allocation failure")
    monkeypatch.setattr(bridge.mmap, "mmap", fail)
    with pytest.raises(OSError, match="allocation failure"):
        FramePublisher(prefix, version=3)
    monkeypatch.setattr(bridge.mmap, "mmap", real_mmap)
    with FramePublisher(prefix, version=3):
        pass

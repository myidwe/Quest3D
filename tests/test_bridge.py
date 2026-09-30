"""Wire-format and real Windows named-IPC tests; no screenshots or input injection.

Each IPC test uses an unpredictable private Local namespace. The independent
reader below only reads the mapping; it is a test harness, not the product client.
"""

from contextlib import contextmanager
import ctypes
import mmap
import struct
import subprocess
import sys
import threading
import uuid

import numpy as np
import pytest

from quest3d.bridge import (
    CAPACITY, CAPTURE_RECEIPT, FULL_SBS, HEADER_SIZE, MAGIC,
    MAX_HEIGHT, MAX_WIDTH, ORIGINAL_2D, INPUT_ENABLED, FrameHeader, FramePublisher, kernel32,
)


def _header(**overrides):
    values = dict(width=64, height=32, frame_id=123, capture_ns=456, publish_ns=789,
                  generation=7, flags=FULL_SBS | CAPTURE_RECEIPT,
                  source_left=-1920, source_top=-200, source_width=1920,
                  source_height=1080, stream_epoch=987654321)
    values.update(overrides)
    return FrameHeader(**values)


def test_header_offsets_match_128_byte_little_endian_contract():
    header = _header()
    packed = header.pack()
    assert len(packed) == HEADER_SIZE == 128
    assert packed[:8] == MAGIC == b"Q3DFRM2\0"
    assert struct.unpack_from("<6I", packed, 8) == (2, 128, 64, 32, 256, 1)
    assert struct.unpack_from("<4Q", packed, 32) == (123, 456, 789, 7)
    assert struct.unpack_from("<2I", packed, 64) == (64 * 32 * 4, FULL_SBS | CAPTURE_RECEIPT)
    assert struct.unpack_from("<2i2I", packed, 72) == (-1920, -200, 1920, 1080)
    assert struct.unpack_from("<Q", packed, 88) == (987654321,)
    assert packed[96:] == bytes(32)
    assert FrameHeader.unpack(packed) == header
    assert CAPACITY == 128 + MAX_WIDTH * MAX_HEIGHT * 4


@pytest.mark.parametrize("offset,fmt,value", [
    (8, "I", 1), (12, "I", 127), (16, "I", 0), (16, "I", MAX_WIDTH + 1),
    (20, "I", MAX_HEIGHT + 1), (24, "I", 255), (28, "I", 2),
    (64, "I", 64 * 32 * 4 - 1),
])
def test_consumer_rejects_unsupported_format_dimensions_and_payload(offset, fmt, value):
    damaged = bytearray(_header().pack())
    struct.pack_into("<" + fmt, damaged, offset, value)
    with pytest.raises(ValueError):
        FrameHeader.unpack(damaged)


@pytest.mark.parametrize("dimension", ["source_width", "source_height"])
def test_consumer_rejects_zero_source_dimensions(dimension):
    damaged = bytearray(_header().pack())
    struct.pack_into("<I", damaged, 80 if dimension == "source_width" else 84, 0)
    with pytest.raises(ValueError):
        FrameHeader.unpack(damaged)


@pytest.mark.parametrize("size", [0, 1, 127, 129])
def test_wrong_header_length_is_rejected(size):
    with pytest.raises((ValueError, struct.error)):
        FrameHeader.unpack(bytes(size))


def test_wrong_magic_is_rejected():
    damaged = bytearray(_header().pack())
    damaged[:8] = b"NOTAFRM\0"
    with pytest.raises(ValueError):
        FrameHeader.unpack(damaged)


def test_v2_content_geometry_and_input_flags_have_explicit_offsets():
    header = _header(content_left=2, content_top=4, content_width=28, content_height=24,
                     flags=FULL_SBS | ORIGINAL_2D | INPUT_ENABLED)
    packed = header.pack()
    assert struct.unpack_from("<4I", packed, 96) == (2, 4, 28, 24)
    assert packed[112:] == bytes(16)
    assert FrameHeader.unpack(packed) == header


@pytest.mark.parametrize("overrides", [
    {"flags": FULL_SBS | INPUT_ENABLED},
    {"content_left": 1},
    {"content_width": 32},
    {"content_left": 20, "content_width": 32, "content_height": 32},
    {"content_width": 32, "content_top": 1, "content_height": 32},
    {"flags": 16},
])
def test_v2_rejects_missing_or_out_of_bounds_content_before_input(overrides):
    with pytest.raises(ValueError):
        _header(**overrides).pack()


def test_v2_does_not_accept_nonzero_reserved_bytes():
    wire = bytearray(_header().pack())
    wire[112] = 1
    with pytest.raises(ValueError, match="Reserved"):
        FrameHeader.unpack(wire)


@pytest.mark.parametrize("overrides", [{"width": 0}, {"height": MAX_HEIGHT + 1},
                                        {"source_width": 0}, {"source_height": -1}])
def test_publisher_header_rejects_invalid_dimensions(overrides):
    with pytest.raises(ValueError):
        _header(**overrides).pack()


windows_only = pytest.mark.skipif(sys.platform != "win32", reason="real Windows named IPC required")


@pytest.fixture
def prefix():
    return "Local\\Quest3D.Test." + uuid.uuid4().hex


@contextmanager
def _reader(prefix):
    kernel = kernel32()
    mutex = kernel.CreateMutexW(None, False, prefix + ".Mutex.v2")
    if not mutex:
        raise ctypes.WinError(ctypes.get_last_error())
    memory = None
    try:
        memory = mmap.mmap(-1, CAPACITY, tagname=prefix + ".v2", access=mmap.ACCESS_READ)
        yield kernel, mutex, memory
    finally:
        if memory is not None:
            memory.close()
        kernel.CloseHandle(mutex)


def _read_snapshot(reader):
    kernel, mutex, memory = reader
    result = kernel.WaitForSingleObject(mutex, 1000)
    if result == 0x80:
        kernel.ReleaseMutex(mutex)
        raise ValueError("reader must discard abandoned-mutex data")
    if result != 0:
        raise RuntimeError(f"reader mutex wait failed: {result:#x}")
    try:
        wire = bytes(memory[:128])
        header = FrameHeader.unpack(wire)
        length = struct.unpack_from("<I", wire, 64)[0]
        return header, bytes(memory[128:128 + length])
    finally:
        kernel.ReleaseMutex(mutex)


def _publish(publisher, image, frame_id=1):
    return publisher.publish(image, frame_id=frame_id, capture_ns=1000000 + frame_id,
                             generation=3, flags=FULL_SBS | ORIGINAL_2D | CAPTURE_RECEIPT,
                             source_rect=(-1920, -120, 1920, 1080))


@windows_only
def test_actual_mapping_reader_receives_complete_owned_bytes_and_metadata(prefix):
    underlying = np.arange(32 * 128 * 4, dtype=np.uint8).reshape(32, 128, 4)
    image = underlying[:, ::2, :]  # exercise a valid noncontiguous BGRA input
    assert not image.flags.c_contiguous
    with FramePublisher(prefix) as publisher, _reader(prefix) as reader:
        assert _publish(publisher, image)
        header, payload = _read_snapshot(reader)
        assert (header.width, header.height, header.frame_id, header.generation) == (64, 32, 1, 3)
        assert (header.source_left, header.source_top) == (-1920, -120)
        assert header.stream_epoch == publisher.epoch
        assert header.flags == 7
        assert payload == image.tobytes(order="C")
        image[:] = 0
        assert _read_snapshot(reader)[1] == payload


@windows_only
@pytest.mark.parametrize("image", [np.zeros((16, 16, 3), np.uint8), np.zeros((16, 16), np.uint8),
                                   np.zeros((16, 16, 4), np.float32)])
def test_actual_publisher_rejects_wrong_pixel_format(prefix, image):
    with FramePublisher(prefix) as publisher:
        with pytest.raises(ValueError, match="HWC uint8 BGRA"):
            _publish(publisher, image)


@windows_only
def test_busy_reader_causes_skip_without_overwriting_previous_frame(prefix):
    locked, release = threading.Event(), threading.Event()
    errors = []

    def hold_reader():
        try:
            with _reader(prefix) as (kernel, mutex, _memory):
                assert kernel.WaitForSingleObject(mutex, 1000) == 0
                locked.set()
                try:
                    assert release.wait(5), "test reader was not released"
                finally:
                    kernel.ReleaseMutex(mutex)
        except BaseException as exc:
            errors.append(exc)
            locked.set()

    image = np.full((16, 32, 4), 7, dtype=np.uint8)
    with FramePublisher(prefix) as publisher, _reader(prefix) as reader:
        assert _publish(publisher, image, 1)
        holder = threading.Thread(target=hold_reader)
        holder.start()
        try:
            assert locked.wait(5)
            assert not errors
            assert _publish(publisher, image + 1, 2) is False
            assert publisher.skipped == 1
        finally:
            release.set()
            holder.join(5)
        assert not holder.is_alive() and not errors
        header, payload = _read_snapshot(reader)
        assert header.frame_id == 1
        assert payload == image.tobytes()


@windows_only
def test_new_producer_uses_new_epoch_with_reader_mapping_held_open(prefix):
    image = np.full((16, 32, 4), 11, dtype=np.uint8)
    first = FramePublisher(prefix)
    with _reader(prefix) as reader:
        try:
            assert _publish(first, image, 100)
            first_header, _ = _read_snapshot(reader)
        finally:
            first.close()
        with FramePublisher(prefix) as second:
            assert _publish(second, image + 1, 1)
            next_header, payload = _read_snapshot(reader)
            assert next_header.frame_id == 1
            assert next_header.stream_epoch != first_header.stream_epoch
            assert payload == (image + 1).tobytes()


@windows_only
def test_competing_producer_in_another_process_is_rejected(prefix):
    child = """
import sys
from quest3d.bridge import FramePublisher
try:
    publisher = FramePublisher(sys.argv[1])
except RuntimeError:
    sys.exit(0)
else:
    publisher.close()
    sys.exit(7)
"""
    with FramePublisher(prefix):
        result = subprocess.run([sys.executable, "-c", child, prefix],
                                capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stdout + result.stderr


@windows_only
def test_second_producer_in_same_thread_cannot_bypass_exclusivity(prefix):
    with FramePublisher(prefix):
        with pytest.raises(RuntimeError, match="producer"):
            duplicate = FramePublisher(prefix)
            # Ensure this failing case still closes every handle it created.
            duplicate.close()


@windows_only
def test_frame_id_cannot_move_backwards_in_a_stream_epoch(prefix):
    image = np.zeros((16, 32, 4), dtype=np.uint8)
    with FramePublisher(prefix) as publisher, _reader(prefix) as reader:
        assert _publish(publisher, image, 10)
        with pytest.raises(ValueError):
            _publish(publisher, image + 1, 9)
        assert _read_snapshot(reader)[0].frame_id == 10


@windows_only
def test_parallel_reader_never_observes_header_payload_from_different_frames(prefix):
    ready, allow_publish, finished = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def produce():
        try:
            with FramePublisher(prefix) as publisher:
                assert _publish(publisher, np.full((32, 64, 4), 1, np.uint8), 1)
                ready.set()
                assert allow_publish.wait(5)
                for number in range(2, 101):
                    _publish(publisher, np.full((32, 64, 4), number, np.uint8), number)
                # Keep named objects alive until reader has copied the last frame.
                assert finished.wait(5)
        except BaseException as exc:
            errors.append(exc)
            ready.set()

    producer = threading.Thread(target=produce)
    producer.start()
    try:
        assert ready.wait(5)
        assert not errors
        with _reader(prefix) as reader:
            allow_publish.set()
            previous_id = 0
            for _ in range(100):
                header, payload = _read_snapshot(reader)
                assert header.frame_id >= previous_id
                previous_id = header.frame_id
                assert payload == bytes([header.frame_id]) * (header.width * header.height * 4)
    finally:
        allow_publish.set()
        finished.set()
        producer.join(5)
    assert not producer.is_alive() and not errors


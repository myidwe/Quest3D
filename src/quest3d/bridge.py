"""Bounded latest-frame Windows IPC; see docs/FRAME_BRIDGE.md."""

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import mmap
import secrets
import struct
import time

import numpy as np

MAGIC = b"Q3DFRM2\0"
HEADER = struct.Struct("<8s6I4Q2I2i2IQ4I16x")
HEADER_SIZE = 128
MAX_WIDTH, MAX_HEIGHT = 4096, 2160
CAPACITY = HEADER_SIZE + MAX_WIDTH * MAX_HEIGHT * 4
FULL_SBS, ORIGINAL_2D, CAPTURE_RECEIPT, INPUT_ENABLED = 1, 2, 4, 8
assert HEADER.size == HEADER_SIZE


@dataclass(frozen=True)
class FrameHeader:
    width: int
    height: int
    frame_id: int
    capture_ns: int
    publish_ns: int
    generation: int
    flags: int
    source_left: int
    source_top: int
    source_width: int
    source_height: int
    stream_epoch: int
    content_left: int = 0
    content_top: int = 0
    content_width: int = 0
    content_height: int = 0

    def pack(self) -> bytes:
        if not (0 < self.width <= MAX_WIDTH and 0 < self.height <= MAX_HEIGHT):
            raise ValueError("Frame dimensions exceed the bridge capacity")
        if self.source_width <= 0 or self.source_height <= 0:
            raise ValueError("Source dimensions must be positive")
        if self.flags & ~15 or (self.flags & ORIGINAL_2D and not self.flags & FULL_SBS):
            raise ValueError("Invalid bridge flags")
        if self.flags & FULL_SBS and self.width % 2:
            raise ValueError("Full-SBS requires an even packed width")
        eye_width = self.width // 2 if self.flags & FULL_SBS else self.width
        if min(self.content_left, self.content_top, self.content_width, self.content_height) < 0:
            raise ValueError("Content geometry must be nonnegative")
        if bool(self.content_width) != bool(self.content_height):
            raise ValueError("Content width and height must both be present")
        if not self.content_width and (self.content_left or self.content_top):
            raise ValueError("Absent content geometry must be all zero")
        if self.content_width and (self.content_left + self.content_width > eye_width
                                  or self.content_top + self.content_height > self.height):
            raise ValueError("Content geometry exceeds the eye image")
        if self.flags & INPUT_ENABLED and not self.content_width:
            raise ValueError("Input requires explicit valid content geometry")
        if self.stream_epoch <= 0 or min(self.frame_id, self.capture_ns, self.publish_ns, self.generation) < 0:
            raise ValueError("Invalid epoch/frame timestamp metadata")
        return HEADER.pack(
            MAGIC, 2, HEADER_SIZE, self.width, self.height, self.width * 4, 1,
            self.frame_id, self.capture_ns, self.publish_ns, self.generation,
            self.width * self.height * 4, self.flags,
            self.source_left, self.source_top, self.source_width, self.source_height,
            self.stream_epoch, self.content_left, self.content_top, self.content_width, self.content_height,
        )

    @classmethod
    def unpack(cls, data: bytes) -> "FrameHeader":
        fields = HEADER.unpack(data)
        magic, version, size, width, height, stride, fmt = fields[:7]
        if (magic, version, size, fmt) != (MAGIC, 2, HEADER_SIZE, 1):
            raise ValueError("Unsupported bridge header")
        if not (0 < width <= MAX_WIDTH and 0 < height <= MAX_HEIGHT):
            raise ValueError("Invalid bridge dimensions")
        if stride != width * 4 or fields[11] != stride * height:
            raise ValueError("Invalid bridge payload/stride")
        result = cls(width, height, *fields[7:11], fields[12], *fields[13:22])
        result.pack()  # Apply the same semantic validation to external data.
        if any(data[112:128]):
            raise ValueError("Reserved bridge bytes must be zero for v2")
        return result


def kernel32():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateMutexW.restype = wintypes.HANDLE
    kernel.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.CreateEventW.restype = wintypes.HANDLE
    kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel.WaitForSingleObject.restype = wintypes.DWORD
    for name in ("ReleaseMutex", "SetEvent", "CloseHandle"):
        getattr(kernel, name).argtypes = [wintypes.HANDLE]
        getattr(kernel, name).restype = wintypes.BOOL
    return kernel


class FramePublisher:
    def __init__(self, prefix: str = "Local\\Quest3D.Frame", *, version: int = 2):
        self._payload_owner = None
        self._payload_layout = None
        self._last_metrics = {}
        self._counts = dict(publish_count=0, copied_count=0, reused_count=0,
                            skipped_count=0, error_count=0, copied_bytes_total=0,
                            reused_bytes_total=0)
        if type(version) is not int or version not in (2, 3):
            raise ValueError("Explicit bridge protocol must be 2 or 3")
        self.version = version
        self.header_size = HEADER_SIZE if version == 2 else 192
        suffix = f".v{version}"
        self.kernel = kernel32()
        self.mutex = self.kernel.CreateMutexW(None, False, prefix + ".Mutex" + suffix)
        self.ready = self.kernel.CreateEventW(None, False, False, prefix + ".Ready" + suffix)
        ctypes.set_last_error(0)
        # A v3 owner is not READY until any surviving mapping's old header has
        # been invalidated. Otherwise a new live owner can expose old pixels.
        self.owner = self.kernel.CreateMutexW(None, version == 2, prefix + ".Producer" + suffix)
        owner_error = ctypes.get_last_error()
        if not all((self.mutex, self.ready, self.owner)):
            self.close()
            raise ctypes.WinError(ctypes.get_last_error())
        # Existing mutexes can be acquired recursively by the same Windows
        # thread. Object existence, not another wait, enforces one producer.
        if owner_error == 183:  # ERROR_ALREADY_EXISTS
            self.close()
            raise RuntimeError("Another Quest3D producer is already running")
        self.owns = version == 2
        try:
            self.memory = mmap.mmap(-1, self.header_size + MAX_WIDTH * MAX_HEIGHT * 4,
                                    tagname=prefix + suffix, access=mmap.ACCESS_WRITE)
            if version == 3:
                acquired = self.kernel.WaitForSingleObject(self.mutex, 1000)
                if acquired not in (0, 0x80):
                    raise RuntimeError("Cannot invalidate the previous v3 frame while its reader is busy")
                try:
                    self.memory[:self.header_size] = bytes(self.header_size)
                    acquired_owner = self.kernel.WaitForSingleObject(self.owner, 1000)
                    if acquired_owner not in (0, 0x80):
                        raise RuntimeError("Cannot establish the initialized v3 producer lifetime")
                    self.owns = True
                finally:
                    self.kernel.ReleaseMutex(self.mutex)
        except BaseException:
            self.close()
            raise
        self.epoch = secrets.randbits(63) or 1
        self.skipped = 0
        self.last_frame_id = -1

    def publish(self, bgra: np.ndarray, *, frame_id: int, capture_ns: int,
                generation: int, flags: int, source_rect: tuple[int, int, int, int],
                content_rect: tuple[int, int, int, int] | None = None, source_identity=None,
                immutable_payload: bool = False) -> bool:
        """Publish a complete frame; optionally reuse an identical immutable array.

        The opt-in caller guarantees that the array's pixels, including all
        writable aliases, remain unchanged while this publisher retains it.
        NumPy's writeable flag alone does not establish that guarantee. A strong
        reference prevents identity/address reuse. Metadata and the ready event
        are always refreshed, even when the payload copy is unnecessary.
        """
        metrics = dict(payload_bytes=0, copied_bytes=0, reused_bytes=0,
                       mutex_wait_ms=0.0, write_ms=0.0,
                       immutable_payload=immutable_payload, reused_payload=False,
                       status="error")
        self._last_metrics = metrics
        try:
            if type(immutable_payload) is not bool:
                raise ValueError("immutable_payload must be an explicit bool")
            if getattr(self, "memory", None) is None:
                raise RuntimeError("Frame publisher is closed")
            if not isinstance(bgra, np.ndarray) or bgra.dtype != np.uint8 or bgra.ndim != 3 or bgra.shape[2] != 4:
                raise ValueError("Expected HWC uint8 BGRA frame")
            if frame_id <= self.last_frame_id:
                raise ValueError("Frame IDs must increase within one stream epoch")
            layout = (bgra.shape, bgra.strides, bgra.dtype.str,
                      bgra.__array_interface__["data"][0], bool(bgra.flags.c_contiguous))
            reuse = (immutable_payload and self._payload_owner is bgra
                     and self._payload_layout == layout)
            header = FrameHeader(bgra.shape[1], bgra.shape[0], frame_id, capture_ns,
                                 time.perf_counter_ns(), generation, flags, *source_rect, self.epoch,
                                 *(content_rect or (0, 0, 0, 0)))
            if self.version == 3:
                from dataclasses import asdict
                from .bridge_v3 import SourceFrameHeader
                header = SourceFrameHeader(**asdict(header), source_identity=source_identity)
            elif source_identity is not None:
                raise ValueError("v2 cannot silently carry a v3 source identity")
            packed = header.pack()
            # Keep normal contiguous conversion outside the reader's mutex.
            # A reuse hit avoids the allocation/copy altogether.
            image = None if reuse else np.ascontiguousarray(bgra)
            metrics["payload_bytes"] = bgra.size
            wait_start = time.perf_counter_ns()
            try:
                result = self.kernel.WaitForSingleObject(self.mutex, 0)
            finally:
                metrics["mutex_wait_ms"] = (time.perf_counter_ns() - wait_start) / 1e6
            # No failed or skipped attempt can certify the mapping's payload.
            self._invalidate_payload()
            if result == 0x102:
                self.skipped += 1
                self._counts["skipped_count"] += 1
                metrics["status"] = "skipped"
                return False
            if result not in (0, 0x80):
                raise ctypes.WinError(ctypes.get_last_error())
            # An abandoned reader/writer lock is never evidence of valid data.
            reuse = reuse and result == 0
            write_start = time.perf_counter_ns()
            try:
                # Invalidate before touching pixels. A partial payload/header
                # failure must not expose an earlier valid header with new data.
                self.memory.seek(0)
                self._write_exact(bytes(self.header_size))
                if not reuse:
                    if image is None:  # A formerly reusable lock was abandoned.
                        image = np.ascontiguousarray(bgra)
                    self.memory.seek(self.header_size)
                    self._write_exact(memoryview(image).cast("B"))
                    metrics["copied_bytes"] = bgra.size
                self.memory.seek(0)
                self._write_exact(packed)
                # A committed header consumes this ID even if signaling fails.
                self.last_frame_id = frame_id
            except BaseException:
                # A short final-header write may have restored a valid magic.
                # Best-effort zeroing happens before releasing the mutex.
                try:
                    self.memory.seek(0)
                    self._write_exact(bytes(self.header_size))
                except BaseException:
                    pass
                raise
            finally:
                metrics["write_ms"] = (time.perf_counter_ns() - write_start) / 1e6
                if not self.kernel.ReleaseMutex(self.mutex):
                    raise ctypes.WinError(ctypes.get_last_error())
            if not self.kernel.SetEvent(self.ready):
                raise ctypes.WinError(ctypes.get_last_error())
            if immutable_payload and result == 0:
                self._payload_owner = bgra
                self._payload_layout = layout
            metrics.update(status="published", reused_payload=reuse,
                           reused_bytes=bgra.size if reuse else 0)
            self._counts["publish_count"] += 1
            self._counts["reused_count" if reuse else "copied_count"] += 1
            self._counts["copied_bytes_total"] += metrics["copied_bytes"]
            self._counts["reused_bytes_total"] += metrics["reused_bytes"]
            return True
        except BaseException:
            self._invalidate_payload()
            self._counts["error_count"] += 1
            raise

    def _write_exact(self, data):
        if self.memory.write(data) != len(data):
            raise OSError("Incomplete frame bridge write")

    def _invalidate_payload(self):
        self._payload_owner = None
        self._payload_layout = None

    def snapshot(self) -> dict:
        """Last-attempt timings/bytes and successful-publication byte totals."""
        return {**self._last_metrics, **self._counts}

    def close(self):
        self._invalidate_payload()
        if getattr(self, "memory", None) is not None:
            self.memory.close()
            self.memory = None
        if getattr(self, "owns", False):
            self.kernel.ReleaseMutex(self.owner)
            self.owns = False
        for name in ("owner", "mutex", "ready"):
            handle = getattr(self, name, None)
            if handle:
                self.kernel.CloseHandle(handle)
                setattr(self, name, None)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

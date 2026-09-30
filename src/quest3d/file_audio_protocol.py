"""Bounded PCM epoch wire format; TLS/request binding supplies authentication.

This is a serializer, not a new audio device, HTTP service or playout ACK.
Sample counts always denote stereo frames, each containing two float scalars.
"""
from dataclasses import dataclass
import struct

import numpy as np

HEADER_BYTES, MAX_FRAMES, RATE, CHANNELS = 128, 480, 48_000, 2
FLAG_EOF = 1
CONTENT_TYPE = "application/vnd.quest3d.pcm-v1"
MAGIC = b"Q3DPCM1\0"
REASONS = {None: 0, "start": 1, "seek": 2, "gap": 3, "overlap": 4, "format_change": 5}


def _uint(value, name, maximum=(1 << 64) - 1):
    if type(value) is not int or not 0 <= value <= maximum:
        raise ValueError(f"Invalid {name}")


def _id(value):
    if (type(value) is not str or len(value) != 32 or value == "0" * 32
            or any(c not in "0123456789abcdef" for c in value)):
        raise ValueError("Expected nonzero lowercase 128-bit identifier")
    return bytes.fromhex(value)


@dataclass(frozen=True, slots=True)
class Scope:
    file_session: str
    transport: str
    channel: str
    epoch: int
    generation: int

    def __post_init__(self):
        for value in (self.file_session, self.transport, self.channel):
            _id(value)
        _uint(self.epoch, "epoch")
        _uint(self.generation, "generation")

    def request(self):
        return dict(file_session=self.file_session, transport=self.transport, channel=self.channel,
                    epoch=str(self.epoch), generation=str(self.generation))


@dataclass(frozen=True, slots=True)
class Block:
    scope: Scope
    sequence: int
    block_id: int
    pts_ns: int
    total_samples: int
    offset_samples: int
    samples: np.ndarray
    flags: int = 0
    discontinuity: str | None = None

    def __post_init__(self):
        if not isinstance(self.scope, Scope):
            raise ValueError("Expected an immutable file scope")
        _uint(self.sequence, "sequence")
        _uint(self.block_id, "block_id")
        if type(self.pts_ns) is not int or not -(1 << 63) <= self.pts_ns < (1 << 63):
            raise ValueError("Invalid media PTS")
        for name in ("total_samples", "offset_samples"):
            _uint(getattr(self, name), name, MAX_FRAMES)
        if type(self.flags) is not int or self.flags not in (0, FLAG_EOF):
            raise ValueError("Invalid flags")
        if self.discontinuity not in REASONS:
            raise ValueError("Unknown discontinuity")
        values = np.asarray(self.samples)
        if values.dtype != np.float32 or values.ndim != 2 or values.shape[1] != CHANNELS:
            raise ValueError("Expected interleaved float32 stereo frames")
        if not np.isfinite(values).all():
            raise ValueError("PCM must be finite")
        count = len(values)
        if self.flags == FLAG_EOF:
            if count or self.total_samples or self.offset_samples or self.discontinuity is not None:
                raise ValueError("EOF has no PCM, offsets or discontinuity")
        elif not (0 < self.total_samples <= MAX_FRAMES and 0 <= self.offset_samples < self.total_samples
                  and 0 < count <= self.total_samples - self.offset_samples):
            raise ValueError("Invalid original block suffix")
        owned = np.array(values, dtype=np.float32, order="C", copy=True)
        owned.setflags(write=False)
        object.__setattr__(self, "samples", owned)

    @property
    def count(self):
        return len(self.samples)


def encode(block: Block) -> bytes:
    if not isinstance(block, Block):
        raise ValueError("Expected a PCM block")
    payload = block.samples.astype("<f4", copy=False).tobytes(order="C")
    header = bytearray(HEADER_BYTES)
    struct.pack_into("<8sHHI", header, 0, MAGIC, 1, HEADER_BYTES, len(payload))
    header[16:32], header[32:48], header[48:64] = map(_id,
        (block.scope.file_session, block.scope.transport, block.scope.channel))
    struct.pack_into("<QQQQqIHHHBBIB", header, 64,
        block.scope.epoch, block.scope.generation, block.sequence, block.block_id, block.pts_ns,
        RATE, block.total_samples, block.offset_samples, block.count, CHANNELS, 1,
        block.flags, REASONS[block.discontinuity])
    return bytes(header) + payload


def decode(data: bytes, expected: Scope | None = None) -> Block:
    if not isinstance(data, bytes) or not HEADER_BYTES <= len(data) <= HEADER_BYTES + MAX_FRAMES * 8:
        raise ValueError("Invalid bounded PCM record")
    magic, version, size, payload_bytes = struct.unpack_from("<8sHHI", data)
    if (magic, version, size) != (MAGIC, 1, HEADER_BYTES) or payload_bytes != len(data) - size:
        raise ValueError("Invalid header or payload length")
    if any(data[121:128]):
        raise ValueError("Reserved bytes must be zero")
    epoch, generation, sequence, block_id, pts, rate, total, offset, count, channels, fmt, flags, reason = (
        struct.unpack_from("<QQQQqIHHHBBIB", data, 64))
    if rate != RATE or channels != CHANNELS or fmt != 1 or payload_bytes != count * 8 or reason > 5:
        raise ValueError("Invalid PCM format or count")
    scope = Scope(data[16:32].hex(), data[32:48].hex(), data[48:64].hex(), epoch, generation)
    if expected is not None and scope != expected:
        raise ValueError("Wrong immutable scope")
    samples = np.frombuffer(data, dtype="<f4", offset=HEADER_BYTES).reshape(-1, CHANNELS)
    return Block(scope, sequence, block_id, pts, total, offset, samples, flags, tuple(REASONS)[reason])


def from_scheduled(scope: Scope, sequence: int, packet) -> Block:
    """Copy the exact FileAVPlayback original-block suffix without acknowledging it."""
    token = packet.token
    if type(token) is not tuple or len(token) != 4 or token[:3] != (scope.file_session, scope.epoch, scope.generation):
        raise ValueError("Scheduled PCM belongs to another file epoch")
    _id(token[0])
    _uint(token[1], "scheduled epoch")
    _uint(token[2], "scheduled generation")
    _uint(packet.offset_samples, "scheduled offset", MAX_FRAMES)
    if type(packet.pts_ns) is not int or not -(1 << 63) <= packet.pts_ns < (1 << 63):
        raise ValueError("Invalid scheduled PTS")
    original_pts = packet.pts_ns - round(packet.offset_samples * 1_000_000_000 / RATE)
    return Block(scope, sequence, token[3], original_pts, packet.total_samples, packet.offset_samples,
                 packet.samples, discontinuity=packet.discontinuity)


def eof(scope: Scope, sequence: int, block_id: int, pts_ns: int) -> Block:
    return Block(scope, sequence, block_id, pts_ns, 0, 0, np.empty((0, CHANNELS), dtype=np.float32), FLAG_EOF)

"""Strict, shared parser for the Quest3D single-file Windows bootstrap overlay."""
from __future__ import annotations

import hashlib
import struct

MAGIC = b"Q3DSETUPZIPv1".ljust(16, b"\0")
SETUP_MARKER = "Q3D_SETUP_BOOTSTRAP_V1"
FOOTER = struct.Struct("<16sQQ32s")
FOOTER_SIZE = FOOTER.size


def has_setup_marker(data: bytes) -> bool:
    return MAGIC in data or any(SETUP_MARKER.encode(encoding) in data for encoding in ("ascii", "utf-16-le", "utf-16-be"))


def pack_footer(stub_size: int, payload: bytes) -> bytes:
    if not isinstance(stub_size, int) or isinstance(stub_size, bool) or not 2 <= stub_size < 2**64:
        raise ValueError("Invalid bootstrap size")
    if not payload.startswith(b"PK\x03\x04"):
        raise ValueError("Expected a nonempty ZIP payload")
    return FOOTER.pack(MAGIC, stub_size, len(payload), hashlib.sha256(payload).digest())


def read_payload(data: bytes) -> tuple[bytes, dict]:
    if len(data) < FOOTER_SIZE + 6 or data[:2] != b"MZ":
        raise ValueError("Missing Windows bootstrap or payload footer")
    magic, stub_size, size, expected = FOOTER.unpack(data[-FOOTER_SIZE:])
    if magic != MAGIC or stub_size < 2 or size < 4 or stub_size + size + FOOTER_SIZE != len(data):
        raise ValueError("Invalid bootstrap footer or payload bounds")
    payload = data[stub_size:stub_size + size]
    if not payload.startswith(b"PK\x03\x04") or hashlib.sha256(payload).digest() != expected:
        raise ValueError("ZIP payload integrity failure")
    return payload, {"stub_bytes": stub_size, "payload_bytes": size, "payload_sha256": expected.hex()}

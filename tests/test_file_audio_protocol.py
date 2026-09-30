"""Protocol structure/identity tests; no network or sound output is implied."""
from dataclasses import replace
from fractions import Fraction
import struct

import numpy as np
import pytest

from quest3d.file_audio_protocol import Scope, Block, encode, decode, eof, from_scheduled
from quest3d.media_playout import ScheduledPCM


def scope():
    return Scope("11" * 16, "22" * 16, "33" * 16, 7, 9)


def sample():
    return Block(scope(), 0, 12, -100, 4, 1, np.array([[.1, -.1], [.2, -.2], [.3, -.3]], np.float32),
                 discontinuity="seek")


def test_exact_suffix_signed_pts_and_owned_samples_roundtrip():
    block = sample()
    wire = encode(block)
    copy = decode(wire, scope())
    assert wire[:16] == b"Q3DPCM1\0\x01\x00\x80\x00\x18\x00\x00\x00"
    assert copy.scope == block.scope and copy.pts_ns == -100 and copy.offset_samples == 1
    assert copy.count == 3 and copy.total_samples == 4 and copy.discontinuity == "seek"
    np.testing.assert_array_equal(copy.samples, block.samples)
    assert not copy.samples.flags.writeable and encode(copy) == wire


def test_source_array_cannot_mutate_serialized_owned_block():
    values = np.zeros((480, 2), np.float32)
    block = Block(scope(), 0, 0, 0, 480, 0, values)
    before = encode(block)
    values[:] = .9
    assert encode(block) == before and len(before) == 3968


def test_eof_is_an_explicit_empty_record_with_its_scope_and_final_pts():
    wire = encode(eof(scope(), 2, 15, 123456))
    result = decode(wire, scope())
    assert len(wire) == 128 and result.flags == 1 and result.count == 0
    assert result.sequence == 2 and result.block_id == 15 and result.pts_ns == 123456


@pytest.mark.parametrize("field,value", [("sequence", -1), ("block_id", True), ("pts_ns", 1 << 63),
    ("total_samples", 481), ("offset_samples", 4), ("flags", 2), ("discontinuity", "unknown"),
    ("samples", np.array([[float("nan"), 0]], np.float32)), ("samples", np.ones((3, 1), np.float32)),
    ("samples", np.ones((3, 2), np.float64)), ("samples", np.empty((0, 2), np.float32))])
def test_bad_producer_blocks_rejected(field, value):
    with pytest.raises(ValueError):
        replace(sample(), **{field: value})


@pytest.mark.parametrize("offset,value", [(0, 0), (8, 2), (10, 127), (12, 25), (104, 0),
    (108, 1), (110, 4), (112, 4), (114, 1), (115, 2), (116, 2), (120, 6), (121, 1), (127, 1)])
def test_malformed_wire_rejected_before_use(offset, value):
    wire = bytearray(encode(sample()))
    wire[offset] = value
    with pytest.raises(ValueError):
        decode(bytes(wire), scope())


def test_wrong_scope_truncation_appended_bytes_and_nonfinite_payload_rejected():
    wire = encode(sample())
    for invalid in (wire[:-1], wire + b"\0", wire[:128] + struct.pack("<I", 0x7F800000) + wire[132:]):
        with pytest.raises(ValueError):
            decode(invalid, scope())
    with pytest.raises(ValueError):
        decode(wire, replace(scope(), generation=10))


def test_scheduled_original_token_and_pts_preserved_without_handoff_ack():
    packet = ScheduledPCM((scope().file_session, 7, 9, 12), sample().samples, 2_000_000,
        Fraction(2, 1000), 3_000_000, 1, 4, "seek")
    block = from_scheduled(scope(), 0, packet)
    assert block.pts_ns == packet.pts_ns - round(1e9 / 48000)
    assert block.block_id == 12 and block.offset_samples == 1 and block.total_samples == 4
    with pytest.raises(ValueError):
        from_scheduled(replace(scope(), epoch=8), 0, packet)


@pytest.mark.parametrize("field,value", [("file_session", "0" * 32), ("transport", "ab"),
    ("channel", "AA" * 16), ("epoch", True), ("generation", -1)])
def test_scope_request_preserves_uint64_and_rejects_invalid_identity(field, value):
    with pytest.raises(ValueError):
        replace(scope(), **{field: value})
    assert replace(scope(), epoch=(1 << 64) - 1).request()["epoch"] == "18446744073709551615"

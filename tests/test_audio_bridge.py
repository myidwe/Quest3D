"""Private real Windows mapping tests; no audio device or network is opened."""
from dataclasses import replace
from fractions import Fraction
import ctypes
import hashlib
import os
import secrets
import struct
import time
import threading

import numpy as np
import pytest

from quest3d.audio_bridge import AudioPublisher, EncoderAck, CAPACITY, HEADER_BYTES, SLOT_BYTES, U32, U64, pack_offer
from quest3d.media_playout import ScheduledPCM

SESSION = "a" * 32


def packet(block=1, *, epoch=0, generation=0, total=480, offset=0, due=None):
    samples = np.full((total - offset, 2), .125, dtype=np.float32)
    return ScheduledPCM((SESSION, epoch, generation, block), samples, round(offset * 1e9 / 48000),
        Fraction(offset, 48000), time.perf_counter_ns() + 100_000_000 if due is None else due,
        offset, total, None)


def test_wire_layout_hash_and_suffix():
    value = packet(total=251, offset=11, due=1_000_000_000)
    packed = pack_offer(value)
    assert len(packed) == SLOT_BYTES and CAPACITY == 205056
    assert struct.unpack_from("<IIII", packed, 24) == (251, 11, 240, 11)
    assert packed[56:88] == hashlib.sha256(packed[:36] + packed[40:44] + packed[88:104] + packed[128:128 + 240 * 8]).digest()
    np.testing.assert_array_equal(np.frombuffer(packed, "<f4", count=480, offset=128).reshape(-1, 2), value.samples)


@pytest.mark.parametrize("change", [dict(total_samples=0), dict(offset_samples=480), dict(samples=np.ones((480, 2), np.float64)),
    dict(samples=np.full((480, 2), np.nan, np.float32)), dict(due_ns=-1), dict(token=(SESSION, -1, 0, 1)), dict(discontinuity="other")])
def test_malformed_offer_rejected(change):
    with pytest.raises(ValueError): pack_offer(replace(packet(), **change))


@pytest.mark.skipif(os.name != "nt", reason="Windows mapping")
def test_single_producer_and_duplicate_content_rules():
    with AudioPublisher(SESSION) as bridge:
        with pytest.raises(RuntimeError): AudioPublisher(SESSION, channel=bridge.channel)
        value = packet()
        assert bridge.offer(value) and bridge.offer(value)
        with pytest.raises(ValueError): bridge.offer(replace(value, due_ns=value.due_ns + 1))
        with pytest.raises(ValueError): bridge.offer(replace(value, samples=value.samples * 2))
        assert bridge.snapshot()["slots"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows mapping")
def test_full_ring_retains_unconfirmed_encoder_cursors_and_backpressures():
    with AudioPublisher(SESSION) as bridge:
        base_due = time.perf_counter_ns() + 1_000_000_000
        for i in range(50): assert bridge.offer(packet(i, due=base_due + i * 10_000_000))
        assert not bridge.offer(packet(50, due=base_due + 500_000_000))
        # Inject the same guarded memory mutation the real native encoder commits.
        with bridge._lock(): bridge._put(HEADER_BYTES + 36, 480, U32)
        acks = bridge.poll_acks()
        assert acks == bridge.poll_acks() == (EncoderAck((SESSION, 0, 0, 0), 480),)
        assert not bridge.offer(packet(50, due=base_due + 500_000_000))
        bridge.confirm_acks(acks)
        assert bridge.offer(packet(50, due=base_due + 500_000_000))
        assert bridge.snapshot()["buffered_samples"] == 24000


@pytest.mark.skipif(os.name != "nt", reason="Windows mapping")
def test_partial_ack_new_suffix_matches_original_and_stale_scope_rejects():
    with AudioPublisher(SESSION) as bridge:
        original = packet(due=1_000_000_000)
        assert bridge.offer(original)
        with bridge._lock(): bridge._put(HEADER_BYTES + 36, 240, U32)
        suffix = replace(original, samples=original.samples[240:], offset_samples=240,
                         due_ns=1_005_000_000, pts_ns=5_000_000)
        assert bridge.offer(suffix)
        with pytest.raises(ValueError): bridge.offer(replace(suffix, samples=suffix.samples * 2))
        with pytest.raises(ValueError): bridge.offer(packet(2, epoch=1))
        with pytest.raises(ValueError): bridge.confirm_acks([EncoderAck((SESSION, 0, 0, 1), 480)])
        assert bridge.poll_acks()[0].consumed_samples == 240


@pytest.mark.skipif(os.name != "nt", reason="Windows mapping")
def test_eof_and_unconfirmed_flush_do_not_claim_consumption_or_resume():
    with AudioPublisher(SESSION) as bridge:
        value = packet()
        assert bridge.offer(value)
        bridge.mark_decoder_eof()
        state = bridge.snapshot()
        assert state["decoder_eof"] and not state["encoder_drained"]
        with pytest.raises(ValueError): bridge.offer(packet(2))
        sequence = bridge.begin_flush()
        with pytest.raises(TimeoutError): bridge.finish_flush(sequence, timeout=.02)
        with pytest.raises(RuntimeError): bridge.resume(0, 1)
        assert not bridge.poll_acks()


@pytest.mark.skipif(os.name != "nt", reason="Windows kernel object ACL")
def test_mapping_has_protected_current_user_and_system_only_acl():
    with AudioPublisher(SESSION) as bridge:
        advapi = ctypes.WinDLL("advapi32", use_last_error=True)
        advapi.GetSecurityInfo.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint32] + [ctypes.c_void_p] * 5
        advapi.GetSecurityInfo.restype = ctypes.c_uint32
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [ctypes.c_void_p, ctypes.c_uint32,
            ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p]
        advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.restype = ctypes.c_int
        descriptor, text = ctypes.c_void_p(), ctypes.c_void_p()
        try:
            assert advapi.GetSecurityInfo(bridge.mapping, 6, 4, None, None, None, None, ctypes.byref(descriptor)) == 0
            assert advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(descriptor, 1, 4, ctypes.byref(text), None)
            sddl = ctypes.wstring_at(text)
            assert sddl.startswith("D:P") and sddl.count("(A;") == 2 and ";;;SY)" in sddl
            assert all(value not in sddl for value in (";;;WD)", ";;;AU)", ";;;BU)"))
        finally:
            if text: bridge.win.k.LocalFree(text)
            if descriptor: bridge.win.k.LocalFree(descriptor)


@pytest.mark.skipif(os.name != "nt", reason="Windows abandoned mutex")
def test_abandoned_transaction_becomes_sticky_without_consuming_pcm():
    with AudioPublisher(SESSION) as bridge:
        assert bridge.offer(packet())
        results = []
        thread = threading.Thread(target=lambda: results.append(bridge.win.k.WaitForSingleObject(bridge.mutex, 50)))
        thread.start(); thread.join()
        assert results == [0]
        assert bridge.snapshot()["fault"] == "abandoned_mutex"
        assert not bridge.poll_acks()
        with pytest.raises(RuntimeError): bridge.offer(packet(2))


@pytest.mark.skipif(os.name != "nt", reason="Windows mapping")
def test_empty_scope_preparation_cannot_relabel_an_existing_offer():
    with AudioPublisher(SESSION) as bridge:
        with pytest.raises(RuntimeError): bridge.prepare_scope(0, 1)
        # Inject only consumer readiness; real-process readiness is covered by the native probe.
        with bridge._lock(): bridge._put(192, 1, U32)
        bridge.prepare_scope(0, 1)
        assert bridge.snapshot()["generation"] == 1
        with pytest.raises(ValueError): bridge.prepare_scope(0, 0)
        assert bridge.offer(packet(generation=1))
        with pytest.raises(RuntimeError): bridge.prepare_scope(0, 2)
        with bridge._lock(): bridge._put(HEADER_BYTES + 36, 480, U32)
        bridge.confirm_acks(bridge.poll_acks())
        assert bridge.snapshot()["slots"] == 0
        with pytest.raises(RuntimeError): bridge.prepare_scope(0, 2)

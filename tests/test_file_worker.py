import importlib.util
import io
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import threading
import time
import uuid

import pytest

from quest3d.file_worker import PreparedFileWorker, run_control
from quest3d.file_worker_protocol import MAX_LINE_BYTES, decode_request, encode_message
from quest3d.file_audio_protocol import Scope, decode
from test_media import video

ROOT = Path(__file__).resolve().parents[1]
VECTORS = ROOT / "artifacts/audio/file-audio-vectors-20260910-a"
SOURCE = VECTORS / "known-av.nut"
NONCE = "12" * 16
TRANSPORT = "34" * 16
NATIVE_CLI = ROOT / "artifacts/host/cmake-build-file-audio-ipc/tests/file_audio_ipc_reader_cli.exe"
windows_file = pytest.mark.skipif(os.name != "nt" or not SOURCE.is_file(), reason="Actual private Windows file fixture")


def request(op="describe", data=None, seq="1", **changes):
    value = dict(v=1, worker_nonce=NONCE, seq=seq, op=op, data=data or {})
    value.update(changes)
    return encode_message(value)


def test_control_unicode_and_exact_sequence():
    line = request("open", {"path": "A:\\한글 영상\\테스트.nut"})
    decoded = decode_request(line, NONCE, 1)
    assert decoded["data"]["path"] == "A:\\한글 영상\\테스트.nut"
    with pytest.raises(ValueError, match="sequence"):
        decode_request(line, NONCE, 2)


@pytest.mark.parametrize("line", [
    b"", b"{}", b"{}\r\n", b"{}\n{}\n", b"\xff\n", b"\0\n",
    b"[1]\n", b"{" + b"x" * MAX_LINE_BYTES + b"}\n",
    request(v=True), request(seq="01"), request(seq="0"), request(seq="18446744073709551616"),
    request(seq=1), request(worker_nonce="56" * 16), request(data="bad"), request(op="A"),
    request(extra=1), request().replace(b'"v":1', b'"v":1,"v":1'),
    request(data={"item": 1}).replace(b'"item":1', b'"item":1,"item":2'),
    request(data={"item": 1}).replace(b'"item":1', b'"item":NaN')])
def test_invalid_envelope_rejected_before_dispatch(line):
    with pytest.raises(ValueError):
        decode_request(line, NONCE, 1)


def test_response_byte_bound():
    with pytest.raises(ValueError, match="bounded"):
        encode_message({"message": "한" * 4000})


def wait_ready(worker, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        status = worker.dispatch("describe", {})
        assert status["error"] is None, status
        if status["preroll_ready"]:
            return status
        time.sleep(.005)
    raise AssertionError("Actual file preroll did not become ready")


@windows_file
def test_actual_file_preparation_clock_and_guard(tmp_path):
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "no-video-publication")
    original = None
    try:
        worker.dispatch("open", {"path": str(SOURCE)})
        original = worker.player
        before = wait_ready(worker)
        with pytest.raises(RuntimeError):
            worker.dispatch("bind_audio", dict(file_session=before["file_session"], epoch=before["epoch"],
                generation=before["generation"], channel=uuid.uuid4().hex))
        assert worker.publisher is None
        prepared = worker.dispatch("prepare", {})
        assert int(prepared["generation"]) == int(before["generation"]) + 1
        assert worker.player.clock_snapshot().preroll
        with pytest.raises(RuntimeError, match="already prepared"):
            worker.dispatch("prepare", {})
        binding = dict(file_session=prepared["file_session"], epoch=prepared["epoch"],
                       generation=prepared["generation"], channel=uuid.uuid4().hex)
        with pytest.raises(ValueError, match="grant"):
            worker.dispatch("bind_audio", {**binding, "epoch": str(int(prepared["epoch"]) + 1)})
        worker.dispatch("bind_audio", binding)
        waiting = worker.dispatch("pump", {})
        assert waiting["waiting_for_native_consumer"] and waiting["accepted"] == 0
        assert worker.player.status()["audio_consumed_samples"] == 0
        with pytest.raises(RuntimeError):
            worker.player.set_paused(True)
        release = {k: binding[k] for k in ("file_session", "epoch", "generation")}
        with pytest.raises(ValueError, match="past"):
            worker.dispatch("release", dict(**release, start_host_ns="0"))
        released = worker.dispatch("release", dict(**release, start_host_ns=str(time.perf_counter_ns() + 5_000_000_000)))
        assert released["media_clock_released"] and not released["quest_ready_verified"]
        assert released["generation"] == prepared["generation"]
        assert not released["video_publication_active"]
        assert worker.player.clock_snapshot().position(time.perf_counter_ns()) == 0
        assert worker.dispatch("close", {})["closed"]
        assert original and all(not thread.is_alive() for thread in original._threads)
    finally:
        worker.close()


def load_native_probe():
    spec = importlib.util.spec_from_file_location("file_ipc_fixture", ROOT / "native/host/probe_file_audio_ipc.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@windows_file
@pytest.mark.skipif(not NATIVE_CLI.is_file(), reason="Build fixed native IPC CLI first")
def test_actual_prepared_pcm_native_handoff_preserves_all_samples_without_ack(tmp_path):
    fixture = load_native_probe()
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "file")
    try:
        worker.dispatch("open", {"path": str(SOURCE)})
        wait_ready(worker)
        prepared = worker.dispatch("prepare", {})
        binding = dict(file_session=prepared["file_session"], epoch=prepared["epoch"],
                       generation=prepared["generation"], channel=uuid.uuid4().hex)
        worker.dispatch("bind_audio", binding)
        scope = Scope(binding["file_session"], TRANSPORT, binding["channel"],
                      int(binding["epoch"]), int(binding["generation"]))
        with fixture.native(scope, tmp_path) as peer:
            fixture.bind(worker.publisher, peer)
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                state = worker.dispatch("pump", {})
                assert state["audio_frames_consumed"] == 0
                if state["ipc"]["queued_records"] == 8:
                    break
                time.sleep(.005)
            else:
                raise AssertionError("Actual PCM did not fill the bounded IPC")
            first_sequence = worker.sequence
            assert worker.dispatch("pump", {})["accepted"] == 0
            assert worker.sequence == first_sequence
            eof_seen = False
            for _ in range(30):
                worker.dispatch("pump", {})
                offer = peer.request("peek")
                if not offer["ok"]:
                    time.sleep(.005)
                    continue
                accepted = peer.request("accept")
                assert accepted["ok"], accepted
                if accepted["eof_handed"]:
                    eof_seen = True
                    break
            assert eof_seen
            status = worker.player.status()
            assert status["audio_consumed_samples"] == 0 and not status["finished"]
            assert status["audio_samples"] == 5003 and status["preroll"]
            assert worker.sequence == 12 and len(worker.exported) == 11
            fixture.retire(peer, worker.publisher)
        wire = (tmp_path / "native.wire").read_bytes()
        records = []
        while wire:
            size = 128 + struct.unpack_from("<I", wire, 12)[0]
            records.append(decode(wire[:size], scope))
            wire = wire[size:]
        assert b"".join(block.samples.tobytes() for block in records) == (VECTORS / "first.pcm").read_bytes()
        assert records[-2].count == 203 and records[-1].flags == 1 and records[-1].pts_ns == 104229167
        golden = json.loads((VECTORS / "manifest.json").read_text())["cases"][0]["rows"]
        assert [b.pts_ns for b in records[:-1]] == [row["pts_ns"] for row in golden]
        assert [b.block_id for b in records[:-1]] == list(range(1, 12))
    finally:
        worker.close()


@windows_file
def test_control_rejected_operation_then_close_and_actual_eof_cleanup(tmp_path):
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "private")
    incoming = io.BytesIO(request("open", {"path": str(SOURCE)}) + request("unknown", seq="2") + request("close", seq="3"))
    outgoing = io.BytesIO()
    assert run_control(incoming, outgoing, worker) == 0
    rows = [json.loads(line) for line in outgoing.getvalue().splitlines()]
    assert rows[0]["type"] == "hello" and rows[0]["pid"] == os.getpid()
    assert rows[1]["ok"] and not rows[2]["ok"] and rows[3]["ok"]
    assert rows[3]["result"]["process_exit_pending"] and worker.closed


@windows_file
def test_control_invalid_framing_closes_actual_player(tmp_path):
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "private")
    worker.dispatch("open", {"path": str(SOURCE)})
    original = worker.player
    with pytest.raises(ValueError):
        run_control(io.BytesIO(request(seq="2")), io.BytesIO(), worker)
    assert worker.closed and all(not thread.is_alive() for thread in original._threads)


@windows_file
@pytest.mark.parametrize("seek_after_audio", [False, True])
def test_no_remaining_pcm_explicitly_skips_audio_binding(tmp_path, video, seek_after_audio):
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "unused")
    try:
        data = {"path": str(SOURCE if seek_after_audio else video)}
        if seek_after_audio:
            data["seek_ns"] = "350000000"
        worker.dispatch("open", data)
        limit = time.monotonic() + 5
        while time.monotonic() < limit:
            wait_ready(worker)
            try:
                prepared = worker.prepare({})
                break
            except RuntimeError as error:
                assert "not ready" in str(error)
        else:
            raise AssertionError("Initial silent epoch did not prepare")
        status = worker.describe({})
        assert not status["audio_required"] and status["audio_eof"] and status["audio_frames_buffered"] == 0
        assert status["has_audio"] == seek_after_audio
        with pytest.raises(RuntimeError, match="skip the audio binding"):
            worker.bind_audio({k: prepared[k] for k in ("file_session", "epoch", "generation")} | {"channel": uuid.uuid4().hex})
        assert worker.publisher is None
    finally:
        worker.close()


@windows_file
def test_partial_3d_open_failure_closes_actual_decoder_threads(tmp_path, monkeypatch):
    from quest3d.media_playout import FileAVPlayback
    entered = []
    real_enter = FileAVPlayback.__enter__
    def record_enter(player):
        value = real_enter(player)
        entered.append(player)
        return value
    monkeypatch.setattr(FileAVPlayback, "__enter__", record_enter)
    existing = tmp_path / "preserved-output"
    existing.mkdir()
    worker = PreparedFileWorker(NONCE, TRANSPORT, existing)
    with pytest.raises(FileExistsError):
        worker.dispatch("open", {"path": str(SOURCE), "mode": "3d"})
    assert worker.closed and worker.closing and worker.player is None
    assert len(entered) == 1 and all(not t.is_alive() for t in entered[0]._threads)
    with pytest.raises(RuntimeError, match="closed"):
        worker.dispatch("prepare", {})


@windows_file
def test_cleanup_failure_closes_actor_gate_until_actual_retry(tmp_path):
    from types import SimpleNamespace
    worker = PreparedFileWorker(NONCE, TRANSPORT, tmp_path / "unused")
    worker.dispatch("open", {"path": str(SOURCE)})
    wait_ready(worker)
    before = worker.player.clock_snapshot()
    release = threading.Event()
    thread = threading.Thread(target=release.wait)
    thread.start()
    def close_held_worker():
        thread.join(.01)
        if thread.is_alive():
            raise RuntimeError("Held worker did not close")
    worker.ai = SimpleNamespace(close=close_held_worker, snapshot=lambda: {"ready": True, "error": None})
    try:
        with pytest.raises(RuntimeError, match="Held worker"):
            worker.close()
        assert worker.closing and not worker.closed and worker.player is not None
        for op in ("open", "prepare", "release", "bind_audio", "pump"):
            with pytest.raises(RuntimeError, match="cleanup is pending"):
                worker.dispatch(op, {})
        assert worker.player.clock_snapshot() == before
        assert worker.dispatch("describe", {})["closing"]
    finally:
        release.set()
        thread.join(5)
        worker.close()
    assert worker.closed and worker.player is None


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows CRT/OS standard handles")
def test_native_stdout_is_separate_from_private_control_pipe():
    code = """
import os,ctypes
from ctypes import wintypes as W
from quest3d.file_worker import isolate_control_stdout
control=isolate_control_stdout()
print('python-log')
os.write(1,b'crt-fd-log\\n')
k=ctypes.WinDLL('kernel32',use_last_error=True)
k.GetStdHandle.argtypes=[W.DWORD];k.GetStdHandle.restype=W.HANDLE
k.WriteFile.argtypes=[W.HANDLE,ctypes.c_void_p,W.DWORD,ctypes.POINTER(W.DWORD),ctypes.c_void_p];k.WriteFile.restype=W.BOOL
data=b'win32-handle-log\\n'; count=W.DWORD()
assert k.WriteFile(k.GetStdHandle(W.DWORD(-11)),data,len(data),ctypes.byref(count),None)
assert count.value==len(data)
control.write(b'{"control":true}\\n');control.flush();control.close()
"""
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=10,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b'{"control":true}\n'
    assert all(marker in result.stderr for marker in (b"python-log", b"crt-fd-log", b"win32-handle-log"))

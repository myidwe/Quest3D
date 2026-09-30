"""Actual private worker process, local AI and native PCM handoff; no Quest ACK."""
from datetime import datetime
import hashlib
import importlib.util
import json
from pathlib import Path
import queue
import shutil
import struct
import sys
import time
import traceback
import uuid

from quest3d.file_audio_protocol import Scope, decode
from quest3d.file_worker_protocol import encode_message

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location("ipc_probe", ROOT / "native/host/probe_file_audio_ipc.py")
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
TRACKED = [Path(__file__), ROOT / "native/host/probe_file_audio_ipc.py",
           ROOT / "src/quest3d/file_worker.py", ROOT / "src/quest3d/file_worker_protocol.py",
           ROOT / "src/quest3d/file_audio_protocol.py", ROOT / "src/quest3d/file_audio_ipc.py",
           ROOT / "src/quest3d/media_playout.py", ROOT / "src/quest3d/file_session.py",
           fixture.CLI, fixture.SOURCE, fixture.VECTORS / "manifest.json", fixture.VECTORS / "first.pcm"]


def main():
    destination = Path(sys.argv[1]).resolve()
    destination.relative_to((ROOT / "artifacts").resolve())
    destination.mkdir(parents=True, exist_ok=False)
    mode = sys.argv[2] if len(sys.argv) > 2 else "3d"
    nonce, transport = uuid.uuid4().hex, uuid.uuid4().hex
    before = {str(p.relative_to(ROOT)): fixture.digest(p.read_bytes()) for p in TRACKED}
    command = [sys.executable, "-m", "quest3d.file_worker", "--worker-nonce", nonce,
               "--transport", transport, "--output", str(destination / "worker-ai")]
    result = dict(started_at=datetime.now().astimezone().isoformat(), actual_file_worker_process=True,
                  actual_native_reader_process=True, authenticated_host_coordinator=False,
                  actual_quest=False, audio_device_verified=False, video_publication=False,
                  mode=mode, passed=False)
    try:
        with fixture.ProcessPeer(command, destination, "worker") as child:
            def read():
                try:
                    line = child.lines.get(timeout=15)
                except queue.Empty as error:
                    raise TimeoutError("Actual file worker did not respond") from error
                assert line is not None, "File worker ended before its response"
                row = json.loads(line)
                assert row["v"] == 1 and row["worker_nonce"] == nonce, row
                child.rows.append(dict(at=time.perf_counter_ns(), **row))
                return row
            hello = read()
            assert hello["type"] == "hello"
            child.retain_actual_producer(hello["pid"], int(hello["creation_filetime"]))
            result["hello"] = hello
            next_sequence = 1
            def request(op, data=None):
                nonlocal next_sequence
                seq = str(next_sequence)
                next_sequence += 1
                # Use the binary pipe: Windows text newline translation must not
                # insert CR into the production JSON-line protocol.
                child.process.stdin.buffer.write(encode_message(dict(v=1, worker_nonce=nonce,
                    seq=seq, op=op, data=data or {})))
                child.process.stdin.buffer.flush()
                row = read()
                assert row["type"] == "reply" and row["seq"] == seq and row["ok"], row
                return row["result"]
            request("open", {"path": str(fixture.SOURCE), "mode": mode})
            begin = time.perf_counter()
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = request("describe")
                assert status["error"] is None and not (status["ai"] and status["ai"]["error"]), status
                if status["preroll_ready"]:
                    break
                time.sleep(.05)
            else:
                raise TimeoutError("Actual decoder/AI preroll did not finish")
            result["preroll_seconds"] = time.perf_counter() - begin
            if mode == "3d":
                assert status["ai"]["accepted"] > 0 and status["ai"]["ready"], status
            result["prepared_ai"] = status["ai"]
            prepared = request("prepare")
            binding = {k: prepared[k] for k in ("file_session", "epoch", "generation")}
            binding["channel"] = uuid.uuid4().hex
            bound = request("bind_audio", binding)
            assert bound["ipc"]["producer_pid"] == hello["pid"]
            scope = Scope(binding["file_session"], transport, binding["channel"],
                          int(binding["epoch"]), int(binding["generation"]))
            with fixture.native(scope, destination, "reader") as reader:
                opened = reader.receive("open")
                assert opened["ok"] and opened["producer_pid"] == hello["pid"]
                assert opened["producer_creation"] == int(hello["creation_filetime"])
                full = request("pump")
                assert full["ipc"]["consumer_pid"] == reader.process.pid
                assert full["ipc"]["queued_records"] == 8 and full["audio_frames_consumed"] == 0
                for _ in range(3):
                    repeated = request("pump")
                    assert repeated["accepted"] == 0 and repeated["ipc"] == full["ipc"]
                eof_seen = False
                for _ in range(20):
                    offered = reader.request("peek")
                    assert offered["ok"], offered
                    accepted = reader.request("accept")
                    assert accepted["ok"], accepted
                    state = request("pump")
                    assert state["audio_frames_consumed"] == 0
                    if accepted["eof_handed"]:
                        eof_seen = True
                        break
                assert eof_seen
                result["final_ipc"] = state["ipc"]
                result["before_close"] = request("describe")
                assert result["before_close"]["preroll"] and result["before_close"]["paused"]
                assert result["before_close"]["audio_frames_buffered"] == 5003
                result["reader_retirement"] = fixture.retire(reader)
            closed = request("close")
            assert closed["closed"] and closed["process_exit_pending"]
        result["worker_actual_exit_code"] = child.process.returncode
        wire = (destination / "reader.wire").read_bytes()
        records = []
        remaining = wire
        while remaining:
            length = 128 + struct.unpack_from("<I", remaining, 12)[0]
            records.append(decode(remaining[:length], scope))
            remaining = remaining[length:]
        pcm = b"".join(block.samples.tobytes() for block in records)
        assert pcm == (fixture.VECTORS / "first.pcm").read_bytes()
        assert len(records) == 12 and records[-2].count == 203 and records[-1].pts_ns == 104229167
        golden = json.loads((fixture.VECTORS / "manifest.json").read_text())["cases"][0]["rows"]
        assert [b.pts_ns for b in records[:-1]] == [r["pts_ns"] for r in golden]
        assert [b.block_id for b in records] == list(range(1, 13))
        result.update(passed=True, actual_pcm_frames=len(pcm)//8, records=len(records),
                      pcm_sha256=hashlib.sha256(pcm).hexdigest(), wire_sha256=hashlib.sha256(wire).hexdigest(),
                      original_pts_and_eof_preserved=True, handed_pcm_acknowledged_to_player=False,
                      stdout_only_protocol=True)
    except BaseException:
        result["error"] = traceback.format_exc()
        raise
    finally:
        after = {str(p.relative_to(ROOT)): fixture.digest(p.read_bytes()) for p in TRACKED}
        result.update(finished_at=datetime.now().astimezone().isoformat(), source_sha256=before,
                      source_after_sha256=after, source_unchanged=before == after)
        if before != after:
            result["passed"] = False
        for p in TRACKED:
            if p.suffix in (".py", ".json"):
                target = destination / "source-snapshot" / p.relative_to(ROOT)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
        (destination / "verification.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    assert result["passed"] and result["source_unchanged"]
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

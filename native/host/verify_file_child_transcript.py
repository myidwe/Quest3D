"""Strictly verify preserved native-helper to actual-file-worker messages and lifetime facts."""
from pathlib import Path
import hashlib
import json
import sys


def unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def verify(directory):
    path = directory / "actual-worker-transcript.jsonl"
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    assert len(lines) >= 4
    rows = []
    for line in lines:
        assert line.endswith(b"\n") and 1 < len(line) <= 8193
        assert b"\r" not in line and b"\0" not in line
        rows.append(json.loads(line.decode("utf8"), object_pairs_hook=unique,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Nonfinite JSON"))))
    nonce = "1" * 32
    hello = rows[0]
    assert set(hello) == {"v", "type", "worker_nonce", "pid", "creation_filetime"}
    assert hello["type"] == "hello" and type(hello["pid"]) is int
    for index, row in enumerate(rows):
        assert type(row["v"]) is int and row["v"] == 1 and row["worker_nonce"] == nonce
        if index:
            assert set(row) == {"v", "type", "worker_nonce", "seq", "ok", "result"}
            assert row["type"] == "reply" and row["seq"] == str(index) and row["ok"] is True
    prepared = rows[-2]["result"]
    assert prepared["metadata_ready"] is True and prepared["preroll_ready"] is True
    assert prepared["mode"] == "2d" and prepared["has_audio"] is True
    assert prepared["audio_frames_buffered"] == 5003 and prepared["audio_frames_consumed"] == 0
    assert prepared["error"] is None and prepared["video_publication_active"] is False
    assert prepared["quest_ready_verified"] is False and prepared["paused"] is True
    assert rows[-1]["result"]["closed"] is True
    lifetime_path = directory / "actual-worker-lifetime.json"
    lifetime = json.loads(lifetime_path.read_text())
    assert lifetime["worker_pid"] == hello["pid"]
    assert lifetime["worker_creation"] == hello["creation_filetime"]
    assert lifetime["original_pid"] > 0 and int(lifetime["original_creation"]) > 0
    assert all(lifetime[name] == 1 for name in ("process_exited", "worker_exited", "job_empty", "resources_released"))
    assert lifetime["exit_code"] == 0 and lifetime["authenticated_host_coordinator"] is False
    proof = {"passed": True, "messages": len(rows), "actual_native_helper": True,
             "actual_file_worker": True, "actual_file_pcm_buffered_frames": 5003,
             "worker_is_distinct_from_original": lifetime["worker_pid"] != lifetime["original_pid"],
             "authenticated_host_coordinator": False, "quest_device_verified": False,
             "sha256": {path.name: hashlib.sha256(raw).hexdigest(),
                        lifetime_path.name: hashlib.sha256(lifetime_path.read_bytes()).hexdigest(),
                        "verifier": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}}
    (directory / "transcript-verification.json").write_text(json.dumps(proof, indent=2) + "\n")
    return proof


if __name__ == "__main__":
    print(json.dumps(verify(Path(sys.argv[1]).resolve()), indent=2))

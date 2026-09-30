"""Real filesystem concurrency and bounded snapshots, including Win32 sharing."""
import threading

import pytest

from quest3d.session_control import atomic_json, read_json


def test_atomic_replacement_and_concurrent_reader_preserve_complete_snapshots(tmp_path):
    path = tmp_path / "state.json"
    atomic_json(path, {"sequence": 0, "payload": "0" * 2000})
    errors = []
    def writer():
        try:
            for number in range(1, 201):
                atomic_json(path, {"sequence": number, "payload": str(number) * 2000})
        except BaseException as exc:
            errors.append(exc)
    thread = threading.Thread(target=writer)
    thread.start()
    for _ in range(300):
        state = read_json(path)
        assert state["payload"] == str(state["sequence"]) * 2000
    thread.join(3)
    assert not thread.is_alive()
    assert not errors
    assert read_json(path)["sequence"] == 200


def test_oversized_and_invalid_snapshots_are_rejected(tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'"' + b"x" * 9000 + b'"')
    with pytest.raises(ValueError, match="exceeds"):
        read_json(path, max_bytes=8192)
    path.write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        read_json(path)
    with pytest.raises(FileNotFoundError):
        read_json(tmp_path / "missing.json")

"""Recovery policy tests use fake audio endpoints; Windows tests never route audio."""

from contextlib import nullcontext
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from quest3d import audio_recovery as ar


def _record(phase="active"):
    return {
        "schema": 1, "owner_pid": 123, "owner_creation_filetime": "1234567890", "phase": phase,
        "format_changes": False, "mute_changes": False, "volume_changes": False,
        "roles": [{"role": role, "original": f"original-{role}", "target": "virtual", "applied": phase == "active"} for role in range(3)],
    }


class FakeAudio:
    def __init__(self):
        self.defaults = ["virtual"] * 3
        self.available = {"virtual", "user-choice", *(f"original-{role}" for role in range(3))}
        self.calls = []
        self.fail_roles = set()
        self.after_set = None

    def current(self, role):
        return self.defaults[role]

    def endpoint_active(self, endpoint):
        return endpoint in self.available

    def set_default(self, role, endpoint):
        if role in self.fail_roles:
            raise ar.RecoveryError("Injected COM failure")
        self.calls.append((role, endpoint))
        self.defaults[role] = endpoint
        if self.after_set:
            self.after_set(role)


class FakeCoupledAudio(FakeAudio):
    coupled_roles = ((0, 1),)

    def set_default(self, role, endpoint):
        super().set_default(role, endpoint)
        if role in (0, 1):
            self.defaults[1 - role] = endpoint


@pytest.fixture
def fixture(tmp_path):
    path = tmp_path / "audio-route.json"
    path.write_text(json.dumps(_record()), encoding="utf-8")
    return path, FakeAudio()


def _recover(path, backend, **kwargs):
    return ar.recover_journal(path, backend, allowed_root=path.parent,
                              inspect_owner=lambda *_: ar.OwnerState("dead"), lock=lambda _: nullcontext(), **kwargs)


def test_dry_run_does_not_write_or_route(fixture):
    path, backend = fixture
    original = path.read_bytes()
    result = _recover(path, backend)
    assert result["status"] == "dry_run"
    assert [role["status"] for role in result["roles"]] == ["would_restore"] * 3
    assert path.read_bytes() == original
    assert backend.calls == []


def test_restore_independent_roles_and_idempotent_completion(fixture):
    path, backend = fixture
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restored"
    assert backend.defaults == [f"original-{role}" for role in range(3)]
    assert result["changed_roles"] == [0, 1, 2]
    assert ar.validate_journal(path.read_bytes())["phase"] == "restored"
    assert _recover(path, backend, apply=True)["status"] == "already_restored"
    assert len(backend.calls) == 3


def test_planned_crash_between_com_and_active_write_is_recoverable(fixture):
    path, backend = fixture
    path.write_text(json.dumps(_record("planned")), encoding="utf-8")
    backend.defaults[1:] = ["original-1", "original-2"]
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restored"
    assert backend.calls == [(0, "original-0")]


@pytest.mark.parametrize("owner", ["alive", "unknown", "unsupported"])
def test_live_or_unknown_owner_blocks_mutation(fixture, owner):
    path, backend = fixture
    before = path.read_bytes()
    result = ar.recover_journal(path, backend, apply=True, allowed_root=path.parent,
                                inspect_owner=lambda *_: ar.OwnerState(owner), lock=lambda _: nullcontext())
    assert result["status"] == "blocked_owner"
    assert path.read_bytes() == before
    assert backend.calls == []


def test_external_selection_is_preserved(fixture):
    path, backend = fixture
    backend.defaults[1] = "user-choice"
    result = _recover(path, backend, apply=True)
    assert result["roles"][1]["status"] == "external_preserved"
    assert backend.defaults[1] == "user-choice"
    assert result["changed_roles"] == [0, 2]


def test_change_during_journal_commit_is_rechecked(fixture):
    path, backend = fixture
    def writer(path, record):
        ar.write_journal(path, record)
        if record["roles"][0].get("recovery_status") == "restoring":
            backend.defaults[0] = "user-choice"
    result = _recover(path, backend, apply=True, writer=writer)
    assert result["roles"][0]["status"] == "external_preserved"
    assert (0, "original-0") not in backend.calls


def test_unavailable_original_remains_pending(fixture):
    path, backend = fixture
    backend.available.remove("original-1")
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restore_pending"
    assert result["roles"][1]["status"] == "original_unavailable"
    assert backend.defaults[1] == "virtual"
    assert ar.validate_journal(path.read_bytes())["phase"] == "restore_pending"


def test_partial_failure_and_retry_do_not_reclaim_completed_roles(fixture):
    path, backend = fixture
    backend.fail_roles.add(1)
    first = _recover(path, backend, apply=True)
    assert first["status"] == "restore_pending"
    assert first["changed_roles"] == [0, 2]
    # A later user choice of the old target must not give a completed role back.
    backend.defaults[0] = "virtual"
    backend.fail_roles.clear()
    second = _recover(path, backend, apply=True)
    assert second["status"] == "restored"
    assert second["roles"][0]["status"] == "completed_earlier"
    assert second["changed_roles"] == [1]
    assert backend.defaults[0] == "virtual"


def test_preflight_write_failure_happens_before_audio_mutation(fixture):
    path, backend = fixture
    before = path.read_bytes()
    def failing_writer(*_):
        raise ar.RecoveryError("Injected disk failure")
    result = _recover(path, backend, apply=True, writer=failing_writer)
    assert result["status"] == "refused"
    assert backend.calls == []
    assert path.read_bytes() == before


def test_commit_failure_after_com_can_resume_without_repeating_role(fixture):
    path, backend = fixture
    calls = 0
    def failing_writer(path, record):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise ar.RecoveryError("Injected post-COM disk failure")
        ar.write_journal(path, record)
    first = _recover(path, backend, apply=True, writer=failing_writer)
    assert first["status"] == "refused"
    assert first["attempted_roles"] == [0]
    assert backend.defaults[0] == "original-0"
    second = _recover(path, backend, apply=True)
    assert second["status"] == "restored"
    assert backend.calls.count((0, "original-0")) == 1


def test_readback_preserves_new_external_choice(fixture):
    path, backend = fixture
    backend.after_set = lambda role: backend.defaults.__setitem__(role, "user-choice")
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restored"
    assert all(role["status"] == "external_preserved" for role in result["roles"])
    assert backend.defaults == ["user-choice"] * 3


def test_coupled_windows_roles_restore_once_and_verify_both(fixture):
    path, _ = fixture
    backend = FakeCoupledAudio()
    record = _record()
    record["roles"][1]["original"] = "original-0"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restored"
    assert backend.calls == [(0, "original-0"), (2, "original-2")]
    assert result["roles"][1]["status"] == "already_original"


def test_coupled_distinct_originals_remain_pending_without_overwrite(fixture):
    path, _ = fixture
    backend = FakeCoupledAudio()
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restore_pending"
    assert [row["status"] for row in result["roles"][:2]] == ["coupled_roles_conflict"] * 2
    assert backend.calls == [(2, "original-2")]
    assert backend.defaults[:2] == ["virtual", "virtual"]


def test_coupled_peer_external_choice_is_preserved(fixture):
    path, _ = fixture
    backend = FakeCoupledAudio()
    backend.defaults[1] = "user-choice"
    record = _record()
    record["roles"][1]["original"] = "original-0"
    path.write_text(json.dumps(record), encoding="utf-8")
    result = _recover(path, backend, apply=True)
    assert result["status"] == "restore_pending"
    assert result["roles"][0]["status"] == "coupled_roles_conflict"
    assert result["roles"][1]["status"] == "external_preserved"
    assert backend.defaults[:2] == ["virtual", "user-choice"]


@pytest.mark.parametrize("mutation", [
    lambda row: row.update(schema=True),
    lambda row: row.update(schema=2),
    lambda row: row.update(owner_pid=True),
    lambda row: row.update(owner_pid=-1),
    lambda row: row.pop("owner_creation_filetime"),
    lambda row: row.update(owner_creation_filetime="x"),
    lambda row: row.update(owner_creation_filetime="9" * 1000),
    lambda row: row.update(phase=[]),
    lambda row: row.update(mute_changes="false"),
    lambda row: row.update(volume_changes=True),
    lambda row: row.update(format_changes=True),
    lambda row: row["roles"][1].update(role=0),
    lambda row: row["roles"][1].update(applied=1),
    lambda row: row["roles"][1].update(original=""),
    lambda row: row["roles"][1].update(original="nul\0device"),
    lambda row: row["roles"][1].update(recovery_status=[]),
    lambda row: row.update(phase="restored"),
])
def test_bad_or_legacy_journal_is_not_overwritten(fixture, mutation):
    path, backend = fixture
    record = _record()
    mutation(record)
    before = json.dumps(record).encode()
    path.write_bytes(before)
    result = _recover(path, backend, apply=True)
    assert result["status"] == "refused"
    assert backend.calls == []
    assert path.read_bytes() == before


@pytest.mark.parametrize("raw", [b"", b"not JSON", b'{"schema":1,"schema":1}', b" " * 65537], ids=["empty", "malformed", "duplicate-key", "oversized"])
def test_invalid_raw_journal_rejected(raw):
    with pytest.raises(ar.RecoveryError):
        ar.validate_journal(raw)


def test_artifact_directory_boundary(fixture, tmp_path):
    path, backend = fixture
    outside = tmp_path / "other"
    outside.mkdir()
    result = ar.recover_journal(path, backend, apply=True, allowed_root=outside)
    assert result["status"] == "invalid_path"
    assert backend.calls == []


def test_watch_waits_for_same_owner_and_never_writes(fixture):
    path, _ = fixture
    original = path.read_bytes()
    states = iter(["alive", "alive", "dead"])
    elapsed = [0.0]
    result = ar.wait_for_owner_exit(path, allowed_root=path.parent, inspect_owner=lambda *_: ar.OwnerState(next(states)),
                                    clock=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    assert result["status"] == "owner_exited"
    assert elapsed[0] == pytest.approx(0.1)
    assert path.read_bytes() == original


def test_watch_times_out_without_treating_elapsed_time_as_owner_death(fixture):
    path, _ = fixture
    elapsed = [0.0]
    result = ar.wait_for_owner_exit(path, timeout=0.1, allowed_root=path.parent, inspect_owner=lambda *_: ar.OwnerState("alive"),
                                    clock=lambda: elapsed[0], sleep=lambda seconds: elapsed.__setitem__(0, elapsed[0] + seconds))
    assert result["status"] == "watch_timeout"


def test_watch_does_not_follow_replacement_session(fixture):
    path, _ = fixture
    def replace_owner(_):
        record = _record()
        record["owner_creation_filetime"] = "9999999"
        path.write_text(json.dumps(record), encoding="utf-8")
    result = ar.wait_for_owner_exit(path, allowed_root=path.parent, inspect_owner=lambda *_: ar.OwnerState("alive"), sleep=replace_owner)
    assert result["status"] == "owner_replaced"


def test_watch_unknown_owner_does_not_wait_or_recover(fixture):
    path, _ = fixture
    result = ar.wait_for_owner_exit(path, allowed_root=path.parent, inspect_owner=lambda *_: ar.OwnerState("unknown"))
    assert result["status"] == "blocked_owner"


def test_new_owner_after_watch_before_lock_is_not_recovered(fixture):
    path, backend = fixture
    result = _recover(path, backend, apply=True, expected_owner=(123, "different-owner"))
    assert result["status"] == "owner_replaced"
    assert backend.calls == []


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows lock/lifetime calls")
def test_real_windows_lock_is_exclusive_and_released_after_exception(tmp_path):
    path = tmp_path / "lock.json"
    with pytest.raises(RuntimeError, match="injected"):
        with ar.journal_lock(path):
            with pytest.raises(ar.RecoveryError, match="locked"):
                with ar.journal_lock(path):
                    pytest.fail("Duplicate owner acquired the lock")
            raise RuntimeError("injected")
    with ar.journal_lock(path):
        pass
    assert not Path(str(path) + ".lock").exists()


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows process identity")
def test_real_current_owner_and_pid_reuse_are_distinct():
    identity = ar.process_identity(os.getpid())
    assert identity[2] is True
    assert ar.owner_state(os.getpid(), str(identity[0])).state == "alive"
    assert ar.owner_state(os.getpid(), str(identity[0] - 1)).state == "pid_reused"


@pytest.mark.skipif(os.name != "nt", reason="Actual Windows child crash releases file lock")
def test_real_child_exit_releases_journal_and_is_not_a_live_owner(tmp_path):
    path = tmp_path / "child.json"
    code = "import os,sys; from pathlib import Path; from quest3d.audio_recovery import journal_lock,process_identity; ctx=journal_lock(Path(sys.argv[1])); ctx.__enter__(); print(str(os.getpid())+' '+str(process_identity(os.getpid())[0]),flush=True); sys.stdin.readline(); os._exit(17)"
    child = subprocess.Popen([sys.executable, "-c", code, str(path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        pid, created = child.stdout.readline().strip().split()
        assert ar.owner_state(int(pid), created).state == "alive"
        with pytest.raises(ar.RecoveryError):
            with ar.journal_lock(path):
                pytest.fail("Child owns this lock")
        child.communicate("exit\n", timeout=10)
        assert child.returncode == 17
        assert ar.owner_state(int(pid), created).state in {"dead", "pid_reused"}
        with ar.journal_lock(path):
            pass
    finally:
        if child.poll() is None:
            child.kill()
            child.communicate(timeout=10)


@pytest.mark.skipif(os.name != "nt", reason="Read-only Core Audio")
def test_real_core_audio_snapshot_and_read_only_setter_guard():
    with ar.WindowsAudioBackend() as backend:
        before = backend.snapshot()
        assert len(before["default_roles"]) == 3
        assert before["active_render_endpoints"]
        with pytest.raises(ar.RecoveryError, match="Read-only"):
            backend.set_default(0, before["default_roles"][0])
        assert backend.snapshot() == before

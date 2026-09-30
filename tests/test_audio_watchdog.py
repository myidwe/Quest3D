"""Watchdog lifecycle policy without changing any Windows endpoint."""
import copy
import json
import os
from pathlib import Path
import uuid

import pytest

from quest3d import audio_recovery as recovery
from quest3d import audio_watchdog as watchdog


def record():
    return {"schema": 1, "owner_pid": 123, "owner_creation_filetime": "456",
            "phase": "planned", "watchdog_armed": False,
            "format_changes": False, "mute_changes": False, "volume_changes": False,
            "roles": [{"role": role, "original": "original", "target": "virtual", "applied": False}
                      for role in range(3)]}


def test_ready_waits_for_plan_and_does_not_expire_during_long_host_lifetime(tmp_path):
    path = tmp_path / "route.json"
    ready = []
    steps = []
    def advance(_):
        steps.append(1)
        if len(steps) == 2:
            path.write_text(json.dumps(record()))
    result = watchdog.watch_registered_owner(path, (123, "456"),
        inspect_lifetime=lambda: recovery.OwnerState("alive" if len(steps) < 5 else "dead"),
        signal_ready=lambda: ready.append(len(steps)), allowed_root=tmp_path,
        sleep=advance, clock=lambda: len(steps) * 864000.0)
    assert ready == [2]
    assert result["status"] == "owner_exited"
    assert json.loads(path.read_text()) == record()


def test_dead_host_without_route_has_nothing_to_recover(tmp_path):
    result = watchdog.watch_registered_owner(tmp_path / "route.json", (123, "456"),
        inspect_lifetime=lambda: recovery.OwnerState("dead"), signal_ready=lambda: pytest.fail("READY"),
        allowed_root=tmp_path)
    assert result["status"] == "no_route"


@pytest.mark.parametrize("change", ["owner", "active", "armed", "legacy", "malformed"])
def test_wrong_owner_late_or_invalid_plan_cannot_authorize_route(tmp_path, change):
    path = tmp_path / "route.json"
    value = record()
    if change == "owner": value["owner_creation_filetime"] = "457"
    if change == "active": value.update(phase="active", watchdog_armed=True)
    if change == "armed": value["watchdog_armed"] = True
    if change == "legacy": del value["watchdog_armed"]
    path.write_text("{" if change == "malformed" else json.dumps(value))
    call = lambda: watchdog.watch_registered_owner(path, (123, "456"),
        inspect_lifetime=lambda: recovery.OwnerState("alive"), signal_ready=lambda: pytest.fail("READY"),
        allowed_root=tmp_path)
    if change == "malformed":
        with pytest.raises(recovery.RecoveryError): call()
    else:
        assert call()["status"] in {"owner_replaced", "late_registration"}


def test_replacement_after_ready_stops_old_watchdog(tmp_path):
    path = tmp_path / "route.json"
    value = record()
    path.write_text(json.dumps(value))
    ready = []
    def replace(_):
        value["owner_pid"] = 789
        path.write_text(json.dumps(value))
    result = watchdog.watch_registered_owner(path, (123, "456"),
        inspect_lifetime=lambda: recovery.OwnerState("alive"), signal_ready=lambda: ready.append(True),
        allowed_root=tmp_path, sleep=replace)
    assert result["status"] == "owner_replaced" and ready == [True]
    assert json.loads(path.read_text())["owner_pid"] == 789


def test_unknown_owner_never_authorizes_a_route(tmp_path):
    result = watchdog.watch_registered_owner(tmp_path / "route.json", (123, "456"),
        inspect_lifetime=lambda: recovery.OwnerState("unknown"), signal_ready=lambda: pytest.fail("READY"),
        allowed_root=tmp_path)
    assert result["status"] == "blocked_owner"


@pytest.mark.skipif(os.name != "nt", reason="Win32 objects")
def test_duplicate_token_does_not_reset_first_watchdogs_ready_event():
    token = uuid.uuid4().hex
    first = watchdog.WatchdogSignals(token)
    try:
        first.arm()
        assert first.kernel.WaitForSingleObject(first.ready, 0) == 0
        with pytest.raises(recovery.RecoveryError):
            watchdog.WatchdogSignals(token)
        assert first.kernel.WaitForSingleObject(first.ready, 0) == 0
    finally:
        first.close()


@pytest.mark.skipif(os.name != "nt", reason="Win32 process identity")
def test_open_process_handle_rejects_wrong_creation_time():
    created = recovery.process_identity(os.getpid())[0]
    wrong = watchdog.ProcessLifetime(os.getpid(), str(created + 1))
    right = watchdog.ProcessLifetime(os.getpid(), str(created))
    try:
        assert wrong.state().state == "dead"
        assert right.state().state == "alive"
    finally:
        wrong.close()
        right.close()


def test_unarmed_record_preserves_a_later_user_selection_of_our_target(tmp_path):
    path = tmp_path / "route.json"
    path.write_text(json.dumps(record()))
    class NeverRoute:
        def current(self, _): return "virtual"
        def endpoint_active(self, _): return True
        def set_default(self, *_): pytest.fail("An unarmed record cannot own the user's target selection")
    from contextlib import nullcontext
    result = recovery.recover_journal(path, NeverRoute(), apply=True, allowed_root=tmp_path,
        inspect_owner=lambda *_: recovery.OwnerState("dead"), lock=lambda _: nullcontext())
    assert result["status"] == "restored" and result["unarmed"]
    assert all(role["recovery_status"] == "never_changed" for role in json.loads(path.read_text())["roles"])


@pytest.mark.parametrize("field,value", [("watchdog_armed", 1), ("phase", "active")])
def test_unarmed_schema_cannot_claim_active_routing(field, value):
    candidate = copy.deepcopy(record())
    candidate[field] = value
    with pytest.raises(recovery.RecoveryError):
        recovery.validate_journal(json.dumps(candidate).encode())

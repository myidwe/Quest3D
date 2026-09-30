"""Desktop lifecycle contracts using temporary files and injected observations.

No real processes, monitor enumeration, GUI, network, model, or GPU are used.
Process absence is deliberately independent of clean producer-stop evidence.
"""
from __future__ import annotations

import copy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from quest3d import desktop_backend as backend
from quest3d import session_control


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def forbidden(*args, **kwargs):
    pytest.fail("Actual process/capture/GUI access is forbidden in this fixture")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    clock = SimpleNamespace(seconds=100.0, on_sleep=None)

    def sleep(seconds):
        clock.seconds += seconds
        if clock.on_sleep:
            clock.on_sleep()

    monkeypatch.setattr(backend.time, "monotonic", lambda: clock.seconds)
    monkeypatch.setattr(backend.time, "perf_counter_ns", lambda: int(clock.seconds * 1e9))
    monkeypatch.setattr(backend.time, "sleep", sleep)
    monkeypatch.setattr(backend.subprocess, "Popen", forbidden)
    monkeypatch.setattr(backend.psutil, "process_iter", forbidden)
    monkeypatch.setattr(backend.psutil, "net_connections", forbidden)
    monkeypatch.setattr(backend.psutil, "net_if_addrs", forbidden)
    if hasattr(backend.os, "startfile"):
        monkeypatch.setattr(backend.os, "startfile", forbidden)

    controller = backend.DesktopController(tmp_path, start_worker=False)
    directory = tmp_path / "artifacts/session-existing"
    producer = {"pid": 101, "exe": str(tmp_path / "Python312/python.exe"), "birth": "12340001"}
    host = {"pid": 202, "exe": str((controller.runtime / "sunshine.exe").resolve()), "birth": "12340002"}
    state = SimpleNamespace(producer=producer, host=host, directory=directory,
                            live={101: producer, 202: host})
    monitor = {"device_name": r"\\.\DISPLAY2", "index": 1, "label": "monitor"}

    def observed_identity(pid):
        if pid not in state.live:
            raise backend.psutil.NoSuchProcess(pid)
        return dict(state.live[pid])

    monkeypatch.setattr(backend, "identity", observed_identity)

    def scan():
        controller._set(monitors=[monitor], selected_monitor=controller.preferences["monitor_device"])
        controller._last_source_scan = clock.seconds

    monkeypatch.setattr(controller, "_scan_sources", scan)
    monkeypatch.setattr(controller, "_find_producer", lambda path: (
        (dict(state.producer), [str(controller.python), "-m", "quest3d.cli", "serve", "--monitor", "1",
                                "--output", str(path)])
        if state.producer and path == state.directory and state.producer["pid"] in state.live else (None, None)))
    monkeypatch.setattr(controller, "_host_identity", lambda: (
        dict(state.host) if state.host and state.host["pid"] in state.live else None))
    monkeypatch.setattr(controller, "_connection", lambda ok: (None, "injected connection observation"))
    monkeypatch.setattr(controller, "_run", forbidden)
    # Readiness is a separate boundary with focused fault tests below.
    monkeypatch.setattr(controller, "_verify_host_ready", lambda **kwargs: None)
    monkeypatch.setattr(backend, "sha256_file", lambda path: backend.HOST_SHA)

    status = dict(session_id="a" * 32, stream_epoch=1234567890123456789,
                  running=True, requested_mode="3d", effective_mode="3d", disparity=22.32,
                  revision=19, eye_width=1920, eye_height=1080, disparity_profile="comfort",
                  view_layout="enlarged", bridge_protocol=2, media=None,
                  ai_ready=True, ai_error=None, error=None, capture_unavailable=None,
                  updated_monotonic_ns=int(clock.seconds * 1e9), published_frames=200,
                  ai_completed_published=100, applied_request="old-application", seen_request="old-application",
                  rejected_request=None)
    write_json(directory / "status.json", status)
    write_json(tmp_path / "artifacts/active-session.json", {"directory": str(directory), "session_id": status["session_id"]})
    write_json(tmp_path / "artifacts/host/dev/launch.json", {"runtime": str(controller.runtime),
        "host_sha256": backend.HOST_SHA, "source_session": {"session_id": status["session_id"],
        "stream_epoch": str(status["stream_epoch"])}})
    write_json(tmp_path / "artifacts/host/dev/process.json", {"process_id": 202,
        "owner_creation_filetime": host["birth"], "runtime": str(controller.runtime), "executable": host["exe"]})
    return SimpleNamespace(c=controller, state=state, status=status, clock=clock,
                           monitor=monitor, directory=directory, monkeypatch=monkeypatch)


def test_adopt_healthy_existing_pipeline_does_not_launch_or_rebind(rig):
    c = rig.c
    c._start()
    assert c._producer == rig.state.producer and c._host == rig.state.host
    assert c._session == rig.directory
    assert c.get_snapshot()["running"] and c.get_snapshot()["host_running"]
    assert c.preferences["depth_percent"] == pytest.approx(1.1625)
    assert c.preferences["profile"] == "comfort"
    assert not list(c.logs.glob("run-*"))
    c._start()  # Repeated explicit start adopts, never creates a second child.
    assert not list(c.logs.glob("run-*"))


def test_command_queue_rejects_duplicate_start_and_close_prevents_new_work(rig):
    assert rig.c.command("start")
    assert not rig.c.command("start")
    assert rig.c._commands.qsize() == 1
    rig.c.close()
    assert not rig.c.command("refresh")


def test_newly_observed_startup_error_clears_when_real_source_recovers(rig):
    write_json(rig.directory / 'status.json', {**rig.status, 'running': False})
    rig.c._inspect()
    assert rig.c.get_snapshot()['error']
    write_json(rig.directory / 'status.json', rig.status)
    rig.c._inspect()
    state = rig.c.get_snapshot()
    assert state['phase'] == 'running' and state['ready'] and state['error'] is None


def test_health_recovery_does_not_hide_an_unrelated_command_failure(rig):
    rig.c._inspect()
    rig.c._set(phase='error', error='Command requires user attention')
    rig.c._inspect()
    assert rig.c.get_snapshot()['error'] == 'Command requires user attention'


class ReachedStartPreflight(RuntimeError):
    """Stop the injected start before any capture/model/process preparation."""


def preserve_owned_launch(rig, *, pid=404, live=False, directory=None):
    directory = directory or rig.directory
    launcher = dict(pid=pid, exe=str(rig.c.python), birth='owned-launch-lifetime')
    record = dict(directory=str(directory), launcher_identity=launcher)
    rig.c._pending = record
    write_json(rig.c._pending_path, record)
    if live:
        rig.state.live[pid] = launcher
    return record


def preflight_sentinel():
    raise ReachedStartPreflight('reached preflight without starting a process')


@pytest.mark.parametrize('raw', [
    b'\0' * 5597, b'', b'{"session_id":', b'\xff\xfe\x80', b'[]', b'null',
    b'"status"', b'{}', b'{"session_id":"abc"}', b'{"running":false}',
], ids=['nul', 'empty', 'truncated', 'invalid-utf8', 'list', 'null', 'string',
        'empty-object', 'missing-running', 'missing-session-id'])
def test_dead_corrupt_status_preserves_records_and_allows_new_start_preflight(rig, raw):
    rig.state.live.clear()
    record = preserve_owned_launch(rig)
    path = rig.directory / 'status.json'
    path.write_bytes(raw)
    marker = rig.c.root / 'artifacts/active-session.json'
    originals = {p: p.read_bytes() for p in (path, marker, rig.c._pending_path)}
    rig.monkeypatch.setattr(rig.c, '_preflight', preflight_sentinel)

    rig.c._inspect()
    snapshot = rig.c.get_snapshot()
    assert snapshot['phase'] == 'idle' and snapshot['error'] is None
    assert not snapshot['running'] and not snapshot['ready'] and not snapshot['host_running']
    assert rig.c._session == rig.directory and rig.c._pending == record
    with pytest.raises(ReachedStartPreflight):
        rig.c._start()

    assert {p: p.read_bytes() for p in originals} == originals
    assert rig.c._pending == record
    events = [json.loads(line) for line in (rig.c.logs / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
    corrupt = [event for event in events if event['event'] == 'invalid_session_status']
    assert len(corrupt) == 1, 'unchanged corruption must not spam polling logs'
    assert corrupt[0]['preserved'] is True and corrupt[0]['bytes'] == len(raw)


@pytest.mark.parametrize('field', ['session_id', 'running', 'eye_width', 'eye_height',
    'updated_monotonic_ns', 'published_frames', 'stream_epoch', 'disparity', 'requested_mode'])
def test_dead_incomplete_status_is_not_adopted_or_overwritten(rig, field):
    rig.state.live.clear()
    malformed = {key: value for key, value in rig.status.items() if key != field}
    path = rig.directory / 'status.json'
    write_json(path, malformed)
    original = path.read_bytes()
    rig.monkeypatch.setattr(rig.c, '_preflight', preflight_sentinel)
    rig.c._inspect()
    assert rig.c._status is None
    assert rig.c.get_snapshot()['phase'] == 'idle'
    with pytest.raises(ReachedStartPreflight):
        rig.c._start()
    assert path.read_bytes() == original


@pytest.mark.parametrize('remaining', ['producer', 'launcher', 'host'])
def test_corrupt_status_cannot_hide_live_work_or_launch_duplicate(rig, remaining):
    rig.state.live.clear()
    if remaining == 'producer':
        rig.state.live[101] = rig.state.producer
    elif remaining == 'host':
        rig.state.live[202] = rig.state.host
    record = preserve_owned_launch(rig, live=remaining == 'launcher')
    path = rig.directory / 'status.json'
    path.write_bytes(b'\0' * 5597)
    original = path.read_bytes()
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden)

    rig.c._inspect()
    snapshot = rig.c.get_snapshot()
    assert snapshot['phase'] == 'error' and not snapshot['ready'] and snapshot['error']
    assert snapshot['running'] is (remaining != 'host')
    assert snapshot['host_running'] is (remaining == 'host')
    with pytest.raises(RuntimeError):
        rig.c._start()
    assert rig.c._pending == record
    assert json.loads(rig.c._pending_path.read_text(encoding='utf-8')) == record
    assert path.read_bytes() == original


@pytest.mark.parametrize('external_error', [False, True])
def test_corrupt_live_status_recovers_only_its_own_health_error(rig, external_error):
    path = rig.directory / 'status.json'
    path.write_bytes(b'\0' * 5597)
    rig.c._inspect()
    assert rig.c.get_snapshot()['error'] and not rig.c.get_snapshot()['ready']
    if external_error:
        rig.c._set(phase='error', error='unrelated command failed')
    write_json(path, rig.status)
    rig.c._inspect()
    snapshot = rig.c.get_snapshot()
    assert snapshot['running'] and snapshot['ready']
    if external_error:
        assert snapshot['error'] == 'unrelated command failed'
    else:
        assert snapshot['phase'] == 'running' and snapshot['error'] is None


@pytest.mark.parametrize('live', [False, True])
def test_status_corruption_during_process_discovery_is_rechecked_safely(rig, live):
    rig.state.live.clear()
    if live:
        rig.state.live[101] = rig.state.producer
    path = rig.directory / 'status.json'
    find = rig.c._find_producer
    observed = []

    def corrupt_during_discovery(directory):
        observed.append(directory)
        result = find(directory)
        path.write_bytes(b'{"interrupted":')
        return result

    rig.monkeypatch.setattr(rig.c, '_find_producer', corrupt_during_discovery)
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden if live else preflight_sentinel)
    rig.c._inspect()
    assert observed == [rig.directory]
    assert rig.c._status is None
    assert rig.c.get_snapshot()['running'] is live
    assert not rig.c.get_snapshot()['ready']
    if live:
        with pytest.raises(RuntimeError):
            rig.c._start()
    else:
        assert rig.c.get_snapshot()['phase'] == 'idle'
        with pytest.raises(ReachedStartPreflight):
            rig.c._start()
    assert path.read_bytes() == b'{"interrupted":'


@pytest.mark.parametrize('conflict', ['session-id', 'external-directory'])
def test_status_recovery_does_not_relax_session_identity_or_path(rig, conflict):
    rig.state.live.clear()
    marker = rig.c.root / 'artifacts/active-session.json'
    if conflict == 'session-id':
        write_json(rig.directory / 'status.json', {**rig.status, 'session_id': 'b' * 32})
    else:
        # No external file is opened or created: the marker must reject first.
        write_json(marker, {'directory': str(rig.c.root.parent / 'outside-owned-root'),
                            'session_id': rig.status['session_id']})
    originals = {p: p.read_bytes() for p in (marker, rig.directory / 'status.json')}
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden)
    with pytest.raises(RuntimeError):
        rig.c._start()
    assert {p: p.read_bytes() for p in originals} == originals


def test_corrupt_active_session_and_different_live_pending_session_preserve_both(rig):
    pending_dir = rig.c.root / 'artifacts/session-new-pending'
    pending_dir.mkdir()
    record = preserve_owned_launch(rig, live=True, directory=pending_dir)
    (rig.directory / 'status.json').write_bytes(b'\0' * 100)
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden)
    with pytest.raises(RuntimeError, match='서로 다른'):
        rig.c._start()
    assert rig.c._pending == record
    assert backend.same_process(record['launcher_identity'])
    assert backend.same_process(rig.state.producer)
    assert json.loads((rig.c.root / 'artifacts/active-session.json').read_text(encoding='utf-8'))['directory'] == str(rig.directory)


def test_strict_identity_access_denial_is_not_process_absence(rig):
    def denied(pid):
        raise backend.psutil.AccessDenied(pid)

    rig.monkeypatch.setattr(backend, 'identity', denied)
    with pytest.raises(backend.psutil.AccessDenied):
        backend.same_process(rig.state.producer, strict=True)


def test_pending_launcher_access_denial_prevents_start(rig):
    rig.state.live.clear()
    record = preserve_owned_launch(rig)
    (rig.directory / 'status.json').write_bytes(b'\0' * 100)
    observed_identity = backend.identity

    def denied_launcher(pid):
        if pid == record['launcher_identity']['pid']:
            raise backend.psutil.AccessDenied(pid)
        return observed_identity(pid)

    rig.monkeypatch.setattr(backend, 'identity', denied_launcher)
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden)
    with pytest.raises(backend.psutil.AccessDenied):
        rig.c._start()
    assert rig.c._pending == record


@pytest.mark.parametrize('denied_field', ['cmdline', 'cwd', 'exe'])
def test_producer_discovery_access_denial_prevents_start(rig, denied_field):
    rig.state.live.clear()
    (rig.directory / 'status.json').write_bytes(b'\0' * 100)
    process = SimpleNamespace(
        pid=505, info={'pid': 505, 'name': 'python.exe'},
        cmdline=lambda: [str(rig.c.python), '-m', 'quest3d.cli', 'serve', '--monitor', '1',
                         '--output', str(rig.directory)],
        cwd=lambda: str(rig.c.root), exe=lambda: str(rig.c.python), ppid=lambda: 0)

    def denied():
        raise backend.psutil.AccessDenied(505)

    setattr(process, denied_field, denied)
    rig.monkeypatch.setattr(backend.psutil, 'process_iter', lambda fields: iter([process]))
    rig.monkeypatch.setattr(rig.c, '_find_producer',
                          lambda directory: backend.DesktopController._find_producer(rig.c, directory))
    rig.monkeypatch.setattr(rig.c, '_preflight', forbidden)
    with pytest.raises(RuntimeError, match='권한'):
        rig.c._start()


@pytest.mark.parametrize("change", [
    {"updated_monotonic_ns": 1}, {"error": "capture failed"},
    {"ai_error": "model failed"}, {"running": False},
])
def test_start_does_not_report_success_for_unhealthy_existing_pair(rig, change):
    write_json(rig.directory / "status.json", {**rig.status, **change})
    with pytest.raises(RuntimeError):
        rig.c._start()


def test_mismatched_host_epoch_remains_conflict_during_inspect(rig):
    path = rig.c.root / "artifacts/host/dev/launch.json"
    value = json.loads(path.read_text())
    value["source_session"]["stream_epoch"] = "999"
    write_json(path, value)
    rig.c._inspect()
    assert rig.c.get_snapshot()["phase"] == "conflict"
    assert rig.c.get_snapshot()["error"]
    with pytest.raises(RuntimeError):
        rig.c._start()


@pytest.mark.parametrize("raw", [b"{broken", b'{"version":2}',
    b'{"version":1,"depth_percent":true}', b'{"version":1,"profile":"other"}',
    b'{"version":1,"output_profile":"unknown"}', b'{"version":1,"output_profile":[]}'])
def test_malformed_preferences_are_never_overwritten(tmp_path, monkeypatch, raw):
    path = tmp_path / "config/desktop.json"
    path.parent.mkdir()
    path.write_bytes(raw)
    c = backend.DesktopController(tmp_path, start_worker=False)
    c.preferences["depth_percent"] = 2.0
    c._save_preferences()
    assert path.read_bytes() == raw
    with pytest.raises(RuntimeError):
        c._preflight()
    assert path.read_bytes() == raw


@pytest.mark.parametrize("field,value", [("birth", "different-birth"), ("exe", "C:/other/sunshine.exe")])
def test_host_record_rejects_reused_pid_or_other_executable(rig, field, value):
    rig.state.live[202] = {**rig.state.host, field: value}
    with pytest.raises(RuntimeError):
        backend.DesktopController._host_identity(rig.c)


def test_host_record_returns_absent_only_for_absent_process(rig):
    rig.state.live.pop(202)
    assert backend.DesktopController._host_identity(rig.c) is None


def test_same_process_requires_exact_birth_and_path(rig):
    original = dict(rig.state.producer)
    assert backend.same_process(original)
    rig.state.live[101] = {**original, "birth": "new lifetime"}
    assert not backend.same_process(original)


def control_setup(rig):
    rig.c._session, rig.c._producer = rig.directory, dict(rig.state.producer)
    rig.monkeypatch.setattr(rig.c, "_inspect", lambda: None)


def acknowledge(rig, request):
    status = json.loads((rig.directory / "status.json").read_text())
    write_json(rig.directory / "status.json", {**status, "applied_request": request["request_id"],
        "seen_request": request["request_id"], "disparity": request["disparity"],
        "revision": status["revision"] + 1, "requested_mode": request["mode"]})


def test_depth_step_uses_fresh_status_not_cached_ui_and_preserves_profile(rig):
    control_setup(rig)
    rig.c.preferences["depth_percent"] = 3.0
    rig.clock.on_sleep = lambda: acknowledge(rig, json.loads((rig.directory / "request.json").read_text()))
    rig.c._control(depth_delta=.05)
    request = json.loads((rig.directory / "request.json").read_text())
    assert request["disparity"] == pytest.approx(23.28)
    assert request["expected_revision"] == 19
    assert request["session_id"] == rig.status["session_id"]
    assert "disparity_profile" not in request or request["disparity_profile"] == "comfort"


def test_depth_step_does_not_apply_old_delta_under_a_newer_revision(rig):
    control_setup(rig)
    original_send = backend.send_control
    observed = []

    def interleaving_send(directory, **kwargs):
        # Quest changes depth after desktop read, before send_control reads status.
        write_json(rig.directory / "status.json", {**rig.status, "revision": 20, "disparity": 38.4})
        result = original_send(directory, **kwargs)
        request = json.loads((rig.directory / "request.json").read_text())
        observed.append(request)
        write_json(rig.directory / "status.json", {**rig.status, "revision": 20, "disparity": 38.4,
            "rejected_request": result["request_id"]})
        return result

    rig.monkeypatch.setattr(backend, "send_control", interleaving_send)
    # A controller may avoid send_control and write the exact snapshot itself.
    def reject_after_write():
        path = rig.directory / "request.json"
        if path.exists():
            request = json.loads(path.read_text())
            if not observed:
                observed.append(request)
            write_json(rig.directory / "status.json", {**rig.status, "revision": 20,
                "disparity": 38.4, "rejected_request": request["request_id"]})

    rig.clock.on_sleep = reject_after_write
    with pytest.raises(RuntimeError):
        rig.c._control(depth_delta=.05)
    if observed:
        assert observed[0]["expected_revision"] == 19, "old D was paired with a newer CAS revision"
    assert json.loads((rig.directory / "status.json").read_text())["disparity"] == 38.4


def test_control_submission_is_not_reported_as_applied(rig):
    control_setup(rig)
    with pytest.raises(RuntimeError):
        rig.c._control(mode="2d")
    assert json.loads((rig.directory / "request.json").read_text())["mode"] == "2d"
    assert "실제 영상에 적용" not in rig.c.get_snapshot()["message"]


@pytest.mark.parametrize("change", [{"updated_monotonic_ns": 1},
    {"updated_monotonic_ns": 101_000_000_000}, {"running": False}])
def test_control_refuses_stale_future_or_ended_status_without_writing(rig, change):
    control_setup(rig)
    write_json(rig.directory / "status.json", {**rig.status, **change})
    with pytest.raises(RuntimeError):
        rig.c._control(depth_delta=.05)
    assert not (rig.directory / "request.json").exists()


def stop_setup(rig, final_change=None):
    control_setup(rig)
    rig.c._host = None
    final_change = final_change or {}

    def stop_request(directory, **kwargs):
        assert kwargs.get("stop") is True
        request = session_control.send_control(directory, **kwargs)
        write_json(directory / "status.json", {**rig.status, "running": False,
            "seen_request": request["request_id"], **final_change})
        rig.state.live.pop(101)
        return request

    rig.monkeypatch.setattr(backend, "send_control", stop_request)


def test_stop_accepts_seen_without_a_new_applied_video_frame(rig):
    stop_setup(rig)
    rig.c._stop()
    status = json.loads((rig.directory / "status.json").read_text())
    assert status["applied_request"] == "old-application"
    assert status["seen_request"] != status["applied_request"]
    assert rig.c.get_snapshot()["phase"] == "idle"


@pytest.mark.parametrize("change", [
    {"error": "cleanup: AI worker did not exit"}, {"ai_error": "stereo cleanup failed"},
    {"seen_request": "unrelated-request"}, {"session_id": "b" * 32}, {"running": True},
])
def test_process_exit_is_not_enough_to_claim_clean_stop(rig, change):
    stop_setup(rig, change)
    with pytest.raises(RuntimeError):
        rig.c._stop()
    assert rig.c.get_snapshot()["phase"] != "idle"


def test_live_producer_stop_timeout_does_not_force_terminate(rig):
    control_setup(rig)
    rig.c._host = None
    with pytest.raises(RuntimeError):
        rig.c._stop()
    assert backend.same_process(rig.state.producer)
    assert rig.c._producer == rig.state.producer


def test_stop_never_targets_a_replacement_producer_lifetime(rig):
    control_setup(rig)
    rig.c._host = None
    rig.state.live[101] = {**rig.state.producer, "birth": "replacement lifetime"}
    rig.monkeypatch.setattr(backend, "send_control", forbidden)
    with pytest.raises(RuntimeError):
        rig.c._stop()
    assert rig.state.live[101]["birth"] == "replacement lifetime"


@pytest.mark.parametrize("live", [False, True])
def test_absent_or_busy_monitor_selection_preserves_preferences(rig, live):
    rig.monkeypatch.setattr(rig.c, "_inspect", lambda: None)
    rig.c._producer = rig.state.producer if live else None
    rig.c.preferences["monitor_device"] = rig.monitor["device_name"]
    before = copy.deepcopy(rig.c.preferences)
    with pytest.raises(RuntimeError):
        rig.c._select_monitor(r"\\.\REMOVED")
    assert rig.c.preferences == before


def test_missing_monitor_is_not_silently_replaced_during_scan(rig, monkeypatch):
    # Actual scan with an injected capture module, without importing/using WGC.
    replacement = SimpleNamespace(index=1, device_name=r"\\.\DISPLAY9", is_primary=True,
                                  bounds=SimpleNamespace(width=1920, height=1080))
    monkeypatch.setitem(sys.modules, "quest3d.capture", SimpleNamespace(list_monitors=lambda: [None, replacement]))
    monkeypatch.setattr(backend.psutil, "net_if_addrs", lambda: {})
    rig.c.preferences["monitor_device"] = r"\\.\REMOVED"
    backend.DesktopController._scan_sources(rig.c)
    assert rig.c.preferences["monitor_device"] == r"\\.\REMOVED"


def test_partial_start_retains_new_owned_child_when_active_marker_is_absent(rig):
    c = rig.c
    (c.root / "artifacts/active-session.json").unlink()
    rig.state.producer = rig.state.host = None
    rig.state.live.clear()
    rig.monkeypatch.setattr(c, "_preflight", lambda: rig.monitor)
    colour = SimpleNamespace(device_name=rig.monitor["device_name"], hdr_enabled=False)
    rig.monkeypatch.setitem(sys.modules, "quest3d.display_color", SimpleNamespace(
        read_display_colors=lambda: [colour], require_hdr_color=forbidden))

    def launch(argv, **kwargs):
        assert "serve" in argv and "--output" in argv
        directory = Path(argv[argv.index("--output") + 1])
        rig.state.directory = directory
        rig.state.producer = {"pid": 303, "exe": str(c.python), "birth": "new-owned-lifetime"}
        rig.state.live[303] = rig.state.producer
        return SimpleNamespace(pid=303)

    rig.monkeypatch.setattr(backend.subprocess, "Popen", launch)
    rig.monkeypatch.setattr(c, "_wait_ready", lambda path: (_ for _ in ()).throw(RuntimeError("injected startup timeout")))
    with pytest.raises(RuntimeError, match="injected startup timeout"):
        c._start()
    expected_directory = rig.state.directory
    c._inspect()
    assert c._session == expected_directory, "inspection lost the failed start's owned session"
    assert c._producer == rig.state.producer, "inspection lost the still-live owned child"


def test_binding_failure_keeps_existing_producer_and_does_not_launch_host(rig):
    rig.state.host = None
    rig.state.live.pop(202)
    rig.monkeypatch.setattr(rig.c, "_preflight", lambda: rig.monitor)
    calls = []

    def fail_bind(argv, label, **kwargs):
        calls.append(label)
        if label == "host-bind":
            raise RuntimeError("injected rebind failure")
        assert label == "host-check", "host started after failed binding"
        return rig.c._last_log_dir / (label + ".log")

    rig.monkeypatch.setattr(rig.c, "_run", fail_bind)
    with pytest.raises(RuntimeError, match="injected rebind failure"):
        rig.c._start()
    assert calls == ["host-check", "host-bind"]
    assert backend.same_process(rig.state.producer)
    assert rig.c._producer == rig.state.producer


def test_environment_never_syncs_away_the_hdr_candidate_wheel(rig):
    env = rig.c._environment()
    assert env["UV_OFFLINE"] == "1"
    assert env.get("UV_NO_SYNC") == "1"
    assert str(rig.c.root / backend.HDR_PACKAGE) in env["PYTHONPATH"]


@pytest.mark.parametrize("fault", [None, "manifest_runtime", "manifest_hash", "binary_hash", "missing_port", "dead_process"])
def test_host_readiness_checks_pinned_identity_and_exact_process_listeners(rig, fault):
    rig.c._host = dict(rig.state.host)
    path = rig.c.root / "artifacts/host/dev/launch.json"
    launch = json.loads(path.read_text())
    if fault == "manifest_runtime":
        launch["runtime"] = str(rig.c.root / "artifacts/host/another-runtime")
    if fault == "manifest_hash":
        launch["host_sha256"] = "0" * 64
    write_json(path, launch)
    if fault == "binary_hash":
        rig.monkeypatch.setattr(backend, "sha256_file", lambda path: "0" * 64)
    if fault == "dead_process":
        rig.state.live.pop(202)

    calls = []

    def process(pid):
        assert pid == 202
        calls.append(pid)
        ports = [47984, 47989] if fault == "missing_port" else [47984, 47989, 48010]
        rows = [SimpleNamespace(status=backend.psutil.CONN_LISTEN, laddr=SimpleNamespace(port=p)) for p in ports]
        return SimpleNamespace(net_connections=lambda **kwargs: rows)

    rig.monkeypatch.setattr(backend.psutil, "Process", process)
    if fault is None:
        backend.DesktopController._verify_host_ready(rig.c, timeout=.5)
        assert calls == [202] and rig.c._verified_host == rig.state.host
    else:
        with pytest.raises(RuntimeError):
            backend.DesktopController._verify_host_ready(rig.c, timeout=.5)
        assert rig.c._verified_host is None


def test_pending_launcher_without_discovered_producer_prevents_second_start(rig):
    (rig.c.root / "artifacts/active-session.json").unlink()
    rig.state.producer = rig.state.host = None
    rig.state.live.clear()
    launcher = {"pid": 404, "exe": str(rig.c.python), "birth": "pending launcher"}
    rig.state.live[404] = launcher
    rig.c._pending = {"directory": str(rig.directory), "launcher_identity": launcher}
    (rig.directory / "status.json").unlink()
    rig.monkeypatch.setattr(rig.c, "_preflight", forbidden)
    with pytest.raises(RuntimeError):
        rig.c._start()
    assert rig.c._pending["launcher_identity"] == launcher


def test_helper_timeout_persists_identity_and_blocks_retry_across_ui_restart(rig):
    helper = {"pid": 404, "exe": str(rig.c.pwsh), "birth": "pending helper"}
    rig.state.live[404] = helper
    launches = []

    def launch(argv, **kwargs):
        launches.append(argv)

        def wait(timeout):
            raise backend.subprocess.TimeoutExpired(argv, timeout)

        return SimpleNamespace(pid=404, wait=wait)

    rig.monkeypatch.setattr(backend.subprocess, "Popen", launch)
    with pytest.raises(RuntimeError):
        backend.DesktopController._run(rig.c, [str(rig.c.pwsh), "fixture-only"], "fixture-helper", timeout=.1)
    assert json.loads(rig.c._helper_path.read_text()) == helper
    replacement_ui = backend.DesktopController(rig.c.root, start_worker=False)
    with pytest.raises(RuntimeError):
        replacement_ui._check_pending_helper()
    with pytest.raises(RuntimeError):
        rig.c._start()
    assert len(launches) == 1
    rig.state.live.pop(404)
    replacement_ui._check_pending_helper()
    assert not replacement_ui._helper_path.exists()


def test_producer_arguments_preserve_current_quality_and_exclude_removed_features(rig):
    rig.c.preferences.update(mode="3d", depth_percent=1.1625, profile="comfort")
    args = rig.c._producer_argv(rig.monitor, rig.directory, hdr=True)
    assert '--reuse-depth-constants' in args
    assert '--fused-depth-resize' in args
    assert '--fused-stereo-output' in args
    assert '--fused-forward-validation' in args
    assert '--fused-colour-fit' in args
    assert '--reuse-immutable-payload' in args
    assert '--trace-cadence' not in args
    assert args[1:3] == ["-m", "quest3d.cli"]
    values = {
        flag: args[args.index(flag) + 1] for flag in ("--disparity", "--eye-width", "--eye-height",
            "--disparity-profile", "--stereo-method", "--depth-refinement", "--colour-precision")}
    assert float(values["--disparity"]) == pytest.approx(22.32)
    assert values["--eye-width"] == "1920" and values["--eye-height"] == "1080"
    assert values["--stereo-method"] == "forward-cuda" and values["--depth-refinement"] == "none"
    assert values["--colour-precision"] == "float" and values["--disparity-profile"] == "comfort"
    assert not set(args) & {"--enable-input", "--file", "--window", "--inline-rect", "--file-native-pcm"}
    assert "--experimental-hdr" in args


def test_old_preferences_keep_quest2_and_user_view_values():
    old = dict(version=1, monitor_device=r"\\.\DISPLAY2", mode="2d", depth_percent=1.4125, profile="linear")
    parsed = backend.validated_preferences(old)
    assert parsed == {**old, "output_profile": "quest2", "depth_model": backend.DEFAULT_DEPTH_MODEL,
                      "ai_quality": "standard", "audio_output": "pc"}
    assert "output_profile" not in old


@pytest.mark.parametrize("profile,width,height", [("quest2", 1920, 1080), ("quest3", 2048, 1152)])
def test_headset_start_preserves_depth_percent_and_valid_full_sbs_geometry(rig, profile, width, height):
    from quest3d.bridge import FrameHeader, FULL_SBS
    rig.c.preferences.update(output_profile=profile, depth_percent=1.4125, mode="2d", profile="linear")
    args = rig.c._producer_argv(rig.monitor, rig.directory, hdr=False)
    value = lambda flag: args[args.index(flag) + 1]
    assert (int(value('--eye-width')), int(value('--eye-height'))) == (width, height)
    assert float(value('--disparity')) / width * 100 == pytest.approx(1.4125)
    assert value('--mode') == '2d' and value('--disparity-profile') == 'linear'
    assert value('--ai-size') == '280' and value('--fps') == '60'
    # Both actual output geometries fit the unchanged v2 reader/writer contract.
    header = FrameHeader(width*2, height, 1, 1, 2, 1, FULL_SBS, 0, 0, 2560, 1440, 1,
                         0, 0, width, height)
    assert FrameHeader.unpack(header.pack()) == header


def test_stopped_headset_switch_persists_and_can_return_to_quest2(rig):
    rig.state.live.clear()
    rig.c.preferences.update(mode="2d", depth_percent=1.4125, profile="linear")
    before = {k: rig.c.preferences[k] for k in ('mode', 'depth_percent', 'profile')}
    for profile, resolution in [('quest3', '2048 × 1152'), ('quest2', '1920 × 1080')]:
        rig.c._select_output_profile(profile)
        assert rig.c.get_snapshot()['output_profile'] == profile
        assert resolution in rig.c.get_snapshot()['eye_text']
        saved = json.loads(rig.c.settings_path.read_text('utf-8'))
        assert saved['output_profile'] == profile
        assert {k: saved[k] for k in before} == before
        reopened = backend.DesktopController(rig.c.root, start_worker=False)
        assert reopened.get_snapshot()['output_profile'] == profile
        assert resolution in reopened.get_snapshot()['eye_text']


@pytest.mark.parametrize("remaining", [(101,), (202,), (101, 202)])
def test_headset_switch_refuses_live_producer_or_host(rig, remaining):
    rig.state.live = {pid: row for pid, row in rig.state.live.items() if pid in remaining}
    rig.c._inspect()
    before = rig.c.settings_path.read_bytes()
    with pytest.raises(RuntimeError, match='송출 중지'):
        rig.c._select_output_profile('quest3')
    assert rig.c.settings_path.read_bytes() == before
    assert rig.c.preferences['output_profile'] == 'quest2'


def test_headset_switch_refuses_unfinished_launch_helper(rig):
    rig.state.live.clear()
    helper = dict(pid=404, exe='injected helper', birth='4444')
    rig.state.live[404] = helper
    rig.c._helper = helper
    with pytest.raises(RuntimeError, match='아직 실행 중'):
        rig.c._select_output_profile('quest3')
    assert rig.c.preferences['output_profile'] == 'quest2'


@pytest.mark.parametrize("value", ['quest4', '', None, [], 3])
def test_invalid_headset_rejected_without_mutating_preferences(rig, value):
    before = dict(rig.c.preferences)
    with pytest.raises(ValueError):
        rig.c._select_output_profile(value)
    assert rig.c.preferences == before


def test_reopened_ui_adopts_actual_quest3_geometry_without_restarting(rig):
    write_json(rig.directory/'status.json', {**rig.status, 'eye_width': 2048, 'eye_height': 1152,
                                           'disparity': 28.928})
    rig.c._start()
    assert rig.c.preferences['output_profile'] == 'quest3'
    assert rig.c.preferences['depth_percent'] == pytest.approx(1.4125)
    assert rig.c.get_snapshot()['eye_text'] == '눈별 2048 × 1152 · 16:9'
    assert not list(rig.c.logs.glob('run-*'))

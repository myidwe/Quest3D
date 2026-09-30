"""A fresh picture from session B must not drive session A's control mailbox."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from quest3d.session_control import atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("source_launch", ROOT / "native/host/validate-source-session.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)


@pytest.fixture
def session(tmp_path):
    config, probe = tmp_path / "sunshine.conf", tmp_path / "frames.jsonl"
    config.write_text(f"quest3d_control_dir = {tmp_path.as_posix()}\n", encoding="utf-8")
    status = {"session_id": "a" * 32, "running": True, "input_enabled": False,
              "stream_epoch": (1 << 63) + 57, "eye_width": 1280, "eye_height": 720,
              "updated_monotonic_ns": time.perf_counter_ns()}
    frames = [{"frame_id": index, "stream_epoch": status["stream_epoch"], "width": 2560,
               "height": 720, "flags": 7, "source_age_ms": 45} for index in (8, 9)]
    def save():
        atomic_json(tmp_path / "status.json", status)
        probe.write_text("\n".join(json.dumps(frame) for frame in frames), encoding="utf-8")
    save()
    return tmp_path, config, probe, status, frames, save


def test_same_source_matches_actual_frames_and_preserves_uint64(session):
    directory, config, probe, status, _, _ = session
    before = (directory / "status.json").read_bytes()
    staged = launch.validate(directory, config)
    actual = launch.validate(directory, config, probe_path=probe, expected=staged)
    assert actual["actual_frames_checked"] == 2
    assert actual["stream_epoch"] == str(status["stream_epoch"])
    assert (directory / "status.json").read_bytes() == before


def test_new_video_with_old_control_mailbox_is_rejected(session):
    directory, config, probe, _, frames, save = session
    for frame in frames: frame["stream_epoch"] += 1
    save()
    with pytest.raises(ValueError, match="Actual video bridge and control source differ"):
        launch.validate(directory, config, probe_path=probe)


@pytest.mark.parametrize("key,value", [("stream_epoch", 3), ("session_id", "b" * 32), ("eye_width", 640)])
def test_reused_directory_cannot_change_staged_identity(session, key, value):
    directory, config, _, status, _, save = session
    staged = launch.validate(directory, config)
    status[key] = value; save()
    with pytest.raises(ValueError, match="Source changed after staging"):
        launch.validate(directory, config, expected=staged)


@pytest.mark.parametrize("key,value", [("running", False), ("running", 1), ("input_enabled", True),
    ("input_enabled", 0), ("updated_monotonic_ns", 0), ("updated_monotonic_ns", (1 << 63) - 1),
    ("stream_epoch", True), ("stream_epoch", 0), ("session_id", "wrong"), ("eye_height", 0)])
def test_dead_stale_future_or_invalid_source_refuses(session, key, value):
    directory, config, probe, status, _, save = session
    status[key] = value; save()
    with pytest.raises(ValueError): launch.validate(directory, config, probe_path=probe)


@pytest.mark.parametrize("key,value", [("flags", 15), ("flags", 2), ("flags", True),
    ("width", 1280), ("height", 1080), ("stream_epoch", True), ("frame_id", 8),
    ("source_age_ms", float("nan")), ("source_age_ms", -1)])
def test_changed_geometry_enabled_input_and_bad_publications_refuse(session, key, value):
    directory, config, probe, _, frames, save = session
    frames[-1][key] = value; save()
    with pytest.raises(ValueError): launch.validate(directory, config, probe_path=probe)


def test_paused_pixels_are_valid_when_publications_advance(session):
    directory, config, probe, status, frames, save = session
    status.pop("input_enabled")  # Legacy desktop needs real input-OFF flags.
    for frame in frames: frame["source_age_ms"] = 600000
    save()
    assert launch.validate(directory, config, probe_path=probe)["actual_frames_checked"] == 2


def test_old_probe_or_single_snapshot_is_not_live_evidence(session):
    directory, config, probe, _, frames, save = session
    os.utime(probe, (time.time() - 10, time.time() - 10))
    with pytest.raises(ValueError, match="stale"): launch.validate(directory, config, probe_path=probe)
    frames.pop(); save()
    with pytest.raises(ValueError, match="bounded actual"): launch.validate(directory, config, probe_path=probe)


@pytest.mark.parametrize('age_ns,reason', [(-1_000_000, 'future'), (2_001_000_000, 'stale')])
def test_probe_diagnostic_reports_actual_age_without_relaxing_limit(session, monkeypatch, age_ns, reason):
    directory, config, probe, _, _, _ = session
    monkeypatch.setattr(launch.time, 'time_ns', lambda: probe.stat().st_mtime_ns + age_ns)
    with pytest.raises(ValueError, match=reason) as error:
        launch.validate(directory, config, probe_path=probe)
    assert f'age_ms={age_ns / 1e6:.3f}' in str(error.value)


def test_probe_size_diagnostic_is_json_size_not_video_payload(session):
    directory, config, probe, _, _, _ = session
    probe.write_bytes(b' ' * 65537)
    with pytest.raises(ValueError, match=r'oversized \(65537 > 65536 bytes\)'):
        launch.validate(directory, config, probe_path=probe)


@pytest.mark.parametrize('eye_width,eye_height', [(1920, 1080), (2048, 1152), (2048, 2160)])
def test_existing_full_sbs_capacity_includes_quest3(session, monkeypatch, eye_width, eye_height):
    directory, config, probe, status, frames, save = session
    status.update(eye_width=eye_width, eye_height=eye_height)
    for frame in frames:
        frame.update(width=eye_width*2, height=eye_height, payload_bytes=eye_width*2*eye_height*4)
    save()
    # The original inclusive two-second limit remains in effect.
    monkeypatch.setattr(launch.time, 'time_ns', lambda: probe.stat().st_mtime_ns + 2_000_000_000)
    actual = launch.validate(directory, config, probe_path=probe)
    assert actual['width'] == eye_width*2 and actual['height'] == eye_height
    assert actual['actual_frames_checked'] == 2


def test_duplicate_or_other_config_control_directory_refuses(session, tmp_path):
    directory, config, _, _, _, _ = session
    original = config.read_text()
    config.write_text(original + original)
    with pytest.raises(ValueError, match="control directory differ"): launch.validate(directory, config)
    other = tmp_path / "other"; other.mkdir()
    config.write_text(f"quest3d_control_dir = {other.as_posix()}\n")
    with pytest.raises(ValueError, match="control directory differ"): launch.validate(directory, config)


@pytest.mark.skipif(os.name != "nt", reason="Windows product launcher")
@pytest.mark.parametrize("protocol,enable_input", [(2, False), (3, False), (3, True)])
def test_real_powershell_helper_validates_staged_source_and_probe(session, protocol, enable_input):
    directory, config, probe, status, _, save = session
    if enable_input:
        enable_monitor_input(session)
    else:
        status["bridge_protocol"] = protocol
        config.write_text(config.read_text() + f"quest3d_protocol = {protocol}\n")
        for frame in session[4]: frame["bridge_protocol"] = protocol
        save()
    staged = launch.validate(directory, config, enable_input=enable_input)
    manifest = directory / "manifest.json"
    atomic_json(manifest, {"source_session": staged})
    def quote(value): return "'" + str(value).replace("'", "''") + "'"
    script = directory / "check.ps1"
    script.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'",
        ". " + quote(ROOT / "native/host/source-session-launch.ps1"),
        f"$Selection = Get-Quest3DBridgeProtocol ([pscustomobject]@{{bridge_protocol={protocol}}}) (Get-Content -LiteralPath {quote(config)} -Raw)",
        f"if ($Selection -ne {protocol}) {{ throw 'Protocol selection changed' }}",
        "$Result = Get-Quest3DSourceSession " + " ".join(quote(v) for v in (ROOT, directory, config, probe, manifest))
            + (" -EnableInput $true" if enable_input else ""),
        "if ($Result.input_opt_in -ne " + ("$true" if enable_input else "$false") + ") { throw 'Input opt-in changed' }",
        "if ($Result.stream_epoch -isnot [string] -or $Result.stream_epoch -cne '9223372036854775865') { throw 'Epoch precision was lost' }",
        "if ($Result.actual_frames_checked -ne 2) { throw 'Actual video not checked' }",
        "Write-Output 'PASS: source session and real probe contract'"
    ]), encoding="utf-8")
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(.05):
            status["updated_monotonic_ns"] = time.perf_counter_ns()
            atomic_json(directory / "status.json", status)
    worker = threading.Thread(target=heartbeat); worker.start()
    try:
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script)],
                                capture_output=True, text=True, encoding="utf-8", timeout=15)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS:" in result.stdout
    finally:
        stop.set(); worker.join()


def test_explicit_v3_requires_source_and_actual_probe_agreement(session):
    directory, config, probe, status, frames, save = session
    config.write_text(config.read_text() + "quest3d_protocol = 3\n")
    with pytest.raises(ValueError, match="source bridge protocol differ"):
        launch.validate(directory, config)
    status["bridge_protocol"] = 3; save()
    with pytest.raises(ValueError, match="probe protocol differs"):
        launch.validate(directory, config, probe_path=probe)
    for frame in frames: frame["bridge_protocol"] = 3
    save()
    staged = launch.validate(directory, config)
    assert staged["bridge_protocol"] == 3
    assert launch.validate(directory, config, probe_path=probe, expected=staged)["actual_frames_checked"] == 2
    staged["bridge_protocol"] = 2
    with pytest.raises(ValueError, match="Source changed after staging"):
        launch.validate(directory, config, probe_path=probe, expected=staged)


@pytest.mark.parametrize("value", ["1", "4", "true", "3.0", "3 # comment", "2\nquest3d_protocol = 3"])
def test_invalid_or_duplicate_config_protocol_refuses(session, value):
    directory, config, _, _, _, _ = session
    config.write_text(config.read_text() + f"quest3d_protocol = {value}\n")
    with pytest.raises(ValueError, match="configured bridge protocol"):
        launch.validate(directory, config)


@pytest.mark.parametrize("value", [True, "2", 2.0, 1, 4])
def test_source_protocol_must_be_explicit_integer(session, value):
    directory, config, _, status, _, save = session
    status["bridge_protocol"] = value; save()
    with pytest.raises(ValueError, match="source bridge protocol"):
        launch.validate(directory, config)


def enable_monitor_input(session):
    directory, config, probe, status, frames, save = session
    config.write_text(config.read_text() + "quest3d_protocol = 3\nquest3d_input = enabled\n")
    status.update(bridge_protocol=3, input_opt_in=True, input_enabled=True,
                  requested_mode="2d", effective_mode="2d", error=None)
    for frame in frames:
        frame.update(bridge_protocol=3, flags=15, source_kind="monitor")
    save()
    return directory, config, probe, status, frames, save


def test_input_requires_separate_explicit_launch_opt_in_and_actual_frames(session):
    directory, config, probe, _, _, _ = enable_monitor_input(session)
    with pytest.raises(ValueError, match="explicit launch/config"):
        launch.validate(directory, config, probe_path=probe)
    staged = launch.validate(directory, config, enable_input=True)
    assert staged["input_opt_in"] is True
    assert launch.validate(directory, config, probe_path=probe, expected=staged,
                           enable_input=True)["actual_frames_checked"] == 2
    staged["input_opt_in"] = False
    with pytest.raises(ValueError, match="Source changed after staging"):
        launch.validate(directory, config, probe_path=probe, expected=staged, enable_input=True)


@pytest.mark.parametrize("key,value", [("input_opt_in", False), ("input_opt_in", 1),
    ("input_enabled", False), ("requested_mode", "3d"), ("effective_mode", "3d"),
    ("error", "capture failed")])
def test_enabled_launch_rejects_wrong_producer_permission_or_mode(session, key, value):
    directory, config, probe, status, _, save = enable_monitor_input(session)
    status[key] = value
    save()
    with pytest.raises(ValueError):
        launch.validate(directory, config, probe_path=probe, enable_input=True)


@pytest.mark.parametrize("key,value", [("flags", 7), ("flags", 13), ("source_kind", "window"),
    ("source_kind", "video"), ("source_age_ms", 500.001), ("bridge_protocol", 2)])
def test_enabled_launch_rejects_stale_or_noninteractive_actual_frame(session, key, value):
    directory, config, probe, _, frames, save = enable_monitor_input(session)
    frames[-1][key] = value
    save()
    with pytest.raises(ValueError):
        launch.validate(directory, config, probe_path=probe, enable_input=True)


@pytest.mark.parametrize("value", ["true", "enabled\nquest3d_input = disabled", "1", "ENABLED"])
def test_ambiguous_or_invalid_input_configuration_is_rejected(session, value):
    directory, config, _, _, _, _ = session
    config.write_text(config.read_text() + f"quest3d_input = {value}\n")
    with pytest.raises(ValueError, match="configured input gate"):
        launch.validate(directory, config)


def test_input_opt_in_cannot_silently_upgrade_v2_or_disabled_configuration(session):
    directory, config, probe, _, _, _ = session
    with pytest.raises(ValueError, match="explicit launch/config"):
        launch.validate(directory, config, probe_path=probe, enable_input=True)
    config.write_text(config.read_text() + "quest3d_input = enabled\n")
    with pytest.raises(ValueError, match="bridge v3"):
        launch.validate(directory, config, probe_path=probe, enable_input=True)

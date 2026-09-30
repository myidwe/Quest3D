"""Exercise the actual PowerShell transaction with isolated files and source validation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import pytest

from quest3d.session_control import atomic_json

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows runtime helper")


def quote(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def fixture(tmp_path):
    host = tmp_path / "artifacts/host"
    runtime, dev = host / "runtime-test", host / "dev"
    runtime.mkdir(parents=True)
    dev.mkdir()
    source = tmp_path / "new source"
    source.mkdir()
    exe = runtime / "sunshine.exe"
    exe.write_bytes(b"An isolated never-executed runtime fixture")
    digest = hashlib.sha256(exe.read_bytes()).hexdigest()
    config = runtime / "sunshine.conf"
    config.write_bytes(("\ufeff# Preserve comments, BOM and CRLF\r\n"
        "capture = quest3d\r\nencoder = nvenc\r\nport = 47989\r\n"
        "keyboard = disabled\r\nmouse = disabled\r\ncontroller = disabled\r\n"
        "upnp = disabled\r\ninstall_steam_audio_drivers = disabled\r\nstream_audio = disabled\r\n"
        "custom_unknown = retain this exact value\r\n"
        f"quest3d_control_dir = {tmp_path.as_posix()}\r\n# Tail retained\r\n").encode("utf-8"))
    manifest = {"runtime": str(runtime), "config": str(config), "host_sha256": digest,
        "control_directory": str(tmp_path), "port": 47989, "input_enabled": False,
        "audio_enabled": False, "file_pcm": None, "audio_watchdog_protocol": 1,
        "server_started": False, "credentials_file": "unchanged/path", "unknown": {"retain": [1, "二", False]}}
    paths = (config, runtime / "manifest.json", dev / "launch.json")
    for path in paths[1:]:
        path.write_text(json.dumps(manifest, ensure_ascii=False, indent=3), encoding="utf-8-sig")
    status = {"session_id": "a" * 32, "running": True, "input_enabled": False,
        "stream_epoch": (1 << 63) + 57, "eye_width": 1280, "eye_height": 720,
        "updated_monotonic_ns": time.perf_counter_ns()}
    frames = [{"frame_id": i, "stream_epoch": status["stream_epoch"], "width": 2560,
               "height": 720, "flags": 7, "source_age_ms": 40} for i in (7, 8)]
    atomic_json(source / "status.json", status)
    probe = tmp_path / "fixture-probe.jsonl"
    probe.write_text("\n".join(json.dumps(f) for f in frames), encoding="utf-8")
    return {"root": tmp_path, "host": host, "runtime": runtime, "source": source,
            "digest": digest, "paths": paths, "manifest": manifest, "status": status, "frames": frames, "probe": probe}


def run(fixture, *, check=False, setup="", live=False, heartbeat=True, audio='pc'):
    f = fixture
    script = f["root"] / "run.ps1"
    # Source validation is the production helper through the real locked uv env;
    # only process/port enumeration and native probe acquisition use fixtures.
    script.write_text("\n".join([
        "$ErrorActionPreference = 'Stop'",
        ". " + quote(ROOT / "native/host/dev-source-rebind.ps1"),
        "$OriginalSource = ${function:Get-Quest3DSourceSession}",
        "function Get-Quest3DSourceSession($TaskRoot, $ControlDirectory, $Config, $Probe, $ExpectedManifest = '', $EnableInput = $false) { & $OriginalSource "
            + quote(ROOT) + " $ControlDirectory $Config $Probe $ExpectedManifest $EnableInput }",
        "function Get-CimInstance { " + ("[pscustomobject]@{ExecutablePath=" + quote(f["runtime"] / "sunshine.exe") + "}" if live else "") + " }",
        "function Get-Quest3DPortConflict { }",
        "function Write-Quest3DRebindProbe($TaskRoot,$Protocol,$ProbePath) { [IO.File]::Copy(" + quote(f["probe"]) + ", $ProbePath); [IO.File]::SetLastWriteTimeUtc($ProbePath, [datetime]::UtcNow) }",
        setup,
        "Invoke-Quest3DSourceRebind " + " ".join(quote(f[k]) for k in ("root", "source", "runtime", "digest"))
             + (" -CheckOnly" if check else "") + " -AudioOutput " + quote(audio) + " | ConvertTo-Json -Depth 12",
    ]), encoding="utf-8")
    stop = threading.Event()
    def refresh():
        while not stop.wait(.04):
            f["status"]["updated_monotonic_ns"] = time.perf_counter_ns()
            atomic_json(f["source"] / "status.json", f["status"])
    worker = threading.Thread(target=refresh)
    if heartbeat:
        worker.start()
    try:
        return subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, encoding="utf-8", timeout=20)
    finally:
        stop.set()
        if heartbeat:
            worker.join()


def before(f):
    return [p.read_bytes() for p in f["paths"]]


def assert_unchanged(f, old):
    assert before(f) == old


def test_check_only_uses_real_source_validator_and_writes_no_runtime_or_journal(fixture):
    old = before(fixture)
    result = run(fixture, check=True)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    assert value["mode"] == "check_only" and value["source_session"]["actual_frames_checked"] == 2
    assert value["source_session"]["stream_epoch"] == "9223372036854775865"
    assert_unchanged(fixture, old)
    assert not list(fixture["host"].glob("rebind-*"))


def test_initial_validator_startup_does_not_age_fresh_probe(fixture):
    """First environment startup may be slow; keep the real 2s probe limit."""
    old = before(fixture)
    setup = """
$script:ValidatorCalls = 0
function Get-Quest3DSourceSession($TaskRoot, $ControlDirectory, $Config, $Probe = '', $ExpectedManifest = '', $EnableInput = $false) {
    $script:ValidatorCalls++
    $Clock = [Diagnostics.Stopwatch]::StartNew()
    $Record = [ordered]@{call=$script:ValidatorCalls; probe_exists=([bool]$Probe -and [IO.File]::Exists($Probe)); started_utc=[datetime]::UtcNow.ToString('o')}
    try {
        if ($script:ValidatorCalls -eq 1) { Start-Sleep -Milliseconds 2300 }
        & $OriginalSource REALROOT $ControlDirectory $Config $Probe $ExpectedManifest $EnableInput
    } finally {
        $Record.elapsed_ms = $Clock.Elapsed.TotalMilliseconds
        [IO.File]::AppendAllText(TRACEPATH, ($Record | ConvertTo-Json -Compress) + [Environment]::NewLine)
    }
}
""".replace('REALROOT', quote(ROOT)).replace('TRACEPATH', quote(fixture['root'] / 'validator-timing.jsonl'))
    result = run(fixture, check=True, setup=setup)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(result.stdout)['source_session']['actual_frames_checked'] == 2
    assert_unchanged(fixture, old)
    timing = [json.loads(line) for line in (fixture['root'] / 'validator-timing.jsonl').read_text().splitlines()]
    assert timing[0]['probe_exists'] is False and timing[0]['elapsed_ms'] >= 2300
    assert timing[1]['probe_exists'] is True


def test_apply_preserves_config_bytes_other_fields_and_has_complete_backup(fixture):
    old = before(fixture)
    result = run(fixture)
    assert result.returncode == 0, result.stdout + result.stderr
    value = json.loads(result.stdout)
    journal = Path(value["journal"])
    assert fixture["paths"][0].read_bytes() == old[0].replace(fixture["root"].as_posix().encode(), fixture["source"].as_posix().encode())
    for path in fixture["paths"][1:]:
        new = json.loads(path.read_text(encoding="utf-8-sig"))
        for key, item in fixture["manifest"].items():
            if key != "control_directory":
                assert new[key] == item
        assert new["control_directory"] == str(fixture["source"])
        assert new["bridge_protocol"] == 2 and new["source_session"]["actual_frames_checked"] == 2
    for name, data in zip(("sunshine.conf", "runtime-manifest.json", "dev-launch.json"), old):
        assert (journal / name).read_bytes() == data
    assert json.loads((journal / "journal.json").read_text(encoding="utf-8-sig"))["state"] == "committed"


@pytest.mark.parametrize("fault", ["hash", "foreign_epoch", "stale", "input", "audio", "protocol", "live", "port", "path", "duplicate_control"])
def test_rejects_unsafe_or_unmatched_binding_without_runtime_writes(fixture, fault):
    f = fixture
    setup, heartbeat = "", True
    if fault == "hash":
        f["digest"] = "0" * 64
    elif fault == "foreign_epoch":
        f["frames"][-1]["stream_epoch"] += 1
        f["probe"].write_text("\n".join(json.dumps(v) for v in f["frames"]))
    elif fault == "stale":
        f["status"]["updated_monotonic_ns"] = 0
        atomic_json(f["source"] / "status.json", f["status"])
        heartbeat = False
    elif fault == "input":
        f["status"]["input_enabled"] = True
    elif fault in ("audio", "protocol"):
        for p in f["paths"][1:]:
            m = json.loads(p.read_text(encoding="utf-8-sig"))
            m["audio_enabled" if fault == "audio" else "bridge_protocol"] = True if fault == "audio" else 3
            p.write_text(json.dumps(m))
    elif fault == "port":
        setup = "function Get-Quest3DPortConflict { [pscustomobject]@{protocol='UDP';port=47998;process_id=42} }"
    elif fault == "path":
        for p in f["paths"][1:]:
            m = json.loads(p.read_text(encoding="utf-8-sig")); m["config"] = str(f["root"] / "elsewhere.conf")
            p.write_text(json.dumps(m))
    elif fault == "duplicate_control":
        with f["paths"][0].open("ab") as stream: stream.write(b"quest3d_control_dir = other\r\n")
    old = before(f)
    result = run(f, setup=setup, live=fault == "live", heartbeat=heartbeat)
    assert result.returncode != 0, result.stdout
    assert_unchanged(f, old)


def test_partial_second_write_rolls_back_every_byte(fixture):
    old = before(fixture)
    setup = """
$OriginalWriter = ${function:Write-Quest3DRebindLockedFile}
$script:WriteCount = 0
function Write-Quest3DRebindLockedFile($Stream, $Bytes) {
    $script:WriteCount++
    if ($script:WriteCount -eq 2) { $Stream.Position=0; $Stream.WriteByte(88); throw 'Injected second-file partial write' }
    & $OriginalWriter $Stream $Bytes
}
"""
    result = run(fixture, setup=setup)
    assert result.returncode != 0 and "rolled_back" in result.stderr
    assert_unchanged(fixture, old)
    journal = next(fixture["host"].glob("rebind-*/journal.json"))
    assert json.loads(journal.read_text(encoding="utf-8-sig"))["state"] == "rolled_back"


def test_file_changed_during_preflight_is_not_overwritten(fixture):
    old = before(fixture)
    setup = """
$OriginalProbe = ${function:Write-Quest3DRebindProbe}
function Write-Quest3DRebindProbe($TaskRoot,$Protocol,$ProbePath) {
    & $OriginalProbe $TaskRoot $Protocol $ProbePath
    [IO.File]::AppendAllText(PATH, '# concurrent edit')
}
""".replace("PATH", quote(fixture["paths"][0]))
    result = run(fixture, setup=setup)
    assert result.returncode != 0 and "changed after preflight" in result.stderr
    assert fixture["paths"][0].read_bytes() == old[0] + b"# concurrent edit"
    assert [p.read_bytes() for p in fixture["paths"][1:]] == old[1:]


def test_port_owner_appearing_before_commit_is_preserved(fixture):
    old = before(fixture)
    result = run(fixture, setup="""
$script:PortReads = 0
function Get-Quest3DPortConflict {
    $script:PortReads++
    if ($script:PortReads -ge 2) { [pscustomobject]@{protocol='TCP';port=47989;process_id=777} }
}
""")
    assert result.returncode != 0 and "ports are in use" in result.stderr
    assert_unchanged(fixture, old)


def test_actual_private_native_frames_validate_rebind_check(fixture):
    """Real Win32 mapping -> native reader -> PS -> uv validator, no default map."""
    import uuid
    import numpy as np
    from quest3d.bridge import FramePublisher

    native = ROOT / "artifacts/host/frame_bridge_probe.exe"
    if not native.is_file():
        pytest.skip("Build the pinned native bridge probe first")
    prefix = "Local\\Quest3D.RebindTest." + uuid.uuid4().hex
    stop, ready = threading.Event(), threading.Event()
    errors = []
    def publish():
        try:
            with FramePublisher(prefix) as publisher:
                fixture["status"].update(stream_epoch=publisher.epoch, eye_width=64, eye_height=32)
                pixels = np.full((32, 128, 4), 127, np.uint8)
                pixels[:, :, 3] = 255
                index = 1
                while not stop.is_set():
                    publisher.publish(pixels, frame_id=index, capture_ns=time.perf_counter_ns(),
                        generation=1, flags=7, source_rect=(0, 0, 64, 32))
                    fixture["status"]["updated_monotonic_ns"] = time.perf_counter_ns()
                    atomic_json(fixture["source"] / "status.json", fixture["status"])
                    ready.set()
                    index += 1
                    stop.wait(.02)
        except BaseException as exc:
            errors.append(exc)
            ready.set()
    worker = threading.Thread(target=publish)
    worker.start()
    try:
        assert ready.wait(3) and not errors
        old = before(fixture)
        setup = """
function Write-Quest3DRebindProbe($TaskRoot,$Protocol,$ProbePath) {
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = NATIVE
    $Start.UseShellExecute=$false; $Start.CreateNoWindow=$true
    $Start.RedirectStandardOutput=$true; $Start.RedirectStandardError=$true
    foreach($Argument in @('1','--protocol',[string]$Protocol,'--prefix',PREFIX)) { $Start.ArgumentList.Add($Argument) }
    $Process=[Diagnostics.Process]::Start($Start)
    $Output=$Process.StandardOutput.ReadToEndAsync(); $ErrorText=$Process.StandardError.ReadToEndAsync()
    if (!$Process.WaitForExit(5000)) { throw 'Private native probe exceeded timeout' }
    if ($Process.ExitCode -ne 0) { throw $ErrorText.GetAwaiter().GetResult() }
    [IO.File]::WriteAllText($ProbePath,$Output.GetAwaiter().GetResult(),[Text.UTF8Encoding]::new($false))
    [IO.File]::Copy($ProbePath,OBSERVED)
}
""".replace("NATIVE", quote(native)).replace("PREFIX", quote(prefix)).replace("OBSERVED", quote(fixture["root"] / "observed-probe.jsonl"))
        result = run(fixture, check=True, setup=setup, heartbeat=False)
        assert result.returncode == 0, result.stdout + result.stderr
        actual = json.loads(result.stdout)["source_session"]
        assert actual["actual_frames_checked"] >= 2
        assert (actual["width"], actual["height"], actual["bridge_protocol"]) == (128, 32, 2)
        assert actual["stream_epoch"] == str(fixture["status"]["stream_epoch"])
        assert_unchanged(fixture, old)
        assert not list(fixture["host"].glob("rebind-*"))
        atomic_json(fixture["root"] / "private-native-verification.json", {
            "source_session": actual, "prefix": prefix,
            "probe_sha256": hashlib.sha256(native.read_bytes()).hexdigest(),
            "observed_frames_sha256": hashlib.sha256((fixture["root"] / "observed-probe.jsonl").read_bytes()).hexdigest(),
            "check_only": True, "runtime_changed": False, "production_runtime_used": False,
        })
    finally:
        stop.set()
        worker.join(3)
        assert not worker.is_alive() and not errors


@pytest.mark.parametrize('selected', ['quest', 'both'])
def test_audio_rebind_changes_only_owned_audio_and_preserves_video(fixture, selected):
    old = before(fixture)
    endpoint = '{0.0.0.00000000}.{22222222-2222-2222-2222-222222222222}'
    setup = """
function Get-Quest3DSystemAudioPlan($TaskRoot, $Mode) {
    [pscustomobject]@{audio_output=$Mode; audio_enabled=($Mode -ne 'pc'); audio_endpoint=$(if ($Mode -eq 'quest') { 'ENDPOINT' } else { $null })}
}
""".replace('ENDPOINT', endpoint)
    checked = run(fixture, audio=selected, setup=setup, check=True)
    assert checked.returncode == 0, checked.stderr
    assert_unchanged(fixture, old)
    result = run(fixture, audio=selected, setup=setup)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data['audio_output'] == selected and data['audio_enabled'] is True
    text = fixture['paths'][0].read_text(encoding='utf-8-sig')
    assert 'stream_audio = enabled' in text
    assert ('audio_sink = ' + endpoint in text) is (selected == 'quest')
    for path in fixture['paths'][1:]:
        staged = json.loads(path.read_text(encoding='utf-8-sig'))
        assert staged['audio_output'] == selected and staged['input_enabled'] is False
        assert staged['file_pcm'] is None
    returned = run(fixture)
    assert returned.returncode == 0, returned.stderr
    text = fixture['paths'][0].read_text(encoding='utf-8-sig')
    assert 'stream_audio = disabled' in text and 'audio_sink' not in text and 'virtual_sink' not in text


def test_audio_endpoint_preflight_failure_preserves_every_runtime_byte(fixture):
    old = before(fixture)
    result = run(fixture, audio='quest', setup="function Get-Quest3DSystemAudioPlan { throw 'Device disconnected' }")
    assert result.returncode != 0 and 'Device disconnected' in result.stderr
    assert_unchanged(fixture, old)

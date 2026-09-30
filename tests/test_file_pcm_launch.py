"""Real private mapping launch checks. No host, sound device or network starts."""
import ctypes
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

from quest3d.audio_bridge import AudioPublisher, HEADER_BYTES, U32, U64, inspect_channel
from quest3d.session_control import atomic_json

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("pcm_launch", ROOT / "native/host/validate-file-pcm.py")
launch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(launch)
pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows private mapping")


@pytest.fixture
def session(tmp_path):
    with AudioPublisher("a" * 32) as publisher:
        state = publisher.snapshot()
        manifest = {"version": 1, "session_id": "b" * 32, "file_session_id": publisher.session_id,
                    **{key: state[key] for key in ("channel", "producer_pid", "producer_creation_filetime")}}
        status = {"session_id": manifest["session_id"], "running": True, "input_enabled": False,
                  "file_clock": "common-av-candidate", "audio_requested": True, "audio_integrated": False,
                  "media": {"session_id": publisher.session_id}, "audio": {**state, "error": None}, "stream_epoch": 321,
                  "updated_monotonic_ns": time.perf_counter_ns()}
        atomic_json(tmp_path / "audio-channel.json", manifest)
        atomic_json(tmp_path / "status.json", status)
        yield publisher, tmp_path, manifest, status


def test_live_exact_header_owner_and_observer_are_read_only(session):
    publisher, directory, manifest, _ = session
    before = ctypes.string_at(publisher.pointer, HEADER_BYTES)
    result = launch.validate(directory)
    assert all(result[key] == value for key, value in manifest.items())
    assert result["producer_pid"] == os.getpid()
    assert ctypes.string_at(publisher.pointer, HEADER_BYTES) == before
    assert not publisher.snapshot()["consumer_ready"]


@pytest.mark.parametrize("field,value", [
    ("version", True), ("version", 2), ("session_id", "0" * 32), ("channel", "a" * 31),
    ("producer_pid", True), ("producer_creation_filetime", 0), ("extra", "ignored")])
def test_invalid_manifest_fields_refuse(session, field, value):
    _, directory, manifest, _ = session
    manifest[field] = value
    atomic_json(directory / "audio-channel.json", manifest)
    with pytest.raises(ValueError): launch.validate(directory)


@pytest.mark.parametrize("field,value", [
    ("running", False), ("input_enabled", True), ("file_clock", "desktop"),
    ("audio_requested", False), ("audio_integrated", 1), ("session_id", "c" * 32),
    ("updated_monotonic_ns", 0), ("updated_monotonic_ns", (1 << 63) - 1),
    ("stream_epoch", 0),
    ("media", {"session_id": "c" * 32})])
def test_stale_or_wrong_session_status_refuses(session, field, value):
    _, directory, _, status = session
    status[field] = value
    atomic_json(directory / "status.json", status)
    with pytest.raises(ValueError): launch.validate(directory)


def test_manifest_must_match_actual_mapping_and_birth(session):
    publisher, directory, manifest, status = session
    # JSON agrees with itself, but it cannot redirect the actual mapping owner.
    manifest["producer_creation_filetime"] += 1
    status["audio"]["producer_creation_filetime"] += 1
    atomic_json(directory / "audio-channel.json", manifest)
    atomic_json(directory / "status.json", status)
    with pytest.raises(ValueError, match="Actual PCM mapping"): launch.validate(directory)
    with publisher._lock(): publisher._put(48, manifest["producer_creation_filetime"])
    with pytest.raises(RuntimeError, match="birth time"): inspect_channel(publisher.channel)


@pytest.mark.parametrize("offset,value,word", [(36, 1, U32), (36, 2, U32), (36, 4, U32), (148, 7, U32), (8, 2, U32)])
def test_invalid_closed_or_faulted_actual_header_refuses(session, offset, value, word):
    publisher, directory, _, _ = session
    with publisher._lock(): publisher._put(offset, value, word)
    with pytest.raises((ValueError, RuntimeError)): launch.validate(directory)


def test_missing_oversized_and_consumer_claimed_refuse(session):
    publisher, directory, manifest, _ = session
    path = directory / "audio-channel.json"
    path.unlink()
    with pytest.raises(OSError): launch.validate(directory)
    path.write_bytes(b" " * 4097)
    with pytest.raises(ValueError, match="exceeds"): launch.validate(directory)
    atomic_json(path, manifest)
    (directory / "status.json").write_bytes(b" " * 65537)
    with pytest.raises(ValueError, match="exceeds"): launch.validate(directory)
    with publisher._lock(): publisher._put(44, os.getpid(), U32)
    assert inspect_channel(publisher.channel)["consumer_pid"] == os.getpid()


def test_existing_consumer_cannot_be_replaced(session):
    publisher, directory, _, _ = session
    with publisher._lock(): publisher._put(44, os.getpid(), U32)
    with pytest.raises(RuntimeError, match="already has a consumer"): launch.validate(directory)


def test_observer_does_not_erase_native_abandoned_mutex_detection(session):
    publisher, _, _, _ = session
    def abandon():
        assert publisher.win.k.WaitForSingleObject(publisher.mutex, 50) == 0
    worker = threading.Thread(target=abandon)
    worker.start(); worker.join()
    before = ctypes.string_at(publisher.pointer, HEADER_BYTES)
    inspect_channel(publisher.channel)
    assert ctypes.string_at(publisher.pointer, HEADER_BYTES) == before
    # The first actual consumer still observes the abandoned transaction.
    result = publisher.win.k.WaitForSingleObject(publisher.mutex, 50)
    assert result == 0x80
    publisher.win.k.ReleaseMutex(publisher.mutex)


def test_actual_producer_death_with_mapping_retained(session):
    publisher, _, _, _ = session
    child = subprocess.Popen([sys.executable, "-c", "import sys; sys.stdin.read(1)"], stdin=subprocess.PIPE,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    process = publisher.win.k.OpenProcess(0x100000 | 0x1000, False, child.pid)
    # The inert child waits for our pipe EOF before exiting. We retain its
    # actual lifetime handle instead of looking up a PID after termination.
    if process:
        created = publisher.win.creation(process)
        child.stdin.close()
        child.wait(timeout=5)
        with publisher._lock():
            publisher._put(40, child.pid, U32); publisher._put(48, created)
        try:
            with pytest.raises(RuntimeError, match="absent or its birth"): inspect_channel(publisher.channel)
        finally:
            publisher.win.k.CloseHandle(process)
    else:
        child.stdin.close()
        child.wait(timeout=5)
        pytest.fail("The inert child exited before its lifetime handle could be opened")


def test_powershell_launcher_selection_and_environment(session, tmp_path):
    _, directory, manifest, status = session
    def quote(value): return "'" + str(value).replace("'", "''") + "'"
    script = tmp_path / "launcher-check.ps1"
    script.write_text("""
$ErrorActionPreference = 'Stop'
. ROOT_HELPER
$Original = "capture = ddx`nkeyboard = enabled`nkeyboard = disabled`nstream_audio = enabled`n"
$Desktop = Set-Quest3DInitialSettings $Original $false
$File = Set-Quest3DInitialSettings $Original $true
Assert-Quest3DInitialSettings $Desktop $false
Assert-Quest3DInitialSettings $File $true
if ($Original -notmatch 'capture = ddx') { throw 'Original configuration was mutated.' }
$Rejected = $false
try { Assert-Quest3DInitialSettings ($Desktop + "`nstream_audio = enabled") $false } catch { $Rejected = $true }
if (!$Rejected) { throw 'Duplicate audio gate was accepted.' }
$Pcm = Get-Quest3DFilePcm ROOT DIR
if ($Pcm.channel -cne CHANNEL_EXPECTED) { throw 'Wrong live channel.' }
$File += "`nquest3d_control_dir = $($Pcm.control_directory)`n"
Assert-Quest3DFilePcmVideo $Pcm @(@{stream_epoch = 321}) $File
Assert-Quest3DPcmSelection @{audio_enabled = $true; file_pcm = $Pcm; input_enabled = $false} $true
Assert-Quest3DPcmSelection @{audio_enabled = $false; file_pcm = $null; input_enabled = $false} $false
Assert-Quest3DPcmSelection @{audio_enabled = $false; file_pcm = $null; input_enabled = $true} $false $true
foreach ($Case in @({ Assert-Quest3DFilePcmVideo $Pcm @(@{stream_epoch = 322}) $File },
    { Assert-Quest3DFilePcmVideo $Pcm @(@{stream_epoch = 321}) ($File -replace [regex]::Escape($Pcm.control_directory), 'A:/different-session') },
    { Assert-Quest3DPcmSelection @{audio_enabled = $true; file_pcm = $Pcm; input_enabled = $false} $false },
    { Assert-Quest3DPcmSelection @{audio_enabled = $false; file_pcm = $null; input_enabled = $false} $true },
    { Assert-Quest3DPcmSelection @{audio_enabled = $true; file_pcm = $Pcm; input_enabled = $true} $true },
    { Assert-Quest3DPcmSelection @{audio_enabled = $false; file_pcm = $null; input_enabled = $true} $false },
    { Assert-Quest3DPcmSelection @{audio_enabled = $false; file_pcm = $null; input_enabled = $false} $false $true },
    { Assert-Quest3DPcmSelection @{audio_enabled = $true; file_pcm = $Pcm; input_enabled = $true} $true $true })) {
    $Rejected = $false
    try { & $Case } catch { $Rejected = $true }
    if (!$Rejected) { throw 'Mismatched PCM selection or video/control session was accepted.' }
}
$Expected = $Pcm.PSObject.Copy(); $Expected.session_id = 'cccccccccccccccccccccccccccccccc'
$Rejected = $false
try { Get-Quest3DFilePcm ROOT DIR $Expected | Out-Null } catch { $Rejected = $true }
if (!$Rejected) { throw 'Changed staged session was accepted.' }
$env:QUEST3D_FILE_AUDIO_CHANNEL = 'inherited-invalid-channel'
foreach ($Selection in @($null, $Pcm)) {
    $Log = Join-Path DIR ('env-' + [guid]::NewGuid().ToString('N') + '.txt')
    $Child = Start-Process -FilePath (Join-Path $PSHOME 'pwsh.exe') -WindowStyle Hidden -PassThru -Wait -ArgumentList '-NoProfile', '-Command', 'Write-Output $env:QUEST3D_FILE_AUDIO_CHANNEL' -Environment (Get-Quest3DFilePcmEnvironment $Selection) -RedirectStandardOutput $Log
    $Actual = ([string](Get-Content -LiteralPath $Log -Raw)).Trim()
    if ($Child.ExitCode -ne 0 -or ($Selection -and $Actual -cne $Selection.channel) -or (!$Selection -and $Actual)) { throw 'Child channel environment did not match explicit selection.' }
}
foreach ($Name in @('prepare-dev-host.ps1', 'run-dev-host.ps1')) {
    $Tokens = $null; $ParseErrors = $null
    $null = [Management.Automation.Language.Parser]::ParseFile((Join-Path ROOT 'native/host' $Name), [ref]$Tokens, [ref]$ParseErrors)
    if ($ParseErrors.Count) { throw 'Launcher syntax errors.' }
}
Write-Output 'PASS: settings, live descriptor, changed session, child environment and script syntax'
""".replace("ROOT_HELPER", quote(ROOT / "native/host/file-pcm-launch.ps1"))
        .replace("ROOT", quote(ROOT)).replace("DIR", quote(directory)).replace("CHANNEL_EXPECTED", quote(manifest["channel"])), encoding="utf-8")
    # Keep a real publisher status fresh during the separate read-only uv/Python
    # startup processes. No PCM is offered or consumed by this fixture.
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(.05):
            status["updated_monotonic_ns"] = time.perf_counter_ns()
            atomic_json(directory / "status.json", status)
    worker = threading.Thread(target=heartbeat)
    worker.start()
    try:
        result = subprocess.run(["pwsh", "-NoProfile", "-File", str(script)], capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stdout + result.stderr
        assert "PASS:" in result.stdout
    finally:
        stop.set(); worker.join()

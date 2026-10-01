"""Exercise the actual launcher lock without installation or elevation."""
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not SHELL.is_file(), reason="Windows PowerShell required")


def quoted(value):
    return "'" + str(value).replace("'", "''") + "'"


def fixture(tmp_path, exit_code=0):
    folder = tmp_path / "package" / "scripts" / "release"
    folder.mkdir(parents=True)
    launcher = folder / "installer-launcher.ps1"
    launcher.write_bytes((ROOT / "scripts/release/installer-launcher.ps1").read_bytes())
    child = folder / "install-ui.ps1"
    child.write_text(f"param([switch]$SelfTest)\nWrite-Output 'fixture UI ended'\nexit {exit_code}\n", "utf-8-sig")
    data = child.read_bytes()
    manifest = {"schema": 1, "release": "fixture", "files": {
        "scripts/release/install-ui.ps1": {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    }}
    (folder.parents[1] / "distribution-manifest.json").write_text(json.dumps(manifest), "utf-8")
    return launcher


def invoke(launcher):
    return subprocess.run([str(SHELL), "-NoProfile", "-STA", "-ExecutionPolicy", "Bypass", "-File", str(launcher),
                           "-Target", "pc", "-NoDialog", "-SelfTest"], capture_output=True, timeout=30)


def test_other_installer_window_blocks_then_releases(tmp_path):
    launcher = fixture(tmp_path)
    ready = tmp_path / "ready.flag"
    release = tmp_path / "release.flag"
    holder = tmp_path / "hold-lock.ps1"
    holder.write_text(
        "$ErrorActionPreference='Stop'\n"
        "$identity=[Security.Principal.WindowsIdentity]::GetCurrent()\n"
        "try {$name='Local\\Quest3D.Installer.'+$identity.User.Value} finally {$identity.Dispose()}\n"
        "$mutex=New-Object Threading.Mutex($false,$name)\n"
        "try {\n"
        "if (!$mutex.WaitOne(0)) {throw 'Already busy'}\n"
        f"[IO.File]::WriteAllText({quoted(ready)},'held')\n"
        f"while (!(Test-Path -LiteralPath {quoted(release)})) {{Start-Sleep -Milliseconds 50}}\n"
        "} finally {$mutex.ReleaseMutex();$mutex.Dispose()}\n", "utf-8-sig")
    process = subprocess.Popen([str(SHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(holder)],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 15
        while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.05)
        assert ready.exists(), "Lock holder did not start"
        assert invoke(launcher).returncode != 0
    finally:
        release.write_text("release", "utf-8")
        process.communicate(timeout=15)
    assert process.returncode == 0
    assert invoke(launcher).returncode == 0


def test_child_failure_does_not_leave_lock_or_claim_success(tmp_path):
    failed = fixture(tmp_path / "failed", 12)
    assert invoke(failed).returncode != 0
    good = fixture(tmp_path / "good")
    assert invoke(good).returncode == 0


def test_changed_child_is_rejected_before_execution(tmp_path):
    launcher = fixture(tmp_path)
    child = launcher.with_name("install-ui.ps1")
    marker = tmp_path / "should-not-exist.flag"
    child.write_text(f"[IO.File]::WriteAllText({quoted(marker)},'executed')", "utf-8-sig")
    assert invoke(launcher).returncode != 0
    assert not marker.exists()

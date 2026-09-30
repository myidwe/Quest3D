"""Exercise the actual PowerShell installer with an isolated fake ADB transport."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not shutil.which("powershell.exe"), reason="Windows PowerShell required")


def fixture(tmp_path, devices="fixture-quest device", model="Meta Quest 3", installed_version=1, version_code=2):
    root = tmp_path / "package"
    scripts = root / "scripts/release"
    scripts.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/release/install-quest.ps1", scripts / "install-quest.ps1")
    apk = root / "Quest3D-Quest.apk"
    apk.write_bytes(b"test APK transport fixture")
    manifest = {"schema": 1, "apk": apk.name, "sha256": hashlib.sha256(apk.read_bytes()).hexdigest(),
                "package": "app.questto3d.client.debug", "preserve_data": True, "version_code": version_code}
    (root / "quest-install.json").write_text(json.dumps(manifest), "utf-8")
    adb = root / "adb.cmd"
    listing = "\n".join("echo " + row for row in devices.splitlines())
    adb.write_text(f'''@echo off
echo %*>>"%~dp0calls.txt"
if "%1"=="devices" (
echo List of devices attached
{listing}
exit /b 0
)
if "%3"=="shell" if "%4"=="getprop" (
echo {model}
exit /b 0
)
if "%3"=="shell" if "%4"=="dumpsys" (
echo versionCode={installed_version}
exit /b 0
)
if "%3"=="install" (
echo Success
exit /b 0
)
exit /b 1
''', "ascii")
    return root, adb


def run(root, adb, *options):
    # Use structured argv, never build a command from device data or paths.
    environment = os.environ.copy()
    # A Python child of PowerShell 7 inherits PS7-only module paths. Let PS5.1
    # construct its own standard module paths, as when a user opens the CMD.
    for key in list(environment):
        if key.casefold() == "psmodulepath":
            environment.pop(key)
    return subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                           str(root / "scripts/release/install-quest.ps1"), "-Adb", str(adb), *options],
                          capture_output=True, timeout=30, env=environment)


def calls(root):
    path = root / "calls.txt"
    return path.read_text("ascii") if path.exists() else ""


@pytest.mark.parametrize("state", ["unauthorized", "offline"])
def test_unapproved_or_offline_device_never_installs(tmp_path, state):
    root, adb = fixture(tmp_path, devices=f"fixture-quest {state}")
    result = run(root, adb)
    assert result.returncode != 0
    assert "devices" in calls(root)
    assert " install " not in calls(root)


def test_search_lists_ready_and_unapproved_devices_without_installing(tmp_path):
    root, adb = fixture(tmp_path, devices="fixture-one device\nfixture-two unauthorized")
    result = run(root, adb, "-ListDevices")
    assert result.returncode == 0, result.stderr
    value = json.loads(result.stdout)
    assert value["devices"] == [{"serial": "fixture-one", "state": "device", "model": "Meta Quest 3", "quest": True},
                                 {"serial": "fixture-two", "state": "unauthorized", "model": "", "quest": False}]
    assert " install " not in calls(root)


def test_multiple_devices_require_explicit_selection(tmp_path):
    root, adb = fixture(tmp_path, devices="fixture-one device\nfixture-two device")
    assert run(root, adb).returncode != 0
    assert "devices" in calls(root)
    assert " install " not in calls(root)
    result = run(root, adb, "-Serial", "fixture-two")
    assert result.returncode == 0, result.stderr
    log = calls(root)
    assert "-s fixture-two install -r --no-streaming" in log
    assert "uninstall" not in log and " clear " not in log


def test_non_quest_is_not_installed(tmp_path):
    root, adb = fixture(tmp_path, model="Generic Android phone")
    assert run(root, adb).returncode != 0
    assert "shell getprop ro.product.model" in calls(root)
    assert " install " not in calls(root)


def test_downgrade_does_not_install_or_remove_existing_app(tmp_path):
    root, adb = fixture(tmp_path, installed_version=3, version_code=2)
    assert run(root, adb).returncode != 0
    assert "dumpsys package app.questto3d.client.debug" in calls(root)
    assert " install " not in calls(root) and "uninstall" not in calls(root)


def test_apk_tampering_fails_before_contacting_adb(tmp_path):
    root, adb = fixture(tmp_path)
    (root / "Quest3D-Quest.apk").write_bytes(b"modified")
    assert run(root, adb).returncode != 0
    assert not (root / "calls.txt").exists()


def test_check_only_does_not_install(tmp_path):
    root, adb = fixture(tmp_path)
    assert run(root, adb, "-CheckOnly").returncode == 0
    assert " install " not in calls(root)


def test_ps51_uses_os_modules_even_with_an_inherited_invalid_module_path(tmp_path):
    root, adb = fixture(tmp_path)
    environment = os.environ.copy()
    environment["PSMODULEPATH"] = str(tmp_path / "nonexistent-ps7-modules")
    result = subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                             str(root / "scripts/release/install-quest.ps1"), "-Adb", str(adb), "-CheckOnly"],
                             capture_output=True, timeout=30, env=environment)
    assert result.returncode == 0, result.stderr
    assert "shell getprop ro.product.model" in calls(root) and " install " not in calls(root)

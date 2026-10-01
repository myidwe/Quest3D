"""Real Windows/PowerShell filesystem transactions; never touch installed user apps.

Run with --basetemp on the A: workspace for isolated fixtures. No downloads,
GPU work, default Desktop/Start-menu shortcut writes, or firewall changes.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not SHELL, reason="Windows PowerShell 5.1 required")
HELPER = ROOT / "scripts/release/installation-lifecycle.ps1"


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def package(folder: Path, release="old", *, changed=None):
    folder.mkdir(parents=True)
    files = {
        ".python-version": b"3.12.6\n",
        "config/distribution.json": json.dumps(dict(schema=1, host_runtime="artifacts/host/runtime-public", host_sha256="7" * 64)).encode(),
        "src/quest3d/desktop.py": b"# original application\n",
        "resources/old.txt": b"old asset",
    }
    if changed:
        for name, value in changed.items():
            if value is None:
                files.pop(name, None)
            else:
                files[name] = value
    entries = {}
    for name, value in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        entries[name] = dict(bytes=len(value), sha256=sha(value))
    manifest = dict(schema=1, release=release, files=entries, metadata={})
    (folder / "distribution-manifest.json").write_text(json.dumps(manifest), "utf-8")
    return manifest


def own(folder: Path, *, completed=True):
    manifest_path = folder / "distribution-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8-sig"))
    (folder / "quest3d-install.json").write_text(json.dumps(dict(product="Quest3D Desktop", release=manifest["release"], package_manifest_sha256=sha(manifest_path.read_bytes()), completed=completed)), "utf-8")


def installed(folder: Path):
    package(folder)
    own(folder)
    for name, value in {
        ".venv/Scripts/pythonw.exe": b"old fake interpreter; never executed",
        "config/desktop.json": b"fixture viewing settings",
        "artifacts/host/dev/credentials/cakey.pem": b"fixture pairing key",
        "artifacts/host/dev/state.json": b"fixture pairing state",
        "models/user-model.bin": b"fixture model",
        "user-notes.txt": b"fixture user file",
    }.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)


def private_bytes(folder: Path):
    return {name: (folder / name).read_bytes() for name in (
        "config/desktop.json", "artifacts/host/dev/credentials/cakey.pem", "artifacts/host/dev/state.json", "models/user-model.bin", "user-notes.txt")}


def ps(tmp: Path, body: str, *, expect=0):
    runner = tmp / ("run-" + sha(body.encode())[:12] + ".ps1")
    runner.write_text("$ErrorActionPreference='Stop'\n. '" + str(HELPER).replace("'", "''") + "'\n" + body, "utf-8-sig")
    checked = subprocess.run([SHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner)], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90, cwd=ROOT)
    if expect == 0:
        assert checked.returncode == 0, checked.stdout + checked.stderr
    else:
        assert checked.returncode != 0, checked.stdout
    return checked


def q(path: Path):
    return "'" + str(path).replace("'", "''") + "'"


def start_body(old: Path, new: Path):
    return f"$package=Get-Quest3DPackage {q(new)} -VerifyFiles\n$t=Start-Quest3DUpdate {q(old)} $package\n"


def test_update_commit_and_manual_rollback_preserve_personal_data(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    before = private_bytes(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"# new application\n", "resources/old.txt": None, "resources/new.txt": b"new asset"})
    ps(tmp_path, start_body(old, new) + f"""
Invoke-Quest3DUpdateFiles $t $package
Move-Quest3DUpdateEnvironment $t
$venv=Join-Path {q(old)} '.venv/Scripts'
[void](New-Item -ItemType Directory -Force -Path $venv)
[IO.File]::WriteAllText((Join-Path $venv 'pythonw.exe'),'new fake interpreter')
Write-Quest3DJson (Join-Path {q(old)} 'quest3d-install.json') @{{product='Quest3D Desktop';release='new';package_manifest_sha256=$package.hash;completed=$true}}
Complete-Quest3DUpdate $t
if((Get-Quest3DTransaction {q(old)}).journal.phase -ne 'committed'){{throw 'not committed'}}
""")
    assert private_bytes(old) == before
    assert not (old / "resources/old.txt").exists()
    assert (old / "resources/new.txt").read_bytes() == b"new asset"
    ps(tmp_path, f"$result=Restore-Quest3DUpdate {q(old)}\nif(!$result.restored){{throw 'not restored'}}")
    assert private_bytes(old) == before
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"
    assert (old / "resources/old.txt").read_bytes() == b"old asset"
    assert not (old / "resources/new.txt").exists()
    assert (old / ".venv/Scripts/pythonw.exe").read_bytes() == b"old fake interpreter; never executed"
    assert json.loads((old / "quest3d-install.json").read_text("utf-8-sig"))["release"] == "old"


def test_copy_failure_after_original_move_can_restore(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    ps(tmp_path, start_body(old, new) + f"""
Remove-Item -LiteralPath (Join-Path {q(new)} 'src/quest3d/desktop.py')
$failed=$false
try {{ Invoke-Quest3DUpdateFiles $t $package }} catch {{ $failed=$true }}
if(!$failed){{throw 'copy did not fail'}}
$null=Restore-Quest3DUpdate {q(old)}
""")
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"
    assert (old / ".venv/Scripts/pythonw.exe").exists()


def test_interrupted_update_is_recovered_in_new_process(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    ps(tmp_path, start_body(old, new) + "Invoke-Quest3DUpdateFiles $t $package\nMove-Quest3DUpdateEnvironment $t")
    assert not (old / ".venv").exists()
    ps(tmp_path, f"$null=Restore-Quest3DUpdate {q(old)}")
    assert (old / ".venv/Scripts/pythonw.exe").exists()
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"


@pytest.mark.parametrize("kind", ["edited", "collision", "python", "host"])
def test_unsafe_or_incompatible_update_is_rejected_before_mutation(tmp_path, kind):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    changes = {"src/quest3d/desktop.py": b"new source"}
    if kind == "edited":
        (old / "src/quest3d/desktop.py").write_bytes(b"local source modification")
    elif kind == "collision":
        (old / "resources/user.txt").write_bytes(b"user asset")
        changes["resources/user.txt"] = b"replacement"
    elif kind == "python":
        changes[".python-version"] = b"3.12.7\n"
    else:
        changes["config/distribution.json"] = json.dumps(dict(schema=1, host_runtime="artifacts/host/runtime-other", host_sha256="8" * 64)).encode()
    package(new, "new", changed=changes)
    before = {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()}
    checked = ps(tmp_path, start_body(old, new), expect=1)
    expected = {"edited": "Installed application file changed", "collision": "unowned file", "python": "Python-version migration", "host": "host-version migration"}[kind]
    assert expected in checked.stdout + checked.stderr
    assert {str(p.relative_to(old)): p.read_bytes() for p in old.rglob("*") if p.is_file()} == before


def test_changed_updated_file_blocks_entire_rollback(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source", "resources/old.txt": b"changed asset"})
    ps(tmp_path, start_body(old, new) + "Invoke-Quest3DUpdateFiles $t $package")
    (old / "src/quest3d/desktop.py").write_bytes(b"user changed updated source")
    ps(tmp_path, f"$null=Restore-Quest3DUpdate {q(old)}", expect=1)
    assert (old / "resources/old.txt").read_bytes() == b"changed asset"
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"user changed updated source"


def test_missing_original_file_backup_blocks_all_rollback_mutation(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source", "resources/old.txt": b"changed asset"})
    ps(tmp_path, start_body(old, new) + "Invoke-Quest3DUpdateFiles $t $package")
    pointer = json.loads((old / ".cache/install/update-current.json").read_text("utf-8-sig"))
    (old / ".cache/install/updates" / pointer["transaction"] / "payload/src/quest3d/desktop.py").unlink()
    manifest_before = (old / "distribution-manifest.json").read_bytes()
    checked = ps(tmp_path, f"$null=Restore-Quest3DUpdate {q(old)}", expect=1)
    assert "Original file backup is missing" in checked.stdout + checked.stderr
    assert (old / "distribution-manifest.json").read_bytes() == manifest_before
    assert (old / "resources/old.txt").read_bytes() == b"changed asset"


def test_missing_saved_environment_blocks_rollback_before_file_restore(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    ps(tmp_path, start_body(old, new) + "Invoke-Quest3DUpdateFiles $t $package\nMove-Quest3DUpdateEnvironment $t")
    pointer = json.loads((old / ".cache/install/update-current.json").read_text("utf-8-sig"))
    backup = old / ".cache/install/updates" / pointer["transaction"] / "environment"
    # Retain it outside its recorded name, rather than deleting a directory.
    backup.rename(backup.with_name("externally-moved-environment"))
    checked = ps(tmp_path, f"$null=Restore-Quest3DUpdate {q(old)}", expect=1)
    assert "Original environment backup is missing" in checked.stdout + checked.stderr
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"new source"


def test_interrupted_update_ui_offers_only_safe_rollback(tmp_path):
    profile, new = tmp_path / "fixture-profile", tmp_path / "new"
    old = profile / "Quest3D Desktop"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    ps(tmp_path, start_body(old, new) + "Invoke-Quest3DUpdateFiles $t $package")
    checked = ps(tmp_path, f"$env:LOCALAPPDATA={q(profile)}\n& {q(ROOT/'scripts/release/install-ui.ps1')} -SelfTest")
    assert "rollback=True; launch=False; remove=False" in checked.stdout


def test_tampered_journal_cannot_delete_an_unowned_file(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    ps(tmp_path, start_body(old, new))
    pointer = json.loads((old / ".cache/install/update-current.json").read_text("utf-8-sig"))
    journal_path = old / ".cache/install/updates" / pointer["transaction"] / "journal.json"
    journal = json.loads(journal_path.read_text("utf-8-sig"))
    journal["actions"].append(dict(path="user-notes.txt", old_sha256=None, new_sha256=sha(b"fixture user file")))
    journal_path.write_text(json.dumps(journal), "utf-8")
    ps(tmp_path, f"$null=Restore-Quest3DUpdate {q(old)}", expect=1)
    assert (old / "user-notes.txt").read_bytes() == b"fixture user file"


def test_reparse_payload_is_rejected_without_traversing_it(tmp_path):
    old, external = tmp_path / "installed", tmp_path / "external"
    installed(old)
    external.mkdir()
    (external / "secret.txt").write_bytes(b"fixture outside file")
    ps(tmp_path, f"""
$junction=Join-Path {q(old)} 'linked'
[void](New-Item -ItemType Junction -Path $junction -Target {q(external)})
try {{
    $blocked=$false
    try {{ $null=Get-Quest3DInstallPath {q(old)} 'linked/secret.txt' }} catch {{ $blocked=$true }}
    if(!$blocked){{throw 'junction was followed'}}
}} finally {{
    if(!(Get-Item -LiteralPath $junction).Attributes.HasFlag([IO.FileAttributes]::ReparsePoint)){{throw 'fixture is not a junction'}}
    [IO.Directory]::Delete($junction)
}}
""")
    assert (external / "secret.txt").read_bytes() == b"fixture outside file"


@pytest.mark.parametrize("broken", [False, True])
def test_missing_child_and_broken_junction_still_reject_ancestor(tmp_path, broken):
    old, external = tmp_path / "installed", tmp_path / "external"
    installed(old)
    external.mkdir()
    (external / "secret.txt").write_bytes(b"fixture outside file")
    create = f"[void](New-Item -ItemType Junction -Path (Join-Path {q(old)} 'linked') -Target {q(external)})"
    ps(tmp_path, create)
    held = external.with_name("external-retained")
    if broken:
        external.rename(held)
    try:
        checked = ps(tmp_path, f"Assert-Quest3DNoReparse (Join-Path {q(old)} 'linked/missing/child.txt')", expect=1)
        assert "Reparse point refused" in checked.stdout + checked.stderr
    finally:
        ps(tmp_path, f"[IO.Directory]::Delete((Join-Path {q(old)} 'linked'))")
    assert ((held if broken else external) / "secret.txt").read_bytes() == b"fixture outside file"


@pytest.mark.parametrize("exception_type", ["UnauthorizedAccessException", "IO.IOException"])
def test_wrapped_native_attribute_errors_fail_closed(tmp_path, exception_type):
    # Windows can return attributes despite an ACL deny on ReadAttributes. Inject
    # only the native-call outcome in a disposable copy of the actual guard, to
    # check PowerShell's wrapped-error classification without changing user ACLs.
    checked = ps(tmp_path, f"""
$guard=(Get-Command Assert-Quest3DNoReparse).ScriptBlock.ToString()
$native='$attributes = [IO.File]::GetAttributes($current)'
if(($guard.Split(@($native),[StringSplitOptions]::None)).Count -ne 2){{throw 'native call site not found'}}
$fault='throw [Management.Automation.MethodInvocationException]::new("fixture wrapper", [{exception_type}]::new("fixture native error"))'
$isolated=[ScriptBlock]::Create($guard.Replace($native,$fault))
$blocked=$false
try {{ & $isolated {q(tmp_path)} }} catch {{
    if($_.Exception.ToString() -notmatch 'fixture native error'){{throw}}
    $blocked=$true
}}
if(!$blocked){{throw 'attribute error was ignored'}}
""")
    assert checked.returncode == 0


def test_real_installer_update_failure_restores_files_environment_and_pairing(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    before = private_bytes(old)
    changes = {
        "src/quest3d/desktop.py": b"new source",
        "scripts/release/install.ps1": (ROOT / "scripts/release/install.ps1").read_bytes(),
        "scripts/release/python-discovery.ps1": (ROOT / "scripts/release/python-discovery.ps1").read_bytes(),
        "scripts/release/installation-lifecycle.ps1": HELPER.read_bytes(),
    }
    package(new, "new", changed=changes)
    invalid_python = tmp_path / "invalid-python.exe"
    invalid_python.write_bytes(b"not a Windows executable")
    checked = ps(tmp_path, f"& {q(new/'scripts/release/install.ps1')} -Destination {q(old)} -Update -NoShortcuts -Python {q(invalid_python)}", expect=1)
    assert "Previous version restored" in checked.stdout
    assert private_bytes(old) == before
    assert (old / ".venv/Scripts/pythonw.exe").read_bytes() == b"old fake interpreter; never executed"
    assert (old / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"
    assert json.loads((old / "quest3d-install.json").read_text("utf-8-sig"))["completed"] is True


def test_owned_shortcut_replacement_uses_real_com_in_fixture_directory(tmp_path):
    old, new, links = tmp_path / "old", tmp_path / "new", tmp_path / "links"
    installed(old)
    installed(new)
    script = ROOT / "scripts/install-desktop-shortcut.ps1"
    ps(tmp_path, f"& {q(script)} -Root {q(old)} -ShortcutDirectory {q(links)}")
    shortcut = links / "Sterevi Desktop.lnk"
    old_bytes = shortcut.read_bytes()
    ps(tmp_path, f"& {q(script)} -Root {q(new)} -ShortcutDirectory {q(links)}", expect=1)
    assert shortcut.read_bytes() == old_bytes
    ps(tmp_path, f"& {q(script)} -Root {q(new)} -ShortcutDirectory {q(links)} -ReplaceOwned\n$shell=New-Object -ComObject WScript.Shell\n$link=$shell.CreateShortcut({q(shortcut)})\nif($link.WorkingDirectory -ine {q(new)}){{throw 'wrong shortcut owner'}}")
    assert shortcut.read_bytes() != old_bytes


def test_unowned_shortcut_is_preserved_even_with_replace_owned(tmp_path):
    old, new, links = tmp_path / "old", tmp_path / "new", tmp_path / "links"
    installed(old)
    installed(new)
    links.mkdir()
    shortcut = links / "Sterevi Desktop.lnk"
    script = ROOT / "scripts/install-desktop-shortcut.ps1"
    ps(tmp_path, f"$shell=New-Object -ComObject WScript.Shell\n$link=$shell.CreateShortcut({q(shortcut)})\n$link.TargetPath=$env:ComSpec\n$link.Description='User owned launcher'\n$link.Save()")
    before = shortcut.read_bytes()
    ps(tmp_path, f"& {q(script)} -Root {q(new)} -ShortcutDirectory {q(links)} -ReplaceOwned", expect=1)
    assert shortcut.read_bytes() == before
    assert not (new / "Sterevi Desktop.lnk").exists()


def test_uninstall_retains_all_personal_data_and_removes_only_owned_shortcuts(tmp_path):
    old, links = tmp_path / "installed", tmp_path / "links"
    installed(old)
    before = private_bytes(old)
    ps(tmp_path, f"& {q(ROOT/'scripts/install-desktop-shortcut.ps1')} -Root {q(old)} -ShortcutDirectory {q(links)}")
    report = tmp_path / "removal.json"
    ps(tmp_path, f"& {q(ROOT/'scripts/release/uninstall.ps1')} -Root {q(old)} -ShortcutDirectory {q(links)} -NoSystemShortcuts -ReportPath {q(report)}")
    result = json.loads(report.read_text("utf-8-sig"))
    archive = Path(result["archive"])
    assert result["success"] and result["data_retained"] and not result["disk_space_reclaimed"]
    assert archive.parent == old.parent and archive.name.startswith(".Quest3D-removed-")
    assert not old.exists() and not (links / "Sterevi Desktop.lnk").exists()
    assert private_bytes(archive) == before


def test_uninstall_refuses_an_unowned_directory(tmp_path):
    folder = tmp_path / "user-folder"
    folder.mkdir()
    (folder / "user.txt").write_bytes(b"important fixture")
    ps(tmp_path, f"& {q(ROOT/'scripts/release/uninstall.ps1')} -Root {q(folder)} -NoSystemShortcuts", expect=1)
    assert (folder / "user.txt").read_bytes() == b"important fixture"


def test_ps51_installer_ui_constructs_without_installing(tmp_path):
    ps(tmp_path, f"& {q(ROOT/'scripts/release/install-ui.ps1')} -SelfTest -PreviewImage {q(tmp_path/'installer.png')}")
    assert (tmp_path / "installer.png").stat().st_size > 1000


def test_running_installation_is_rejected_before_transaction_metadata(tmp_path):
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={"src/quest3d/desktop.py": b"new source"})
    worker = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)", str(old)], cwd=ROOT)
    try:
        checked = ps(tmp_path, start_body(old, new), expect=1)
        assert "installation is running" in checked.stdout + checked.stderr
        assert not (old / ".cache/install/update-current.json").exists()
        assert (old / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"
    finally:
        worker.terminate()
        worker.wait(timeout=10)


def test_actual_installer_cache_is_local_and_previous_env_is_restored_on_failure(tmp_path):
    probe = tmp_path / "cache-probe.exe"
    # Real bounded .NET console process, replacing only the fixture dependency tool.
    ps(tmp_path, f"Add-Type -TypeDefinition 'using System; public class CacheProbe {{ public static int Main(string[] args) {{ Console.WriteLine(\"CACHE=\"+Environment.GetEnvironmentVariable(\"UV_CACHE_DIR\")); return 42; }} }}' -OutputAssembly {q(probe)} -OutputType ConsoleApplication")
    old, new = tmp_path / "installed", tmp_path / "new"
    installed(old)
    package(new, "new", changed={
        "src/quest3d/desktop.py": b"new source",
        "scripts/release/install.ps1": (ROOT / "scripts/release/install.ps1").read_bytes(),
        "scripts/release/installation-lifecycle.ps1": HELPER.read_bytes(),
        ".tools/desktop/uv.exe": probe.read_bytes(),
        "scripts/release/python-discovery.ps1": (ROOT / "scripts/release/python-discovery.ps1").read_bytes(),
    })
    checked = ps(tmp_path, f"""
$env:UV_CACHE_DIR='C:\\fixture-cache-that-must-not-be-used'
$failed=$false
try {{ & {q(new/'scripts/release/install.ps1')} -Destination {q(old)} -Update -NoShortcuts -Python {q(Path(sys.executable))} }} catch {{ $failed=$true }}
if(!$failed){{throw 'fixture dependency failure did not propagate'}}
if($env:UV_CACHE_DIR -cne 'C:\\fixture-cache-that-must-not-be-used'){{throw 'prior cache environment was not restored'}}
""")
    assert "CACHE=" + str(old / ".cache/uv") in checked.stdout
    assert "Previous version restored" in checked.stdout
    assert (old / ".venv/Scripts/pythonw.exe").read_bytes() == b"old fake interpreter; never executed"

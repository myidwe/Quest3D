"""Real COM shortcut migration and filesystem rollback in disposable Windows fixtures."""
from __future__ import annotations

import json
import hashlib
from pathlib import Path

import pytest

from test_installation_lifecycle import ROOT, installed, package, private_bytes, ps, q, start_body, pytestmark

SHORTCUT = ROOT / "scripts/install-desktop-shortcut.ps1"


def legacy_link(tmp: Path, root: Path, path: Path, *, owned=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    ps(tmp, f"""
$shell=New-Object -ComObject WScript.Shell
try {{
  $link=$shell.CreateShortcut({q(path)})
  $link.TargetPath={q(root/'.venv/Scripts/pythonw.exe') if owned else '$env:ComSpec'}
  $link.WorkingDirectory={q(root)}
  $link.Arguments='-m quest3d.desktop --root "' + {q(root)} + '"'
  $link.Description={"'Quest3D Desktop | ' + " + q(root) if owned else "'User launcher'"}
  $link.Save()
}} finally {{[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell)}}
""")


def test_fresh_default_is_sterevi_and_owned_legacy_default_is_reused(tmp_path):
    profile = tmp_path / "profile"
    profile.mkdir()
    expected = profile / "Sterevi Desktop"
    checked = ps(tmp_path, f"Get-Quest3DPreferredInstallRoot -LocalAppData {q(profile)} -ShortcutDirectories @()")
    assert checked.stdout.strip() == str(expected)
    old = profile / "Quest3D Desktop"
    installed(old)
    before = private_bytes(old)
    checked = ps(tmp_path, f"Get-Quest3DPreferredInstallRoot -LocalAppData {q(profile)} -ShortcutDirectories @()")
    assert checked.stdout.strip() == str(old)
    assert private_bytes(old) == before and not expected.exists()


def test_custom_owned_legacy_shortcut_finds_existing_install_without_moving_it(tmp_path):
    old, profile, links = tmp_path / "custom install", tmp_path / "profile", tmp_path / "links"
    installed(old)
    profile.mkdir()
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, old, legacy)
    before = legacy.read_bytes()
    checked = ps(tmp_path, f"Get-Quest3DPreferredInstallRoot -LocalAppData {q(profile)} -ShortcutDirectories @({q(links)})")
    assert checked.stdout.strip() == str(old) and legacy.read_bytes() == before


def test_owned_legacy_shortcut_migrates_with_exact_backup_and_recovers(tmp_path):
    root, links = tmp_path / "installed", tmp_path / "links"
    installed(root)
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, root, legacy)
    before = legacy.read_bytes()
    personal = private_bytes(root)
    result = tmp_path / "migration.json"
    ps(tmp_path, f"& {q(SHORTCUT)} -Root {q(root)} -ShortcutDirectory {q(links)} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath {q(result)}")
    records = json.loads(result.read_text("utf-8-sig"))
    receipt_name = next(row["migration_receipt"] for row in records if "migration_receipt" in row)
    receipt = json.loads((root / receipt_name).read_text("utf-8-sig"))
    action = next(row for row in receipt["actions"] if row["path"] == str(legacy))
    assert not legacy.exists()
    assert (root / receipt_name).parent.joinpath(action["before_file"]).read_bytes() == before
    assert (links / "Sterevi Desktop.lnk").exists()
    assert private_bytes(root) == personal
    ps(tmp_path, f"Restore-Quest3DShortcutMigration {q(root)} '{receipt_name}'")
    assert legacy.read_bytes() == before and not (links / "Sterevi Desktop.lnk").exists()
    assert private_bytes(root) == personal


def test_unowned_old_name_is_preserved_and_new_name_collision_blocks_every_write(tmp_path):
    root, links = tmp_path / "installed", tmp_path / "links"
    installed(root)
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, root, legacy, owned=False)
    before = legacy.read_bytes()
    ps(tmp_path, f"& {q(SHORTCUT)} -Root {q(root)} -ShortcutDirectory {q(links)}")
    assert legacy.read_bytes() == before and (links / "Sterevi Desktop.lnk").exists()
    other, blocked = tmp_path / "other install", tmp_path / "blocked"
    installed(other)
    new_name = blocked / "Sterevi Desktop.lnk"
    legacy_link(tmp_path, other, new_name, owned=False)
    collision = new_name.read_bytes()
    ps(tmp_path, f"& {q(SHORTCUT)} -Root {q(other)} -ShortcutDirectory {q(blocked)} -ReplaceOwned", expect=1)
    assert new_name.read_bytes() == collision and not (other / "Sterevi Desktop.lnk").exists()


def test_product_update_rollback_restores_legacy_shortcuts_and_pairing(tmp_path):
    root, incoming, links = tmp_path / "installed", tmp_path / "incoming", tmp_path / "links"
    installed(root)
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, root, legacy)
    shortcut_before, personal = legacy.read_bytes(), private_bytes(root)
    package(incoming, "0.1.3-preview", changed={"src/quest3d/desktop.py": b"# branded app\n"})
    ps(tmp_path, start_body(root, incoming) + f"""
Invoke-Quest3DUpdateFiles $t $package
$records=@(& {q(SHORTCUT)} -Root {q(root)} -ShortcutDirectory {q(links)})
$receipt=@($records | Where-Object {{$_.PSObject.Properties.Name -contains 'migration_receipt'}})[0].migration_receipt
$t.journal | Add-Member -NotePropertyName 'shortcut_migration' -NotePropertyValue $receipt
Write-Quest3DJson $t.path $t.journal
$null=Restore-Quest3DUpdate {q(root)}
""")
    assert legacy.read_bytes() == shortcut_before and not (links / "Sterevi Desktop.lnk").exists()
    assert (root / "src/quest3d/desktop.py").read_bytes() == b"# original application\n"
    assert private_bytes(root) == personal


def test_changed_link_and_escape_receipt_are_never_overwritten_by_recovery(tmp_path):
    root, links = tmp_path / "installed", tmp_path / "links"
    installed(root)
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, root, legacy)
    records_path = tmp_path / "records.json"
    ps(tmp_path, f"& {q(SHORTCUT)} -Root {q(root)} -ShortcutDirectory {q(links)} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath {q(records_path)}")
    receipt_name = next(row["migration_receipt"] for row in json.loads(records_path.read_text("utf-8-sig")) if "migration_receipt" in row)
    created = links / "Sterevi Desktop.lnk"
    created.write_bytes(b"fixture user replaced shortcut")
    ps(tmp_path, f"Restore-Quest3DShortcutMigration {q(root)} '{receipt_name}'", expect=1)
    assert created.read_bytes() == b"fixture user replaced shortcut" and not legacy.exists()
    # A forged pointer cannot escape the installed recovery directory.
    ps(tmp_path, f"Restore-Quest3DShortcutMigration {q(root)} '../receipt.json'", expect=1)
    assert created.read_bytes() == b"fixture user replaced shortcut"


def test_resealed_unowned_backup_cannot_be_restored_as_an_app_shortcut(tmp_path):
    root, links = tmp_path / "installed", tmp_path / "links"
    installed(root)
    legacy = links / "Quest3D Desktop.lnk"
    legacy_link(tmp_path, root, legacy)
    records_path = tmp_path / "records.json"
    ps(tmp_path, f"& {q(SHORTCUT)} -Root {q(root)} -ShortcutDirectory {q(links)} | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 -LiteralPath {q(records_path)}")
    receipt_name = next(row["migration_receipt"] for row in json.loads(records_path.read_text("utf-8-sig")) if "migration_receipt" in row)
    receipt_path = root / receipt_name
    receipt = json.loads(receipt_path.read_text("utf-8-sig"))
    action = next(row for row in receipt["actions"] if row["path"] == str(legacy))
    backup = receipt_path.parent / action["before_file"]
    legacy_link(tmp_path, root, backup, owned=False)
    action["before_sha256"] = hashlib.sha256(backup.read_bytes()).hexdigest()
    receipt_path.write_text(json.dumps(receipt), "utf-8")
    created = links / "Sterevi Desktop.lnk"
    before = created.read_bytes()
    ps(tmp_path, f"Restore-Quest3DShortcutMigration {q(root)} '{receipt_name}'", expect=1)
    assert created.read_bytes() == before and not legacy.exists()

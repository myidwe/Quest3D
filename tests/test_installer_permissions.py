"""Real PS5 filesystem/UI-controller checks with stub elevation. No system changes."""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
SHELL = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
HELPER = ROOT / "scripts/release/installer-network-task.ps1"
pytestmark = pytest.mark.skipif(sys.platform != "win32" or not SHELL.is_file(), reason="Windows PowerShell 5.1 required")


def sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def q(path: Path | str) -> str:
    return "'" + str(path).replace("'", "''") + "'"


def run(tmp: Path, body: str, expected=0):
    runner = tmp / ("runner-" + sha(body.encode())[:14] + ".ps1")
    runner.write_text("$ErrorActionPreference='Stop'\n. " + q(HELPER) + "\n" + body, "utf-8-sig")
    environment = {k: v for k, v in os.environ.items() if k.casefold() != "psmodulepath"}
    temporary = tmp / "temporary"
    temporary.mkdir(exist_ok=True)
    profile = tmp / "profile"
    profile.mkdir(exist_ok=True)
    environment.update(TEMP=str(temporary), TMP=str(temporary), LOCALAPPDATA=str(profile))
    checked = subprocess.run([str(SHELL), "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=90,
                             cwd=ROOT, env=environment)
    if expected == 0:
        assert checked.returncode == 0, checked.stdout + checked.stderr
    else:
        assert checked.returncode != 0, checked.stdout
    return checked


def installed(tmp: Path):
    folder = tmp / "installed"
    host = b"fixture host, never executed"
    files = {
        ".python-version": b"3.12.6\n",
        "config/distribution.json": json.dumps(dict(schema=1, host_runtime="artifacts/host/runtime-public", host_sha256=sha(host))).encode(),
        "artifacts/host/runtime-public/sunshine.exe": host,
        "src/quest3d/desktop.py": b"# fixture application",
        "native/host/configure-installed-network.ps1": b"# fixture helper, never executed",
        ".tools/desktop/powershell/pwsh.exe": b"fixture shell, never executed",
        "scripts/release/installer-network-task.ps1": HELPER.read_bytes(),
    }
    entries = {}
    for name, value in files.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
        entries[name] = dict(bytes=len(value), sha256=sha(value))
    manifest = json.dumps(dict(schema=1, release="fixture", files=entries, metadata={})).encode()
    (folder / "distribution-manifest.json").write_bytes(manifest)
    (folder / "quest3d-install.json").write_text(json.dumps(dict(product="Quest3D Desktop", package_manifest_sha256=sha(manifest), release="fixture", completed=True)), "utf-8")
    for name, value in {
        "config/desktop.json": b"fixture personal settings",
        "models/fixture-model.bin": b"fixture local model",
        "artifacts/host/pairing-fixture/key.pem": b"fixture only; not a real key",
        "user-notes.txt": b"fixture user data",
    }.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(value)
    return folder


def request(tmp: Path, folder: Path, action="apply"):
    return json.loads(run(tmp, f"New-Quest3DNetworkRequest {q(folder)} '{action}' | ConvertTo-Json -Depth 5").stdout)


def write_request(tmp: Path, value: dict):
    path = tmp / "request.json"
    path.write_text(json.dumps(value), "utf-8")
    return "$r=Get-Content -LiteralPath " + q(path) + " -Raw | ConvertFrom-Json\n"


def successful(value: dict):
    result = dict(schema=1, operation_id=value["operation_id"], action=value["action"], success=True,
                  program=value["expected_program"], outcome={"apply": "applied", "remove": "removed", "status": "status"}[value["action"]],
                  verified_after=True, requested_applied=value["action"] != "status", partial=False, error=None)
    if value["action"] == "status":
        result.update(known=True, apply_satisfied=False, owned_rules_absent=True, rules_state="absent")
    return result


def test_request_uses_fresh_bounded_operation_without_touching_saved_settings(tmp_path):
    folder = installed(tmp_path)
    first = request(tmp_path, folder)
    second = request(tmp_path, folder)
    assert first["operation_id"] != second["operation_id"]
    assert Path(first["report_path"]).parent == folder / "config/network-operations" / first["operation_id"]
    assert not Path(first["report_path"]).exists()
    assert (folder / "config/desktop.json").read_bytes() == b"fixture personal settings"


@pytest.mark.parametrize("changed", ["native/host/configure-installed-network.ps1", ".tools/desktop/powershell/pwsh.exe", "scripts/release/installer-network-task.ps1", "config/distribution.json", "artifacts/host/runtime-public/sunshine.exe"])
def test_changed_network_launch_file_is_rejected_before_operation_or_elevation(tmp_path, changed):
    folder = installed(tmp_path)
    (folder / changed).write_bytes(b"changed fixture bytes")
    checked = run(tmp_path, f"New-Quest3DNetworkRequest {q(folder)} 'apply'", expected=1)
    assert "changed" in checked.stdout + checked.stderr
    assert not (folder / "config/network-operations").exists()


def test_start_rechecks_actual_helper_and_tracks_elevation_without_running_it(tmp_path):
    folder = installed(tmp_path)
    value = request(tmp_path, folder)
    captured = tmp_path / "launch.json"
    body = write_request(tmp_path, value) + f"""
function Start-Process {{
 param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru)
 @{{file=$FilePath;args=$ArgumentList;verb=$Verb;tracked=[bool]$PassThru}} | ConvertTo-Json | Set-Content -LiteralPath {q(captured)}
 return [pscustomobject]@{{Id=123;HasExited=$false}}
}}
$p=Start-Quest3DNetworkRequest $r
if ($p.Id -ne 123) {{ throw 'Child process was not retained.' }}
"""
    run(tmp_path, body)
    launch = json.loads(captured.read_text("utf-8-sig"))
    assert launch["tracked"] and launch["verb"] == "RunAs"
    assert value["operation_id"] in launch["args"] and value["report_path"] in launch["args"]
    (folder / "native/host/configure-installed-network.ps1").write_bytes(b"changed after request")
    run(tmp_path, body, expected=1)


@pytest.mark.parametrize("change", ["operation_id", "action", "schema", "success", "verified_after", "outcome", "partial", "program", "requested_applied", "exit", "missing", "malformed"])
def test_incomplete_or_stale_result_never_confirms_current_task(tmp_path, change):
    folder = installed(tmp_path)
    value = request(tmp_path, folder)
    report = successful(value)
    edits = dict(operation_id="0" * 32, action="remove", schema=9, success=False, verified_after=False, outcome="failed", partial=True, program=str(tmp_path / "another-install/sunshine.exe"), requested_applied=False)
    if change in edits:
        report[change] = edits[change]
    result_path = Path(value["report_path"])
    if change != "missing":
        result_path.write_text("{broken" if change == "malformed" else json.dumps(report), "utf-8")
    # An old compatibility journal is never substituted for a missing fresh receipt.
    (folder / "config/network-rules.json").write_text(json.dumps(successful(value)), "utf-8")
    output = run(tmp_path, write_request(tmp_path, value) + f"Read-Quest3DNetworkResult $r {7 if change == 'exit' else 0} | ConvertTo-Json -Depth 6")
    assert json.loads(output.stdout)["accepted"] is False


def test_success_requires_exact_receipt_and_child_exit(tmp_path):
    folder = installed(tmp_path)
    value = request(tmp_path, folder)
    Path(value["report_path"]).write_text(json.dumps(successful(value)), "utf-8")
    output = run(tmp_path, write_request(tmp_path, value) + "Read-Quest3DNetworkResult $r 0 | ConvertTo-Json -Depth 6")
    assert json.loads(output.stdout)["accepted"] is True


def test_uac_cancel_is_distinct_from_start_failure(tmp_path):
    output = run(tmp_path, """
try { throw [ComponentModel.Win32Exception]::new(1223) } catch { $cancelled=Test-Quest3DUacCancelled $_ }
try { throw [ComponentModel.Win32Exception]::new(5) } catch { $denied=Test-Quest3DUacCancelled $_ }
@{cancelled=$cancelled;denied=$denied} | ConvertTo-Json
""")
    assert json.loads(output.stdout) == dict(cancelled=True, denied=False)


def test_read_only_status_process_never_requests_elevation(tmp_path):
    folder = installed(tmp_path)
    value = request(tmp_path, folder, "status")
    output = run(tmp_path, write_request(tmp_path, value) + """
function Start-Process {
 param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
 return [pscustomobject]@{verb=$Verb;args=$ArgumentList;tracked=[bool]$PassThru}
}
Start-Quest3DNetworkRequest $r | ConvertTo-Json
""")
    launch = json.loads(output.stdout)
    assert not launch["verb"] and launch["tracked"] and "-Status" in launch["args"]


def ui_controller(folder: Path):
    # Load the actual UI functions and actual timer handler through its PS AST.
    # Dummy controls/processes avoid dialogs, admin operations and app execution.
    return f"""
Add-Type -AssemblyName System.Windows.Forms
$tokens=$null; $parseErrors=$null
$ast=[Management.Automation.Language.Parser]::ParseFile({q(ROOT/'scripts/release/install-ui.ps1')},[ref]$tokens,[ref]$parseErrors)
if ($parseErrors.Count) {{ throw 'UI parse failed.' }}
$uiScripts={q(ROOT/'scripts/release')}
$ast.FindAll({{param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst]}},$true) | ForEach-Object {{ . ([scriptblock]::Create($_.Extent.Text.Replace('$PSScriptRoot',([char]39+$uiScripts+[char]39)))) }}
$tick=$ast.Find({{param($node) $node -is [Management.Automation.Language.InvokeMemberExpressionAst] -and $node.Member.Extent.Text -eq 'Add_Tick'}},$true).Arguments[0].ScriptBlock.Extent.Text
$tickBody=[scriptblock]::Create($tick.Substring(1,$tick.Length-2))
$script:busy=$false; $script:installProcess=$null; $script:logFolder=$null; $script:networkRequest=$null
$script:pendingMaintenance=$null; $script:maintenanceRoot=$null; $script:maintenanceMode=$false
$script:requestedNetworkAction=$null
$script:adminStatusVerification=$false
$script:installedRoot={q(folder)}; $packageRoot={q(folder)}
$location=[pscustomobject]@{{Text={q(folder)};Enabled=$true}}
foreach ($name in @('install','browse','shortcuts','update','rollback','remove','launch','network','logs','close')) {{
 Set-Variable -Name $name -Value ([pscustomobject]@{{Enabled=$true;Text='';Checked=$false}})
}}
$status=[pscustomobject]@{{Text=''}}; $detail=[pscustomobject]@{{Text=''}}
$status | Add-Member ScriptMethod Refresh {{}}
$detail | Add-Member ScriptMethod Refresh {{}}
$progress=[pscustomobject]@{{Style=[Windows.Forms.ProgressBarStyle]::Blocks;Value=0}}
$timer=[pscustomobject]@{{Started=$false}}
$timer | Add-Member ScriptMethod Start {{$this.Started=$true}}
$timer | Add-Member ScriptMethod Stop {{$this.Started=$false}}
$script:launches=0
function Start-Quest3DNetworkRequest($Request) {{
 $script:launches++
 $p=[pscustomobject]@{{HasExited=$false;ExitCode=0}}
 foreach ($name in @('Refresh','WaitForExit','Dispose')) {{ $p | Add-Member ScriptMethod $name {{}} }}
 return $p
}}
$script:retirement=$null
function Start-Quest3DMaintenanceWorker($Action,$Root,$NetworkOperationId,$NetworkReceiptAction='remove') {{
 $script:retirement=@{{action=$Action;root=$Root;id=$NetworkOperationId;receipt_action=$NetworkReceiptAction}}
}}
$script:confirmations=0
function Confirm-Quest3DNetworkChange($Action) {{ $script:confirmations++; return $true }}
function Write-FixtureNetworkReceipt([bool]$Applied=$false,[bool]$Absent=$false,[bool]$Known=$true) {{
 $r=$script:networkRequest
 $outcome=if($r.action -eq 'status'){{'status'}}elseif($r.action -eq 'apply'){{'applied'}}else{{'removed'}}
 $state=if(!$Known){{'unknown'}}elseif($Applied){{'applied'}}elseif($Absent){{'absent'}}else{{'not_applied'}}
 @{{schema=1;operation_id=$r.operation_id;action=$r.action;program=$r.expected_program;success=$Known;verified_after=$Known;outcome=$outcome;partial=$false;requested_applied=($r.action -ne 'status' -or $Applied);known=$Known;apply_satisfied=$Applied;owned_rules_absent=$Absent;rules_state=$state;error=$null}} | ConvertTo-Json | Set-Content -LiteralPath $r.report_path
}}
"""


def compile_status_stub(tmp: Path, folder: Path, mode="absent"):
    """A real fixture exe emits bounded status receipts; never calls NetSecurity."""
    assembly = tmp / "status-probe.exe"
    source = r'''
using System;
using System.IO;
using System.Collections.Generic;
using System.Web.Script.Serialization;
public class StatusProbe {
 public static int Main(string[] args) {
  string operation=null, path=null;
  for(int i=0;i+1<args.Length;i++) {
   if(args[i]=="-OperationId") operation=args[i+1];
   if(args[i]=="-ReportPath") path=args[i+1];
  }
  if(path==null || operation==null) { Console.Error.WriteLine("fixture shell blocked before script execution"); return 9; }
  string root=new DirectoryInfo(Path.GetDirectoryName(path)).Parent.Parent.Parent.FullName;
  string mode=File.ReadAllText(Path.Combine(root,"fixture-status-mode.txt")).Trim();
  bool known=mode!="unknown", applied=mode=="present", absent=mode=="absent";
  var result=new Dictionary<string,object>();
  result["schema"]=1;result["operation_id"]=operation;result["action"]="status";result["outcome"]="status";
  result["program"]=Path.Combine(root,"artifacts","host","runtime-public","sunshine.exe");
  result["success"]=known;result["known"]=known;result["verified_after"]=known;result["partial"]=false;
  result["requested_applied"]=applied;result["apply_satisfied"]=applied;result["owned_rules_absent"]=absent;
  result["rules_state"]=known?(applied?"applied":"absent"):"unknown";result["error"]=null;
  File.WriteAllText(path,new JavaScriptSerializer().Serialize(result));
  return 0;
 }
}
'''
    run(tmp, "Add-Type -TypeDefinition @'\n" + source + "\n'@ -ReferencedAssemblies System.Web.Extensions -OutputType ConsoleApplication -OutputAssembly " + q(assembly))
    target = folder / ".tools/desktop/powershell/pwsh.exe"
    target.write_bytes(assembly.read_bytes())
    manifest_path = folder / "distribution-manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["files"][target.relative_to(folder).as_posix()] = dict(bytes=target.stat().st_size, sha256=sha(target.read_bytes()))
    manifest_path.write_text(json.dumps(manifest), "utf-8")
    owner_path = folder / "quest3d-install.json"
    owner = json.loads(owner_path.read_text("utf-8"))
    owner["package_manifest_sha256"] = sha(manifest_path.read_bytes())
    owner_path.write_text(json.dumps(owner), "utf-8")
    (folder / "fixture-status-mode.txt").write_text(mode, "ascii")


def test_ui_waits_locks_duplicate_actions_then_confirms_fresh_receipt(tmp_path):
    folder = installed(tmp_path)
    output = run(tmp_path, ui_controller(folder) + """
Start-Quest3DInstallerNetwork 'apply' $script:installedRoot $false
if (!$script:busy -or $launch.Enabled -or $network.Enabled -or $close.Enabled) { throw 'Active controls were not locked.' }
$id=$script:networkRequest.operation_id
Start-Quest3DInstallerNetwork 'apply' $script:installedRoot $false
& $tickBody
if ($script:launches -ne 1 -or !$script:busy) { throw 'Duplicate action or premature completion.' }
Write-FixtureNetworkReceipt $false $true
$script:installProcess.HasExited=$true
& $tickBody
if ($script:networkRequest.action -ne 'apply' -or !$script:busy -or $script:launches -ne 2) { throw 'Needed administrator task was not tracked.' }
Write-FixtureNetworkReceipt
$script:installProcess.HasExited=$true
& $tickBody
if ($script:busy -or !$close.Enabled -or $progress.Value -ne 6) { throw 'Verified task did not unlock.' }
Write-Output 'tracked completion passed'
""")
    assert "tracked completion passed" in output.stdout


@pytest.mark.parametrize("action", ["apply", "remove"])
def test_exact_existing_state_skips_confirmation_and_admin_request(tmp_path, action):
    folder = installed(tmp_path)
    body = ui_controller(folder) + f"""
Start-Quest3DInstallerNetwork '{action}' $script:installedRoot ${'true' if action == 'remove' else 'false'}
Write-FixtureNetworkReceipt ${'true' if action == 'apply' else 'false'} ${'true' if action == 'remove' else 'false'}
$script:installProcess.HasExited=$true
& $tickBody
@{{launches=$script:launches;confirmations=$script:confirmations;retirement=$script:retirement;busy=$script:busy}} | ConvertTo-Json -Depth 5
"""
    result = json.loads(run(tmp_path, body).stdout)
    assert result["launches"] == 1 and result["confirmations"] == 0
    if action == "remove":
        assert result["retirement"]["receipt_action"] == "status" and result["busy"]
    else:
        assert result["retirement"] is None and not result["busy"]


@pytest.mark.parametrize("case", ["absent", "already", "trusted_unknown", "invalid_unknown", "failed", "cancel"])
def test_apply_button_only_requests_needed_windows_approval(tmp_path, case):
    folder = installed(tmp_path)
    body = "$realNetworkStart=(Get-Item Function:Start-Quest3DNetworkRequest).ScriptBlock\n" + ui_controller(folder)
    body += f"""
# Use the actual verified launcher while stubbing only its OS process boundary.
Set-Item -LiteralPath Function:Start-Quest3DNetworkRequest -Value $realNetworkStart
$script:uacRequests=0; $script:statusRequests=0; $script:inlineAtUac=$null
function Start-Process {{
 param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
 if ($Verb -eq 'RunAs') {{
  $script:uacRequests++
  $script:inlineAtUac=@{{status=$status.Text;detail=$detail.Text}}
  if ('{case}' -eq 'cancel') {{ throw [ComponentModel.Win32Exception]::new(1223) }}
 }} else {{ $script:statusRequests++ }}
 $p=[pscustomobject]@{{HasExited=$false;ExitCode=0}}
 foreach ($name in @('Refresh','WaitForExit','Dispose')) {{ $p | Add-Member ScriptMethod $name {{}} }}
 return $p
}}
Start-Quest3DInstallerNetwork 'apply' $script:installedRoot $false
Write-FixtureNetworkReceipt ${'true' if case in {'already', 'invalid_unknown'} else 'false'} ${'true' if case in {'absent', 'failed', 'cancel'} else 'false'} ${'false' if case in {'trusted_unknown', 'invalid_unknown'} else 'true'}
$script:installProcess.HasExited=$true
& $tickBody
"""
    if case in {"absent", "trusted_unknown", "failed"}:
        body += f"""
Write-FixtureNetworkReceipt
$script:installProcess.HasExited=$true
$script:installProcess.ExitCode={7 if case == 'failed' else 0}
& $tickBody
"""
    body += "@{confirmations=$script:confirmations;uac=$script:uacRequests;queries=$script:statusRequests;inline=$script:inlineAtUac;status=$status.Text;busy=$script:busy;retirement=$script:retirement} | ConvertTo-Json -Depth 5\n"
    result = json.loads(run(tmp_path, body).stdout)
    assert result["confirmations"] == 0 and result["queries"] == 1
    assert result["uac"] == (0 if case in {"already", "invalid_unknown"} else 1)
    assert not result["busy"] and result["retirement"] is None
    if case == "trusted_unknown":
        assert "관리자" in result["inline"]["status"] and "조회 미완료" in result["inline"]["detail"]
    if case in {"cancel", "failed", "invalid_unknown"}:
        assert "완료" not in result["status"] or "미완료" in result["status"]
    assert (folder / "user-notes.txt").read_bytes() == b"fixture user data"


def test_ui_construction_keeps_network_scope_tooltip_without_starting_install(tmp_path):
    output = run(tmp_path, "& " + q(ROOT / "scripts/release/install-ui.ps1") + " -SelfTest\n")
    assert "Installer UI construction passed; no installation started." in output.stdout


def test_app_retirement_worker_runs_in_original_user_context(tmp_path):
    folder = installed(tmp_path)
    output = run(tmp_path, ui_controller(folder) + """
$function=$ast.Find({param($n) $n -is [Management.Automation.Language.FunctionDefinitionAst] -and $n.Name -eq 'Start-Quest3DMaintenanceWorker'},$true)
. ([scriptblock]::Create($function.Extent.Text.Replace('$PSScriptRoot',([char]39+$uiScripts+[char]39))))
function Start-Process {
 param($FilePath,$ArgumentList,$Verb,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError)
 $script:launch=@{verb=$Verb;args=$ArgumentList;tracked=[bool]$PassThru}
 return [pscustomobject]@{HasExited=$false;ExitCode=0}
}
$script:logFolder=New-Quest3DInstallerLog
Start-Quest3DMaintenanceWorker 'uninstall' $script:installedRoot '00000000000000000000000000000000' 'status'
$script:launch | ConvertTo-Json
""")
    launched = json.loads(output.stdout)
    assert not launched["verb"] and launched["tracked"]
    assert '-NetworkReceiptAction "status"' in launched["args"]
    assert '-RemoveNetworkRules' not in launched["args"]


@pytest.mark.parametrize("case", ["cancel", "decline", "invalid_unknown", "trusted_unknown", "failed", "stale", "success"])
def test_ui_retirement_only_follows_this_successful_admin_cleanup(tmp_path, case):
    folder = installed(tmp_path)
    body = ui_controller(folder)
    if case == "cancel":
        body += "function Start-Quest3DNetworkRequest($Request) { if($Request.action -ne 'status'){throw [ComponentModel.Win32Exception]::new(1223)}; $p=[pscustomobject]@{HasExited=$false;ExitCode=0}; foreach($n in @('Refresh','WaitForExit','Dispose')){$p|Add-Member ScriptMethod $n {}};return $p }\n"
    if case == "decline":
        body += "function Confirm-Quest3DNetworkChange($Action) { $script:confirmations++; return $false }\n"
    body += "Start-Quest3DInstallerNetwork 'remove' $script:installedRoot $true\n"
    body += f"Write-FixtureNetworkReceipt ${'false' if case == 'trusted_unknown' else 'true'} $false ${'false' if case in {'invalid_unknown', 'trusted_unknown'} else 'true'}\n$script:installProcess.HasExited=$true\n& $tickBody\n"
    if case not in {"cancel", "decline", "invalid_unknown"}:
        body += f"""
Write-FixtureNetworkReceipt
if ('{case}' -eq 'stale') {{ $wrong=Get-Content -LiteralPath $script:networkRequest.report_path -Raw | ConvertFrom-Json; $wrong.operation_id='00000000000000000000000000000000'; $wrong | ConvertTo-Json | Set-Content -LiteralPath $script:networkRequest.report_path }}
$script:installProcess.HasExited=$true
$script:installProcess.ExitCode={7 if case == 'failed' else 0}
& $tickBody
"""
    body += "@{retirement=$script:retirement;busy=$script:busy;confirmations=$script:confirmations} | ConvertTo-Json -Depth 5\n"
    result = json.loads(run(tmp_path, body).stdout)
    if case in {"success", "trusted_unknown"}:
        assert result["retirement"]["action"] == "uninstall" and result["busy"]
    else:
        assert result["retirement"] is None and not result["busy"]
    if case == "invalid_unknown":
        assert result["confirmations"] == 0
    else:
        assert result["confirmations"] == 1
    assert (folder / "user-notes.txt").read_bytes() == b"fixture user data"


@pytest.mark.parametrize("action", ["remove", "status"])
def test_uninstall_rejects_unverified_cleanup_and_preserves_personal_data(tmp_path, action):
    folder = installed(tmp_path)
    value = request(tmp_path, folder, action)
    body = f"& {q(ROOT/'scripts/release/uninstall.ps1')} -Root {q(folder)} -NoSystemShortcuts -NetworkOperationId '{value['operation_id']}' -NetworkReceiptAction '{action}'"
    run(tmp_path, body, expected=1)
    assert folder.is_dir() and (folder / "config/desktop.json").exists()
    compile_status_stub(tmp_path, folder)
    Path(value["report_path"]).write_text(json.dumps(successful(value)), "utf-8")
    report_path = tmp_path / "retirement.json"
    run(tmp_path, body + " -ReportPath " + q(report_path))
    report = json.loads(report_path.read_text("utf-8-sig"))
    assert report["network_rules_removed"] == (action == "remove") and report["network_operation_id"] == value["operation_id"]
    assert report["network_cleanup_not_needed"] == (action == "status")
    archive = Path(report["archive"])
    assert not folder.exists()
    assert (archive / "config/desktop.json").read_bytes() == b"fixture personal settings"
    assert (archive / "user-notes.txt").read_bytes() == b"fixture user data"


def test_expired_retirement_receipt_never_launches_readback_or_moves_app(tmp_path):
    folder = installed(tmp_path)
    value = request(tmp_path, folder, "remove")
    receipt = Path(value["report_path"])
    receipt.write_text(json.dumps(successful(value)), "utf-8")
    os.utime(receipt, (time.time() - 600, time.time() - 600))
    output = run(tmp_path, f"Use-Quest3DNetworkRetirementReceipt {q(folder)} '{value['operation_id']}' 'remove'", expected=1)
    assert "expired" in output.stdout + output.stderr
    assert (folder / "user-notes.txt").exists() and not receipt.with_name("retirement-consumed.json").exists()


def test_retirement_receipt_is_single_use_even_without_moving_app(tmp_path):
    folder = installed(tmp_path)
    compile_status_stub(tmp_path, folder)
    value = request(tmp_path, folder, "remove")
    receipt = Path(value["report_path"])
    receipt.write_text(json.dumps(successful(value)), "utf-8")
    body = f"Use-Quest3DNetworkRetirementReceipt {q(folder)} '{value['operation_id']}' 'remove' | ConvertTo-Json"
    result = json.loads(run(tmp_path, body).stdout)
    assert result["readback_known"] and result["readback_owned_rules_absent"]
    output = run(tmp_path, body, expected=1)
    assert "already used" in output.stdout + output.stderr
    assert (folder / "user-notes.txt").read_bytes() == b"fixture user data"


@pytest.mark.parametrize("action,mode,allowed", [("status", "present", False), ("remove", "present", False), ("status", "unknown", False), ("remove", "unknown", True)])
def test_current_readback_controls_retirement_without_extra_admin_prompt(tmp_path, action, mode, allowed):
    folder = installed(tmp_path)
    compile_status_stub(tmp_path, folder, mode)
    value = request(tmp_path, folder, action)
    Path(value["report_path"]).write_text(json.dumps(successful(value)), "utf-8")
    body = f"Use-Quest3DNetworkRetirementReceipt {q(folder)} '{value['operation_id']}' '{action}' | ConvertTo-Json"
    output = run(tmp_path, body, expected=0 if allowed else 1)
    if allowed:
        assert json.loads(output.stdout)["readback_known"] is False
    assert (folder / "user-notes.txt").read_bytes() == b"fixture user data"


def launcher_fixture(tmp: Path, *, body="param([switch]$SelfTest)\nWrite-Output 'fixture GUI finished'\n"):
    folder = tmp / "package with spaces"
    release = folder / "scripts/release"
    release.mkdir(parents=True)
    (release / "installer-launcher.ps1").write_bytes((ROOT / "scripts/release/installer-launcher.ps1").read_bytes())
    child = release / "install-ui.ps1"
    child.write_text(body, "utf-8-sig")
    (folder / "distribution-manifest.json").write_text(json.dumps(dict(schema=1, files={"scripts/release/install-ui.ps1": dict(bytes=child.stat().st_size, sha256=sha(child.read_bytes()))})), "utf-8")
    return release / "installer-launcher.ps1", child


@pytest.mark.parametrize("failure", ["child", "tamper", "missing"])
def test_launcher_reports_bootstrap_failure_without_changing_security(tmp_path, failure):
    launcher, child = launcher_fixture(tmp_path, body="throw 'fixture startup failed'\n" if failure == "child" else "Write-Output 'fixture'\n")
    if failure == "tamper":
        child.write_text("throw 'changed child must not run'", "utf-8-sig")
    if failure == "missing":
        child.rename(child.with_suffix(".preserved"))
    log = tmp_path / "temporary/Quest3D-Start-fixture.details.log"
    output = run(tmp_path, f"& {q(SHELL)} -NoProfile -ExecutionPolicy Bypass -File {q(launcher)} -Target pc -NoDialog -SelfTest -LogPath {q(log)}\nexit $LASTEXITCODE", expected=1)
    assert log.exists() and log.stat().st_size
    if failure == "child":
        assert "fixture startup failed" in log.read_text("utf-8")
    else:
        assert "missing or changed" in log.read_text("utf-8")
        assert "changed child must not run" not in output.stdout


def test_launcher_success_and_untrusted_log_path(tmp_path):
    launcher, child = launcher_fixture(tmp_path)
    log = tmp_path / "temporary/Quest3D-Start-fixture.details.log"
    run(tmp_path, f"& {q(SHELL)} -NoProfile -ExecutionPolicy Bypass -File {q(launcher)} -Target pc -NoDialog -SelfTest -LogPath {q(log)}\nexit $LASTEXITCODE")
    assert "fixture GUI finished" in log.read_text("utf-8")
    outside = tmp_path / "outside.txt"
    run(tmp_path, f"& {q(SHELL)} -NoProfile -ExecutionPolicy Bypass -File {q(launcher)} -Target pc -NoDialog -SelfTest -LogPath {q(outside)}\nexit $LASTEXITCODE", expected=1)
    assert not outside.exists()


def test_cmd_reports_script_policy_failure_and_preserves_gpo():
    spec = importlib.util.spec_from_file_location("bundle_permissions", ROOT / "scripts/release/build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for target in ("pc", "quest"):
        command = module.installer_launcher_cmd(target).decode("ascii")
        assert 'installer-launcher.ps1' in command and 'if not errorlevel 1' in command and 'pause' in command
        assert 'Set-ExecutionPolicy' not in command and 'start ""' not in command
        assert f'-Target {target}' in command


def test_actual_cmd_keeps_initial_shell_failure_visible_and_logged(tmp_path):
    folder = installed(tmp_path)
    compile_status_stub(tmp_path, folder)
    launch_folder = tmp_path / "launcher with spaces"
    launch_folder.mkdir()
    # cmd searches its current folder first. This fixture shell fails before any
    # PowerShell script can execute, covering the batch-only diagnostic layer.
    (launch_folder / "powershell.exe").write_bytes((folder / ".tools/desktop/powershell/pwsh.exe").read_bytes())
    spec = importlib.util.spec_from_file_location("bundle_cmd_probe", ROOT / "scripts/release/build_bundle.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    command = launch_folder / "Install-fixture.cmd"
    command.write_bytes(module.installer_launcher_cmd("pc"))
    temporary = tmp_path / "batch-temporary"
    temporary.mkdir()
    environment = {k: v for k, v in os.environ.items() if k.casefold() not in {"temp", "tmp"}}
    environment.update(TEMP=str(temporary), TMP=str(temporary))
    cmd = Path(os.environ["SystemRoot"]) / "System32/cmd.exe"
    checked = subprocess.run([str(cmd), "/d", "/c", str(command)], input="\n", capture_output=True,
                             text=True, encoding="utf-8", errors="replace", cwd=launch_folder,
                             env=environment, timeout=30)
    assert checked.returncode == 1, checked.stdout + checked.stderr
    assert "installer did not start" in checked.stdout and "fixture shell blocked" in checked.stdout
    logs = list(temporary.glob("Quest3D-Start-*.log"))
    assert len(logs) == 1 and "fixture shell blocked" in logs[0].read_text("utf-8")

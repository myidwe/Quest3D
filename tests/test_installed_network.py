"""Execute the actual PowerShell policy against in-memory firewall cmdlets.

No Windows firewall API is invoked and no administrator permission is requested.
"""
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PWSH = ROOT / '.tools/desktop/powershell/pwsh.exe'
PWSH = BUNDLED_PWSH if BUNDLED_PWSH.is_file() else Path(shutil.which('pwsh') or ROOT / '.missing-powershell/pwsh.exe')
WINDOWS_PS = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe'

DRIVER = r'''
param([string]$Policy, [string]$Scenario)
$ErrorActionPreference = 'Stop'
. $Policy
$Program = 'C:\Apps\Quest3D\host\sunshine.exe'
if($Scenario -eq 'long-program'){$Program='C:\'+('p'*180)+'\sunshine.exe'}
$Key = '012345ABCDEF'
$Description = "Quest3D installation $Key"
$Specs = @(
  @{name="Quest3D-$Key-TCP";protocol='TCP';ports=@(47984,47989,48010)},
  @{name="Quest3D-$Key-UDP";protocol='UDP';ports=@(47998,47999,48000)}
)
$script:Rows = @{}
$script:Actions = [Collections.Generic.List[string]]::new()
foreach ($Spec in $Specs) {
  $script:Rows[$Spec.name] = [pscustomobject]@{Name=$Spec.name;Description=$Description;Program=$Program;
    Direction='Inbound';Action='Allow';Enabled='True';Profile='Private';EdgeTraversalPolicy='Block';
    Protocol=$(if($Spec.protocol -eq 'TCP'){'6'}else{'UDP'});LocalPort=$Spec.ports;RemotePort='Any';
    LocalAddress='Any';RemoteAddress='LocalSubnet'}
}
$script:Queries = 0
function Get-NetFirewallRule {
  [CmdletBinding()]param([string]$PolicyStore)
  $script:Queries++
  if($Scenario -in @('query-failure','query-failure-remove')){throw 'Simulated provider access denial'}
  if($Scenario -eq 'post-query-failure' -and $script:Queries -gt 1){throw 'Simulated post-operation query denial'}
  if($Scenario -eq 'post-verification-missing' -and $script:Queries -eq 2){$script:Rows.Remove($Specs[1].name)}
  if($Scenario -eq 'rollback-conflict' -and $script:Queries -eq 2){$script:Rows[$Specs[0].name].Description='changed by another application'}
  @($script:Rows.Values)
}
function Get-NetFirewallApplicationFilter {
  [CmdletBinding()]param([Parameter(ValueFromPipeline)]$InputObject)
  process { [pscustomobject]@{Program=$InputObject.Program} }
}
function Get-NetFirewallPortFilter {
  [CmdletBinding()]param([Parameter(ValueFromPipeline)]$InputObject)
  process { [pscustomobject]@{Protocol=$InputObject.Protocol;LocalPort=$InputObject.LocalPort;RemotePort=$InputObject.RemotePort} }
}
function Get-NetFirewallAddressFilter {
  [CmdletBinding()]param([Parameter(ValueFromPipeline)]$InputObject)
  process { [pscustomobject]@{LocalAddress=$InputObject.LocalAddress;RemoteAddress=$InputObject.RemoteAddress} }
}
function Remove-NetFirewallRule {
  [CmdletBinding()]param([Parameter(ValueFromPipeline)]$InputObject, [string]$Name)
  process {
    if(!$Name){$Name=$InputObject.Name}
    $script:Actions.Add('remove:'+$Name)
    if($Scenario -eq 'deletion-failure' -and $Name -eq $Specs[1].name){throw 'Simulated UDP removal failure'}
    $script:Rows.Remove($Name)
  }
}
function New-NetFirewallRule {
  [CmdletBinding()]param($PolicyStore,$Name,$DisplayName,$Description,$Group,$Direction,$Action,$Enabled,$Profile,$Program,$Protocol,$LocalPort,$RemoteAddress,$EdgeTraversalPolicy)
  $script:Actions.Add('create:'+$Name)
  # MS-FASP FW_RULE / INetFwRule reject the literal pipe even in short strings.
  if(!$Description -or $Description.Length -ge 10000 -or $Description.Contains('|')){throw 'Invalid firewall description'}
  if($Scenario -eq 'creation-failure' -and $Protocol -eq 'UDP'){throw 'Simulated UDP creation failure'}
  $script:Rows[$Name] = [pscustomobject]@{Name=$Name;Description=$Description;Program=$Program;
    Direction=$Direction;Action=$Action;Enabled=[string]$Enabled;Profile=$Profile;EdgeTraversalPolicy=$EdgeTraversalPolicy;
    Protocol=$Protocol;LocalPort=$LocalPort;RemotePort='Any';LocalAddress='Any';RemoteAddress=$RemoteAddress}
}
$Delete = $Scenario -in @('conflict-remove','remove','remove-again','deletion-failure','query-failure-remove','foreign-program-remove')
switch($Scenario) {
  'conflict-remove' {$script:Rows[$Specs[1].name].Description='owned by another app'}
  'conflict-create' {$script:Rows.Remove($Specs[0].name);$script:Rows[$Specs[1].name].Description='owned by another app'}
  'changed-policy' {$script:Rows[$Specs[1].name].Profile='Public'}
  'foreign-program-create' {$script:Rows.Remove($Specs[0].name);$script:Rows[$Specs[1].name].Program='C:\Apps\Another\sunshine.exe'}
  'foreign-program-remove' {$script:Rows[$Specs[1].name].Program='C:\Apps\Another\sunshine.exe'}
  'legacy-description' {$script:Rows[$Specs[1].name].Description="Quest3D installation $Key | $Program"}
  'create' {$script:Rows.Clear()}
  'long-program' {$script:Rows.Clear()}
  'missing-rule' {$script:Rows.Remove($Specs[1].name)}
  'creation-failure' {$script:Rows.Clear()}
  'post-query-failure' {$script:Rows.Clear()}
  'post-verification-missing' {$script:Rows.Clear()}
  'rollback-conflict' {$script:Rows.Clear()}
  'remove-again' {$script:Rows.Clear()}
}
try {
  $Result = Update-Quest3DInstalledNetworkRules $Program $Key $Specs $Delete
  $PolicyStatus = 'ok'; $ErrorText = $null
} catch { $PolicyStatus='error';$ErrorText=$_.Exception.Message;$Result=$_.Exception.Data['Quest3DNetworkResult'] }
$Retry=$null
if($Scenario -eq 'deletion-failure') {
  $Scenario='retry';$Retry=Update-Quest3DInstalledNetworkRules $Program $Key $Specs $true
}
@{status=$PolicyStatus;error=$ErrorText;actions=@($script:Actions.ToArray());remaining=@($script:Rows.Keys | Sort-Object);rows=@($script:Rows.Values);result=$Result;retry=$Retry;queries=$script:Queries} | ConvertTo-Json -Depth 8 -Compress
'''


@pytest.fixture(scope='module', params=[PWSH, WINDOWS_PS], ids=['PS7', 'WindowsPS51'])
def run_policy(tmp_path_factory, request):
    shell = request.param
    if not shell.is_file():
        pytest.skip('Bundled or installed PowerShell runtime required for the isolated policy tests')
    script = tmp_path_factory.mktemp('network-policy') / 'driver.ps1'
    script.write_text(DRIVER, encoding='utf-8')
    def run(scenario):
        completed = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-File', str(script),
            '-Policy', str(ROOT/'native/host/configure-installed-network.ps1'), '-Scenario', scenario],
            capture_output=True, text=True, encoding='utf-8', timeout=20)
        assert completed.returncode == 0, completed.stderr
        return json.loads(completed.stdout)
    return run


TCP, UDP = 'Quest3D-012345ABCDEF-TCP', 'Quest3D-012345ABCDEF-UDP'


def test_repeat_apply_preserves_both_correct_rules_without_mutations(run_policy):
    result = run_policy('idempotent')
    assert result['status'] == 'ok' and result['actions'] == []
    assert sorted(result['result']['kept']) == [TCP, UDP]
    assert result['result']['verified_after'] and result['result']['requested_applied']


@pytest.mark.parametrize('scenario', ['create', 'long-program'])
def test_fresh_apply_uses_valid_description_and_preserves_program_and_scope(run_policy, scenario):
    result = run_policy(scenario)
    assert result['status'] == 'ok' and result['actions'] == ['create:'+TCP, 'create:'+UDP]
    assert result['result']['verified_after'] and result['result']['requested_applied']
    assert sorted(result['result']['created']) == [TCP, UDP]
    assert result['result']['rolled_back'] == [] and not result['result']['partial']
    expected_program = r'C:\Apps\Quest3D\host\sunshine.exe' if scenario == 'create' else 'C:\\'+'p'*180+r'\sunshine.exe'
    for row in result['rows']:
        assert row['Description'] == 'Quest3D installation 012345ABCDEF'
        assert len(row['Description']) == 33 and '|' not in row['Description']
        assert row['Program'] == expected_program
        assert (row['Direction'], row['Action'], row['Enabled'], row['Profile'], row['RemoteAddress'], row['EdgeTraversalPolicy']) == (
            'Inbound', 'Allow', 'True', 'Private', 'LocalSubnet', 'Block')
        assert row['LocalAddress'] == 'Any' and row['RemotePort'] == 'Any'
        assert row['LocalPort'] == ([47984, 47989, 48010] if row['Protocol'] == 'TCP' else [47998, 47999, 48000])


@pytest.mark.parametrize('scenario', ['foreign-program-create', 'foreign-program-remove', 'legacy-description'])
def test_short_description_never_adopts_or_mutates_foreign_program_rules(run_policy, scenario):
    result = run_policy(scenario)
    assert result['status'] == 'error' and result['actions'] == []
    assert UDP in result['remaining']
    if scenario != 'foreign-program-create':
        assert TCP in result['remaining']


@pytest.mark.parametrize('scenario', ['conflict-remove', 'conflict-create', 'changed-policy'])
def test_all_rules_are_checked_before_creating_or_removing_any_rule(run_policy, scenario):
    result = run_policy(scenario)
    assert result['status'] == 'error' and result['actions'] == []
    assert UDP in result['remaining']
    if scenario != 'conflict-create':
        assert TCP in result['remaining'], 'A later UDP conflict must preserve the TCP rule'


def test_partial_existing_install_adds_only_missing_rule(run_policy):
    result = run_policy('missing-rule')
    assert result['status'] == 'ok' and result['actions'] == ['create:'+UDP]
    assert result['result']['kept'] == [TCP]


def test_creation_failure_rolls_back_only_this_attempt_and_retains_receipt(run_policy):
    result = run_policy('creation-failure')
    assert result['status'] == 'error'
    assert result['actions'] == ['create:'+TCP, 'create:'+UDP, 'remove:'+TCP]
    assert result['remaining'] == []
    assert result['result']['created'] == [TCP] and result['result']['rollback_errors'] == []
    assert result['result']['rolled_back'] == [TCP]


@pytest.mark.parametrize('scenario,actions', [('remove', ['remove:'+TCP, 'remove:'+UDP]), ('remove-again', [])])
def test_remove_is_repeatable_and_only_affects_owned_rules(run_policy, scenario, actions):
    result = run_policy(scenario)
    assert result['status'] == 'ok' and result['actions'] == actions
    assert result['remaining'] == []
    assert result['result']['verified_after']


@pytest.mark.parametrize('scenario', ['query-failure', 'query-failure-remove'])
def test_provider_query_denial_is_never_treated_as_missing_rules(run_policy, scenario):
    result = run_policy(scenario)
    assert result['status'] == 'error' and result['actions'] == []
    assert sorted(result['remaining']) == [TCP, UDP]
    assert not result['result']['verified_after']
    assert not result['result']['requested_applied']


def test_partial_delete_is_reported_and_retry_only_removes_remaining_rule(run_policy):
    result = run_policy('deletion-failure')
    assert result['status'] == 'error'
    assert result['result']['removed'] == [TCP]
    assert result['result']['partial'] and not result['result']['verified_after']
    assert result['result']['actual_rules'] == [
        {'name': TCP, 'present': False, 'scope_verified': False},
        {'name': UDP, 'present': True, 'scope_verified': True},
    ]
    assert result['retry']['removed'] == [UDP] and result['retry']['verified_after']
    assert result['remaining'] == []


def test_unreadable_post_state_cannot_report_success_or_safe_rollback(run_policy):
    result = run_policy('post-query-failure')
    assert result['status'] == 'error' and not result['result']['verified_after']
    assert result['result']['partial']
    assert sorted(result['result']['rollback_errors']) == [TCP, UDP]
    assert sorted(result['remaining']) == [TCP, UDP]


def test_post_apply_missing_rule_causes_verification_failure_and_rollback(run_policy):
    result = run_policy('post-verification-missing')
    assert result['status'] == 'error' and result['remaining'] == []
    assert not result['result']['requested_applied']
    assert sorted(result['result']['rolled_back']) == [TCP, UDP]


def test_rollback_preserves_rule_whose_owner_changed_after_creation(run_policy):
    result = run_policy('rollback-conflict')
    assert result['status'] == 'error' and result['remaining'] == [TCP]
    assert result['result']['rollback_errors'] == [TCP]
    assert result['result']['partial']


def ps_quote(value):
    return "'" + str(value).replace("'", "''") + "'"


def run_body(tmp_path, body, *, shell=PWSH):
    if not shell.is_file():
        pytest.skip('Actual Windows PowerShell runtime required')
    script = tmp_path / 'isolated-body.ps1'
    script.write_text("$ErrorActionPreference='Stop'\n. " + ps_quote(ROOT / 'native/host/configure-installed-network.ps1') + "\n" + body, 'utf-8-sig')
    environment = dict(os.environ)
    # Python may inherit PS7-only module paths from the developer's shell.
    # Let each isolated PowerShell select its own built-in modules instead.
    environment.pop('PSModulePath', None)
    result = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-File', str(script)],
                            capture_output=True, text=True, encoding='utf-8', timeout=25, env=environment)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


PINNED_HOST = '77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63'
PUBLIC_HOST = '86eb2ee5e3177a892f15ecd5ba869b27d3e1b9131848b1adacf9a25301767d42'


@pytest.fixture
def installation(tmp_path):
    """No executable is launched. Only the host checksum oracle is simulated."""
    folder = tmp_path / 'installed'
    files = {}
    contents = {
        'native/host/configure-installed-network.ps1': (ROOT / 'native/host/configure-installed-network.ps1').read_bytes(),
        '.tools/desktop/powershell/pwsh.exe': b'non-executable PowerShell fixture',
        'artifacts/host/runtime-public/sunshine.exe': b'non-executable host fixture',
        'config/distribution.json': json.dumps(dict(schema=1, host_runtime='artifacts/host/runtime-public', host_sha256=PINNED_HOST)).encode(),
    }
    for name, data in contents.items():
        path = folder / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        files[name] = dict(bytes=len(data), sha256=PINNED_HOST if name.endswith('/sunshine.exe') else hashlib.sha256(data).hexdigest())
    manifest = folder / 'distribution-manifest.json'
    manifest.write_text(json.dumps(dict(schema=1, release='fixture', files=files)), 'utf-8')
    owner = folder / 'quest3d-install.json'
    owner.write_text(json.dumps(dict(product='Quest3D Desktop', completed=True, release='fixture',
                                    package_manifest_sha256=hashlib.sha256(manifest.read_bytes()).hexdigest())), 'utf-8')
    return folder


CHECKSUM_ORACLE = r'''
# Windows PS5 exports Get-FileHash as a function, unlike PS7's cmdlet. Avoid
# recursively resolving that shadowed function: hash all other real files with
# the same SHA256 stream primitive. Only the non-executable host is simulated.
function Get-FileHash {
  [CmdletBinding()]param([string]$LiteralPath,[string]$Algorithm)
  if($LiteralPath.EndsWith('\sunshine.exe')){return [pscustomobject]@{Hash=$Script:HostDigest}}
  if($Algorithm -ne 'SHA256'){throw 'This fixture preserves exact SHA256 file checks only.'}
  $hash=[Security.Cryptography.SHA256]::Create();$stream=[IO.File]::OpenRead($LiteralPath)
  try{return [pscustomobject]@{Hash=([BitConverter]::ToString($hash.ComputeHash($stream))).Replace('-','')}}
  finally{$stream.Dispose();$hash.Dispose()}
}
function Get-NetFirewallRule { throw 'A validation test must not query or change system firewall.' }
function New-NetFirewallRule { throw 'A validation test must not change system firewall.' }
function Remove-NetFirewallRule { throw 'A validation test must not change system firewall.' }
'''


def validate_install(tmp_path, installation, digest=PINNED_HOST):
    return run_body(tmp_path, "$Script:HostDigest=" + ps_quote(digest) + '\n' + CHECKSUM_ORACLE +
                    '\ntry {$item=Get-Quest3DNetworkInstallation ' + ps_quote(installation) +
                    ";@{ok=$true;program=$item.program;rules=$item.rules}|ConvertTo-Json -Depth 6} catch {@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json}")


def test_installed_owner_and_required_file_hashes_are_checked_without_network(tmp_path, installation):
    result = validate_install(tmp_path, installation)
    assert result['ok']
    assert result['program'] == str(installation / 'artifacts/host/runtime-public/sunshine.exe')
    assert [rule['ports'] for rule in result['rules']] == [[47984, 47989, 48010], [47998, 47999, 48000]]


def test_actual_installer_uppercase_manifest_hash_is_accepted(tmp_path, installation):
    path = installation / 'quest3d-install.json'
    owner = json.loads(path.read_text()); owner['package_manifest_sha256'] = owner['package_manifest_sha256'].upper()
    path.write_text(json.dumps(owner))
    assert validate_install(tmp_path, installation)['ok']


def select_install_host(installation, pin, *, manifest_pin=None):
    """Rebind only an isolated non-executable fixture and its owned manifest."""
    config_path = installation/'config/distribution.json'
    config = json.loads(config_path.read_text())
    config['host_sha256'] = pin
    config_path.write_text(json.dumps(config))
    manifest_path = installation/'distribution-manifest.json'
    manifest = json.loads(manifest_path.read_text())
    manifest['files']['config/distribution.json'] = dict(bytes=config_path.stat().st_size,
        sha256=hashlib.sha256(config_path.read_bytes()).hexdigest())
    manifest['files']['artifacts/host/runtime-public/sunshine.exe']['sha256'] = manifest_pin or pin
    manifest_path.write_text(json.dumps(manifest))
    owner_path = installation/'quest3d-install.json'
    owner = json.loads(owner_path.read_text())
    owner['package_manifest_sha256'] = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    owner_path.write_text(json.dumps(owner))


@pytest.mark.parametrize('shell', [PWSH, WINDOWS_PS], ids=['PS7', 'WindowsPS51'])
@pytest.mark.parametrize('selected,actual,ok', [(PINNED_HOST,PINNED_HOST,True),
    (PUBLIC_HOST,PUBLIC_HOST,True), (PUBLIC_HOST,PINNED_HOST,False),
    (PINNED_HOST,PUBLIC_HOST,False), ('0'*64,'0'*64,False)])
def test_network_action_selects_only_reviewed_exact_binary_before_os_query(tmp_path, installation, shell, selected, actual, ok):
    select_install_host(installation, selected)
    result = run_body(tmp_path, "$Script:HostDigest=" + ps_quote(actual) + '\n' + CHECKSUM_ORACLE +
        '\ntry {$item=Get-Quest3DNetworkInstallation '+ps_quote(installation)+
        ";@{ok=$true;program=$item.program}|ConvertTo-Json} catch {@{ok=$false;error=$_.Exception.Message}|ConvertTo-Json}", shell=shell)
    assert result['ok'] is ok, result
    if not ok:
        assert 'must not query' not in result['error']


def test_public_network_action_rejects_historical_payload_manifest_even_with_public_config(tmp_path, installation):
    select_install_host(installation, PUBLIC_HOST, manifest_pin=PINNED_HOST)
    result = validate_install(tmp_path, installation, PUBLIC_HOST)
    assert not result['ok'] and 'checksum mismatch' in result['error']


def test_python_and_powershell_product_approve_the_same_exact_two_host_versions():
    import re
    from quest3d.desktop_backend import APPROVED_HOST_SHAS
    policy = (ROOT/'native/host/configure-installed-network.ps1').read_text('utf-8')
    actual = set(re.findall(r"'([0-9a-f]{64})'", policy))
    assert actual == APPROVED_HOST_SHAS == frozenset({PINNED_HOST, PUBLIC_HOST})


@pytest.mark.parametrize('failure', ['owner', 'helper', 'pwsh', 'host', 'payload-path'])
def test_changed_or_unowned_installation_is_refused_before_firewall_query(tmp_path, installation, failure):
    digest = PINNED_HOST
    if failure == 'owner':
        p = installation / 'quest3d-install.json'
        value = json.loads(p.read_text()); value['completed'] = False; p.write_text(json.dumps(value))
    elif failure in {'helper', 'pwsh'}:
        p = installation / ('native/host/configure-installed-network.ps1' if failure == 'helper' else '.tools/desktop/powershell/pwsh.exe')
        p.write_bytes(b'changed after installation')
    elif failure == 'host':
        digest = '0' * 64
    else:
        p = installation / 'distribution-manifest.json'
        value = json.loads(p.read_text()); value['files']['../escape'] = dict(bytes=0, sha256='0' * 64)
        p.write_text(json.dumps(value))
        owner_path = installation / 'quest3d-install.json'
        owner = json.loads(owner_path.read_text()); owner['package_manifest_sha256'] = hashlib.sha256(p.read_bytes()).hexdigest()
        owner_path.write_text(json.dumps(owner))
    result = validate_install(tmp_path, installation, digest)
    assert not result['ok'] and 'must not query' not in result['error']


def test_receipt_is_exact_operation_create_new_and_keeps_compatibility_journal(tmp_path, installation):
    operation = '1' * 32
    body = f'''$paths=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{operation}' ''
$value=[ordered]@{{schema=1;operation_id='{operation}';action='apply';program='fixture host';success=$true;outcome='applied';error=$null;verified_after=$true;errors=@()}}
Write-Quest3DNetworkReceipt $paths $value
$again=$false
try{{$null=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{operation}' ''}}catch{{$again=$true}}
@{{fresh=(Get-Content -LiteralPath $paths.path -Raw|ConvertFrom-Json);repeat_refused=$again;journal=(Get-Content -LiteralPath $paths.journal -Raw|ConvertFrom-Json)}}|ConvertTo-Json -Depth 6
'''
    result = run_body(tmp_path, body)
    assert result['repeat_refused']
    assert result['fresh'] == result['journal']
    assert result['fresh']['operation_id'] == operation and result['fresh']['success']


@pytest.mark.parametrize('kind', ['outside', 'other-operation', 'bad-id', 'journal-directory'])
def test_receipt_preflight_refuses_unbounded_stale_or_invalid_paths(tmp_path, installation, kind):
    operation = '2' * 32
    requested = ''
    if kind == 'outside': requested = str(tmp_path / 'outside.json')
    elif kind == 'other-operation': requested = str(installation / ('config/network-operations/' + '3' * 32 + '/result.json'))
    elif kind == 'bad-id': operation = '../escape'
    else: (installation / 'config/network-rules.json').mkdir()
    result = run_body(tmp_path, f"$refused=$false;try{{$null=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} {ps_quote(operation)} {ps_quote(requested)}}}catch{{$refused=$true}};@{{refused=$refused}}|ConvertTo-Json")
    assert result['refused']
    assert not (tmp_path / 'outside.json').exists()


def test_network_receipt_ancestor_junction_is_refused_and_external_target_is_untouched(tmp_path, installation):
    external = tmp_path / 'external'; external.mkdir(); (external / 'sentinel.txt').write_bytes(b'preserve')
    link = installation / 'config/network-operations'
    body = f'''$null=New-Item -ItemType Junction -Path {ps_quote(link)} -Target {ps_quote(external)}
$refused=$false;try{{$null=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{'4'*32}' ''}}catch{{$refused=$true}};@{{refused=$refused}}|ConvertTo-Json
'''
    assert run_body(tmp_path, body)['refused']
    assert sorted(path.name for path in external.iterdir()) == ['sentinel.txt']
    assert (external / 'sentinel.txt').read_bytes() == b'preserve'


def test_receipt_write_probe_collision_preserves_the_preexisting_file(tmp_path, installation):
    operation = '6' * 32
    probe = installation / ('config/.network-write-probe-' + operation)
    probe.write_bytes(b'preexisting unrelated data')
    result = run_body(tmp_path, f"$refused=$false;try{{$null=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{operation}' ''}}catch{{$refused=$true}};@{{refused=$refused}}|ConvertTo-Json")
    assert result['refused']
    assert probe.read_bytes() == b'preexisting unrelated data'


def test_receipt_records_journal_write_failure_without_claiming_rule_failure_or_success(tmp_path, installation):
    journal = installation / 'config/network-rules.json'
    journal.write_text('{"previous":true}', 'utf-8')
    body = f'''$paths=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{'5'*32}' ''
$lock=[IO.File]::Open($paths.journal,[IO.FileMode]::Open,[IO.FileAccess]::Read,[IO.FileShare]::Read)
$value=[ordered]@{{schema=1;operation_id='{'5'*32}';action='apply';program='fixture host';success=$true;outcome='applied';error=$null;verified_after=$true;requested_applied=$true;partial=$false;errors=@()}}
$failed=$false
try{{Write-Quest3DNetworkReceipt $paths $value}}catch{{$failed=$true}}finally{{$lock.Dispose()}}
@{{failed=$failed;receipt=(Get-Content -LiteralPath $paths.path -Raw|ConvertFrom-Json)}}|ConvertTo-Json -Depth 6
'''
    result = run_body(tmp_path, body)
    assert result['failed'] and not result['receipt']['success']
    assert result['receipt']['outcome'] == 'failed'
    assert result['receipt']['verified_after'] and result['receipt']['requested_applied']
    assert json.loads(journal.read_text()) == {'previous': True}


@pytest.mark.parametrize('query_failed', [False, True])
def test_read_only_status_separates_owned_rule_state_and_incomplete_diagnostics(tmp_path, query_failed):
    prefix = DRIVER[DRIVER.index('$Program ='):DRIVER.index('$Delete =')]
    body = prefix + f'''\n$Scenario='{ "query-failure" if query_failed else "idempotent" }'
function Get-NetConnectionProfile {{throw 'Simulated restricted diagnostic'}}
function Get-NetFirewallProfile {{[pscustomobject]@{{Name='Private';Enabled='True'}}}}
$context=@{{program=$Program;key=$Key;rules=$Specs}}
$view=Get-Quest3DInstalledNetworkStatus $context
@{{view=$view;mutations=@($script:Actions.ToArray())}}|ConvertTo-Json -Depth 8
'''
    result = run_body(tmp_path, body)
    assert result['mutations'] == []
    view = result['view']
    assert view['rules_state'] == ('unknown' if query_failed else 'applied')
    assert view['read_only']
    assert view['known'] is (not query_failed)
    assert view['verified_after'] is (not query_failed)
    assert view['success'] is (not query_failed)
    assert not view['owned_rules_absent']
    assert view['partial'] is False
    assert view['diagnostics']['effective_policy_scope'] == 'partial'
    assert view['diagnostics']['discovery_policy'] == 'unknown'
    assert view['diagnostics']['enabled_app_block_rules'] == ('unknown' if query_failed else 0)
    assert view['errors'], 'A denied diagnostic must remain visible even when owned rules are valid'
    assert view['diagnostics_errors']


@pytest.mark.parametrize('state', ['absent', 'conflict', 'one-rule'])
def test_status_never_mistakes_conflict_or_partial_existing_rules_for_verified_absence(tmp_path, state):
    prefix = DRIVER[DRIVER.index('$Program ='):DRIVER.index('$Delete =')]
    setup = {
        'absent': '$script:Rows.Clear()',
        'conflict': "$script:Rows[$Specs[1].name].Description='other owner'",
        'one-rule': '$script:Rows.Remove($Specs[1].name)',
    }[state]
    body = prefix + "\n$Scenario='idempotent'\n" + setup + '''
function Get-NetConnectionProfile {[pscustomobject]@{NetworkCategory='Private'}}
function Get-NetFirewallProfile {[pscustomobject]@{Name='Private';Enabled='True'}}
$view=Get-Quest3DInstalledNetworkStatus @{program=$Program;key=$Key;rules=$Specs}
@{view=$view;mutations=@($script:Actions.ToArray())}|ConvertTo-Json -Depth 8
'''
    result = run_body(tmp_path, body)
    assert result['mutations'] == []
    view = result['view']
    assert not view['apply_satisfied']
    assert view['owned_rules_absent'] is (state == 'absent')
    assert view['known'] is (state != 'conflict')
    assert view['success'] is (state != 'conflict')


@pytest.mark.parametrize('shell', [PWSH, WINDOWS_PS], ids=['PS7', 'WindowsPS51'])
def test_status_receipt_does_not_touch_the_compatibility_network_journal(tmp_path, installation, shell):
    journal = installation / 'config/network-rules.json'; original = b'{"previous_apply":true}'
    journal.write_bytes(original)
    body = f'''$paths=Initialize-Quest3DNetworkReceipt {ps_quote(installation)} '{'7'*32}' '' $true
$value=[ordered]@{{schema=1;operation_id='{'7'*32}';action='status';program='fixture host';success=$true;outcome='status';error=$null;known=$true;verified_after=$true;apply_satisfied=$false;owned_rules_absent=$true;partial=$false;errors=@()}}
Write-Quest3DNetworkReceipt $paths $value
Get-Content -LiteralPath $paths.path -Raw
'''
    result = run_body(tmp_path, body, shell=shell)
    assert result['known'] and result['owned_rules_absent'] and result['outcome'] == 'status'
    assert journal.read_bytes() == original


def test_actual_windows_powershell_51_parser_accepts_policy_without_executing_it(tmp_path):
    shell = WINDOWS_PS
    body = "$tokens=$null;$parseErrors=$null;[void][Management.Automation.Language.Parser]::ParseFile(" + ps_quote(ROOT / 'native/host/configure-installed-network.ps1') + ",[ref]$tokens,[ref]$parseErrors);@{errors=@($parseErrors).Count;major=$PSVersionTable.PSVersion.Major}|ConvertTo-Json"
    # Parse directly, without dot-sourcing the policy in this compatibility test.
    script = tmp_path / 'parse-only.ps1'; script.write_text(body, 'utf-8-sig')
    completed = subprocess.run([str(shell), '-NoProfile', '-NonInteractive', '-File', str(script)], capture_output=True, text=True, encoding='utf-8', timeout=20)
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {'errors': 0, 'major': 5}

"""Interrupted installer recovery uses the saved account; it never resets pairing."""
import hashlib
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).parents[1]
PWSH = ROOT / '.tools/desktop/powershell/pwsh.exe'
pytestmark = pytest.mark.skipif(os.name != 'nt' or not PWSH.is_file(), reason='Windows DPAPI/PowerShell 7 required')
PASSWORD = '0123456789ABCDEF' * 4  # Public test fixture, never a real user credential.
SALT = 'Ab12!%&()=-xy789'


def credentials(password=PASSWORD, salt=SALT, username='quest3d'):
    return {'username': username, 'salt': salt,
            'password': hashlib.sha256((password + salt).encode()).digest()[::-1].hex().upper()}


def run_case(tmp_path, credential_data, *, saved_account=True, corrupt_access=False, host_failure=False, owned=True):
    input_file = tmp_path / 'input.json'
    input_file.write_text(json.dumps({'saved': saved_account, 'corrupt_access': corrupt_access,
        'credential_data': credential_data, 'host_failure': host_failure, 'owned': owned,
        'valid_credentials': credentials(), 'fixture_password': PASSWORD}), encoding='utf-8')
    script = tmp_path / 'fixture.ps1'
    script.write_text(r'''
param([string]$Helper, [string]$InputFile)
$ErrorActionPreference = 'Stop'
. $Helper
$spec = Get-Content -LiteralPath $InputFile -Raw | ConvertFrom-Json
$dir = Split-Path -Parent $InputFile
$access = Join-Path $dir 'account.clixml'
$creds = Join-Path $dir 'credentials.json'
if ($spec.saved) {
    $account = [PSCredential]::new('quest3d',(ConvertTo-SecureString $spec.fixture_password -AsPlainText -Force))
    $account | Export-Clixml -LiteralPath $access
    $account.Password.Dispose()
}
if ($spec.corrupt_access) { [IO.File]::WriteAllText($access,'broken-dpapi') }
if ($null -ne $spec.credential_data) { [IO.File]::WriteAllText($creds, $spec.credential_data) }
$before = if (Test-Path -LiteralPath $access) { (Get-FileHash -LiteralPath $access).Hash } else { $null }
$script:calls = 0
function Invoke-Quest3DCommand($Exe,$Working,$Arguments,$Log) {
    $script:calls++
    if ($spec.host_failure) { throw 'fixture host failed' }
    if ($Arguments[2] -cne 'quest3d') { throw 'wrong account' }
    # An independent SHA256 implementation supplies the C++ golden file when
    # restoring an existing account. New account uses the captured --creds args.
    $salt = 'Ab12!%&()=-xy789'
    $hash = [Security.Cryptography.SHA256]::HashData([Text.Encoding]::UTF8.GetBytes($Arguments[3]+$salt))
    [Array]::Reverse($hash)
    @{username=$Arguments[2]; salt=$salt; password=[Convert]::ToHexString($hash)} | ConvertTo-Json | Set-Content -LiteralPath $creds
}
$success = $false
$errorType = $null
try {
    $result = Initialize-Quest3DInstalledCredentials -AccessPath $access -CredentialsPath $creds -Executable 'fixture.exe' -WorkingDirectory $dir -ConfigPath (Join-Path $dir 'fixture.conf') -LogPath (Join-Path $dir 'fixture.txt') -OwnedSetup:$spec.owned
    $success = $true
} catch { $errorType = $_.Exception.Message }
$after = if (Test-Path -LiteralPath $access) { (Get-FileHash -LiteralPath $access).Hash } else { $null }
$backups = @(Get-ChildItem -LiteralPath $dir -Filter 'credentials.json.failed-*')
@{success=$success; error=$errorType; calls=$script:calls; account_preserved=($before -eq $after); backup_count=$backups.Count; backup_text=$(if($backups.Count){[IO.File]::ReadAllText($backups[0].FullName)}else{$null})} | ConvertTo-Json
''', encoding='utf-8')
    result = subprocess.run([str(PWSH), '-NoProfile', '-File', str(script),
        '-Helper', str(ROOT/'native/host/installed-credentials.ps1'), '-InputFile', str(input_file)],
        capture_output=True, text=True, encoding='utf-8', timeout=25)
    assert result.returncode == 0, result.stderr
    assert PASSWORD not in result.stdout and PASSWORD not in result.stderr
    return json.loads(result.stdout)


def test_valid_matching_sunshine_credentials_are_not_rewritten(tmp_path):
    result = run_case(tmp_path, json.dumps(credentials()))
    assert result['success'] and result['calls'] == 0 and result['account_preserved']
    assert result['backup_count'] == 0


@pytest.mark.parametrize('broken', ['{truncated', '{}', 'null', '[]', json.dumps(credentials(password='another-public-fixture')), json.dumps(credentials(username='different')), json.dumps({**credentials(), 'salt': 'too-short'}), json.dumps({**credentials(), 'password': credentials()['password'].lower()})])
def test_invalid_credentials_backed_up_and_regenerated_from_same_dpapi(tmp_path, broken):
    result = run_case(tmp_path, broken)
    assert result['success'] and result['calls'] == 1 and result['account_preserved']
    assert result['backup_count'] == 1 and result['backup_text'] == broken


def test_unreadable_dpapi_does_not_change_credentials(tmp_path):
    result = run_case(tmp_path, '{truncated', corrupt_access=True)
    assert not result['success'] and result['calls'] == 0 and result['account_preserved']
    assert result['backup_count'] == 0


def test_host_failure_preserves_original_bad_file_and_saved_account(tmp_path):
    result = run_case(tmp_path, '{truncated', host_failure=True)
    assert not result['success'] and result['calls'] == 1 and result['account_preserved']
    assert result['backup_text'] == '{truncated'


def test_recovery_requires_ownership_before_mutation(tmp_path):
    result = run_case(tmp_path, '{truncated', owned=False)
    assert not result['success'] and result['calls'] == 0 and result['account_preserved']
    assert result['backup_count'] == 0


def test_first_setup_creates_verified_account(tmp_path):
    result = run_case(tmp_path, None, saved_account=False)
    assert result['success'] and result['calls'] == 1 and result['backup_count'] == 0


def test_missing_credentials_resume_the_saved_dpapi_account(tmp_path):
    result = run_case(tmp_path, None)
    assert result['success'] and result['calls'] == 1 and result['account_preserved']
    assert result['backup_count'] == 0

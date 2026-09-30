<# Restore only a journaled, stopped Quest3D update. No data or pairing reset. #>
[CmdletBinding()]
param([string]$Root = (Join-Path $env:LOCALAPPDATA 'Quest3D Desktop'), [string]$ReportPath)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installation-lifecycle.ps1')
try {
    $result = Restore-Quest3DUpdate $Root
    if (!$result.restored) { throw 'No restorable update backup is available.' }
    if ($ReportPath) { Write-Quest3DJson $ReportPath @{success=$true;action='rollback';release=$result.release;root=$Root;backup=$result.backup} }
    Write-Output ('Previous version restored: ' + $result.release + '. Current settings, pairing and models were preserved.')
} catch {
    if ($ReportPath) { Write-Quest3DJson $ReportPath @{success=$false;action='rollback';error=$_.Exception.Message;root=$Root} }
    throw
}

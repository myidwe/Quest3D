[CmdletBinding()]
param(
    [string]$Journal,
    [switch]$Apply,
    [switch]$WatchOwner,
    [ValidateRange(1, 86400)][int]$TimeoutSeconds = 300,
    [switch]$Snapshot
)
$ErrorActionPreference = 'Stop'
$AudioRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if ($Snapshot -and ($Journal -or $Apply -or $WatchOwner)) { throw 'Snapshot cannot be combined with recovery.' }
if (!$Snapshot -and !$Journal) { throw 'An explicit absolute Journal path is required.' }
if ($WatchOwner -and !$Apply) { throw 'WatchOwner requires Apply.' }
$AudioArguments = @('run', '--no-sync', '--extra', 'gpu-capture', 'python', '-m', 'quest3d.audio_recovery')
if ($Snapshot) { $AudioArguments += '--snapshot' }
else {
    $AudioArguments += @('--journal', $Journal)
    if ($Apply) { $AudioArguments += '--apply' }
    if ($WatchOwner) { $AudioArguments += @('--watch-owner', '--timeout', "$TimeoutSeconds") }
}
Push-Location $AudioRoot
try {
    & uv @AudioArguments
    if ($LASTEXITCODE -ne 0) { throw 'Audio recovery refused or remains pending; inspect the structured result. No journal was discarded.' }
} finally { Pop-Location }

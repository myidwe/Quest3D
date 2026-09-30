[CmdletBinding()]
param(
    [ValidateRange(1, 16)][int]$Jobs = 8,
    [ValidateSet('all', 'host', 'bridge')][string]$Mode = 'all'
)
$ErrorActionPreference = 'Stop'
$taskRoot = Split-Path -Parent $PSScriptRoot
$taskShell = Join-Path $taskRoot 'native\host\tools\msys64\msys2_shell.cmd'
if (-not (Test-Path -LiteralPath $taskShell)) {
    throw 'The isolated MSYS2 build environment is missing. Follow docs/build/HOST.md.'
}
$taskScript = (Join-Path $taskRoot 'native\host\build-host.sh').Replace('\', '/')
# Project paths are data inside a single-quoted bash argument; reject a quote instead of guessing escaping.
if ($taskScript.Contains("'")) { throw 'The build workspace path must not contain a single quote.' }
$taskBashCommand = "bash '$taskScript' $Jobs $Mode"
$taskLogDir = Join-Path $taskRoot 'artifacts\host'
New-Item -ItemType Directory -Path $taskLogDir -Force | Out-Null
$taskLog = Join-Path $taskLogDir ('build-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '.log')
& $taskShell -defterm -here -no-start -ucrt64 -c $taskBashCommand 2>&1 | Tee-Object -FilePath $taskLog
if ($LASTEXITCODE -ne 0) { throw "Host build failed with exit code $LASTEXITCODE. Log: $taskLog" }
Write-Host "Build complete. Log: $taskLog"

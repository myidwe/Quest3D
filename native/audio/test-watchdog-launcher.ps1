[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
. (Join-Path $TaskRoot 'native/host/audio-watchdog.ps1')
$TaskDirectory = Join-Path $TaskRoot ('artifacts/audio/watchdog-launcher-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $TaskDirectory | Out-Null
$TaskToken = [guid]::NewGuid().ToString('N')
# This probe only opens kernel objects. It has no audio, input or network code.
$TaskProbe = Start-Process -FilePath (Join-Path $PSScriptRoot 'dist/audio_watchdog_probe.exe') -ArgumentList '1500' -WindowStyle Hidden -PassThru -Environment @{ QUEST3D_AUDIO_WATCHDOG_TOKEN = $TaskToken } -RedirectStandardOutput (Join-Path $TaskDirectory 'probe.log') -RedirectStandardError (Join-Path $TaskDirectory 'probe-error.log')
$TaskWatcher = Start-Quest3DAudioWatchdog $TaskProbe $TaskRoot $TaskDirectory $TaskToken
if (!$TaskProbe.WaitForExit(5000) -or $TaskProbe.ExitCode -ne 3) { throw 'The inert native probe did not refuse a missing plan.' }
Complete-Quest3DAudioWatchdog $TaskWatcher | Out-Null
if (!$TaskWatcher.Process.HasExited -or $TaskWatcher.Process.ExitCode -ne 0) { throw 'The launched watchdog did not complete its exact owner lifetime.' }
$TaskResult = Get-Content -LiteralPath (Join-Path $TaskDirectory 'result.json') -Raw | ConvertFrom-Json
if ($TaskResult.status -ne 'no_route') { throw 'No journal existed, so no endpoint recovery was allowed.' }
if ((Get-Content -LiteralPath (Join-Path $TaskDirectory 'probe.log') -Raw) -notmatch 'REFUSED') { throw 'Native gate did not report its refusal.' }
[ordered]@{ tests = 1; status = 'passed'; directory = $TaskDirectory; owner_creation_filetime = $TaskWatcher.OwnerCreated; actual_routing_changed = $false } | ConvertTo-Json

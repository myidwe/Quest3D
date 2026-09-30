[CmdletBinding()]
param([switch]$BuildOnly, [ValidateRange(1,16)][int]$MonitorOrdinal=2, [ValidateRange(20,40)][int]$Count=40)
$ErrorActionPreference='Stop'
$TaskRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskScript=(Join-Path $PSScriptRoot 'build-dxgi-snapshot-capture.sh').Replace('\','/')
if($TaskScript.Contains("'")){throw 'Unsupported quote in workspace path.'}
& (Join-Path $PSScriptRoot 'tools/msys64/msys2_shell.cmd') -defterm -here -no-start -ucrt64 -c "bash '$TaskScript'"
if($LASTEXITCODE -ne 0){throw 'Isolated DXGI capture DLL build failed.'}
& (Join-Path $TaskRoot 'artifacts/host/dxgi_snapshot_capture_test.exe')
if($LASTEXITCODE -ne 0){throw 'DXGI capture logical tests failed.'}
if($BuildOnly){return}
$TaskOutput=Join-Path $TaskRoot ('artifacts/host/dxgi-snapshot-capture-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.jsonl')
& (Join-Path $TaskRoot 'artifacts/host/dxgi-snapshot-capture-probe.exe') $MonitorOrdinal $Count | Tee-Object -FilePath $TaskOutput
$TaskCode=$LASTEXITCODE
Write-Output "Probe result: $TaskOutput (exit $TaskCode). Full-frame CPU copy, no image files."
if($TaskCode -ne 0){throw 'DXGI capture DLL proof failed; inspect recorded failures.'}

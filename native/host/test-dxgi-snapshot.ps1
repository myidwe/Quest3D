[CmdletBinding()]
param([switch]$BuildOnly, [ValidateRange(20,40)][int]$Count=24, [string]$Monitor='primary', [ValidateRange(0,1000)][int]$IntervalMs=250)
$ErrorActionPreference='Stop'
$TaskRoot=(Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskScript=(Join-Path $PSScriptRoot 'build-dxgi-snapshot-probe.sh').Replace('\','/')
if($TaskScript.Contains("'")){throw 'Unsupported quote in workspace path.'}
& (Join-Path $PSScriptRoot 'tools/msys64/msys2_shell.cmd') -defterm -here -no-start -ucrt64 -c "bash '$TaskScript'"
if($LASTEXITCODE -ne 0){throw 'Isolated DXGI probe build failed.'}
if($BuildOnly){return}
$TaskOutput=Join-Path $TaskRoot ('artifacts/host/dxgi-snapshot-'+(Get-Date -Format 'yyyyMMdd-HHmmss')+'.jsonl')
& (Join-Path $TaskRoot 'artifacts/host/dxgi-snapshot-probe.exe') --count $Count --monitor $Monitor --interval-ms $IntervalMs | Tee-Object -FilePath $TaskOutput
$TaskCode=$LASTEXITCODE
Write-Output "Probe result: $TaskOutput (exit $TaskCode). No images were saved."
if($TaskCode -ne 0){throw 'DXGI probe did not satisfy all snapshot checks; inspect its recorded failures.'}

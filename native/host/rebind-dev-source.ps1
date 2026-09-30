[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ControlDirectory,
    [Parameter(Mandatory)][string]$ExpectedRuntime,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-fA-F]{64}$')][string]$ExpectedHostSha256,
    [switch]$CheckOnly,
    [ValidateSet('pc', 'quest', 'both')][string]$AudioOutput = 'pc'
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-source-rebind.ps1')
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
Invoke-Quest3DSourceRebind $TaskRoot $ControlDirectory $ExpectedRuntime $ExpectedHostSha256 -CheckOnly:$CheckOnly -AudioOutput $AudioOutput | ConvertTo-Json -Depth 8

[CmdletBinding()]
param([switch]$CheckOnly, [switch]$Foreground, [switch]$Background, [switch]$FilePcm, [switch]$EnableInput,
    [ValidateSet('pc', 'quest', 'both')][string]$AudioOutput = 'pc')
$ErrorActionPreference = 'Stop'
if ($Foreground -and $Background) { throw 'Choose Foreground or Background, not both.' }
. (Join-Path $PSScriptRoot 'dev-host-common.ps1')
. (Join-Path $PSScriptRoot 'audio-watchdog.ps1')
. (Join-Path $PSScriptRoot 'file-pcm-launch.ps1')
. (Join-Path $PSScriptRoot 'system-audio-launch.ps1')
. (Join-Path $PSScriptRoot 'source-session-launch.ps1')
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskManifest = Read-Quest3DDevManifest $TaskRoot
$TaskExe = Join-Path $TaskManifest.runtime 'sunshine.exe'
if ((Get-FileHash -LiteralPath $TaskExe).Hash -ne $TaskManifest.host_sha256) { throw 'Staged runtime checksum changed.' }
if ($TaskManifest.audio_watchdog_protocol -ne 1) { throw 'This launcher requires a staged host verified with the native audio watchdog gate. Rebuild, test and stage before starting it.' }
Assert-Quest3DPcmSelection $TaskManifest ([bool]$FilePcm) ([bool]$EnableInput) $AudioOutput
$TaskConflicts = @(Get-Quest3DPortConflict $TaskManifest.port)
if ($TaskConflicts.Count -gt 0) { throw ('Required ports are in use; existing processes were preserved: ' + ($TaskConflicts | ConvertTo-Json -Compress)) }
$TaskSettings = Get-Content -LiteralPath $TaskManifest.config -Raw
Assert-Quest3DInitialSettings $TaskSettings ([bool]$FilePcm) $AudioOutput
if (!$FilePcm) {
    Assert-Quest3DSystemAudioSettings $TaskSettings $AudioOutput ([string]$TaskManifest.audio_endpoint)
    $TaskAudioPlan = Get-Quest3DSystemAudioPlan $TaskRoot $AudioOutput
    if ([string]$TaskAudioPlan.audio_endpoint -cne [string]$TaskManifest.audio_endpoint) { throw 'Audio device changed since preparation; select output again.' }
}
$TaskProtocol = Get-Quest3DBridgeProtocol $TaskManifest $TaskSettings
$TaskProbeLog = Join-Path $TaskRoot 'artifacts/host/dev/preflight-bridge.jsonl'
$TaskProbeError = Join-Path $TaskRoot 'artifacts/host/dev/preflight-bridge.txt'
$TaskProbeArgs = @('1')
if ($TaskProtocol -eq 3) { $TaskProbeArgs += @('--protocol', '3') }
& (Join-Path $TaskRoot 'artifacts/host/frame_bridge_probe.exe') @TaskProbeArgs 2> $TaskProbeError | Set-Content -LiteralPath $TaskProbeLog -Encoding utf8
if ($LASTEXITCODE -ne 0) { throw "Start the real source writer first; no fresh v$TaskProtocol frame was available." }
$TaskFrames = @(Get-Content -LiteralPath $TaskProbeLog | ForEach-Object { $_ | ConvertFrom-Json })
if (!$TaskFrames.Count) { throw 'The source did not publish any actual frames.' }
$TaskSource = Get-Quest3DSourceSession $TaskRoot $TaskManifest.control_directory $TaskManifest.config $TaskProbeLog (Join-Path $TaskManifest.runtime 'manifest.json') ([bool]$EnableInput)
$TaskPcm = if ($FilePcm) { Get-Quest3DFilePcm $TaskRoot $TaskManifest.control_directory $TaskManifest.file_pcm } else { $null }
if ($TaskPcm) { Assert-Quest3DFilePcmVideo $TaskPcm $TaskFrames $TaskSettings }
if ($CheckOnly) {
    Write-Output "Preflight passed: $($TaskFrames.Count) fresh frames; input opt-in $([bool]$EnableInput); file PCM $([bool]$FilePcm); required ports free. Server not started."
    return
}
$TaskConfigArgument = '"' + $TaskManifest.config + '"'
$TaskAudioToken = [guid]::NewGuid().ToString('N')
$TaskAudioDirectory = Join-Path $TaskManifest.runtime ('audio-' + $TaskAudioToken)
New-Item -ItemType Directory -Path $TaskAudioDirectory | Out-Null
$TaskHostEnvironment = @{
    QUEST3D_AUDIO_ROUTE_JOURNAL = (Join-Path $TaskAudioDirectory 'route.json')
    QUEST3D_AUDIO_WATCHDOG_TOKEN = $TaskAudioToken
}
$TaskPcmEnvironment = Get-Quest3DFilePcmEnvironment $TaskPcm
foreach ($TaskKey in $TaskPcmEnvironment.Keys) { $TaskHostEnvironment[$TaskKey] = $TaskPcmEnvironment[$TaskKey] }
if ($Background) {
    $TaskProcess = Start-Process -FilePath $TaskExe -ArgumentList $TaskConfigArgument -WorkingDirectory $TaskManifest.runtime -WindowStyle Hidden -PassThru -Environment $TaskHostEnvironment -RedirectStandardOutput (Join-Path $TaskManifest.runtime 'stdout.log') -RedirectStandardError (Join-Path $TaskManifest.runtime 'stderr.log')
} else {
    # Keep the invoking terminal session alive. This avoids treating a helper that
    # disappears with its parent tool session as a running server.
    $TaskProcess = Start-Process -FilePath $TaskExe -ArgumentList $TaskConfigArgument -WorkingDirectory $TaskManifest.runtime -NoNewWindow -PassThru -Environment $TaskHostEnvironment
}
$TaskAttempt = @{ process_id = $TaskProcess.Id; runtime = $TaskManifest.runtime; executable = $TaskExe; started_at = (Get-Date).ToString('o'); audio_directory = $TaskAudioDirectory; audio_output = $AudioOutput; file_pcm = [bool]$FilePcm; file_session_id = $(if ($TaskPcm) { $TaskPcm.file_session_id } else { $null }) }
$TaskAttemptPath = Join-Path $TaskAudioDirectory 'process.json'
$TaskAttempt | ConvertTo-Json | Set-Content -LiteralPath $TaskAttemptPath -Encoding utf8
try {
    $TaskAudioWatchdog = Start-Quest3DAudioWatchdog $TaskProcess $TaskRoot $TaskAudioDirectory $TaskAudioToken
    $TaskAttempt.audio_watchdog_pid = $TaskAudioWatchdog.Process.Id
    $TaskAttempt.owner_creation_filetime = $TaskAudioWatchdog.OwnerCreated
    $TaskAttempt | ConvertTo-Json | Set-Content -LiteralPath $TaskAttemptPath -Encoding utf8
} catch {
    $TaskStartFailure = $_
    Stop-Quest3DStartupHost $TaskProcess $TaskRoot $TaskExe $TaskAudioDirectory
    throw $TaskStartFailure
}
Start-Sleep -Milliseconds 1500
$TaskProcess.Refresh()
if ($TaskProcess.HasExited) {
    Complete-Quest3DAudioWatchdog $TaskAudioWatchdog
    throw "Development host exited during startup with code $($TaskProcess.ExitCode). Inspect $($TaskManifest.runtime) and the dev Sunshine log."
}
if ($TaskAudioWatchdog.Process.HasExited) {
    Stop-Quest3DStartupHost $TaskProcess $TaskRoot $TaskExe $TaskAudioDirectory
    Complete-Quest3DAudioWatchdog $TaskAudioWatchdog
    throw 'Audio watchdog exited during startup; the new host received a verified graceful shutdown request.'
}
$TaskAttempt | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TaskRoot 'artifacts/host/dev/process.json') -Encoding utf8
Write-Output "Development host process $($TaskProcess.Id) started from staged runtime."
Write-Output "Local pairing UI: $($TaskManifest.web_url)"
Write-Output 'Startup/stream success must be verified from logs and the headset; process creation alone is not a pass.'
if (!$Background) {
    Write-Output 'Foreground lifetime: keep this terminal session open while using the headset.'
    $TaskWatchdogReported = $false
    while (!$TaskProcess.WaitForExit(500)) {
        if ($TaskAudioWatchdog.Process.HasExited -and !$TaskWatchdogReported) {
            $TaskWatchdogReported = $true
            Write-Warning "Audio watchdog exited while the host is alive. Native routing is blocked; inspect $TaskAudioDirectory."
        }
    }
    Complete-Quest3DAudioWatchdog $TaskAudioWatchdog
    Write-Output "Development host exited with code $($TaskProcess.ExitCode)."
    if ($TaskProcess.ExitCode -ne 0) { throw 'Development host ended with an error.' }
}

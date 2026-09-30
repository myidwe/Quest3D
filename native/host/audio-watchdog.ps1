# One independent watchdog per host lifetime; it never routes audio while the host lives.
function Stop-Quest3DStartupHost($HostProcess, [string]$TaskRoot, [string]$Executable, [string]$Directory) {
    # Retain the original Process handle and use the existing executable/window
    # verifier. Never fall back to Stop-Process or a port/PID-only termination.
    for ($TaskTry = 0; $TaskTry -lt 3; $TaskTry++) {
        if ($HostProcess.HasExited) { return }
        & uv run --quiet --locked --extra gpu-capture python (Join-Path $TaskRoot 'scripts/stop-verified-host.py') --exe $Executable --pid $HostProcess.Id *> (Join-Path $Directory 'startup-cleanup.log')
        if ($LASTEXITCODE -eq 0 -or $HostProcess.WaitForExit(500)) { return }
    }
    Write-Warning "The newly created host $($HostProcess.Id) could not be stopped gracefully. Its identity and pending audio journal are preserved in $Directory; forced termination was not used."
}

function Start-Quest3DAudioWatchdog($HostProcess, [string]$TaskRoot, [string]$Directory, [string]$Token) {
    $TaskCreated = $HostProcess.StartTime.ToFileTimeUtc().ToString([Globalization.CultureInfo]::InvariantCulture)
    $TaskStart = [Diagnostics.ProcessStartInfo]::new()
    $TaskStart.FileName = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $TaskStart.WorkingDirectory = $TaskRoot
    $TaskStart.UseShellExecute = $false
    $TaskStart.CreateNoWindow = $true
    $TaskArguments = @('run', '--quiet', '--locked', '--extra', 'gpu-capture', 'python', '-m', 'quest3d.audio_watchdog',
        '--journal', (Join-Path $Directory 'route.json'), '--owner-pid', "$($HostProcess.Id)",
        '--owner-created', $TaskCreated, '--token', $Token, '--apply',
        '--events-path', (Join-Path $Directory 'events.jsonl'), '--result-path', (Join-Path $Directory 'result.json'))
    foreach ($Argument in $TaskArguments) { $TaskStart.ArgumentList.Add($Argument) }
    $TaskWatcher = [Diagnostics.Process]::Start($TaskStart)
    return [pscustomobject]@{ Process = $TaskWatcher; Directory = $Directory; OwnerCreated = $TaskCreated }
}

function Complete-Quest3DAudioWatchdog($Watchdog) {
    # The watcher uses the original process handle, never a newly reused PID.
    if (!$Watchdog.Process.WaitForExit(5000)) {
        Write-Warning "Audio recovery remains active: process $($Watchdog.Process.Id), journal $($Watchdog.Directory)/route.json. It was preserved."
        return
    }
    $TaskResultPath = Join-Path $Watchdog.Directory 'result.json'
    if (!(Test-Path -LiteralPath $TaskResultPath)) {
        Write-Warning "Audio watchdog ended without a result. Inspect $($Watchdog.Directory); no journal was discarded."
        return
    }
    $TaskAudioResult = Get-Content -LiteralPath $TaskResultPath -Raw | ConvertFrom-Json
    if ($Watchdog.Process.ExitCode -ne 0 -or $TaskAudioResult.status -notin @('no_route', 'restored', 'already_restored')) {
        Write-Warning "Audio recovery status is $($TaskAudioResult.status). Preserve and inspect $($Watchdog.Directory)."
    } else {
        Write-Output "Audio watchdog finished: $($TaskAudioResult.status)."
    }
}

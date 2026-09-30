# Pure settings/environment helpers plus a bounded read-only Python preflight.
function Get-Quest3DFilePcm([string]$TaskRoot, [string]$ControlDirectory, $Expected = $null) {
    if (!$ControlDirectory) { throw 'FilePcm requires the concrete file session ControlDirectory.' }
    $TaskStart = [Diagnostics.ProcessStartInfo]::new()
    $TaskStart.FileName = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $TaskStart.WorkingDirectory = $TaskRoot
    $TaskStart.UseShellExecute = $false
    $TaskStart.CreateNoWindow = $true
    $TaskStart.RedirectStandardOutput = $true
    $TaskStart.RedirectStandardError = $true
    foreach ($Argument in @('run', '--quiet', '--locked', '--extra', 'gpu-capture', 'python',
            (Join-Path $TaskRoot 'native/host/validate-file-pcm.py'), $ControlDirectory)) {
        $TaskStart.ArgumentList.Add($Argument)
    }
    $TaskValidator = [Diagnostics.Process]::Start($TaskStart)
    $TaskOutput = $TaskValidator.StandardOutput.ReadToEndAsync()
    $TaskError = $TaskValidator.StandardError.ReadToEndAsync()
    if (!$TaskValidator.WaitForExit(10000)) { throw 'Read-only PCM preflight exceeded 10 seconds. No host was started.' }
    $TaskResultText = $TaskOutput.GetAwaiter().GetResult()
    $null = $TaskError.GetAwaiter().GetResult()
    if ($TaskValidator.ExitCode -ne 0) { throw ('File PCM preflight refused the session: ' + $TaskResultText.Trim()) }
    if ($TaskResultText.Length -gt 4096) { throw 'Unexpected PCM preflight output size.' }
    $TaskResult = $TaskResultText | ConvertFrom-Json
    if ($Expected) {
        foreach ($TaskKey in @('version', 'session_id', 'file_session_id', 'channel', 'producer_pid', 'producer_creation_filetime', 'control_directory')) {
            if ([string]$Expected.$TaskKey -cne [string]$TaskResult.$TaskKey) { throw "The staged PCM $TaskKey changed. Prepare a new runtime for this file session." }
        }
    }
    return $TaskResult
}

function Set-Quest3DInitialSettings([string]$Settings, [bool]$FilePcm) {
    # Copy into the NEW runtime only; never rewrite the persistent config or a
    # running host. Remove duplicates so a later line cannot override the gate.
    $TaskRequired = [ordered]@{ capture = 'quest3d'; keyboard = 'disabled'; mouse = 'disabled'; controller = 'disabled'; upnp = 'disabled'; install_steam_audio_drivers = 'disabled'; stream_audio = $(if ($FilePcm) { 'enabled' } else { 'disabled' }) }
    foreach ($TaskKey in $TaskRequired.Keys) {
        $Settings = $Settings -replace "(?im)^\s*$TaskKey\s*=.*(?:\r?\n|$)", ''
        $Settings += "`n$TaskKey = $($TaskRequired[$TaskKey])"
    }
    return $Settings + "`n"
}

function Assert-Quest3DInitialSettings([string]$Settings, [bool]$FilePcm, [string]$AudioOutput = 'pc') {
    if ($AudioOutput -cnotin @('pc', 'quest', 'both') -or ($FilePcm -and $AudioOutput -cne 'pc')) { throw 'Invalid or mixed audio selection.' }
    $TaskRequired = @{ capture = 'quest3d'; keyboard = 'disabled'; mouse = 'disabled'; controller = 'disabled'; upnp = 'disabled'; install_steam_audio_drivers = 'disabled'; stream_audio = $(if ($FilePcm -or $AudioOutput -cne 'pc') { 'enabled' } else { 'disabled' }) }
    foreach ($TaskKey in $TaskRequired.Keys) {
        $TaskLines = @($Settings -split '\r?\n' | Where-Object { $_ -match "(?i)^\s*$TaskKey\s*=" })
        if ($TaskLines.Count -ne 1 -or $TaskLines[0] -cnotmatch "^$TaskKey\s*=\s*$($TaskRequired[$TaskKey])\s*$") {
            throw "This runtime requires one exact $TaskKey = $($TaskRequired[$TaskKey]) setting."
        }
    }
}

function Get-Quest3DFilePcmEnvironment($Pcm) {
    # Empty string also removes inherited file PCM from ordinary desktop hosts.
    return @{ QUEST3D_FILE_AUDIO_CHANNEL = $(if ($Pcm) { $Pcm.channel } else { '' }) }
}

function Assert-Quest3DPcmSelection($Manifest, [bool]$FilePcm, [bool]$EnableInput = $false, [string]$AudioOutput = 'pc') {
    $StagedOutput = if ($Manifest.audio_output) { [string]$Manifest.audio_output } else { 'pc' }
    if ($AudioOutput -cnotin @('pc', 'quest', 'both') -or $StagedOutput -cne $AudioOutput -or ($FilePcm -and $AudioOutput -cne 'pc')) { throw 'System audio must match explicit preparation and launch selection.' }
    if ($Manifest.audio_enabled -isnot [bool] -or $Manifest.audio_enabled -ne ($FilePcm -or $AudioOutput -cne 'pc') -or ([bool]$Manifest.file_pcm) -ne $FilePcm -or $Manifest.input_enabled -isnot [bool] -or $Manifest.input_enabled -ne $EnableInput -or ($FilePcm -and $EnableInput)) {
        throw 'FilePcm and input must match preparation and explicit launch selection; FilePcm requires InputEnabled OFF.'
    }
}

function Assert-Quest3DFilePcmVideo($Pcm, $Frames, [string]$Settings) {
    $TaskLines = @($Settings -split '\r?\n' | Where-Object { $_ -match '^quest3d_control_dir\s*=' })
    if ($TaskLines.Count -ne 1) { throw 'File PCM requires one control directory in the runtime config.' }
    $TaskConfigured = [IO.Path]::GetFullPath(($TaskLines[0] -replace '^quest3d_control_dir\s*=\s*', '').Trim())
    if (![string]::Equals($TaskConfigured, $Pcm.control_directory, [StringComparison]::OrdinalIgnoreCase)) { throw 'File PCM and host control use different session directories.' }
    if (!@($Frames).Count -or @($Frames | Where-Object { $_.stream_epoch -ne $Pcm.video_stream_epoch }).Count) {
        throw 'The actual video bridge epoch does not match this file PCM session.'
    }
}

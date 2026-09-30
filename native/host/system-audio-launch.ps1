# System loopback policy. Inspection/configuration never changes Windows devices;
# only the verified native host may route them under its watchdog journal.
function Get-Quest3DAudioOutput($Manifest) {
    $Mode = if ($Manifest.audio_output) { [string]$Manifest.audio_output } else { 'pc' }
    if ($Mode -cnotin @('pc', 'quest', 'both')) { throw 'Unknown system audio output.' }
    return $Mode
}

function Get-Quest3DSystemAudioPlan([string]$TaskRoot, [string]$Mode) {
    if ($Mode -cnotin @('pc', 'quest', 'both')) { throw 'Unknown system audio output.' }
    if ($Mode -ceq 'pc') { return [pscustomobject]@{ audio_output='pc'; audio_enabled=$false; audio_endpoint=$null } }
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = Join-Path $TaskRoot '.venv/Scripts/python.exe'
    $Start.WorkingDirectory = $TaskRoot
    $Start.UseShellExecute = $false
    $Start.CreateNoWindow = $true
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    $Start.Environment['PYTHONPATH'] = Join-Path $TaskRoot 'src'
    foreach ($Argument in @('-m', 'quest3d.audio_output', '--mode', $Mode, '--root', $TaskRoot)) { $Start.ArgumentList.Add($Argument) }
    $Process = [Diagnostics.Process]::Start($Start)
    $Output = $Process.StandardOutput.ReadToEndAsync()
    $Errors = $Process.StandardError.ReadToEndAsync()
    if (!$Process.WaitForExit(8000)) { throw 'Read-only audio preflight exceeded 8 seconds; no host started.' }
    $Text = $Output.GetAwaiter().GetResult()
    $null = $Errors.GetAwaiter().GetResult()
    if ($Text.Length -gt 4096) { throw 'Unexpected audio preflight output.' }
    if ($Process.ExitCode -ne 0) { throw ('System audio unavailable: ' + $Text.Trim()) }
    $Plan = $Text | ConvertFrom-Json
    if ($Plan.audio_output -cne $Mode -or $Plan.audio_enabled -isnot [bool] -or !$Plan.audio_enabled) { throw 'Audio preflight disagrees with selection.' }
    return $Plan
}

function Assert-Quest3DSystemAudioSettings([string]$Settings, [string]$Mode, [string]$Endpoint = '') {
    if ($Mode -cnotin @('pc', 'quest', 'both')) { throw 'Unknown system audio output.' }
    if ($Mode -ceq 'quest') {
        if ($Endpoint -cnotmatch '^\{0\.0\.0\.00000000\}\.\{[0-9a-fA-F-]{36}\}$') { throw 'Quest audio endpoint is invalid.' }
    } elseif ($Endpoint) { throw 'PC and shared audio cannot force an endpoint.' }
    foreach ($Key in @('audio_sink', 'virtual_sink')) {
        $Lines = @([regex]::Matches($Settings, "(?mi)^\s*$Key\s*=\s*([^\r\n]*)"))
        if ($Mode -ceq 'quest') {
            $Expected = $Endpoint
            if ($Lines.Count -ne 1 -or $Lines[0].Groups[1].Value.Trim() -cne $Expected) { throw "Configured $Key disagrees with the owned Quest output." }
        } elseif ($Lines.Count -ne 0) { throw 'Unexpected audio sink override; preserve and inspect configuration.' }
    }
}

function Set-Quest3DSystemAudioSettings([string]$Settings, $Plan) {
    $Mode = [string]$Plan.audio_output
    if ($Mode -cnotin @('pc', 'quest', 'both')) { throw 'Unknown system audio output.' }
    $Value = if ($Mode -ceq 'pc') { 'disabled' } else { 'enabled' }
    $Settings = [regex]::Replace($Settings, '(?mi)^(stream_audio\s*=\s*)[^\r\n]*', ('$1' + $Value))
    $Settings = [regex]::Replace($Settings, '(?mi)^[ \t]*(audio_sink|virtual_sink)[ \t]*=.*(?:\r?\n|$)', '')
    if ($Mode -ceq 'quest') {
        $Settings = $Settings.TrimEnd("`r", "`n") + "`n" +
            "audio_sink = $($Plan.audio_endpoint)`nvirtual_sink = $($Plan.audio_endpoint)`n"
    }
    Assert-Quest3DSystemAudioSettings $Settings $Mode ([string]$Plan.audio_endpoint)
    return $Settings
}

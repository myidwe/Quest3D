# Rebind a stopped, explicitly identified runtime; never copy or launch a host.
. (Join-Path $PSScriptRoot 'dev-host-common.ps1')
. (Join-Path $PSScriptRoot 'source-session-launch.ps1')
. (Join-Path $PSScriptRoot 'file-pcm-launch.ps1')
. (Join-Path $PSScriptRoot 'system-audio-launch.ps1')

function Get-Quest3DRebindHash([byte[]]$Bytes) {
    return [Convert]::ToHexString([Security.Cryptography.SHA256]::HashData($Bytes)).ToLowerInvariant()
}

function Assert-Quest3DRebindPath([string]$Path, [string]$Root) {
    $Full = [IO.Path]::GetFullPath($Path)
    $Prefix = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (!$Full.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Rebind path leaves the project host artifacts.' }
    if ((Get-Item -LiteralPath $Root -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Rebind refuses redirected host artifact roots.' }
    $Item = Get-Item -LiteralPath $Full -Force
    while ($Item -and $Item.FullName.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase)) {
        if ($Item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Rebind refuses redirected runtime or manifest paths.' }
        $Item = if ($Item -is [IO.DirectoryInfo]) { $Item.Parent } else { $Item.Directory }
    }
    return $Full
}

function Assert-Quest3DRebindStopped([string]$Executable, [int]$Port) {
    $Processes = @(Get-CimInstance Win32_Process -Filter "Name = 'sunshine.exe'" -ErrorAction Stop)
    foreach ($Process in $Processes) {
        # An inaccessible candidate cannot prove that this runtime is stopped.
        if (!$Process.ExecutablePath -or [string]::Equals([IO.Path]::GetFullPath($Process.ExecutablePath), $Executable, [StringComparison]::OrdinalIgnoreCase)) {
            throw 'Runtime host may still be alive; existing processes were preserved.'
        }
    }
    $Conflicts = @(Get-Quest3DPortConflict $Port)
    if ($Conflicts.Count) { throw ('Required ports are in use: ' + ($Conflicts | ConvertTo-Json -Compress)) }
}

function Write-Quest3DRebindProbe([string]$TaskRoot, [int]$Protocol, [string]$ProbePath) {
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = Join-Path $TaskRoot 'artifacts/host/frame_bridge_probe.exe'
    $Start.UseShellExecute = $false
    $Start.CreateNoWindow = $true
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    foreach ($Argument in @('1', '--protocol', [string]$Protocol)) { $Start.ArgumentList.Add($Argument) }
    $Process = [Diagnostics.Process]::Start($Start)
    $Output = $Process.StandardOutput.ReadToEndAsync()
    $Errors = $Process.StandardError.ReadToEndAsync()
    if (!$Process.WaitForExit(5000)) { throw 'Native bridge probe is still running after 5 seconds; inspect before retrying.' }
    $Text = $Output.GetAwaiter().GetResult()
    $null = $Errors.GetAwaiter().GetResult()
    if ($Process.ExitCode -ne 0 -or $Text.Length -gt 65536) { throw 'No bounded fresh native bridge frame sequence was available.' }
    [IO.File]::WriteAllText($ProbePath, $Text, [Text.UTF8Encoding]::new($false))
}

function Get-Quest3DRebindText([byte[]]$Bytes) {
    if ($Bytes.Length -gt 65536) { throw 'Rebind config/manifest exceeds 64 KiB.' }
    return [Text.UTF8Encoding]::new($false, $true).GetString($Bytes).TrimStart([char]0xfeff)
}

function ConvertTo-Quest3DRebindBytes([string]$Text, [byte[]]$Original) {
    $Body = [Text.UTF8Encoding]::new($false).GetBytes($Text)
    if ($Original.Length -ge 3 -and $Original[0] -eq 239 -and $Original[1] -eq 187 -and $Original[2] -eq 191) {
        return ,([byte[]](@(239, 187, 191) + $Body))
    }
    return ,$Body
}

function Write-Quest3DRebindLockedFile([IO.FileStream]$Stream, [byte[]]$Bytes) {
    $Stream.Position = 0
    $Stream.Write($Bytes, 0, $Bytes.Length)
    $Stream.SetLength($Bytes.Length)
    $Stream.Flush($true)
}

function Invoke-Quest3DSourceRebind([string]$TaskRoot, [string]$ControlDirectory,
        [string]$ExpectedRuntime, [string]$ExpectedHostSha256, [switch]$CheckOnly,
        [ValidateSet('pc', 'quest', 'both')][string]$AudioOutput = 'pc') {
    if ($ExpectedHostSha256 -notmatch '^[0-9a-fA-F]{64}$') { throw 'An exact expected SHA256 is required.' }
    $HostRoot = Join-Path $TaskRoot 'artifacts/host'
    $LaunchPath = Assert-Quest3DRebindPath (Join-Path $HostRoot 'dev/launch.json') $HostRoot
    $LaunchBytes = [IO.File]::ReadAllBytes($LaunchPath)
    $Launch = (Get-Quest3DRebindText $LaunchBytes) | ConvertFrom-Json -AsHashtable
    $Runtime = Assert-Quest3DRebindPath ([string]$Launch.runtime) $HostRoot
    if (![string]::Equals($Runtime, [IO.Path]::GetFullPath($ExpectedRuntime), [StringComparison]::OrdinalIgnoreCase) -or
            !(Split-Path -Leaf $Runtime).StartsWith('runtime-')) { throw 'Expected runtime and launch manifest path differ.' }
    $ManifestPath = Assert-Quest3DRebindPath (Join-Path $Runtime 'manifest.json') $HostRoot
    $ConfigPath = Assert-Quest3DRebindPath (Join-Path $Runtime 'sunshine.conf') $HostRoot
    $Exe = Assert-Quest3DRebindPath (Join-Path $Runtime 'sunshine.exe') $HostRoot
    $ManifestBytes = [IO.File]::ReadAllBytes($ManifestPath)
    $ConfigBytes = [IO.File]::ReadAllBytes($ConfigPath)
    $Manifest = (Get-Quest3DRebindText $ManifestBytes) | ConvertFrom-Json -AsHashtable
    # Exact existing manifest agreement prevents rebinding another staged runtime.
    if (($Manifest | ConvertTo-Json -Depth 20 -Compress) -cne ($Launch | ConvertTo-Json -Depth 20 -Compress)) { throw 'Runtime and launch manifests disagree.' }
    if (![string]::Equals([IO.Path]::GetFullPath([string]$Manifest.config), $ConfigPath, [StringComparison]::OrdinalIgnoreCase)) { throw 'Manifest config does not name this runtime config.' }
    if ($Manifest.host_sha256 -ine $ExpectedHostSha256 -or (Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash -ine $ExpectedHostSha256) { throw 'Pinned runtime host checksum changed.' }
    $ManifestObject = [pscustomobject]$Manifest
    $OldAudio = Get-Quest3DAudioOutput $ManifestObject
    Assert-Quest3DPcmSelection $ManifestObject $false $false $OldAudio
    if ($Manifest.audio_watchdog_protocol -ne 1) { throw 'Runtime lacks the verified watchdog contract.' }
    if (($Manifest.port -isnot [int] -and $Manifest.port -isnot [long]) -or $Manifest.port -lt 1030 -or $Manifest.port -gt 65514) { throw 'Invalid staged port.' }
    $Settings = Get-Quest3DRebindText $ConfigBytes
    Assert-Quest3DInitialSettings $Settings $false $OldAudio
    Assert-Quest3DSystemAudioSettings $Settings $OldAudio ([string]$Manifest.audio_endpoint)
    $AudioPlan = Get-Quest3DSystemAudioPlan $TaskRoot $AudioOutput
    $Protocol = Get-Quest3DBridgeProtocol $ManifestObject $Settings
    $PortLines = [regex]::Matches($Settings, '(?mi)^\s*port\s*=\s*([^\r\n]*)')
    if ($PortLines.Count -ne 1 -or $PortLines[0].Groups[1].Value.Trim() -cne [string]$Manifest.port) { throw 'Configured and manifest ports disagree.' }
    $ControlDirectory = (Resolve-Path -LiteralPath $ControlDirectory).Path
    if ($ControlDirectory.IndexOfAny([char[]]"`r`n") -ge 0) { throw 'Invalid control path.' }
    $Controls = [regex]::Matches($Settings, '(?mi)^([ \t]*quest3d_control_dir[ \t]*=[ \t]*)([^\r\n]*)(?=\r?$)')
    if ($Controls.Count -ne 1) { throw 'Expected exactly one existing source control setting.' }
    $OldDirectory = [IO.Path]::GetFullPath($Controls[0].Groups[2].Value.Trim())
    if (![string]::Equals($OldDirectory, [IO.Path]::GetFullPath([string]$Manifest.control_directory), [StringComparison]::OrdinalIgnoreCase)) { throw 'Old source config and manifest disagree.' }
    $ControlValue = $Controls[0].Groups[2]
    $NewSettings = $Settings.Substring(0, $ControlValue.Index) + $ControlDirectory.Replace('\', '/') + $Settings.Substring($ControlValue.Index + $ControlValue.Length)
    $NewSettings = Set-Quest3DSystemAudioSettings $NewSettings $AudioPlan
    Assert-Quest3DInitialSettings $NewSettings $false $AudioOutput
    $NewConfig = ConvertTo-Quest3DRebindBytes $NewSettings $ConfigBytes
    Assert-Quest3DRebindStopped $Exe $Manifest.port
    $Scratch = Join-Path ([IO.Path]::GetTempPath()) ('quest3d-rebind-' + [guid]::NewGuid().ToString('N'))
    $null = [IO.Directory]::CreateDirectory($Scratch)
    $Streams = @()
    $Journal = $null
    $Changed = @()
    try {
        $Candidate = Join-Path $Scratch 'candidate.conf'
        $Probe = Join-Path $Scratch 'probe.jsonl'
        [IO.File]::WriteAllBytes($Candidate, $NewConfig)
        # Initialize the same locked/offline validation environment before
        # acquiring time-sensitive frames. This checks the live source/config
        # only; the following call still requires a fresh real video sequence.
        $null = Get-Quest3DSourceSession $TaskRoot $ControlDirectory $Candidate
        Write-Quest3DRebindProbe $TaskRoot $Protocol $Probe
        $Source = Get-Quest3DSourceSession $TaskRoot $ControlDirectory $Candidate $Probe
        if ($Source.actual_frames_checked -lt 2 -or $Source.bridge_protocol -ne $Protocol -or $Source.input_opt_in -ne $false) { throw 'Incomplete actual source preflight.' }
        $Result = [ordered]@{ mode = $(if ($CheckOnly) { 'check_only' } else { 'apply' }); runtime = $Runtime; host_sha256 = $ExpectedHostSha256.ToLowerInvariant(); bridge_protocol = $Protocol; control_directory = $ControlDirectory; source_session = $Source; input_enabled = $false; audio_enabled = $AudioPlan.audio_enabled; audio_output = $AudioOutput; audio_endpoint = $AudioPlan.audio_endpoint; server_started = $false }
        if ($CheckOnly) {
            foreach ($Pair in @(@($ConfigPath, $ConfigBytes), @($ManifestPath, $ManifestBytes), @($LaunchPath, $LaunchBytes))) {
                if ((Get-Quest3DRebindHash ([IO.File]::ReadAllBytes($Pair[0]))) -cne (Get-Quest3DRebindHash $Pair[1])) { throw 'Rebind file changed after preflight; check result is obsolete.' }
            }
            Assert-Quest3DRebindStopped $Exe $Manifest.port
            if ((Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash -ine $ExpectedHostSha256) { throw 'Runtime binary changed during preflight.' }
            return [pscustomobject]$Result
        }
        $Manifest.control_directory = $ControlDirectory
        $Manifest.source_session = $Source
        $Manifest.bridge_protocol = $Protocol
        $Manifest.audio_enabled = $AudioPlan.audio_enabled
        $Manifest.audio_output = $AudioOutput
        $Manifest.audio_endpoint = $AudioPlan.audio_endpoint
        $Json = ($Manifest | ConvertTo-Json -Depth 20) + "`n"
        $Entries = @(
            @{ path = $ConfigPath; before = $ConfigBytes; after = $NewConfig; name = 'sunshine.conf' },
            @{ path = $ManifestPath; before = $ManifestBytes; after = (ConvertTo-Quest3DRebindBytes $Json $ManifestBytes); name = 'runtime-manifest.json' },
            @{ path = $LaunchPath; before = $LaunchBytes; after = (ConvertTo-Quest3DRebindBytes $Json $LaunchBytes); name = 'dev-launch.json' }
        )
        $Journal = Join-Path $HostRoot ('rebind-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N'))
        $null = [IO.Directory]::CreateDirectory($Journal)
        foreach ($Entry in $Entries) { [IO.File]::WriteAllBytes((Join-Path $Journal $Entry.name), $Entry.before) }
        [IO.File]::WriteAllBytes((Join-Path $Journal 'candidate.conf'), $NewConfig)
        [IO.File]::Copy($Probe, (Join-Path $Journal 'probe.jsonl'))
        $State = [ordered]@{ state = 'prepared'; created_at = (Get-Date).ToString('o'); result = $Result; files = @($Entries | ForEach-Object { @{path=$_.path; backup=$_.name; before_sha256=(Get-Quest3DRebindHash $_.before); after_sha256=(Get-Quest3DRebindHash $_.after)} }) }
        $StatePath = Join-Path $Journal 'journal.json'
        $State | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $StatePath -Encoding utf8
        foreach ($Entry in $Entries) {
            $Stream = [IO.File]::Open($Entry.path, [IO.FileMode]::Open, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
            $Streams += $Stream
            $Current = [byte[]]::new($Stream.Length)
            $Stream.ReadExactly($Current, 0, $Current.Length)
            if ((Get-Quest3DRebindHash $Current) -cne (Get-Quest3DRebindHash $Entry.before)) { throw 'Rebind file changed after preflight; no overwrite allowed.' }
        }
        Assert-Quest3DRebindStopped $Exe $Manifest.port
        if ((Get-FileHash -LiteralPath $Exe -Algorithm SHA256).Hash -ine $ExpectedHostSha256) { throw 'Runtime binary changed during preflight.' }
        $Again = Get-Quest3DSourceSession $TaskRoot $ControlDirectory $Candidate $Probe
        if (($Again | ConvertTo-Json -Compress) -cne ($Source | ConvertTo-Json -Compress)) { throw 'Source changed during rebind preflight.' }
        try {
            for ($Index = 0; $Index -lt $Entries.Count; $Index++) {
                $Changed += $Index # A partial Write must also be rolled back.
                Write-Quest3DRebindLockedFile $Streams[$Index] $Entries[$Index].after
            }
            $State.state = 'committed'
            $State | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $StatePath -Encoding utf8
        } catch {
            $Failure = $_
            $RollbackErrors = @()
            foreach ($Index in $Changed) {
                try {
                    # Direct stream operations keep rollback independent of the
                    # commit writer and repair even a partially written file.
                    $Stream = $Streams[$Index]
                    $Bytes = $Entries[$Index].before
                    $Stream.Position = 0
                    $Stream.Write($Bytes, 0, $Bytes.Length)
                    $Stream.SetLength($Bytes.Length)
                    $Stream.Flush($true)
                } catch { $RollbackErrors += $_.Exception.Message }
            }
            $State.state = if ($RollbackErrors.Count) { 'rollback_incomplete' } else { 'rolled_back' }
            $State.rollback_errors = $RollbackErrors
            $State | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $StatePath -Encoding utf8
            throw "Rebind failed ($($Failure.Exception.Message)); $($State.state). Journal: $Journal"
        }
        $Result.journal = $Journal
        return [pscustomobject]$Result
    } finally {
        foreach ($Stream in $Streams) { $Stream.Dispose() }
        # Only this invocation's freshly generated scratch directory is removed.
        foreach ($Name in @('candidate.conf', 'probe.jsonl')) {
            $ScratchFile = Join-Path $Scratch $Name
            if ([IO.File]::Exists($ScratchFile)) { [IO.File]::Delete($ScratchFile) }
        }
        [IO.Directory]::Delete($Scratch)
    }
}

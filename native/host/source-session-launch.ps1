# Read-only source/bridge binding. No capture, host or input is started here.
function Get-Quest3DBridgeProtocol($Manifest, [string]$Settings) {
    $TaskProtocol = if ($Manifest.PSObject.Properties.Name -contains 'bridge_protocol') { $Manifest.bridge_protocol } else { 2 }
    if (($TaskProtocol -isnot [int] -and $TaskProtocol -isnot [long]) -or $TaskProtocol -notin @(2, 3)) {
        throw 'Staged bridge protocol must be integer 2 or 3.'
    }
    $TaskMatches = [regex]::Matches($Settings, '(?mi)^\s*quest3d_protocol\s*=\s*([^\r\n]*)')
    if ($TaskMatches.Count -gt 1) { throw 'Host config has duplicate bridge protocol settings.' }
    $TaskConfigured = if ($TaskMatches.Count) { $TaskMatches[0].Groups[1].Value.Trim() } else { '2' }
    if ($TaskConfigured -notmatch '^[23]$' -or [int]$TaskConfigured -ne $TaskProtocol) {
        throw 'Staged and configured bridge protocols differ; prepare again.'
    }
    return [int]$TaskProtocol
}

function Get-Quest3DSourceSession([string]$TaskRoot, [string]$ControlDirectory, [string]$Config,
        [string]$Probe = '', [string]$ExpectedManifest = '', [bool]$EnableInput = $false) {
    if (!$ControlDirectory) { throw 'The product host requires a concrete source ControlDirectory.' }
    $TaskStart = [Diagnostics.ProcessStartInfo]::new()
    $TaskStart.FileName = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $TaskStart.WorkingDirectory = $TaskRoot
    $TaskStart.UseShellExecute = $false
    $TaskStart.CreateNoWindow = $true
    $TaskStart.RedirectStandardOutput = $true
    $TaskStart.RedirectStandardError = $true
    foreach ($TaskArgument in @('run', '--quiet', '--locked', '--offline', '--extra', 'gpu-capture',
            'python', (Join-Path $TaskRoot 'native/host/validate-source-session.py'), $ControlDirectory, $Config)) {
        $TaskStart.ArgumentList.Add($TaskArgument)
    }
    if ($Probe) { $TaskStart.ArgumentList.Add('--probe'); $TaskStart.ArgumentList.Add($Probe) }
    if ($ExpectedManifest) { $TaskStart.ArgumentList.Add('--expected-manifest'); $TaskStart.ArgumentList.Add($ExpectedManifest) }
    if ($EnableInput) { $TaskStart.ArgumentList.Add('--enable-input') }
    $TaskValidator = [Diagnostics.Process]::Start($TaskStart)
    $TaskOutput = $TaskValidator.StandardOutput.ReadToEndAsync()
    $TaskError = $TaskValidator.StandardError.ReadToEndAsync()
    if (!$TaskValidator.WaitForExit(10000)) { throw 'Source preflight exceeded 10 seconds. Inspect its live process before retrying.' }
    $TaskText = $TaskOutput.GetAwaiter().GetResult()
    $null = $TaskError.GetAwaiter().GetResult()
    if ($TaskValidator.ExitCode -ne 0) { throw ('Source preflight refused launch: ' + $TaskText.Trim()) }
    if ($TaskText.Length -gt 4096) { throw 'Unexpected source preflight output size.' }
    return ($TaskText | ConvertFrom-Json)
}

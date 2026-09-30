# Shared helpers keep configuration preparation and explicit server launch separate.
function Get-Quest3DPortConflict([int]$BasePort) {
    $TcpPorts = @(($BasePort - 5), $BasePort, ($BasePort + 1), ($BasePort + 21))
    $UdpPorts = @(($BasePort + 9), ($BasePort + 10), ($BasePort + 11))
    $Rows = & netstat.exe -ano
    if ($LASTEXITCODE -ne 0) { throw 'Could not inspect port ownership. No server was started.' }
    foreach ($Row in $Rows) {
        if ($Row -match '^\s*(TCP|UDP)\s+(\S+):(\d+)\s+.*\s(\d+)\s*$') {
            $Protocol = $Matches[1]
            $LocalPort = [int]$Matches[3]
            $TaskOwnerId = [int]$Matches[4]
            # TIME_WAIT from a closed server is not a listening-port conflict.
            if (($Protocol -eq 'TCP' -and $Row -match '\sLISTENING\s' -and $LocalPort -in $TcpPorts) -or ($Protocol -eq 'UDP' -and $LocalPort -in $UdpPorts)) {
                [pscustomobject]@{ protocol = $Protocol; port = $LocalPort; process_id = $TaskOwnerId }
            }
        }
    }
}

function Invoke-Quest3DCommand([string]$Executable, [string]$WorkingDirectory, [string[]]$Arguments, [string]$LogPath) {
    $Start = [Diagnostics.ProcessStartInfo]::new()
    $Start.FileName = $Executable
    $Start.WorkingDirectory = $WorkingDirectory
    $Start.UseShellExecute = $false
    $Start.CreateNoWindow = $true
    $Start.RedirectStandardOutput = $true
    $Start.RedirectStandardError = $true
    foreach ($Argument in $Arguments) { $Start.ArgumentList.Add($Argument) }
    $Process = [Diagnostics.Process]::Start($Start)
    $Output = $Process.StandardOutput.ReadToEndAsync()
    $Errors = $Process.StandardError.ReadToEndAsync()
    if (!$Process.WaitForExit(30000)) { throw 'Local command did not finish in 30 seconds; inspect the process before retrying.' }
    ($Output.GetAwaiter().GetResult() + $Errors.GetAwaiter().GetResult()) | Set-Content -LiteralPath $LogPath -Encoding utf8
    if ($Process.ExitCode -ne 0) { throw "Local command failed. See $LogPath" }
}

function Read-Quest3DDevManifest([string]$TaskRoot) {
    $Path = Join-Path $TaskRoot 'artifacts/host/dev/launch.json'
    if (!(Test-Path -LiteralPath $Path)) { throw 'Run native/host/prepare-dev-host.ps1 first.' }
    $Manifest = Get-Content -LiteralPath $Path -Raw | ConvertFrom-Json
    $Runtime = [IO.Path]::GetFullPath($Manifest.runtime)
    $Prefix = [IO.Path]::GetFullPath((Join-Path $TaskRoot 'artifacts/host')) + [IO.Path]::DirectorySeparatorChar
    if (!$Runtime.StartsWith($Prefix, [StringComparison]::OrdinalIgnoreCase) -or !(Split-Path -Leaf $Runtime).StartsWith('runtime-')) {
        throw 'Unexpected runtime path in development launch manifest.'
    }
    return $Manifest
}

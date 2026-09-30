[CmdletBinding()]
param(
    [switch]$Submit,
    [ValidatePattern('^[0-9a-fA-F]{32}$')][string]$PairingId,
    [switch]$PinFromStdin
)
$ErrorActionPreference = 'Stop'
$TaskProcess = $null
$TaskAccount = $null
$TaskSecurePin = $null
$TaskRequest = $null
$TaskPayload = $null
$TaskPinText = $null
$TaskPinJson = $null
$TaskExit = 1
$TaskStage = 'platform'

function ConvertTo-TransientPairingText([Security.SecureString]$SecureValue) {
    $TaskPointer = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($SecureValue)
    try { return [Runtime.InteropServices.Marshal]::PtrToStringBSTR($TaskPointer) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($TaskPointer) }
}

try {
    if (!$IsWindows -or $PSVersionTable.PSVersion.Major -lt 7) { throw 'Windows PowerShell 7 is required.' }
    if ($Submit -and !$PairingId) { throw 'Submit requires the exact request ID from a fresh read-only query.' }
    if (!$Submit -and ($PairingId -or $PinFromStdin)) { throw 'PIN input and request ID are accepted only with Submit.' }
    $TaskStage = 'read_manifest'
    $TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
    $TaskDev = Join-Path $TaskRoot 'artifacts/host/dev'
    $TaskManifest = Get-Content -LiteralPath (Join-Path $TaskDev 'launch.json') -Raw | ConvertFrom-Json
    $TaskAccess = [IO.Path]::GetFullPath((Join-Path $TaskDev 'web-access.clixml'))
    if ([IO.Path]::GetFullPath($TaskManifest.encrypted_web_access) -ne $TaskAccess) { throw 'Unexpected DPAPI credential path.' }
    $TaskStage = 'import_dpapi'
    $TaskAccount = Import-Clixml -LiteralPath $TaskAccess
    if ($TaskAccount -isnot [PSCredential]) { throw 'Expected this Windows user''s DPAPI credential.' }
    $TaskStage = 'prepare_memory_request'
    $TaskRequest = [ordered]@{
        mode = $(if ($Submit) { 'submit' } else { 'inspect' })
        username = $TaskAccount.UserName
        password = (ConvertTo-TransientPairingText $TaskAccount.Password)
    }
    if ($Submit) {
        if ($PinFromStdin) {
            if (![Console]::IsInputRedirected) { throw 'PinFromStdin requires redirected JSON input.' }
            $TaskCharacters = [char[]]::new(1025)
            $TaskLength = [Console]::In.ReadBlock($TaskCharacters, 0, $TaskCharacters.Length)
            if ($TaskLength -gt 1024) { throw 'PIN JSON is too large.' }
            $TaskPinJson = [string]::new($TaskCharacters, 0, $TaskLength)
            [Array]::Clear($TaskCharacters, 0, $TaskCharacters.Length)
            $TaskPinObject = ConvertFrom-Json -InputObject $TaskPinJson -AsHashtable
            if ($TaskPinObject -isnot [System.Collections.IDictionary] -or $TaskPinObject.Count -ne 1 -or !$TaskPinObject.ContainsKey('pin')) { throw 'Expected a JSON object with only pin.' }
            $TaskPinText = $TaskPinObject.pin
            $TaskPinObject.Clear()
        } else {
            $TaskSecurePin = Read-Host 'PIN currently displayed in Quest (4 digits)' -AsSecureString
            $TaskPinText = ConvertTo-TransientPairingText $TaskSecurePin
        }
        if ($TaskPinText -isnot [string] -or $TaskPinText -notmatch '^[0-9]{4}$') { throw 'PIN must be four ASCII digits.' }
        $TaskRequest.pin = $TaskPinText
        $TaskRequest.pairing_id = $PairingId.ToLowerInvariant()
    }
    $TaskPayload = $TaskRequest | ConvertTo-Json -Compress
    if ([Text.Encoding]::UTF8.GetByteCount($TaskPayload) -gt 8192) { throw 'Pairing request is too large.' }
    $TaskStage = 'start_offline_helper'
    $TaskStart = [Diagnostics.ProcessStartInfo]::new()
    $TaskStart.FileName = (Get-Command uv -CommandType Application -ErrorAction Stop | Select-Object -First 1).Source
    $TaskStart.WorkingDirectory = $TaskRoot
    $TaskStart.UseShellExecute = $false
    $TaskStart.CreateNoWindow = $true
    $TaskStart.RedirectStandardInput = $true
    $TaskStart.RedirectStandardOutput = $true
    $TaskStart.RedirectStandardError = $true
    $TaskStart.StandardInputEncoding = [Text.UTF8Encoding]::new($false)
    foreach ($TaskArgument in @('run', '--locked', '--offline', '--extra', 'gpu-capture', 'python', (Join-Path $PSScriptRoot 'pairing_host.py'))) {
        $TaskStart.ArgumentList.Add($TaskArgument)
    }
    $TaskProcess = [Diagnostics.Process]::Start($TaskStart)
    $TaskStage = 'write_memory_stdin'
    $TaskOutput = $TaskProcess.StandardOutput.ReadToEndAsync()
    $TaskErrors = $TaskProcess.StandardError.ReadToEndAsync()
    $TaskWrite = $TaskProcess.StandardInput.WriteAsync($TaskPayload)
    if (!$TaskWrite.Wait(5000)) { throw 'Pairing command input timed out.' }
    $TaskProcess.StandardInput.Close()
    $TaskPayload = $null
    $TaskRequest.Clear()
    $TaskPinText = $TaskPinJson = $null
    $TaskStage = 'wait_helper_result'
    if (!$TaskProcess.WaitForExit(20000)) { throw 'Pairing result is unknown after timeout; do not retry PIN automatically.' }
    $TaskExit = $TaskProcess.ExitCode
    $TaskOutputText = $TaskOutput.GetAwaiter().GetResult()
    $TaskErrorText = $TaskErrors.GetAwaiter().GetResult()
    if ($TaskExit -eq 0 -or $TaskExit -eq 2) {
        if (!$TaskOutputText.Trim()) { throw 'Pairing helper returned no result.' }
        Write-Output $TaskOutputText.Trim()
    } else {
        # Only the helper's fixed error code is exposed, never raw stderr/locals.
        $TaskCode = 'local_pairing_helper_failed'
        try {
            $TaskReported = $TaskErrorText | ConvertFrom-Json
            if ($TaskReported.code -is [string] -and $TaskReported.code -match '^[a-z_]{1,80}$') { $TaskCode = $TaskReported.code }
        } catch { }
        Write-Error "Pairing helper failed: $TaskCode. No automatic PIN retry was performed." -ErrorAction Continue
    }
} catch {
    # Import/JSON/process errors can contain secret values. Do not print $_.
    Write-Error "Local pairing helper failed at stage $TaskStage. No automatic PIN retry was performed; query pending requests again before another explicit submission." -ErrorAction Continue
    $TaskExit = 1
} finally {
    if ($TaskProcess) {
        if (!$TaskProcess.HasExited) {
            $TaskProcess.Kill($true) # Only this wrapper's uv/Python process tree.
            [void]$TaskProcess.WaitForExit(5000)
        }
        $TaskProcess.Dispose()
    }
    if ($TaskRequest) { $TaskRequest.Clear() }
    if ($TaskSecurePin) { $TaskSecurePin.Dispose() }
    if ($TaskAccount -is [PSCredential]) { $TaskAccount.Password.Dispose() }
    $TaskPayload = $TaskPinText = $TaskPinJson = $TaskAccount = $null
}
exit $TaskExit

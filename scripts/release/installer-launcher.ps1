<# Visible startup diagnostics; does not change Windows security policies. #>
[CmdletBinding()]
param([ValidateSet('pc','quest')][string]$Target='pc', [string]$LogPath, [switch]$NoDialog, [switch]$SelfTest)
$ErrorActionPreference = 'Stop'
$env:POWERSHELL_TELEMETRY_OPTOUT = 'true'
# .NET Framework 4.8 process settings; no machine policy or registry changes.
[AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling', $false)
[AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths', $false)
if ($PSVersionTable.PSVersion.Major -le 5) {
    foreach ($module in @('Utility','Management')) {
        Import-Module (Join-Path $PSHOME ('Modules/Microsoft.PowerShell.' + $module + '/Microsoft.PowerShell.' + $module + '.psd1')) -ErrorAction Stop
    }
}
function Assert-LauncherPath([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        try {
            if ([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint) { throw 'A linked launcher path is not supported.' }
        } catch {
            $cause = $_.Exception
            while ($cause.InnerException) { $cause = $cause.InnerException }
            if ($cause -isnot [IO.FileNotFoundException] -and $cause -isnot [IO.DirectoryNotFoundException]) { throw }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if (!$parent -or $parent -eq $current) { break }
        $current = $parent
    }
}
$safeLog = $null
$launcherMutex = $null
$launcherMutexOwned = $false
function Write-LauncherLog([string]$Path, [string]$Content) {
    $stream = [IO.File]::Open($Path,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes($Content)
        $stream.Write($bytes,0,$bytes.Length)
    } finally { $stream.Dispose() }
}
try {
    # The EXE bootstrap uses a separate parent mutex. This one also protects
    # the advanced ZIP/CMD entry, across installation folders for this user.
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    try { $mutexName = 'Local\Quest3D.Installer.' + $identity.User.Value }
    finally { $identity.Dispose() }
    $launcherMutex = New-Object Threading.Mutex($false, $mutexName)
    try { $launcherMutexOwned = $launcherMutex.WaitOne(0) }
    catch [Threading.AbandonedMutexException] { $launcherMutexOwned = $true }
    if (!$launcherMutexOwned) { throw '다른 Quest3D 설치창이 열려 있습니다. 기존 창에서 계속 진행해 주세요.' }
    $temporary = [IO.Path]::GetFullPath([IO.Path]::GetTempPath()).TrimEnd('\','/')
    if (!$LogPath) { $LogPath = Join-Path $temporary ('Quest3D-Start-' + [Guid]::NewGuid().ToString('N') + '.details.log') }
    $candidateLog = [IO.Path]::GetFullPath($LogPath)
    if (![StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetDirectoryName($candidateLog),$temporary) -or
        [IO.Path]::GetFileName($candidateLog) -cnotmatch '^Quest3D-Start-[A-Za-z0-9-]+\.details\.log$') { throw 'Startup logs must use the application filename in the current temporary directory.' }
    Assert-LauncherPath $candidateLog
    if (Test-Path -LiteralPath $candidateLog) { throw 'The startup log already exists. Start the installer again.' }
    $safeLog = $candidateLog
    $root = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
    $name = if ($Target -eq 'pc') { 'install-ui.ps1' } else { 'quest-install-ui.ps1' }
    $child = Join-Path $PSScriptRoot $name
    Assert-LauncherPath $child
    $manifestPath = Join-Path $root 'distribution-manifest.json'
    Assert-LauncherPath $manifestPath
    $manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    $entry = $manifest.files.PSObject.Properties['scripts/release/' + $name]
    if ($manifest.schema -ne 1 -or !$entry -or !(Test-Path -LiteralPath $child -PathType Leaf) -or
        (Get-Item -LiteralPath $child).Length -ne $entry.Value.bytes -or
        (Get-FileHash -LiteralPath $child -Algorithm SHA256).Hash -ine $entry.Value.sha256) { throw 'Installer files are missing or changed. Extract the complete trusted release ZIP again.' }
    $shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    $info = New-Object Diagnostics.ProcessStartInfo
    $info.FileName = $shell
    $info.Arguments = '-NoProfile -STA -ExecutionPolicy Bypass -File "' + $child + '"'
    if ($SelfTest) { $info.Arguments += ' -SelfTest' }
    $info.UseShellExecute = $false; $info.CreateNoWindow = $true
    $info.RedirectStandardOutput = $true; $info.RedirectStandardError = $true
    $process = New-Object Diagnostics.Process
    $process.StartInfo = $info
    if (!$process.Start()) { throw 'The installation window did not start.' }
    $output = $process.StandardOutput.ReadToEndAsync()
    $errors = $process.StandardError.ReadToEndAsync()
    $process.WaitForExit()
    $code = $process.ExitCode
    $captured = 'Target: ' + $Target + "`r`nExit code: " + $code + "`r`n" + $output.Result + "`r`n" + $errors.Result
    $process.Dispose()
    Write-LauncherLog $safeLog $captured
    if ($code -ne 0) { throw 'The installation window could not start. Check the startup log. A managed Windows policy may require your administrator.' }
    Write-Output ('Installer window finished. Startup log: ' + $safeLog)
    exit 0
} catch {
    $message = $_.Exception.Message
    if ($safeLog -and !(Test-Path -LiteralPath $safeLog)) {
        try { Write-LauncherLog $safeLog $message } catch { $safeLog = $null }
    }
    if ($safeLog) { $message += "`r`nLog: " + $safeLog }
    Write-Error -Message $message -ErrorAction Continue
    if (!$NoDialog) {
        try {
            Add-Type -AssemblyName System.Windows.Forms
            [void][Windows.Forms.MessageBox]::Show($message,'Quest3D installer startup',[Windows.Forms.MessageBoxButtons]::OK,[Windows.Forms.MessageBoxIcon]::Error)
        } catch { }
    }
    exit 1
} finally {
    if ($launcherMutexOwned) { $launcherMutex.ReleaseMutex() }
    if ($launcherMutex) { $launcherMutex.Dispose() }
}

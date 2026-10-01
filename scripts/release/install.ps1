<# Per-user installer. No service, startup task, firewall rule or browser setting is changed. #>
[CmdletBinding()]
param(
    [string]$Destination = (Join-Path $env:LOCALAPPDATA 'Quest3D Desktop'),
    [string]$Python,
    [switch]$VerifyOnly,
    [switch]$PrepareOnly,
    [switch]$NoShortcuts,
    [switch]$Update
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'installation-lifecycle.ps1')
$packageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$manifestPath = Join-Path $packageRoot 'distribution-manifest.json'
if (!(Test-Path -LiteralPath $manifestPath -PathType Leaf)) { throw 'Extract the complete installer ZIP first.' }
$manifest = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $null -eq $manifest.files) { throw 'Unsupported package manifest.' }
$packageHash = (Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256).Hash

function Assert-SafePath([string]$Root, [string]$Relative) {
    if ([string]::IsNullOrWhiteSpace($Relative) -or $Relative -match '[:\\\x00]' -or
        $Relative.StartsWith('/') -or $Relative -match '(^|/)(\.{1,2}|[^/]*[. ])(/|$)') { throw "Invalid package path: $Relative" }
    foreach ($part in $Relative.Split('/')) {
        if (!$part -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') { throw 'Reserved package path.' }
    }
    $path = [IO.Path]::GetFullPath((Join-Path $Root $Relative))
    $prefix = $Root.TrimEnd('\', '/') + [IO.Path]::DirectorySeparatorChar
    if (!$path.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) { throw 'Package path escaped root.' }
    $current = $path
    while ($current.Length -ge $Root.Length) {
        if (Test-Path -LiteralPath $current) {
            if ((Get-Item -LiteralPath $current -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point refused: $current" }
        }
        if ($current -eq $Root) { break }
        $current = Split-Path -Parent $current
    }
    return $path
}

Write-Host '[1/6] Checking installer files...'
$reviewedPackage = Get-Quest3DPackage $packageRoot -VerifyFiles
$seen = @{}
foreach ($entry in $manifest.files.PSObject.Properties) {
    if ($seen.ContainsKey($entry.Name.ToLowerInvariant())) { throw 'Duplicate package path.' }
    $seen[$entry.Name.ToLowerInvariant()] = $true
    $file = Assert-SafePath $packageRoot $entry.Name
    if (!(Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -ne $entry.Value.bytes -or
        (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value.sha256) { throw "Damaged installer: $($entry.Name)" }
}
if ($VerifyOnly) { Write-Output 'Package integrity verified. No installation performed.'; return }
$installRoot = Get-Quest3DInstallRoot $Destination
if ($installRoot.IndexOfAny([char[]]"#`"`r`n") -ge 0 -or $installRoot -eq [IO.Path]::GetPathRoot($installRoot)) { throw 'Choose a dedicated installation folder without #, quotes or line breaks.' }
if ($installRoot -eq $packageRoot -or $packageRoot.StartsWith($installRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Installer and destination must be separate folders.' }
$owner = Join-Path $installRoot 'quest3d-install.json'
$transaction = $null
if (Test-Path -LiteralPath $installRoot -PathType Container) {
    $interrupted = Get-Quest3DTransaction $installRoot
    if ($interrupted -and $interrupted.journal.phase -notin @('committed','rolled-back')) {
        $restored = Restore-Quest3DUpdate $installRoot
        Write-Host ('Interrupted update restored: ' + $restored.release)
    }
}
if (Test-Path -LiteralPath $installRoot) {
    if ((Get-Item -LiteralPath $installRoot -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Destination cannot be a reparse point.' }
    if (@(Get-ChildItem -LiteralPath $installRoot -Force).Count -gt 0) {
        if (!(Test-Path -LiteralPath $owner -PathType Leaf)) { throw 'Existing folder is not a Quest3D installation; it was preserved. Choose an empty folder.' }
        $previous = Get-Content -LiteralPath $owner -Raw | ConvertFrom-Json
        $installed = Get-Quest3DOwnedInstall $installRoot
        if ($previous.package_manifest_sha256 -ine $packageHash) {
            if (!$Update) { throw 'Another Quest3D version is installed. Select Update to preserve its settings and pairing.' }
            if ($PrepareOnly) { throw 'Files-only preparation cannot commit an update; use the complete installer.' }
            $transaction = Start-Quest3DUpdate $installRoot $reviewedPackage
        }
    }
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'sunshine.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($installRoot + '\', [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine.IndexOf($installRoot, [StringComparison]::OrdinalIgnoreCase) -ge 0) })
    if ($running.Count) { throw 'This installation is running. Use PC stop and exit in Quest3D Desktop, then run the installer again.' }
}
[void](New-Item -ItemType Directory -Force -Path $installRoot)
try {
if ($transaction) {
    Write-Host '[2/6] Updating owned application files; settings and pairing stay on this PC...'
    Invoke-Quest3DUpdateFiles $transaction $reviewedPackage
    Move-Quest3DUpdateEnvironment $transaction
}
@{ product = 'Quest3D Desktop'; package_manifest_sha256 = $packageHash; release = $manifest.release; completed = $false } |
    ConvertTo-Json | Set-Content -LiteralPath $owner -Encoding UTF8
Write-Host '[2/6] Copying application into your user folder...'
foreach ($entry in $manifest.files.PSObject.Properties) {
    $source = Assert-SafePath $packageRoot $entry.Name
    $target = Assert-SafePath $installRoot $entry.Name
    if (Test-Path -LiteralPath $target) {
        if (!(Test-Path -LiteralPath $target -PathType Leaf) -or (Get-FileHash -LiteralPath $target).Hash -ine $entry.Value.sha256) {
            throw "Existing file changed; preserved: $target"
        }
        continue
    }
    [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target))
    Copy-Item -LiteralPath $source -Destination $target
}
Copy-Item -LiteralPath $manifestPath -Destination (Join-Path $installRoot 'distribution-manifest.json') -Force
if ($PrepareOnly) { Write-Output "Prepared files only: $installRoot. Python/dependencies/shortcuts were not installed."; return }

. (Join-Path $PSScriptRoot 'python-discovery.ps1')
function Test-Python([string]$Candidate) { return Test-Quest3DPythonRuntime $Candidate }
Write-Host '[3/6] Preparing Python 3.12.6 with Tk...'
$localPython = Join-Path $installRoot '.tools/python/python.exe'
if ($Python) {
    if (!(Test-Python $Python)) { throw 'The selected Python must be 64-bit Python 3.12.6 with Tk.' }
    $localPython = [IO.Path]::GetFullPath($Python)
} elseif (!(Test-Python $localPython)) {
    $existingPython = Get-Quest3DExistingPythonRuntime
    if ($existingPython.reused) {
        $localPython = $existingPython.path
        Write-Host 'Reusing compatible Python; existing installations are preserved.'
    } else {
    $downloadDirectory = Join-Path $installRoot '.cache/install'
    [void](New-Item -ItemType Directory -Force -Path $downloadDirectory)
    $installer = Join-Path $downloadDirectory 'python-3.12.6-amd64.exe'
    $pythonHash = '5914748e6580e70bedeb7c537a0832b3071de9e09a2e4e7e3d28060616045e0a'
    if (!(Test-Path -LiteralPath $installer)) {
        [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
        Invoke-WebRequest -UseBasicParsing 'https://www.python.org/ftp/python/3.12.6/python-3.12.6-amd64.exe' -OutFile ($installer + '.partial')
        if ((Get-FileHash -LiteralPath ($installer + '.partial')).Hash -ine $pythonHash) { throw 'Python download checksum failed.' }
        Move-Item -LiteralPath ($installer + '.partial') -Destination $installer
    }
    if ((Get-FileHash -LiteralPath $installer).Hash -ine $pythonHash) { throw 'Python installer checksum failed.' }
    $pythonTarget = Split-Path -Parent $localPython
    # Python's per-user installer may register itself under HKCU. No admin install, PATH,
    # launcher, file association, global shortcuts, or Windows startup entry is requested.
    $arguments = @('/quiet', 'InstallAllUsers=0', ('TargetDir="' + $pythonTarget + '"'), 'Include_tcltk=1',
        'Include_pip=0', 'Include_test=0', 'Include_doc=0', 'Include_launcher=0', 'InstallLauncherAllUsers=0',
        'PrependPath=0', 'AppendPath=0', 'AssociateFiles=0', 'Shortcuts=0')
    $process = Start-Process -FilePath $installer -ArgumentList $arguments -WindowStyle Hidden -PassThru -Wait
    if ($process.ExitCode -notin @(0, 3010) -or !(Test-Python $localPython)) {
        throw 'Python installation did not complete. If Python 3.12 is already installed, rerun with -Python <path-to-python.exe> (3.12.6 + Tk required).'
    }
    }
}
Write-Host '[4/6] Installing pinned GPU libraries. The first download is several GB...'
$uv = Join-Path $installRoot '.tools/desktop/uv.exe'
$oldLocation = Get-Location
$previousUvCache = [Environment]::GetEnvironmentVariable('UV_CACHE_DIR', 'Process')
$env:UV_CACHE_DIR = Get-Quest3DInstallPath $installRoot '.cache/uv'
try {
    Set-Location -LiteralPath $installRoot
    if (!(Test-Path -LiteralPath '.venv')) {
        & $uv venv --python $localPython '.venv'
        if ($LASTEXITCODE -ne 0) { throw 'Cannot create the application Python environment.' }
    } elseif (!(Test-Python (Join-Path $installRoot '.venv/Scripts/python.exe'))) {
        throw 'Existing environment is incompatible; it was preserved. Use a separate install folder.'
    }
    & $uv sync --locked --extra gpu-capture --no-dev --python $localPython
    if ($LASTEXITCODE -ne 0) { throw 'Pinned dependency installation failed. Re-run this installer to resume.' }
    $applicationPython = Join-Path $installRoot '.venv/Scripts/python.exe'
    Write-Host 'Checking Turing sm75 GPU and the packaged CUDA tone/depth/stereo kernels...'
    & $applicationPython (Join-Path $installRoot 'scripts/release/verify_installed_gpu.py') --report (Join-Path $installRoot '.cache/install/gpu-check.json')
    if ($LASTEXITCODE -ne 0) { throw 'CUDA runtime validation failed. This preview requires a supported NVIDIA Turing sm75 GPU and driver; see the install log.' }
    Write-Host '[5/6] Downloading and verifying the pinned Depth Anything V2 Small model (99 MB)...'
    & $applicationPython -m quest3d.cli setup-model
    if ($LASTEXITCODE -ne 0) { throw 'Model setup failed. Re-run this installer to resume.' }
    Write-Host 'Checking Qt 6.8.3, bundled fonts/icons and the actual application screen...'
    & $applicationPython (Join-Path $installRoot 'scripts/release/verify_installed_ui.py') --root $installRoot --report (Join-Path $installRoot '.cache/install/ui-check.json')
    if ($LASTEXITCODE -ne 0) { throw 'Application screen validation failed. Review the install log and retry; this installation is not marked complete.' }
    Write-Host '[6/6] Creating desktop and Start menu shortcuts...'
    if (!$NoShortcuts) { & (Join-Path $installRoot 'scripts/install-desktop-shortcut.ps1') -Root $installRoot -Desktop -StartMenu -ReplaceOwned }
    @{ product = 'Quest3D Desktop'; package_manifest_sha256 = $packageHash; release = $manifest.release; completed = $true; python = $localPython } |
        ConvertTo-Json | Set-Content -LiteralPath $owner -Encoding UTF8
    if ($transaction) { Complete-Quest3DUpdate $transaction }
    Write-Host "Installed: $installRoot"
    Write-Host 'Open Quest3D Desktop -> PC start -> pair/connect from Quest.'
} finally {
    Set-Location -LiteralPath $oldLocation
    $env:UV_CACHE_DIR = $previousUvCache
}
} catch {
    $installFailure = $_
    if ($transaction) {
        try {
            $restored = Restore-Quest3DUpdate $installRoot
            Write-Host ('Update failed. Previous version restored: ' + $restored.release)
        } catch { throw ('Update failed and automatic restoration needs attention. Preserve the installation and its backup. Recovery error: ' + $_.Exception.Message + '. Install error: ' + $installFailure.Exception.Message) }
    }
    throw $installFailure
}

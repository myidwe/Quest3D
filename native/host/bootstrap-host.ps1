[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$taskRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$taskLock = Get-Content -LiteralPath (Join-Path $PSScriptRoot 'toolchain.lock.json') -Raw | ConvertFrom-Json
$taskDownloadDir = Join-Path $taskRoot 'artifacts\host\downloads'
$taskTools = Join-Path $PSScriptRoot 'tools'
New-Item -ItemType Directory -Path $taskDownloadDir,$taskTools -Force | Out-Null

function Get-VerifiedArchive([string]$Url, [string]$Hash, [string]$Destination) {
    if (-not (Test-Path -LiteralPath $Destination)) {
        Invoke-WebRequest -Uri $Url -OutFile $Destination
    }
    $taskActual = (Get-FileHash -LiteralPath $Destination -Algorithm SHA256).Hash
    if ($taskActual -ne $Hash) { throw "Archive checksum mismatch: $Destination" }
}

$taskBaseArchive = Join-Path $taskDownloadDir 'msys2-base-x86_64-20260611.tar.xz'
Get-VerifiedArchive $taskLock.msys2_base.url $taskLock.msys2_base.sha256 $taskBaseArchive
$taskMsys = Join-Path $taskTools 'msys64'
$taskFreshMsys = -not (Test-Path -LiteralPath $taskMsys)
if ($taskFreshMsys) {
    & tar -xf $taskBaseArchive -C $taskTools
    if ($LASTEXITCODE -ne 0) { throw 'MSYS2 extraction failed.' }
}

$taskNodeArchive = Join-Path $taskDownloadDir 'node-v24.20.0-win-x64.zip'
Get-VerifiedArchive $taskLock.node.url $taskLock.node.sha256 $taskNodeArchive
if (-not (Test-Path -LiteralPath (Join-Path $taskTools 'node-v24.20.0-win-x64'))) {
    Expand-Archive -LiteralPath $taskNodeArchive -DestinationPath $taskTools
}

if (-not $taskFreshMsys) {
    Write-Host 'Existing isolated MSYS2 preserved. Use build-host.ps1 to validate its recorded package lock.'
    return
}

$taskPackageDir = Join-Path $taskMsys 'var\cache\pacman\pkg'
New-Item -ItemType Directory -Path $taskPackageDir -Force | Out-Null
$taskPackageNames = foreach ($taskPackage in $taskLock.packages) {
    Get-VerifiedArchive $taskPackage.url $taskPackage.sha256 (Join-Path $taskPackageDir $taskPackage.file)
    '/var/cache/pacman/pkg/' + $taskPackage.file
}
[IO.File]::WriteAllText((Join-Path $taskMsys 'quest3d-packages.txt'), ($taskPackageNames -join "`n") + "`n", [Text.UTF8Encoding]::new($false))
$taskRestore = (Join-Path $PSScriptRoot 'restore-toolchain.sh').Replace('\', '/')
if ($taskRestore.Contains("'")) { throw 'The workspace path must not contain a single quote.' }
$taskShell = Join-Path $taskMsys 'msys2_shell.cmd'
# MSYS2 runtime upgrades close their shell, so perform that transaction separately.
& $taskShell -defterm -here -no-start -ucrt64 -c "bash '$taskRestore' runtime"
if ($LASTEXITCODE -ne 0) { throw 'MSYS2 runtime restore failed.' }
& $taskShell -defterm -here -no-start -ucrt64 -c "bash '$taskRestore' packages"
if ($LASTEXITCODE -ne 0) { throw 'MSYS2 locked package restore failed.' }
Write-Host 'Restored pinned host toolchain; no rolling repository upgrade was requested.'

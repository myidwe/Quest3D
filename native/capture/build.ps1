param([switch]$TestNative, [switch]$Bootstrap, [switch]$ExperimentalHdr, [switch]$OfflineTools)
$ErrorActionPreference = 'Stop'
$captureRepo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$captureBootstrap = Join-Path $captureRepo '.tools\capture-build'
$env:RUSTUP_HOME = Join-Path $captureBootstrap 'rustup'
$env:CARGO_HOME = Join-Path $captureBootstrap 'cargo'
$env:RUSTUP_TOOLCHAIN = '1.88.0'
$env:UV_TOOL_DIR = Join-Path $captureBootstrap 'uv-tools'
$env:UV_CACHE_DIR = Join-Path $captureBootstrap 'uv-cache'
$env:PATH = (Join-Path $env:CARGO_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH
$env:PYO3_PYTHON = Join-Path $captureRepo '.venv\Scripts\python.exe'
$captureSource = Join-Path $captureRepo 'third_party\wc_cuda'
$captureRevision = '6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc'
$capturePatch = Join-Path $PSScriptRoot 'wc_cuda-quest1.patch'
$captureHdrPatch = Join-Path $PSScriptRoot 'wc_cuda-quest2-hdr.patch'
if (-not (Test-Path -LiteralPath $captureSource)) {
    git clone --no-checkout https://github.com/nagadomi/wc_cuda.git $captureSource
    if ($LASTEXITCODE -ne 0) { throw 'Official source clone failed' }
    git -C $captureSource checkout --detach $captureRevision
    if ($LASTEXITCODE -ne 0) { throw 'Pinned source checkout failed' }
}
$captureHead = git -C $captureSource rev-parse HEAD
if ($captureHead -ne $captureRevision) { throw 'Source revision differs; existing checkout has been preserved' }
$captureHdrApplied = $false
if ($ExperimentalHdr) {
    git -C $captureSource apply --reverse --check $captureHdrPatch 2>$null
    $captureHdrApplied = $LASTEXITCODE -eq 0
}
if (!$captureHdrApplied) {
    git -C $captureSource apply --reverse --check $capturePatch 2>$null
    if ($LASTEXITCODE -ne 0) {
        git -C $captureSource apply --check $capturePatch
        if ($LASTEXITCODE -ne 0) { throw 'Patch conflicts with existing source; no files were replaced' }
        git -C $captureSource apply $capturePatch
        if ($LASTEXITCODE -ne 0) { throw 'Applying the local capture patch failed' }
    }
    if ($ExperimentalHdr) {
        git -C $captureSource apply --check $captureHdrPatch
        if ($LASTEXITCODE -ne 0) { throw 'HDR candidate conflicts; source was preserved' }
        git -C $captureSource apply $captureHdrPatch
        if ($LASTEXITCODE -ne 0) { throw 'HDR candidate patch failed' }
    }
}
$captureLock = Join-Path $captureSource 'Cargo.lock'
$captureLockSource = Join-Path $PSScriptRoot $(if ($ExperimentalHdr) { 'Cargo.hdr.lock' } else { 'Cargo.lock' })
if (Test-Path -LiteralPath $captureLock) {
    if ((Get-FileHash -LiteralPath $captureLock).Hash -ne (Get-FileHash -LiteralPath $captureLockSource).Hash) {
        if ($ExperimentalHdr -and (Get-FileHash -LiteralPath $captureLock).Hash -eq (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'Cargo.lock')).Hash) {
            Copy-Item -LiteralPath $captureLockSource -Destination $captureLock
        } else { throw 'Existing Cargo.lock differs from the recorded dependency lock; it has been preserved' }
    }
} else { Copy-Item -LiteralPath $captureLockSource -Destination $captureLock }
if ($Bootstrap) {
    New-Item -ItemType Directory -Path $captureBootstrap -Force | Out-Null
    $captureRustup = Join-Path $captureBootstrap 'rustup-init-1.28.2.exe'
    if (-not (Test-Path -LiteralPath $captureRustup)) {
        Invoke-WebRequest -Uri 'https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-pc-windows-msvc/rustup-init.exe' -OutFile $captureRustup
    }
    if ((Get-FileHash -LiteralPath $captureRustup -Algorithm SHA256).Hash.ToLowerInvariant() -ne '88d8258dcf6ae4f7a80c7d1088e1f36fa7025a1cfd1343731b4ee6f385121fc0') {
        throw 'Rustup 1.28.2 SHA256 mismatch'
    }
    & $captureRustup -y --no-modify-path --profile minimal --default-toolchain 1.88.0 --default-host x86_64-pc-windows-msvc
    if ($LASTEXITCODE -ne 0) { throw 'Isolated Rust bootstrap failed' }
}
if (-not (Test-Path -LiteralPath (Join-Path $env:CARGO_HOME 'bin\cargo.exe'))) {
    throw 'Rust 1.88.0 is missing; rerun this script with -Bootstrap'
}
$captureVswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$captureVs = & $captureVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $captureVs) { throw 'Visual Studio C++ build tools are required' }
Import-Module (Join-Path $captureVs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $captureVs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64'
# Developer PowerShell may reconstruct PATH from its saved baseline.
$env:PATH = (Join-Path $env:CARGO_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH
Push-Location $captureRepo
try {
    if ($TestNative) {
        cargo +1.88.0 test --release --locked --manifest-path third_party/wc_cuda/Cargo.toml
        if ($LASTEXITCODE -ne 0) { throw 'Native lease tests failed' }
    }
    $captureOutput = if ($ExperimentalHdr) { 'artifacts/capture/hdr-experimental' } else { 'artifacts/capture' }
    [string[]]$captureUvOptions = @()
    if ($OfflineTools) { $captureUvOptions += '--offline' }
    uv tool run @captureUvOptions --from maturin==1.9.4 maturin build --release --locked --manifest-path third_party/wc_cuda/Cargo.toml --out $captureOutput --interpreter $env:PYO3_PYTHON
    if ($LASTEXITCODE -ne 0) { throw 'Patched capture wheel build failed' }
    $captureVersion = if ($ExperimentalHdr) { 'quest2' } else { 'quest1' }
    Get-FileHash -LiteralPath (Join-Path (Join-Path $captureRepo $captureOutput) "wc_cuda-0.1.2+$captureVersion-cp310-abi3-win_amd64.whl") -Algorithm SHA256
} finally { Pop-Location }

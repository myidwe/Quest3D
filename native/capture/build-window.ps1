param([switch]$TestNative, [switch]$NativeOnly)
$ErrorActionPreference = 'Stop'
$windowCaptureRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$windowCaptureTools = Join-Path $windowCaptureRoot '.tools\capture-build'
$env:RUSTUP_HOME = Join-Path $windowCaptureTools 'rustup'
$env:CARGO_HOME = Join-Path $windowCaptureTools 'cargo'
$env:RUSTUP_TOOLCHAIN = '1.88.0'
$env:UV_TOOL_DIR = Join-Path $windowCaptureTools 'uv-tools'
$env:UV_CACHE_DIR = Join-Path $windowCaptureTools 'uv-cache'
$env:PYO3_PYTHON = Join-Path $windowCaptureRoot '.venv\Scripts\python.exe'
$windowCaptureSource = Join-Path $windowCaptureRoot 'third_party\wc_cuda'
if ((git -C $windowCaptureSource rev-parse HEAD) -ne '6f6c6eaed91f36f0e937f1da92cf5cfc35a9bfcc') {
    throw 'Unexpected capture source revision; existing checkout preserved'
}
git -C $windowCaptureSource apply --reverse --check (Join-Path $PSScriptRoot 'wc_cuda-quest3-window.patch')
if ($LASTEXITCODE -ne 0) { throw 'The exact quest3 candidate patch is not applied; source preserved' }
if ((Get-FileHash -LiteralPath (Join-Path $windowCaptureSource 'Cargo.lock')).Hash -ne
    (Get-FileHash -LiteralPath (Join-Path $PSScriptRoot 'Cargo.window.lock')).Hash) {
    throw 'Quest3 Cargo lock differs; source preserved'
}
& $env:PYO3_PYTHON (Join-Path $PSScriptRoot 'prepare-window-dependency.py')
if ($LASTEXITCODE -ne 0) { throw 'Vendored capture dependency verification failed' }
$windowCaptureVswhere = 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe'
$windowCaptureVs = & $windowCaptureVswhere -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $windowCaptureVs) { throw 'Visual Studio C++ build tools are required' }
Import-Module (Join-Path $windowCaptureVs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $windowCaptureVs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64'
$env:PATH = (Join-Path $env:CARGO_HOME 'bin') + [IO.Path]::PathSeparator + $env:PATH
Push-Location $windowCaptureRoot
try {
    if ($TestNative -or $NativeOnly) {
        cargo +1.88.0 test --release --locked --offline --manifest-path third_party/wc_cuda/Cargo.toml
        if ($LASTEXITCODE -ne 0) { throw 'Native HWND/capture tests failed' }
    }
    if ($NativeOnly) { return }
    uv tool run --offline --from maturin==1.9.4 maturin build --release --locked --offline --manifest-path third_party/wc_cuda/Cargo.toml --out artifacts/capture/window-experimental --interpreter $env:PYO3_PYTHON
    if ($LASTEXITCODE -ne 0) { throw 'Quest3 window candidate build failed' }
    Get-FileHash -LiteralPath (Join-Path $windowCaptureRoot 'artifacts\capture\window-experimental\wc_cuda-0.1.2+quest3-cp310-abi3-win_amd64.whl') -Algorithm SHA256
} finally { Pop-Location }

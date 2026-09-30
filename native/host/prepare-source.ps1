param([switch]$SkipSubmodules)
$ErrorActionPreference = 'Stop'
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskSource = Join-Path $TaskRoot 'third_party/sunshine'
$TaskPatchDirectory = Join-Path $TaskRoot 'patches/sunshine'
$ExpectedCommit = 'cb72dffa3233c5815cd5ba88f09f049dd679ba75'
$TaskArtifact = Join-Path $TaskRoot 'artifacts/host'
New-Item -ItemType Directory -Path $TaskArtifact -Force | Out-Null
$TaskIgnore = Join-Path $TaskArtifact 'empty-git-ignore'
if (!(Test-Path -LiteralPath $TaskIgnore)) { Set-Content -LiteralPath $TaskIgnore -Value '' -NoNewline }
function Invoke-SourceGit([string[]]$Arguments) {
    & git -c "core.excludesFile=$TaskIgnore" @Arguments
    if ($LASTEXITCODE -ne 0) { throw "Git source preparation failed; existing files were preserved." }
}
if (!(Test-Path -LiteralPath (Join-Path $TaskSource '.git'))) {
    if (Test-Path -LiteralPath $TaskSource) {
        throw "Existing non-Git Sunshine source directory is preserved: $TaskSource"
    }
    Invoke-SourceGit @('clone', '--depth', '1', '--branch', 'v2026.906.222525', 'https://github.com/LizardByte/Sunshine.git', $TaskSource)
}
$ActualCommit = (& git -C $TaskSource rev-parse HEAD).Trim()
if ($LASTEXITCODE -ne 0 -or $ActualCommit -ne $ExpectedCommit) {
    throw "Expected Sunshine $ExpectedCommit; found $ActualCommit. Existing checkout was preserved."
}
if (!$SkipSubmodules) {
    $TaskModules = @(
        'third-party/build-deps', 'third-party/glad', 'third-party/libdisplaydevice',
        'third-party/libvirtualhid', 'third-party/lizardbyte-common', 'third-party/moonlight-common-c',
        'third-party/nvapi', 'third-party/Simple-Web-Server', 'third-party/ViGEmClient'
    )
    Invoke-SourceGit (@('-C', $TaskSource, 'submodule', 'update', '--init', '--recursive', '--depth', '1', '--') + $TaskModules)
}
$TaskPatches = @(Get-ChildItem -LiteralPath $TaskPatchDirectory -Filter '*.patch' -File | Sort-Object Name)
if (!$TaskPatches.Count) { throw "Missing reproducible Sunshine patches: $TaskPatchDirectory" }
foreach ($TaskPatch in $TaskPatches) {
    & git -c "core.excludesFile=$TaskIgnore" -C $TaskSource apply --reverse --check $TaskPatch.FullName 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Output "Pinned Sunshine source already has $($TaskPatch.Name)."
    } else {
        Invoke-SourceGit @('-C', $TaskSource, 'apply', '--check', $TaskPatch.FullName)
        Invoke-SourceGit @('-C', $TaskSource, 'apply', $TaskPatch.FullName)
        Write-Output "Applied $($TaskPatch.Name) to the pinned Sunshine source."
    }
}

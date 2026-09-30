[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskBuild = Join-Path $TaskRoot 'third_party/sunshine/cmake-build-quest3d'
$TaskArtifacts = Join-Path $TaskRoot 'artifacts/host'
$TaskFilter = 'Quest3DPointerRuntime.*:Quest3DPointer.*:Quest3DInputDispatch.*:Quest3DControl.*:Quest3DFrameLedger.*:ClientAuthorizationTest.*:*Framerate*:*CaptureFrameInterval*:*H264Profile*:*DynamicRange*:*SoftwareEncoderConversion*:*NvencVersion*:*NvencDynamicFactory*:*NvencSharedDll*'
$TaskStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$TaskXml = Join-Path $TaskArtifacts "host-regression-$TaskStamp.xml"
$TaskCoverage = Join-Path $TaskArtifacts "coverage-$TaskStamp"
New-Item -ItemType Directory -Path $TaskCoverage -Force | Out-Null
$PreviousCoverage = $env:GCOV_PREFIX
try {
    $env:GCOV_PREFIX = $TaskCoverage
    & (Join-Path $TaskArtifacts 'frame_bridge_test.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Native bridge regression failed.' }
    & (Join-Path $TaskArtifacts 'frame_bridge_v3_test.exe')
    if ($LASTEXITCODE -ne 0) { throw 'Native v3 source bridge regression failed.' }
    Push-Location $TaskBuild
    try {
        & './tests/test_sunshine.exe' "--gtest_filter=$TaskFilter" "--gtest_output=xml:$TaskXml"
        if ($LASTEXITCODE -ne 0) { throw 'Host input/video regression failed.' }
    } finally { Pop-Location }
} finally { $env:GCOV_PREFIX = $PreviousCoverage }
[xml]$TaskResult = Get-Content -LiteralPath $TaskXml -Raw
if ([int]$TaskResult.testsuites.tests -lt 118 -or [int]$TaskResult.testsuites.failures -ne 0 -or [int]$TaskResult.testsuites.disabled -ne 0) {
    throw 'Expected host regressions were not all executed successfully.'
}
Push-Location $TaskRoot
try {
    uv run --locked --extra gpu-capture python native/host/test_singleton.py --probe artifacts/host/instance_guard_probe.exe --host third_party/sunshine/cmake-build-quest3d/sunshine.exe --artifacts artifacts/host
    if ($LASTEXITCODE -ne 0) { throw 'Real process singleton regression failed.' }
} finally { Pop-Location }
$TaskSingleton = Get-Content -LiteralPath (Join-Path $TaskArtifacts 'singleton-verification.json') -Raw | ConvertFrom-Json
if ($TaskSingleton.passed -ne 9) { throw 'Expected singleton checks were not all executed.' }
$TaskProof = [ordered]@{
    verified_at = (Get-Date).ToString('o')
    host_sha256 = (Get-FileHash -LiteralPath (Join-Path $TaskBuild 'sunshine.exe')).Hash
    tests_sha256 = (Get-FileHash -LiteralPath (Join-Path $TaskBuild 'tests/test_sunshine.exe')).Hash
    tests = [int]$TaskResult.testsuites.tests
    singleton_checks = $TaskSingleton.passed
    results = $TaskXml
    actual_os_input = $false
    quest_verified = $false
}
$TaskProof | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $TaskArtifacts 'host-verification.json') -Encoding utf8
Write-Output 'Bridge and host input/video/control/certificate regressions passed. Staging proof was updated.'

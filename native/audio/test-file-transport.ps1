[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskExe = Join-Path $TaskRoot 'third_party/sunshine/cmake-build-quest3d/tests/test_sunshine.exe'
if (!(Test-Path -LiteralPath $TaskExe -PathType Leaf)) { throw 'Build the pinned FilePCM candidate first.' }
$TaskId = (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8)
$TaskDirectory = Join-Path $TaskRoot "artifacts/audio/file-pcm-host-retirement-$TaskId"
$TaskCoverage = Join-Path $TaskDirectory 'coverage'
$TaskXml = Join-Path $TaskDirectory 'native-tests.xml'
$TaskLog = Join-Path $TaskDirectory 'native-tests.log'
$TaskFilter = 'Quest3DReaderRetirement.*:Quest3DFileTransportIdentity.*:Quest3DFileTransport.*:QuestAudioRing.*:QuestAudioPacketGate.*:QuestAudioQueue.*'
New-Item -ItemType Directory -Path $TaskCoverage | Out-Null
$TaskPreviousCoverage = $env:GCOV_PREFIX
try {
    $env:GCOV_PREFIX = $TaskCoverage
    Push-Location -LiteralPath $TaskDirectory
    try {
        & $TaskExe "--gtest_filter=$TaskFilter" "--gtest_output=xml:$TaskXml" *> $TaskLog
        $TaskExit = $LASTEXITCODE
    } finally { Pop-Location }
} finally { $env:GCOV_PREFIX = $TaskPreviousCoverage }
if ($TaskExit -ne 0) { throw "Native FilePCM tests failed ($TaskExit): $TaskLog" }
[xml]$TaskResult = Get-Content -LiteralPath $TaskXml -Raw
if ([int]$TaskResult.testsuites.tests -lt 35 -or [int]$TaskResult.testsuites.failures -ne 0 -or
    [int]$TaskResult.testsuites.disabled -ne 0 -or [int]$TaskResult.testsuites.errors -ne 0 -or
    @($TaskResult.SelectNodes('//testcase/skipped')).Count -ne 0) {
    throw "Expected all native FilePCM and ring tests to run successfully: $TaskXml"
}
[ordered]@{
    tests = [int]$TaskResult.testsuites.tests
    failures = [int]$TaskResult.testsuites.failures
    seconds = [double]$TaskResult.testsuites.time
    filter = $TaskFilter
    directory = $TaskDirectory
    xml = $TaskXml
    log = $TaskLog
    test_executable = $TaskExe
    test_sha256 = (Get-FileHash -LiteralPath $TaskExe -Algorithm SHA256).Hash.ToLowerInvariant()
    actual_full_host_session_verified = $false
    quest_verified = $false
} | ConvertTo-Json

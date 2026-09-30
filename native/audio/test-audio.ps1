[CmdletBinding()]
param()
$ErrorActionPreference = 'Stop'
$AudioRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$AudioBuild = Join-Path $AudioRoot 'third_party/sunshine/cmake-build-quest3d'
$AudioArtifacts = Join-Path $AudioRoot 'artifacts/audio'
$AudioProbe = Join-Path $PSScriptRoot 'dist/audio_probe.exe'
if (!(Test-Path -LiteralPath $AudioProbe)) { throw 'Build native/audio/build-probe.ps1 before running this verification.' }
$AudioStamp = Get-Date -Format 'yyyyMMdd-HHmmss'
$AudioCoverage = Join-Path $AudioArtifacts "coverage-$AudioStamp"
$AudioXml = Join-Path $AudioArtifacts "audio-regression-$AudioStamp.xml"
New-Item -ItemType Directory -Path $AudioCoverage -Force | Out-Null
$AudioBefore = (& $AudioProbe | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw 'Read-only endpoint snapshot failed.' }
$AudioOldCoverage = $env:GCOV_PREFIX
try {
    $env:GCOV_PREFIX = $AudioCoverage
    Push-Location $AudioBuild
    try {
        & './tests/test_sunshine.exe' '--gtest_filter=QuestAudio*:WindowsAudioTest.*' "--gtest_output=xml:$AudioXml"
        if ($LASTEXITCODE -ne 0) { throw 'Audio unit/journal tests failed.' }
    } finally { Pop-Location }
} finally { $env:GCOV_PREFIX = $AudioOldCoverage }
$AudioAfter = (& $AudioProbe | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0) { throw 'Read-only final endpoint snapshot failed.' }
$AudioBeforeDefaults = $AudioBefore.defaults_before | ConvertTo-Json -Depth 10 -Compress
$AudioAfterDefaults = $AudioAfter.defaults_before | ConvertTo-Json -Depth 10 -Compress
if ($AudioBeforeDefaults -ne $AudioAfterDefaults) { throw 'Default endpoint/mute/volume changed during the test; no state was forcibly restored.' }
$AudioBeforeEndpoints = $AudioBefore.active_render_endpoints | Sort-Object id | ConvertTo-Json -Depth 10 -Compress
$AudioAfterEndpoints = $AudioAfter.active_render_endpoints | Sort-Object id | ConvertTo-Json -Depth 10 -Compress
if ($AudioBeforeEndpoints -ne $AudioAfterEndpoints) { throw 'An active render endpoint changed during the test; no external change was forcibly restored.' }
[xml]$AudioResult = Get-Content -LiteralPath $AudioXml -Raw
if ([int]$AudioResult.testsuites.tests -lt 48 -or [int]$AudioResult.testsuites.failures -ne 0 -or [int]$AudioResult.testsuites.disabled -ne 0) {
    throw 'Expected audio tests were not all executed successfully.'
}
$AudioWatchdogResult = (& uv run --locked --extra gpu-capture python (Join-Path $PSScriptRoot 'verify-watchdog.py') | ConvertFrom-Json)
if ($LASTEXITCODE -ne 0 -or $AudioWatchdogResult.tests -ne 5 -or $AudioWatchdogResult.actual_routing_changed) { throw 'Watchdog process lifetime verification failed.' }
$AudioLauncherResult = (& (Join-Path $PSScriptRoot 'test-watchdog-launcher.ps1') | ConvertFrom-Json)
if ($AudioLauncherResult.tests -ne 1 -or $AudioLauncherResult.status -ne 'passed') { throw 'Watchdog launcher verification failed.' }
[ordered]@{
    verified_at = (Get-Date).ToString('o')
    host_sha256 = (Get-FileHash -LiteralPath (Join-Path $AudioBuild 'sunshine.exe')).Hash
    tests_sha256 = (Get-FileHash -LiteralPath (Join-Path $AudioBuild 'tests/test_sunshine.exe')).Hash
    tests = [int]$AudioResult.testsuites.tests
    audio_watchdog_protocol = 1
    watchdog_tests = $AudioWatchdogResult.tests
    watchdog_results = $AudioWatchdogResult.result
    watchdog_launcher_checks = $AudioLauncherResult.tests
    watchdog_launcher_results = $AudioLauncherResult.directory
    results = $AudioXml
    defaults_unchanged = $true
    active_render_endpoints_unchanged = $true
    actual_routing_changed = $false
    quest_audio_verified = $false
    av_sync_verified = $false
} | ConvertTo-Json | Set-Content -LiteralPath (Join-Path $AudioArtifacts 'audio-verification.json') -Encoding utf8
Write-Output 'Audio unit/journal tests passed; current endpoint, mute and volume remained unchanged.'

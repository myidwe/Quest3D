[CmdletBinding()]
param(
    [ValidateRange(1030, 65514)][int]$Port = 47989,
    [string]$ControlDirectory,
    [ValidateSet(2, 3)][int]$BridgeProtocol = 2,
    [switch]$FilePcm,
    [switch]$EnableInput
)
$ErrorActionPreference = 'Stop'
if ($EnableInput -and ($FilePcm -or $BridgeProtocol -ne 3)) { throw 'EnableInput requires bridge v3 and a desktop source without FilePcm.' }
. (Join-Path $PSScriptRoot 'dev-host-common.ps1')
. (Join-Path $PSScriptRoot 'file-pcm-launch.ps1')
. (Join-Path $PSScriptRoot 'source-session-launch.ps1')
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
$TaskBuild = Join-Path $TaskRoot 'third_party/sunshine/cmake-build-quest3d'
$TaskArtifacts = Join-Path $TaskRoot 'artifacts/host'
$TaskDev = Join-Path $TaskArtifacts 'dev'
$TaskProofPath = Join-Path $TaskArtifacts 'host-verification.json'
if (!(Test-Path -LiteralPath $TaskProofPath)) { throw 'Run native/host/test-host.ps1 before staging.' }
$TaskProof = Get-Content -LiteralPath $TaskProofPath -Raw | ConvertFrom-Json
$TaskHash = (Get-FileHash -LiteralPath (Join-Path $TaskBuild 'sunshine.exe')).Hash
if ($TaskHash -ne $TaskProof.host_sha256) { throw 'The host changed after testing. Run native/host/test-host.ps1 again.' }
$TaskAudioProof = Get-Content -LiteralPath (Join-Path $TaskRoot 'artifacts/audio/audio-verification.json') -Raw | ConvertFrom-Json
if ($TaskAudioProof.host_sha256 -ne $TaskHash -or $TaskAudioProof.audio_watchdog_protocol -ne 1 -or $TaskAudioProof.watchdog_tests -ne 5 -or $TaskAudioProof.watchdog_launcher_checks -ne 1) {
    throw 'Run native/audio/test-audio.ps1 for this host before staging the watchdog launcher.'
}
if (@(Get-Quest3DPortConflict $Port).Count -gt 0) {
    Write-Output 'An existing server owns streaming ports. Staging continues; the launcher still requires those ports to be free.'
}
if ($ControlDirectory) {
    $ControlDirectory = (Resolve-Path -LiteralPath $ControlDirectory).Path
    if (!(Test-Path -LiteralPath (Join-Path $ControlDirectory 'status.json') -PathType Leaf)) { throw 'Control directory must contain a publisher status.json.' }
}
$TaskPcm = if ($FilePcm) { Get-Quest3DFilePcm $TaskRoot $ControlDirectory } else { $null }
New-Item -ItemType Directory -Path $TaskDev -Force | Out-Null
$TaskRuntime = Join-Path $TaskArtifacts ('runtime-' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N').Substring(0, 8))
New-Item -ItemType Directory -Path $TaskRuntime | Out-Null
Copy-Item -LiteralPath (Join-Path $TaskBuild 'sunshine.exe') -Destination $TaskRuntime
Copy-Item -LiteralPath (Join-Path $TaskBuild 'LICENSE.txt') -Destination $TaskRuntime
Copy-Item -LiteralPath (Join-Path $TaskRoot 'native/host/tools/msys64/ucrt64/bin/zlib1.dll') -Destination $TaskRuntime
# Copy concrete source shaders rather than the build directory junction.
$TaskAssets = Join-Path $TaskRuntime 'assets'
New-Item -ItemType Directory -Path $TaskAssets | Out-Null
Get-ChildItem -LiteralPath (Join-Path $TaskBuild 'assets') -File | Copy-Item -Destination $TaskAssets
Copy-Item -LiteralPath (Join-Path $TaskBuild 'assets/web') -Destination $TaskAssets -Recurse
Copy-Item -LiteralPath (Join-Path $TaskRoot 'third_party/sunshine/src_assets/windows/assets/shaders') -Destination $TaskAssets -Recurse
if ((Get-FileHash -LiteralPath (Join-Path $TaskRuntime 'sunshine.exe')).Hash -ne $TaskHash) { throw 'Staged host checksum mismatch.' }
$TaskConfig = Join-Path $TaskDev 'sunshine.conf'
$TaskApps = Join-Path $TaskDev 'apps.json'
$TaskCredentials = Join-Path $TaskDev 'credentials.json'
$TaskAccess = Join-Path $TaskDev 'web-access.clixml'
if (!(Test-Path -LiteralPath $TaskApps)) {
    @{ env = @{}; apps = @(@{ name = 'Quest3D Desktop'; 'image-path' = 'desktop.png' }) } | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $TaskApps -Encoding utf8
}
if (!(Test-Path -LiteralPath $TaskConfig)) {
    $TaskConfigText = @"
# Isolated development configuration. First headset connection is display-only.
capture = quest3d
encoder = nvenc
sunshine_name = Quest3D Development
port = $Port
address_family = ipv4
bind_address = 0.0.0.0
upnp = disabled
origin_web_ui_allowed = pc
dd_configuration_option = disabled
dd_resolution_option = disabled
dd_refresh_rate_option = disabled
dd_hdr_option = disabled
nvenc_opengl_vulkan_on_dxgi = disabled
nvenc_latency_over_power = disabled
keyboard = disabled
mouse = disabled
controller = disabled
stream_audio = disabled
install_steam_audio_drivers = disabled
hevc_mode = 2
av1_mode = 1
file_apps = $($TaskApps.Replace('\', '/'))
file_state = $((Join-Path $TaskDev 'state.json').Replace('\', '/'))
credentials_file = $($TaskCredentials.Replace('\', '/'))
log_path = $((Join-Path $TaskDev 'sunshine.log').Replace('\', '/'))
pkey = $((Join-Path $TaskDev 'credentials/cakey.pem').Replace('\', '/'))
cert = $((Join-Path $TaskDev 'credentials/cacert.pem').Replace('\', '/'))
"@
    $TaskConfigText | Set-Content -LiteralPath $TaskConfig -Encoding utf8
} elseif (!(Select-String -LiteralPath $TaskConfig -Pattern "^port\s*=\s*$Port\s*$" -Quiet)) {
    throw 'Existing development configuration uses another port and was preserved.'
}
if (!(Test-Path -LiteralPath $TaskCredentials)) {
    if (Test-Path -LiteralPath $TaskAccess) { throw 'Encrypted access credentials exist without host credentials; both files were preserved for inspection.' }
    $TaskPassword = [Convert]::ToBase64String([Security.Cryptography.RandomNumberGenerator]::GetBytes(24))
    $TaskAccount = [PSCredential]::new('quest3d', (ConvertTo-SecureString $TaskPassword -AsPlainText -Force))
    $TaskAccount | Export-Clixml -LiteralPath $TaskAccess
    Invoke-Quest3DCommand (Join-Path $TaskRuntime 'sunshine.exe') $TaskRuntime @($TaskConfig, '--creds', 'quest3d', $TaskPassword) (Join-Path $TaskDev 'prepare-credentials.log')
    $TaskPassword = $null
    if (!(Test-Path -LiteralPath $TaskCredentials)) { throw 'Sunshine did not create the isolated credentials file.' }
}
# Runtime-specific config avoids changing the configuration of a running host.
# Pairing certificates and credentials remain in the persistent isolated dev dir.
$TaskRuntimeConfig = Join-Path $TaskRuntime 'sunshine.conf'
$TaskRuntimeText = Get-Content -LiteralPath $TaskConfig -Raw
$TaskRuntimeText = Set-Quest3DInitialSettings $TaskRuntimeText ([bool]$FilePcm)
$TaskRuntimeText = $TaskRuntimeText -replace '(?m)^quest3d_protocol\s*=.*(?:\r?\n|$)', ''
$TaskRuntimeText += "`nquest3d_protocol = $BridgeProtocol`n"
$TaskRuntimeText = $TaskRuntimeText -replace '(?mi)^\s*quest3d_input\s*=.*(?:\r?\n|$)', ''
$TaskRuntimeText += "`nquest3d_input = $(if ($EnableInput) { 'enabled' } else { 'disabled' })`n"
$TaskRuntimeText = $TaskRuntimeText -replace '(?m)^quest3d_control_dir\s*=.*(?:\r?\n|$)', ''
if ($ControlDirectory) { $TaskRuntimeText += "`nquest3d_control_dir = $($ControlDirectory.Replace('\', '/'))`n" }
$TaskRuntimeText | Set-Content -LiteralPath $TaskRuntimeConfig -Encoding utf8
$TaskConfig = $TaskRuntimeConfig
$TaskSource = Get-Quest3DSourceSession $TaskRoot $ControlDirectory $TaskConfig -EnableInput ([bool]$EnableInput)
Invoke-Quest3DCommand (Join-Path $TaskRuntime 'sunshine.exe') $TaskRuntime @($TaskConfig, '--version') (Join-Path $TaskRuntime 'version-check.log')
$TaskManifest = [ordered]@{
    prepared_at = (Get-Date).ToString('o'); runtime = $TaskRuntime; config = $TaskConfig; apps = $TaskApps
    credentials_file = $TaskCredentials; encrypted_web_access = $TaskAccess; port = $Port
    web_url = "https://localhost:$($Port + 1)"; host_sha256 = $TaskHash; verification = $TaskProof.results
    input_enabled = [bool]$EnableInput; audio_enabled = [bool]$FilePcm; server_started = $false
    control_directory = $ControlDirectory
    bridge_protocol = $BridgeProtocol
    source_session = $TaskSource
    file_pcm = $TaskPcm
    audio_watchdog_protocol = 1
}
$TaskManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $TaskRuntime 'manifest.json') -Encoding utf8
$TaskManifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $TaskDev 'launch.json') -Encoding utf8
Write-Output "Prepared runtime: $TaskRuntime"
Write-Output "Launch manifest: $(Join-Path $TaskDev 'launch.json')"
Write-Output "Web access is encrypted for the current Windows user: $TaskAccess"
Write-Output 'No server, firewall rule, system service, or input event was started.'

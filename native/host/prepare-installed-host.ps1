[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ControlDirectory,
    [Parameter(Mandatory)][string]$ExpectedRuntime,
    [Parameter(Mandatory)][ValidatePattern('^[0-9a-f]{64}$')][string]$ExpectedHostSha256,
    [switch]$PlanOnly
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'dev-host-common.ps1')
. (Join-Path $PSScriptRoot 'source-session-launch.ps1')
. (Join-Path $PSScriptRoot 'file-pcm-launch.ps1')
. (Join-Path $PSScriptRoot 'installed-credentials.ps1')
$TaskRoot = (Resolve-Path (Join-Path $PSScriptRoot '../..')).Path
if ($TaskRoot.Contains('#') -or $ControlDirectory.Contains('#')) { throw 'Choose an installation folder without #; this character cannot be used in the host configuration.' }
$TaskDistribution = Get-Content -LiteralPath (Join-Path $TaskRoot 'config/distribution.json') -Raw | ConvertFrom-Json
if ($TaskDistribution.schema -ne 1 -or $TaskDistribution.host_sha256 -cne $ExpectedHostSha256) { throw 'Unrecognized installation version.' }
$TaskRuntime = [IO.Path]::GetFullPath($ExpectedRuntime)
$TaskHostRoot = [IO.Path]::GetFullPath((Join-Path $TaskRoot 'artifacts/host'))
if ((Split-Path -Parent $TaskRuntime) -ne $TaskHostRoot -or (Split-Path -Leaf $TaskRuntime) -notmatch '^runtime-[a-zA-Z0-9_-]+$') { throw 'Unexpected runtime location.' }
if ([IO.Path]::GetFullPath((Join-Path $TaskRoot $TaskDistribution.host_runtime)) -ne $TaskRuntime) { throw 'Installation and runtime paths differ.' }
$TaskExe = Join-Path $TaskRuntime 'sunshine.exe'
if ((Get-FileHash -LiteralPath $TaskExe -Algorithm SHA256).Hash -ine $ExpectedHostSha256) { throw 'Installed host checksum mismatch.' }
$TaskSourceDir = [IO.Path]::GetFullPath($ControlDirectory)
$TaskArtifactsPrefix = [IO.Path]::GetFullPath((Join-Path $TaskRoot 'artifacts')) + [IO.Path]::DirectorySeparatorChar
if (!$TaskSourceDir.StartsWith($TaskArtifactsPrefix, [StringComparison]::OrdinalIgnoreCase) -or $TaskSourceDir.IndexOfAny([char[]]"`r`n") -ge 0) { throw 'Source must be inside the installation artifacts directory.' }
$TaskDev = Join-Path $TaskHostRoot 'dev'
$TaskConfig = Join-Path $TaskRuntime 'sunshine.conf'
$TaskLaunch = Join-Path $TaskDev 'launch.json'
if (Test-Path -LiteralPath $TaskLaunch) { throw 'An existing host is already configured. Reuse it; do not replace pairing data.' }
$TaskPaths = @{}
foreach ($TaskEntry in @(@('file_apps','apps.json'), @('file_state','state.json'), @('credentials_file','credentials.json'), @('log_path','sunshine.log'), @('pkey','credentials/cakey.pem'), @('cert','credentials/cacert.pem'))) {
    $TaskPaths[$TaskEntry[0]] = (Join-Path $TaskDev $TaskEntry[1]).Replace('\','/')
}
$TaskText = @'
# Quest3D local installation. Generated once; pairing belongs to this Windows user.
encoder = nvenc
sunshine_name = Quest3D Desktop
port = 47989
address_family = ipv4
bind_address = 0.0.0.0
origin_web_ui_allowed = pc
dd_configuration_option = disabled
dd_resolution_option = disabled
dd_refresh_rate_option = disabled
dd_hdr_option = disabled
nvenc_opengl_vulkan_on_dxgi = disabled
nvenc_latency_over_power = disabled
hevc_mode = 2
av1_mode = 1
capture = quest3d
keyboard = disabled
mouse = disabled
controller = disabled
upnp = disabled
install_steam_audio_drivers = disabled
stream_audio = disabled
quest3d_protocol = 2
'@
foreach ($TaskKey in ($TaskPaths.Keys | Sort-Object)) { $TaskText += "`n$TaskKey = $($TaskPaths[$TaskKey])" }
$TaskText += "`nquest3d_control_dir = $($TaskSourceDir.Replace('\','/'))`n"
Assert-Quest3DInitialSettings $TaskText $false
if ($PlanOnly) {
    [ordered]@{mode='plan';runtime=$TaskRuntime;config=$TaskConfig;launch=$TaskLaunch;settings=$TaskText;secrets_created=$false;server_started=$false} | ConvertTo-Json
    return
}
if (@(Get-Quest3DPortConflict 47989).Count) { throw 'A host is already using the streaming ports. Existing processes were preserved.' }
function Write-Quest3DSetupText([string]$Path, [string]$Text) {
    $TaskTemp = $Path + '.setup-' + [guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($TaskTemp, $Text, [Text.UTF8Encoding]::new($false))
        [IO.File]::Move($TaskTemp, $Path, $true)
    } finally { if (Test-Path -LiteralPath $TaskTemp) { Remove-Item -LiteralPath $TaskTemp } }
}
$null = New-Item -ItemType Directory -Path (Join-Path $TaskDev 'credentials') -Force
$TaskJournal = Join-Path $TaskDev 'setup-journal.json'
$TaskAccess = Join-Path $TaskDev 'web-access.clixml'
$TaskCredentials = Join-Path $TaskDev 'credentials.json'
if (Test-Path -LiteralPath $TaskJournal) {
    $TaskPrevious = Get-Content -LiteralPath $TaskJournal -Raw | ConvertFrom-Json
    if ($TaskPrevious.runtime -cne $TaskRuntime -or $TaskPrevious.host_sha256 -cne $ExpectedHostSha256) { throw 'Setup journal belongs to another runtime. Existing local data was preserved.' }
} elseif ((Test-Path -LiteralPath $TaskAccess) -or (Test-Path -LiteralPath $TaskCredentials)) {
    throw 'Unowned credentials already exist. Preserve this folder and choose a new installation folder.'
}
Write-Quest3DSetupText $TaskJournal (@{runtime=$TaskRuntime;host_sha256=$ExpectedHostSha256;stage='preparing'} | ConvertTo-Json)
Write-Quest3DSetupText $TaskConfig $TaskText
# Validate live source before creating a new local account. No hostname or address
# from another computer's session is copied into this installation.
$TaskSource = Get-Quest3DSourceSession $TaskRoot $TaskSourceDir $TaskConfig
$TaskApps = Join-Path $TaskDev 'apps.json'
if (!(Test-Path -LiteralPath $TaskApps)) { Write-Quest3DSetupText $TaskApps '{"apps":[{"name":"Quest3D Desktop","image-path":"desktop.png"}],"env":{}}' }
$null = Initialize-Quest3DInstalledCredentials -AccessPath $TaskAccess -CredentialsPath $TaskCredentials `
    -Executable $TaskExe -WorkingDirectory $TaskRuntime -ConfigPath $TaskConfig `
    -LogPath (Join-Path $TaskDev 'prepare-credentials.log') -OwnedSetup
$TaskManifest = [ordered]@{
    prepared_at=(Get-Date).ToString('o'); runtime=$TaskRuntime; config=$TaskConfig; apps=$TaskApps
    credentials_file=$TaskCredentials; encrypted_web_access=$TaskAccess; port=47989
    web_url='https://localhost:47990'; host_sha256=$ExpectedHostSha256; verification='packaged-host-sha256'
    input_enabled=$false; audio_enabled=$false; server_started=$false; control_directory=$TaskSourceDir
    bridge_protocol=2; source_session=$TaskSource; file_pcm=$null; audio_watchdog_protocol=1
}
$TaskManifestText = $TaskManifest | ConvertTo-Json -Depth 8
Write-Quest3DSetupText (Join-Path $TaskRuntime 'manifest.json') $TaskManifestText
Write-Quest3DSetupText $TaskLaunch $TaskManifestText
Write-Quest3DSetupText $TaskJournal (@{runtime=$TaskRuntime;host_sha256=$ExpectedHostSha256;stage='ready'} | ConvertTo-Json)
Write-Output 'Local host prepared. Credentials stay on this PC. No firewall rule or server was started.'

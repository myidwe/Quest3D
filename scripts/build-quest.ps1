param(
    [ValidateSet('Status', 'Prepare', 'Native', 'Stream', 'Xr', 'Scripts', 'Tls', 'Identity', 'Apk', 'Verify', 'All')]
    [string]$Stage = 'Status',
    [string]$Distribution = 'Ubuntu-20.04',
    [string]$CachePath
)
$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$artifactRoot = Join-Path $projectRoot 'artifacts/quest'
if ($Stage -eq 'Status') {
    if (Test-Path -LiteralPath $artifactRoot) {
        Get-ChildItem -LiteralPath $artifactRoot -Filter '*.log' | Select-Object Name, LastWriteTime, Length
    }
    Write-Host 'Status does not build, install, or connect to WSL/ADB.'
    return
}
New-Item -ItemType Directory -Force -Path $artifactRoot | Out-Null
$windowsForwardPath = $projectRoot.Replace('\', '/')
$resolvedPath = & wsl.exe -d $Distribution -- wslpath -a $windowsForwardPath
if ($LASTEXITCODE -ne 0 -or -not $resolvedPath) { throw 'Unable to resolve the WSL project path.' }
$wslRoot = $resolvedPath.Trim()
if (-not $wslRoot.StartsWith('/')) { throw 'Invalid WSL project path.' }

$arguments = @('-d', $Distribution, '--', 'env')
if ($CachePath) { $arguments += "QUEST_BUILD_CACHE=$CachePath" }
$arguments += @('bash', "$wslRoot/scripts/quest/run.sh", $Stage)
Write-Host "Quest $Stage with tracked scripts/quest helpers"
$previousErrorAction = $ErrorActionPreference
try {
    # Windows PowerShell 5 turns successful native stderr into ErrorRecords.
    # Log both streams and use the real process exit code to decide failure.
    $ErrorActionPreference = 'Continue'
    & wsl.exe @arguments 2>&1 | ForEach-Object { "$_" } |
        Tee-Object -FilePath (Join-Path $artifactRoot "$($Stage.ToLowerInvariant())-tracked.log") -ErrorAction Stop
    $stageExitCode = $LASTEXITCODE
}
finally { $ErrorActionPreference = $previousErrorAction }
if ($stageExitCode -ne 0) { throw "Quest $Stage failed with exit code $stageExitCode. See artifacts/quest." }

<#
Creates a workspace shortcut by default. Desktop/Start Menu placement is opt-in:
  .\scripts\install-desktop-shortcut.ps1
  .\scripts\install-desktop-shortcut.ps1 -Desktop -StartMenu
Uses the existing venv pythonw; never installs packages, starts the app, creates
autostart entries, tasks or services. Existing unowned shortcuts are preserved.
#>
[CmdletBinding()]
param(
    [string]$Root = (Split-Path -Parent $PSScriptRoot),
    [switch]$Desktop,
    [switch]$StartMenu,
    [switch]$ReplaceOwned,
    [string]$ShortcutDirectory
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
. (Join-Path $PSScriptRoot 'release/installation-lifecycle.ps1')

$workspaceRoot = (Resolve-Path -LiteralPath $Root).ProviderPath
Assert-Quest3DNoReparse $workspaceRoot
if (-not (Test-Path -LiteralPath $workspaceRoot -PathType Container)) { throw 'Root must be an existing directory.' }
$pythonw = Join-Path $workspaceRoot '.venv\Scripts\pythonw.exe'
$desktopModule = Join-Path $workspaceRoot 'src\quest3d\desktop.py'
if (-not (Test-Path -LiteralPath $pythonw -PathType Leaf)) { throw "Existing venv pythonw is missing: $pythonw" }
if (-not (Test-Path -LiteralPath $desktopModule -PathType Leaf)) { throw "Desktop module is missing: $desktopModule" }
if ($workspaceRoot.IndexOfAny([char[]]"`"`r`n") -ge 0) { throw 'Root cannot contain quotes or line breaks.' }

# A quoted Windows argv value ending in a backslash needs that final slash doubled.
$argumentRoot = $workspaceRoot
if ($argumentRoot.EndsWith('\')) { $argumentRoot += '\' }
$launchArguments = '-m quest3d.desktop --root "' + $argumentRoot + '"'
$ownershipDescription = 'Quest3D Desktop | ' + $workspaceRoot
$shortcutName = 'Quest3D Desktop.lnk'
$destinations = [System.Collections.Generic.List[string]]::new()
$destinations.Add((Join-Path $workspaceRoot $shortcutName))
if ($ShortcutDirectory) {
    $customDirectory = Get-Quest3DInstallRoot $ShortcutDirectory
    $destinations.Add((Join-Path $customDirectory $shortcutName))
}
if ($Desktop) {
    $desktopDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
    if ([string]::IsNullOrWhiteSpace($desktopDirectory)) { throw 'Windows Desktop directory is unavailable.' }
    $destinations.Add((Join-Path $desktopDirectory $shortcutName))
}
if ($StartMenu) {
    $programsDirectory = [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    if ([string]::IsNullOrWhiteSpace($programsDirectory)) { throw 'Windows user Start Menu directory is unavailable.' }
    $destinations.Add((Join-Path $programsDirectory $shortcutName))
}
$targets = @($destinations | Select-Object -Unique)
$shell = New-Object -ComObject WScript.Shell

function Assert-OwnedShortcut([string]$Path) {
    Assert-Quest3DNoReparse $Path
    if (-not (Test-Path -LiteralPath $Path)) { return }
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw "Refusing to replace a directory or reparse point: $Path"
    }
    $existing = $shell.CreateShortcut($Path)
    try {
        $sameTarget = [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($existing.TargetPath), $pythonw)
        $sameDirectory = [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($existing.WorkingDirectory), $workspaceRoot)
        if ($sameTarget -and $sameDirectory -and $existing.Arguments -ceq $launchArguments -and
                  $existing.Description -ceq $ownershipDescription) { return }
        if ($ReplaceOwned) {
            $previousRoot = Get-Quest3DInstallRoot $existing.WorkingDirectory
            $previousInstall = Get-Quest3DOwnedInstall $previousRoot
            $previousPython = Join-Path $previousRoot '.venv/Scripts/pythonw.exe'
            $previousArguments = '-m quest3d.desktop --root "' + $previousRoot + '"'
            if ($previousInstall.owner.completed -and
                [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($existing.TargetPath), $previousPython) -and
                $existing.Arguments -ceq $previousArguments -and $existing.Description -ceq ('Quest3D Desktop | ' + $previousRoot)) { return }
        }
        throw "Existing shortcut is not owned by this installation; preserved: $Path"
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($existing) }
}

try {
    # Check all requested destinations before writing any shortcut.
    $originalHashes = @{}
    $originalBytes = @{}
    $written = @{}
    foreach ($target in $targets) {
        Assert-OwnedShortcut $target
        $originalHashes[$target] = if (Test-Path -LiteralPath $target) {
            (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
        } else { $null }
        $originalBytes[$target] = if (Test-Path -LiteralPath $target) { [IO.File]::ReadAllBytes($target) } else { $null }
    }
    foreach ($target in $targets) {
        $parent = Split-Path -Parent $target
        if (-not (Test-Path -LiteralPath $parent -PathType Container)) {
            [void](New-Item -ItemType Directory -Path $parent)
        }
        $temporary = Join-Path $parent ('.Quest3DDesktop.' + [Guid]::NewGuid().ToString('N') + '.lnk')
        try {
            $shortcut = $shell.CreateShortcut($temporary)
            try {
                $shortcut.TargetPath = $pythonw
                $shortcut.Arguments = $launchArguments
                $shortcut.WorkingDirectory = $workspaceRoot
                $shortcut.Description = $ownershipDescription
                $taskIcon = Join-Path $workspaceRoot 'resources\desktop.ico'
                $shortcut.IconLocation = if (Test-Path -LiteralPath $taskIcon) { "$taskIcon,0" } else { "$pythonw,0" }
                $shortcut.WindowStyle = 1
                $shortcut.Save()
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shortcut) }
            Assert-OwnedShortcut $temporary
            # Preserve files that appeared/changed after the initial ownership check.
            $currentHash = if (Test-Path -LiteralPath $target) {
                (Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash
            } else { $null }
            if ($currentHash -cne $originalHashes[$target]) { throw "Shortcut changed during installation; preserved: $target" }
            Assert-OwnedShortcut $target
            if (Test-Path -LiteralPath $target) { [IO.File]::Replace($temporary, $target, [NullString]::Value) }
            else { [IO.File]::Move($temporary, $target) }
            $written[$target] = (Get-FileHash -LiteralPath $target).Hash
            [pscustomobject]@{ shortcut = $target; target = $pythonw; arguments = $launchArguments;
                working_directory = $workspaceRoot; autostart = $false; application_started = $false }
        } finally {
            if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary }
        }
    }
} catch {
    $shortcutFailure = $_
    foreach ($target in $written.Keys) {
        Assert-Quest3DNoReparse $target
        if (!(Test-Path -LiteralPath $target -PathType Leaf) -or (Get-FileHash -LiteralPath $target).Hash -ine $written[$target]) { continue }
        if ($null -eq $originalBytes[$target]) { Remove-Item -LiteralPath $target }
        else {
            $temporary = $target + '.restore-' + [Guid]::NewGuid().ToString('N') + '.lnk'
            [IO.File]::WriteAllBytes($temporary, $originalBytes[$target])
            [IO.File]::Replace($temporary, $target, [NullString]::Value)
        }
    }
    throw $shortcutFailure
} finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }

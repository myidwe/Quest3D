<# Create Sterevi shortcuts; migrate only verified owned legacy links, with backups. #>
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
$pythonw = Join-Path $workspaceRoot '.venv/Scripts/pythonw.exe'
$desktopModule = Join-Path $workspaceRoot 'src/quest3d/desktop.py'
if (!(Test-Path -LiteralPath $pythonw -PathType Leaf) -or !(Test-Path -LiteralPath $desktopModule -PathType Leaf)) { throw 'Existing application and venv pythonw are required.' }
if ($workspaceRoot.IndexOfAny([char[]]"`"`r`n") -ge 0) { throw 'Root cannot contain quotes or line breaks.' }
$argumentRoot = $workspaceRoot
if ($argumentRoot.EndsWith('\')) { $argumentRoot += '\' }
$launchArguments = '-m quest3d.desktop --root "' + $argumentRoot + '"'
$description = 'Sterevi Desktop | ' + $workspaceRoot
$directories = [Collections.Generic.List[string]]::new()
$directories.Add($workspaceRoot)
if ($ShortcutDirectory) { $directories.Add((Get-Quest3DInstallRoot $ShortcutDirectory)) }
if ($Desktop) { $directories.Add([Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)) }
if ($StartMenu) { $directories.Add([Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)) }
$directories = @($directories | Select-Object -Unique)
if (@($directories | Where-Object { [string]::IsNullOrWhiteSpace($_) }).Count) { throw 'Windows shortcut directory is unavailable.' }
$shell = New-Object -ComObject WScript.Shell
$receiptPath = $null
$relativeReceipt = $null
function Get-OwnedLinkRoot([string]$Path) {
    Assert-Quest3DNoReparse $Path
    if (!(Test-Path -LiteralPath $Path)) { return $null }
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { throw 'Existing shortcut path is not a file; preserved.' }
    $link = $shell.CreateShortcut($Path)
    try {
        if (Test-Quest3DShortcutOwner $link $workspaceRoot) { return $workspaceRoot }
        if ($ReplaceOwned) {
            $previous = Get-Quest3DOwnedInstall $link.WorkingDirectory
            if ($previous.owner.completed -and (Test-Quest3DShortcutOwner $link $previous.root)) { return $previous.root }
        }
        throw 'Existing shortcut is not owned by this installation; preserved.'
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
}
try {
    $actions = @()
    # Every new-name collision is checked before writing anything. Unowned old-name links remain untouched.
    foreach ($directory in $directories) {
        $path = Join-Path $directory 'Sterevi Desktop.lnk'
        $beforeRoot = Get-OwnedLinkRoot $path
        $actions += @{path=$path;before_root=$beforeRoot;before_sha256=$(if($beforeRoot){(Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant()}else{$null});before_file=$null;after_sha256=$null;legacy=$false}
    }
    foreach ($directory in $directories) {
        $path = Join-Path $directory 'Quest3D Desktop.lnk'
        try { $beforeRoot = Get-OwnedLinkRoot $path } catch { continue }
        if ($beforeRoot) { $actions += @{path=$path;before_root=$beforeRoot;before_sha256=(Get-FileHash -LiteralPath $path).Hash.ToLowerInvariant();before_file=$null;after_sha256=$null;legacy=$true} }
    }
    $id = [Guid]::NewGuid().ToString('N')
    $relativeReceipt = '.cache/install/shortcut-migrations/' + $id + '/receipt.json'
    $receiptPath = Get-Quest3DInstallPath $workspaceRoot $relativeReceipt
    $backupDirectory = Split-Path -Parent $receiptPath
    [void](New-Item -ItemType Directory -Path $backupDirectory)
    $index = 0
    foreach ($action in $actions) {
        if ($action.before_sha256) {
            $action.before_file = 'before-' + $index.ToString('D4') + '.lnk'
            Copy-Quest3DAtomic $action.path (Join-Path $backupDirectory $action.before_file)
            if ((Get-FileHash -LiteralPath (Join-Path $backupDirectory $action.before_file)).Hash -ine $action.before_sha256) { throw 'Shortcut changed while backing up; preserved.' }
        }
        if (!$action.legacy) {
            $prepared = Join-Path $backupDirectory ('after-' + $index.ToString('D4') + '.lnk')
            $link = $shell.CreateShortcut($prepared)
            try {
                $link.TargetPath = $pythonw
                $link.Arguments = $launchArguments
                $link.WorkingDirectory = $workspaceRoot
                $link.Description = $description
                $icon = Join-Path $workspaceRoot 'resources/desktop.ico'
                $link.IconLocation = if (Test-Path -LiteralPath $icon -PathType Leaf) { "$icon,0" } else { "$pythonw,0" }
                $link.WindowStyle = 1
                $link.Save()
                if (!(Test-Quest3DShortcutOwner $link $workspaceRoot)) { throw 'Prepared shortcut ownership differs.' }
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
            $action.after_sha256 = (Get-FileHash -LiteralPath $prepared).Hash.ToLowerInvariant()
        }
        $index++
    }
    $receipt = @{schema=1;root=$workspaceRoot;phase='prepared';actions=$actions}
    Write-Quest3DJson $receiptPath $receipt
    $index = 0
    foreach ($action in $actions) {
        $current = if (Test-Path -LiteralPath $action.path -PathType Leaf) { (Get-FileHash -LiteralPath $action.path).Hash.ToLowerInvariant() } else { $null }
        Assert-Quest3DNoReparse $action.path
        if ($current -cne $action.before_sha256) { throw 'Shortcut changed during installation; preserved.' }
        if ($current) { $null = Get-OwnedLinkRoot $action.path }
        if ($action.legacy) {
            # Move into this verified installation's recovery folder; never delete the original bytes.
            Move-Item -LiteralPath $action.path -Destination (Join-Path $backupDirectory ('retired-' + $index.ToString('D4') + '.lnk'))
        } else {
            Copy-Quest3DAtomic (Join-Path $backupDirectory ('after-' + $index.ToString('D4') + '.lnk')) $action.path
        }
        $index++
    }
    $receipt.phase = 'committed'
    Write-Quest3DJson $receiptPath $receipt
    foreach ($action in $actions | Where-Object { !$_.legacy }) {
        [pscustomobject]@{shortcut=$action.path;target=$pythonw;arguments=$launchArguments;working_directory=$workspaceRoot;autostart=$false;application_started=$false}
    }
    [pscustomobject]@{migration_receipt=$relativeReceipt;legacy_shortcuts_migrated=@($actions | Where-Object {$_.legacy}).Count;original_bytes_retained=$true}
} catch {
    $failure = $_
    if ($receiptPath -and (Test-Path -LiteralPath $receiptPath -PathType Leaf)) {
        try { Restore-Quest3DShortcutMigration $workspaceRoot $relativeReceipt } catch {
            throw ('Shortcut update failed; recovery backup retained. Recovery error: ' + $_.Exception.Message + '. Original error: ' + $failure.Exception.Message)
        }
    }
    throw $failure
} finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }

<# Remove shortcuts and retire an owned installation into a recoverable sibling folder.
    No recursive deletion; settings, pairing, models and update backups are retained.
#>
[CmdletBinding()]
param(
    [string]$Root,
    [switch]$RemoveNetworkRules,
    [string]$NetworkOperationId,
    [ValidateSet('remove','status')][string]$NetworkReceiptAction='remove',
    [switch]$PlanOnly,
    [string]$ShortcutDirectory,
    [switch]$NoSystemShortcuts,
    [string]$ReportPath
)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installation-lifecycle.ps1')
if (!$PSBoundParameters.ContainsKey('Root')) { $Root = Get-Quest3DPreferredInstallRoot }
$networkReceipt = $null
try {
    $installed = Get-Quest3DOwnedInstall $Root
    $null = Get-Quest3DPackage $installed.root -VerifyFiles
    Assert-Quest3DInstallStopped $installed.root
    Assert-Quest3DTreeNoReparse $installed.root
    $transaction = Get-Quest3DTransaction $installed.root
    if ($transaction -and $transaction.journal.phase -notin @('committed','rolled-back')) { throw 'Restore the interrupted update before removing this installation.' }
    $parent = Split-Path -Parent $installed.root
    if ($ReportPath -and [IO.Path]::GetFullPath($ReportPath).StartsWith($installed.root + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Removal report must be outside the installation.' }
    $archive = Join-Path $parent ('.Quest3D-removed-' + [Guid]::NewGuid().ToString('N'))
    $archive = Get-Quest3DInstallRoot $archive
    if ((Split-Path -Parent $archive) -ine $parent -or (Test-Path -LiteralPath $archive)) { throw 'Invalid retirement destination; preserved.' }
    $shortcutDirectories = @($installed.root)
    if (!$NoSystemShortcuts) {
        $shortcutDirectories += [Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory)
        $shortcutDirectories += [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs)
    }
    if ($ShortcutDirectory) { $shortcutDirectories += Get-Quest3DInstallRoot $ShortcutDirectory }
    $shortcuts = @($shortcutDirectories | ForEach-Object {
        Join-Path $_ 'Sterevi Desktop.lnk'
        Join-Path $_ 'Quest3D Desktop.lnk'
    })
    $owned = @{}
    $shell = New-Object -ComObject WScript.Shell
    try {
        foreach ($path in @($shortcuts | Select-Object -Unique)) {
            Assert-Quest3DNoReparse $path
            if (!(Test-Path -LiteralPath $path -PathType Leaf)) { continue }
            $link = $shell.CreateShortcut($path)
            try {
                if (Test-Quest3DShortcutOwner $link $installed.root) {
                    $owned[$path] = @{hash=(Get-FileHash -LiteralPath $path).Hash;bytes=[IO.File]::ReadAllBytes($path)}
                }
            } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
        }
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    if ($PlanOnly) {
        @{action='uninstall-plan';root=$installed.root;archive=$archive;owned_shortcuts=@($owned.Keys);data_retained=$true;
            disk_space_reclaimed=$false;network_requested=[bool]$RemoveNetworkRules} | ConvertTo-Json -Depth 5
        return
    }
    if ($RemoveNetworkRules) { throw 'Use the install UI to approve network cleanup separately. App retirement must run as the original Windows user.' }
    $networkRemoved = $false
    if ($NetworkOperationId) {
        . (Join-Path $PSScriptRoot 'installer-network-task.ps1')
        $networkReceipt = Use-Quest3DNetworkRetirementReceipt $installed.root $NetworkOperationId $NetworkReceiptAction
        $networkRemoved = $NetworkReceiptAction -ceq 'remove'
    }
    foreach ($path in $owned.Keys) {
        Assert-Quest3DNoReparse $path
        if ((Get-FileHash -LiteralPath $path).Hash -ine $owned[$path].hash) { throw 'Shortcut changed during removal; preserved.' }
    }
    # This move is bounded to the verified installation and an unused sibling.
    Assert-Quest3DInstallStopped $installed.root
    Assert-Quest3DTreeNoReparse $installed.root
    $removed = @()
    try {
        foreach ($path in $owned.Keys) { Remove-Item -LiteralPath $path; $removed += $path }
        Set-Location -LiteralPath $parent
        [Environment]::CurrentDirectory = $parent
        Move-Item -LiteralPath $installed.root -Destination $archive
    } catch {
        foreach ($path in $removed) {
            if (!(Test-Path -LiteralPath $path)) { [IO.File]::WriteAllBytes($path, $owned[$path].bytes) }
        }
        throw
    }
    $result = @{success=$true;action='uninstall';root=$installed.root;archive=$archive;data_retained=$true;
        disk_space_reclaimed=$false;network_rules_removed=$networkRemoved;network_cleanup_not_needed=($NetworkOperationId -and $NetworkReceiptAction -ceq 'status');network_operation_id=$NetworkOperationId;network_retirement_receipt=$networkReceipt;removed_shortcuts=$removed}
    Write-Quest3DJson (Join-Path $archive 'quest3d-retired.json') $result
    if ($ReportPath) { Write-Quest3DJson $ReportPath $result }
    Write-Output ('Installation retired. Settings, pairing and models are retained in: ' + $archive + '. Disk space was not reclaimed.')
} catch {
    $errorMessage = $_.Exception.Message
    if ($networkReceipt) { $errorMessage += ' Network cleanup was verified, but app retirement failed. Preserve this installation and retry from the UI with a new request.' }
    if ($ReportPath) { Write-Quest3DJson $ReportPath @{success=$false;action='uninstall';error=$errorMessage;root=$Root;network_retirement_receipt=$networkReceipt} }
    throw
}

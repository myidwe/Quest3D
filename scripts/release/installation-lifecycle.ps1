<# Shared owned-install transactions. No recursive deletion and no automatic service/network changes. #>
Set-StrictMode -Version Latest
# Keep .NET Framework 4.8 long-path handling local to this installer process.
[AppContext]::SetSwitch('Switch.System.IO.UseLegacyPathHandling', $false)
[AppContext]::SetSwitch('Switch.System.IO.BlockLongPaths', $false)
# Windows PowerShell may be started by a Python environment with a different
# module discovery cache. Load the shipped OS modules, never a user module.
if ($PSVersionTable.PSVersion.Major -le 5) {
    foreach ($module in @('Microsoft.PowerShell.Utility','Microsoft.PowerShell.Management','CimCmdlets')) {
        Import-Module -Name (Join-Path $PSHOME ('Modules/' + $module + '/' + $module + '.psd1')) -Force -ErrorAction Stop
    }
}

function Assert-Quest3DNoReparse([string]$Path) {
    $current = [IO.Path]::GetFullPath($Path)
    while ($current) {
        try {
            $attributes = [IO.File]::GetAttributes($current)
            if ($attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point refused: $current" }
        } catch {
            # Missing descendants are valid while preparing a new installation.
            # Still inspect every ancestor. Permission/IO failures must not make
            # an existing path look missing. PowerShell wraps .NET exceptions.
            $cause = $_.Exception
            while ($cause.InnerException) { $cause = $cause.InnerException }
            if ($cause -isnot [IO.FileNotFoundException] -and $cause -isnot [IO.DirectoryNotFoundException]) { throw }
        }
        $parent = [IO.Path]::GetDirectoryName($current)
        if (!$parent -or $parent -eq $current) { break }
        $current = $parent
    }
}

function Get-Quest3DInstallRoot([string]$Root) {
    $full = [IO.Path]::GetFullPath($Root).TrimEnd('\', '/')
    if (!$full -or $full.Length -le [IO.Path]::GetPathRoot($full).TrimEnd('\', '/').Length -or
        $full.IndexOfAny([char[]]"#`"`r`n") -ge 0) { throw 'Choose a dedicated installation folder without #, quotes or line breaks.' }
    Assert-Quest3DNoReparse $full
    return $full
}

function Get-Quest3DInstallPath([string]$Root, [string]$Relative) {
    if ([string]::IsNullOrWhiteSpace($Relative) -or $Relative -match '[:\\\x00]' -or
        $Relative.StartsWith('/') -or $Relative -match '(^|/)(\.{1,2}|[^/]*[. ])(/|$)') { throw "Invalid package path: $Relative" }
    foreach ($part in $Relative.Split('/')) {
        if (!$part -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$') { throw 'Reserved package path.' }
    }
    $fullRoot = Get-Quest3DInstallRoot $Root
    $path = [IO.Path]::GetFullPath((Join-Path $fullRoot $Relative))
    if (!$path.StartsWith($fullRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Package path escaped root.' }
    Assert-Quest3DNoReparse $path
    return $path
}

function Assert-Quest3DPayloadName([string]$Name) {
    $leaf = ($Name -split '/')[-1]
    if ($Name -match '^(?i:\.venv|\.cache|models|\.tools/python)(/|$)' -or
        $Name -ieq 'quest3d-install.json' -or $Name -ieq 'distribution-manifest.json' -or
        $leaf -match '^(?i:desktop\.json|credentials\.json|web-access\.clixml|sunshine\.conf|sunshine_state\.json|process\.json|launch\.json|\.env)$' -or
        $leaf -match '(?i)\.(pem|key|keystore|jks|pfx|p12|clixml|lnk)$') { throw "Mutable/private path cannot be application payload: $Name" }
}

function Get-Quest3DPackage([string]$Root, [switch]$VerifyFiles) {
    $rootPath = Get-Quest3DInstallRoot $Root
    $path = Get-Quest3DInstallPath $rootPath 'distribution-manifest.json'
    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Extract the complete installer ZIP first.' }
    $manifest = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($manifest.schema -ne 1 -or !$manifest.files -or !$manifest.release -or
        [string]$manifest.release -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$') { throw 'Unsupported package manifest.' }
    $files = @{}
    foreach ($entry in $manifest.files.PSObject.Properties) {
        $file = Get-Quest3DInstallPath $rootPath $entry.Name
        Assert-Quest3DPayloadName $entry.Name
        if ($files.ContainsKey($entry.Name) -or [string]$entry.Value.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            $entry.Value.bytes -isnot [ValueType] -or [long]$entry.Value.bytes -lt 0) { throw 'Invalid or duplicate package file record.' }
        if ($entry.Value.bytes -isnot [int] -and $entry.Value.bytes -isnot [long]) { throw 'Package file size must be an integer.' }
        $files[$entry.Name] = $entry.Value
        if ($VerifyFiles -and (!(Test-Path -LiteralPath $file -PathType Leaf) -or
            (Get-Item -LiteralPath $file).Length -ne [long]$entry.Value.bytes -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ine $entry.Value.sha256)) { throw "Damaged installer: $($entry.Name)" }
    }
    return @{root=$rootPath;manifest=$manifest;files=$files;hash=(Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()}
}

function Get-Quest3DOwnedInstall([string]$Root) {
    $package = Get-Quest3DPackage $Root
    $ownerPath = Get-Quest3DInstallPath $package.root 'quest3d-install.json'
    if (!(Test-Path -LiteralPath $ownerPath -PathType Leaf)) { throw 'Existing folder is not an owned Sterevi installation; it was preserved.' }
    $owner = Get-Content -LiteralPath $ownerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($owner.product -cne 'Quest3D Desktop' -or $owner.package_manifest_sha256 -ine $package.hash -or
        $owner.release -cne $package.manifest.release -or $owner.completed -isnot [bool]) { throw 'Installation ownership does not match its manifest; preserved.' }
    return @{package=$package;owner=$owner;root=$package.root}
}

function Test-Quest3DShortcutOwner([object]$Shortcut, [string]$Root) {
    $rootPath = Get-Quest3DInstallRoot $Root
    $argumentRoot = $rootPath
    if ($argumentRoot.EndsWith('\')) { $argumentRoot += '\' }
    try {
        return [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($Shortcut.TargetPath), (Join-Path $rootPath '.venv/Scripts/pythonw.exe')) -and
            [StringComparer]::OrdinalIgnoreCase.Equals([IO.Path]::GetFullPath($Shortcut.WorkingDirectory), $rootPath) -and
            $Shortcut.Arguments -ceq ('-m quest3d.desktop --root "' + $argumentRoot + '"') -and
            $Shortcut.Description -cin @(('Quest3D Desktop | ' + $rootPath), ('Sterevi Desktop | ' + $rootPath))
    } catch { return $false }
}

function Get-Quest3DPreferredInstallRoot {
    param([string]$LocalAppData=$env:LOCALAPPDATA, [string[]]$ShortcutDirectories)
    # Prefer existing owned folders; do not move settings, environments or Pair keys.
    $fresh = Join-Path $LocalAppData 'Sterevi Desktop'
    foreach ($candidate in @($fresh, (Join-Path $LocalAppData 'Quest3D Desktop'))) {
        try {
            $installed = Get-Quest3DOwnedInstall $candidate
            return $installed.root
        } catch {
            try {
                $transaction = Get-Quest3DTransaction $candidate
                if ($transaction -and $transaction.journal.phase -notin @('committed','rolled-back')) { return $transaction.root }
            } catch { }
        }
    }
    $shell = New-Object -ComObject WScript.Shell
    try {
        if (!$PSBoundParameters.ContainsKey('ShortcutDirectories')) {
            $ShortcutDirectories = @([Environment]::GetFolderPath([Environment+SpecialFolder]::DesktopDirectory),
                                     [Environment]::GetFolderPath([Environment+SpecialFolder]::Programs))
        }
        foreach ($directory in $ShortcutDirectories) {
            if ([string]::IsNullOrWhiteSpace($directory)) { continue }
            foreach ($name in @('Sterevi Desktop.lnk', 'Quest3D Desktop.lnk')) {
                $path = Join-Path $directory $name
                $link = $null
                try {
                    Assert-Quest3DNoReparse $path
                    if (!(Test-Path -LiteralPath $path -PathType Leaf)) { continue }
                    $link = $shell.CreateShortcut($path)
                    $installed = Get-Quest3DOwnedInstall $link.WorkingDirectory
                    if ($installed.owner.completed -and (Test-Quest3DShortcutOwner $link $installed.root)) { return $installed.root }
                } catch { } finally {
                    if ($null -ne $link) { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
                }
            }
        }
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
    return Get-Quest3DInstallRoot $fresh
}

function Restore-Quest3DShortcutMigration([string]$Root, [string]$RelativeReceipt) {
    if ($RelativeReceipt -cnotmatch '^\.cache/install/shortcut-migrations/[a-f0-9]{32}/receipt\.json$') { throw 'Invalid shortcut recovery receipt.' }
    $rootPath = Get-Quest3DInstallRoot $Root
    $receiptPath = Get-Quest3DInstallPath $rootPath $RelativeReceipt
    $directory = Split-Path -Parent $receiptPath
    $receipt = Get-Content -LiteralPath $receiptPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($receipt.schema -ne 1 -or $receipt.root -ine $rootPath -or
        $receipt.phase -notin @('prepared','committed','restored')) { throw 'Invalid shortcut migration ownership.' }
    if ($receipt.phase -eq 'restored') { return }
    $shell = New-Object -ComObject WScript.Shell
    try {
        $seen = @{}
        $index = 0
        # Validate every existing link and backup before restoring any.
        foreach ($action in $receipt.actions) {
            $path = [IO.Path]::GetFullPath([string]$action.path)
            Assert-Quest3DNoReparse $path
            if ((Split-Path -Leaf $path) -cnotin @('Sterevi Desktop.lnk','Quest3D Desktop.lnk') -or $seen.ContainsKey($path)) { throw 'Unsafe shortcut recovery path.' }
            $seen[$path] = $true
            if ($action.before_sha256) {
                if ($action.before_sha256 -cnotmatch '^[a-f0-9]{64}$' -or $action.before_file -cne ('before-' + $index.ToString('D4') + '.lnk')) { throw 'Invalid shortcut backup record.' }
                $backup = Get-Quest3DInstallPath $directory $action.before_file
                if (!(Test-Path -LiteralPath $backup -PathType Leaf) -or (Get-FileHash -LiteralPath $backup).Hash -ine $action.before_sha256) { throw 'Shortcut backup changed; preserved.' }
                $link = $shell.CreateShortcut($backup)
                try {
                    if (!(Test-Quest3DShortcutOwner $link $action.before_root)) { throw 'Shortcut backup is not owned; preserved.' }
                    if ($action.before_root -ine $rootPath) { $null = Get-Quest3DOwnedInstall $action.before_root }
                } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
            } elseif ($action.before_file -or $action.before_root) { throw 'Unexpected shortcut backup metadata.' }
            if ($action.after_sha256 -and $action.after_sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid new shortcut hash.' }
            if (Test-Path -LiteralPath $path) {
                if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw 'Shortcut recovery target is not a file; preserved.' }
                $hash = (Get-FileHash -LiteralPath $path).Hash
                if ($hash -ine $action.before_sha256 -and $hash -ine $action.after_sha256) { throw 'Shortcut changed after migration; preserved.' }
                $link = $shell.CreateShortcut($path)
                try {
                    $expectedRoot = if ($hash -ieq $action.after_sha256) { $rootPath } else { $action.before_root }
                    if (!$expectedRoot -or !(Test-Quest3DShortcutOwner $link $expectedRoot)) { throw 'Shortcut recovery target is not owned; preserved.' }
                } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($link) }
            }
            $index++
        }
        foreach ($action in $receipt.actions) {
            if ($action.before_sha256) { Copy-Quest3DAtomic (Get-Quest3DInstallPath $directory $action.before_file) $action.path }
            elseif (Test-Path -LiteralPath $action.path -PathType Leaf) { Remove-Item -LiteralPath $action.path }
        }
        $receipt.phase = 'restored'
        Write-Quest3DJson $receiptPath $receipt
    } finally { [void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($shell) }
}

function Assert-Quest3DInstallStopped([string]$Root) {
    $rootPath = Get-Quest3DInstallRoot $Root
    $running = @(Get-CimInstance Win32_Process -Filter "Name = 'sunshine.exe' OR Name = 'python.exe' OR Name = 'pythonw.exe'" |
        Where-Object { ($_.ExecutablePath -and $_.ExecutablePath.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) -or
            ($_.CommandLine -and $_.CommandLine.IndexOf($rootPath, [StringComparison]::OrdinalIgnoreCase) -ge 0) })
    if ($running.Count) { throw 'This installation is running. Use PC stop and exit in Sterevi Desktop first.' }
}

function Assert-Quest3DTreeNoReparse([string]$Root) {
    $rootPath = Get-Quest3DInstallRoot $Root
    $pending = [Collections.Generic.Queue[string]]::new()
    $pending.Enqueue($rootPath)
    while ($pending.Count) {
        $directory = $pending.Dequeue()
        Assert-Quest3DNoReparse $directory
        foreach ($item in @(Get-ChildItem -LiteralPath $directory -Force)) {
            if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "Reparse point refused: $($item.FullName)" }
            if (!$item.FullName.StartsWith($rootPath + '\', [StringComparison]::OrdinalIgnoreCase)) { throw 'Tree entry escaped installation.' }
            if ($item.PSIsContainer) { $pending.Enqueue($item.FullName) }
        }
    }
}

function Write-Quest3DJson([string]$Path, $Value) {
    Assert-Quest3DNoReparse $Path
    [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path))
    $temporary = $Path + '.write-' + [Guid]::NewGuid().ToString('N')
    try {
        [IO.File]::WriteAllText($temporary, ($Value | ConvertTo-Json -Depth 16), [Text.UTF8Encoding]::new($false))
        # File.Replace is atomic on Windows and preserves the destination identity.
        if (Test-Path -LiteralPath $Path) { [IO.File]::Replace($temporary, $Path, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Path) }
    } finally { if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary } }
}

function Copy-Quest3DAtomic([string]$Source, [string]$Destination) {
    Assert-Quest3DNoReparse $Source
    Assert-Quest3DNoReparse $Destination
    [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Destination))
    $temporary = $Destination + '.copy-' + [Guid]::NewGuid().ToString('N')
    try {
        Copy-Item -LiteralPath $Source -Destination $temporary
        if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($temporary, $Destination, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Destination) }
    } finally { if (Test-Path -LiteralPath $temporary -PathType Leaf) { Remove-Item -LiteralPath $temporary } }
}

function Get-Quest3DTransaction([string]$Root) {
    $rootPath = Get-Quest3DInstallRoot $Root
    $pointerPath = Get-Quest3DInstallPath $rootPath '.cache/install/update-current.json'
    if (!(Test-Path -LiteralPath $pointerPath -PathType Leaf)) { return $null }
    $pointer = Get-Content -LiteralPath $pointerPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($pointer.schema -ne 1 -or $pointer.transaction -notmatch '^[a-f0-9]{32}$') { throw 'Invalid update transaction pointer; preserved.' }
    $directory = Get-Quest3DInstallPath $rootPath ('.cache/install/updates/' + $pointer.transaction)
    $journalPath = Get-Quest3DInstallPath $directory 'journal.json'
    $journal = Get-Content -LiteralPath $journalPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($journal.schema -ne 1 -or $journal.root -ine $rootPath -or $journal.transaction -cne $pointer.transaction -or
        $journal.old_manifest_sha256 -notmatch '^[a-f0-9]{64}$' -or $journal.new_manifest_sha256 -notmatch '^[a-f0-9]{64}$' -or
        $journal.phase -notin @('prepared','updating','environment','committed','rolled-back')) { throw 'Invalid update journal; preserved.' }
    $backupManifest = Get-Quest3DInstallPath $directory 'metadata/distribution-manifest.json'
    if ((Get-FileHash -LiteralPath $backupManifest).Hash -ine $journal.old_manifest_sha256) { throw 'Update backup manifest changed; preserved.' }
    $backupOwner = Get-Content -LiteralPath (Get-Quest3DInstallPath $directory 'metadata/quest3d-install.json') -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($backupOwner.product -cne 'Quest3D Desktop' -or $backupOwner.package_manifest_sha256 -ine $journal.old_manifest_sha256 -or $backupOwner.completed -ne $true) { throw 'Update backup ownership is invalid; preserved.' }
    $oldPackage = Get-Quest3DPackage (Get-Quest3DInstallPath $directory 'metadata')
    $newPackage = Get-Quest3DPackage (Get-Quest3DInstallPath $directory 'incoming')
    if ($newPackage.hash -ine $journal.new_manifest_sha256) { throw 'Incoming update manifest changed; preserved.' }
    $expected = @{}
    foreach ($name in @(@($oldPackage.files.Keys) + @($newPackage.files.Keys) | Sort-Object -Unique)) {
        $oldEntry, $newEntry = $oldPackage.files[$name], $newPackage.files[$name]
        if ($oldEntry -and $newEntry -and $oldEntry.sha256 -ieq $newEntry.sha256) { continue }
        $expected[$name] = @{old=$(if($oldEntry){$oldEntry.sha256}else{$null});new=$(if($newEntry){$newEntry.sha256}else{$null})}
    }
    $seen = @{}
    foreach ($action in $journal.actions) {
        if (!$expected.ContainsKey($action.path) -or $seen.ContainsKey($action.path) -or
            $action.old_sha256 -ine $expected[$action.path].old -or $action.new_sha256 -ine $expected[$action.path].new) { throw 'Update action does not match owned manifests; preserved.' }
        $seen[$action.path] = $true
    }
    if ($seen.Count -ne $expected.Count) { throw 'Update action list is incomplete; preserved.' }
    return @{root=$rootPath;directory=$directory;journal=$journal;path=$journalPath}
}

function Start-Quest3DUpdate([string]$Root, [hashtable]$Package) {
    $installed = Get-Quest3DOwnedInstall $Root
    if (!$installed.owner.completed) { throw 'Complete or repair the previous installation before upgrading.' }
    Assert-Quest3DInstallStopped $installed.root
    $existing = Get-Quest3DTransaction $installed.root
    if ($existing -and $existing.journal.phase -notin @('committed','rolled-back')) { throw 'An interrupted update must be restored before a new update.' }
    foreach ($relative in @('.python-version','config/distribution.json')) {
        $old = Get-Quest3DInstallPath $installed.root $relative
        $new = Get-Quest3DInstallPath $Package.root $relative
        if (!(Test-Path -LiteralPath $old -PathType Leaf) -or !(Test-Path -LiteralPath $new -PathType Leaf)) { throw 'Update compatibility metadata is missing.' }
        if ($relative -eq '.python-version') {
            if ((Get-Content -LiteralPath $old -Raw).Trim() -cne (Get-Content -LiteralPath $new -Raw).Trim()) { throw 'A Python-version migration requires a separate installation; this installation was preserved.' }
        } else {
            $oldConfig = Get-Content -LiteralPath $old -Raw | ConvertFrom-Json
            $newConfig = Get-Content -LiteralPath $new -Raw | ConvertFrom-Json
            if ($oldConfig.schema -ne 1 -or $newConfig.schema -ne 1 -or $oldConfig.host_sha256 -cne $newConfig.host_sha256 -or
                $oldConfig.host_runtime -cne $newConfig.host_runtime) { throw 'A host-version migration is not yet supported; existing pairing was preserved.' }
        }
    }
    $actions = @()
    $names = @(@($installed.package.files.Keys) + @($Package.files.Keys) | Sort-Object -Unique)
    foreach ($name in $names) {
        $target = Get-Quest3DInstallPath $installed.root $name
        $oldEntry = $installed.package.files[$name]
        $newEntry = $Package.files[$name]
        if ($oldEntry) {
            if (!(Test-Path -LiteralPath $target -PathType Leaf) -or (Get-FileHash -LiteralPath $target).Hash -ine $oldEntry.sha256) { throw "Installed application file changed; preserved: $name" }
        } elseif (Test-Path -LiteralPath $target) { throw "New payload would replace an unowned file; preserved: $name" }
        if ($oldEntry -and $newEntry -and $oldEntry.sha256 -ieq $newEntry.sha256) { continue }
        $actions += @{path=$name;old_sha256=$(if($oldEntry){$oldEntry.sha256}else{$null});new_sha256=$(if($newEntry){$newEntry.sha256}else{$null})}
    }
    $id = [Guid]::NewGuid().ToString('N')
    $directory = Get-Quest3DInstallPath $installed.root ('.cache/install/updates/' + $id)
    [void](New-Item -ItemType Directory -Path $directory)
    foreach ($name in @('distribution-manifest.json','quest3d-install.json')) {
        Copy-Quest3DAtomic (Get-Quest3DInstallPath $installed.root $name) (Get-Quest3DInstallPath $directory ('metadata/' + $name))
    }
    Copy-Quest3DAtomic (Get-Quest3DInstallPath $Package.root 'distribution-manifest.json') (Get-Quest3DInstallPath $directory 'incoming/distribution-manifest.json')
    $journal = @{schema=1;root=$installed.root;transaction=$id;phase='prepared';environment_saved=$false;environment_existed=(Test-Path -LiteralPath (Get-Quest3DInstallPath $installed.root '.venv') -PathType Container);old_manifest_sha256=$installed.package.hash;
        new_manifest_sha256=$Package.hash;old_release=$installed.owner.release;new_release=$Package.manifest.release;actions=$actions}
    $journalPath = Get-Quest3DInstallPath $directory 'journal.json'
    Write-Quest3DJson $journalPath $journal
    Write-Quest3DJson (Get-Quest3DInstallPath $installed.root '.cache/install/update-current.json') @{schema=1;transaction=$id}
    return Get-Quest3DTransaction $installed.root
}

function Invoke-Quest3DUpdateFiles([hashtable]$Transaction, [hashtable]$Package) {
    if ($Transaction.journal.new_manifest_sha256 -cne $Package.hash) { throw 'Update input changed.' }
    $Transaction.journal.phase = 'updating'
    Write-Quest3DJson $Transaction.path $Transaction.journal
    foreach ($action in $Transaction.journal.actions) {
        $target = Get-Quest3DInstallPath $Transaction.root $action.path
        if ($action.old_sha256) {
            if ((Get-FileHash -LiteralPath $target).Hash -ine $action.old_sha256) { throw 'Application file changed while updating.' }
            $backup = Get-Quest3DInstallPath $Transaction.directory ('payload/' + $action.path)
            [void](New-Item -ItemType Directory -Force -Path (Split-Path -Parent $backup))
            Move-Item -LiteralPath $target -Destination $backup
        } elseif (Test-Path -LiteralPath $target) { throw 'Unowned file appeared while updating.' }
        if ($action.new_sha256) {
            $source = Get-Quest3DInstallPath $Package.root $action.path
            if ((Get-FileHash -LiteralPath $source).Hash -ine $action.new_sha256) { throw 'Update package changed while copying.' }
            Copy-Quest3DAtomic $source $target
        }
    }
    Copy-Quest3DAtomic (Get-Quest3DInstallPath $Package.root 'distribution-manifest.json') (Get-Quest3DInstallPath $Transaction.root 'distribution-manifest.json')
}

function Move-Quest3DUpdateEnvironment([hashtable]$Transaction) {
    $Transaction.journal.phase = 'environment'
    Write-Quest3DJson $Transaction.path $Transaction.journal
    $environment = Get-Quest3DInstallPath $Transaction.root '.venv'
    if (Test-Path -LiteralPath $environment) {
        if (!(Test-Path -LiteralPath $environment -PathType Container)) { throw 'Environment path is not a directory; preserved.' }
        Assert-Quest3DTreeNoReparse $environment
        $backup = Get-Quest3DInstallPath $Transaction.directory 'environment'
        if (Test-Path -LiteralPath $backup) { throw 'Environment backup already exists; preserved.' }
        Move-Item -LiteralPath $environment -Destination $backup
        $Transaction.journal.environment_saved = $true
        Write-Quest3DJson $Transaction.path $Transaction.journal
    }
}

function Complete-Quest3DUpdate([hashtable]$Transaction) {
    $installed = Get-Quest3DOwnedInstall $Transaction.root
    if (!$installed.owner.completed -or $installed.package.hash -ine $Transaction.journal.new_manifest_sha256) { throw 'Update cannot commit before installation validation completes.' }
    $null = Get-Quest3DPackage $Transaction.root -VerifyFiles
    $Transaction.journal.phase = 'committed'
    Write-Quest3DJson $Transaction.path $Transaction.journal
}

function Restore-Quest3DUpdate([string]$Root) {
    $transaction = Get-Quest3DTransaction $Root
    if (!$transaction -or $transaction.journal.phase -eq 'rolled-back') { return @{restored=$false} }
    Assert-Quest3DInstallStopped $transaction.root
    # Inspect every target and backup before restoring any: never overwrite user edits.
    foreach ($action in $transaction.journal.actions) {
        Assert-Quest3DPayloadName $action.path
        $target = Get-Quest3DInstallPath $transaction.root $action.path
        $backup = Get-Quest3DInstallPath $transaction.directory ('payload/' + $action.path)
        if (Test-Path -LiteralPath $backup) {
            if (!$action.old_sha256 -or !(Test-Path -LiteralPath $backup -PathType Leaf) -or (Get-FileHash -LiteralPath $backup).Hash -ine $action.old_sha256) { throw 'Update backup changed; manual recovery required.' }
        }
        if (Test-Path -LiteralPath $target) {
            if (!(Test-Path -LiteralPath $target -PathType Leaf)) { throw 'Update target became a directory; preserved.' }
            $hash = (Get-FileHash -LiteralPath $target).Hash
            if ($hash -ine $action.old_sha256 -and $hash -ine $action.new_sha256) { throw "Updated file was edited; rollback preserved it: $($action.path)" }
            if ($action.old_sha256 -and !(Test-Path -LiteralPath $backup -PathType Leaf) -and $hash -ine $action.old_sha256) { throw 'Original file backup is missing; manual recovery required.' }
        } elseif ($action.old_sha256 -and !(Test-Path -LiteralPath $backup -PathType Leaf)) { throw 'Original file and backup are missing; manual recovery required.' }
    }
    $oldEnvironment = Get-Quest3DInstallPath $transaction.directory 'environment'
    $environment = Get-Quest3DInstallPath $transaction.root '.venv'
    $savedProperty = $transaction.journal.PSObject.Properties['environment_saved']
    $environmentSaved = $savedProperty -and $savedProperty.Value -eq $true
    if ($transaction.journal.environment_existed -and !(Test-Path -LiteralPath $oldEnvironment -PathType Container) -and
        ($environmentSaved -or $transaction.journal.phase -eq 'committed' -or
            ($transaction.journal.phase -eq 'environment' -and !(Test-Path -LiteralPath $environment -PathType Container)))) {
        throw 'Original environment backup is missing; manual recovery required.'
    }
    if (Test-Path -LiteralPath $oldEnvironment) {
        Assert-Quest3DTreeNoReparse $oldEnvironment
        if (Test-Path -LiteralPath $environment) { Assert-Quest3DTreeNoReparse $environment }
    }
    $shortcutMigration = $transaction.journal.PSObject.Properties['shortcut_migration']
    if ($shortcutMigration) { Restore-Quest3DShortcutMigration $transaction.root $shortcutMigration.Value }
    foreach ($action in $transaction.journal.actions) {
        $target = Get-Quest3DInstallPath $transaction.root $action.path
        $backup = Get-Quest3DInstallPath $transaction.directory ('payload/' + $action.path)
        if (Test-Path -LiteralPath $backup -PathType Leaf) { Copy-Quest3DAtomic $backup $target }
        elseif (!$action.old_sha256 -and (Test-Path -LiteralPath $target -PathType Leaf)) { Remove-Item -LiteralPath $target }
    }
    if (Test-Path -LiteralPath $oldEnvironment) {
        if (Test-Path -LiteralPath $environment) {
            $held = Get-Quest3DInstallPath $transaction.directory ('failed-environment-' + [Guid]::NewGuid().ToString('N'))
            Move-Item -LiteralPath $environment -Destination $held
        }
        Move-Item -LiteralPath $oldEnvironment -Destination $environment
    } elseif (!$transaction.journal.environment_existed -and $transaction.journal.phase -in @('environment','committed') -and
        (Test-Path -LiteralPath $environment)) {
        Assert-Quest3DTreeNoReparse $environment
        Move-Item -LiteralPath $environment -Destination (Get-Quest3DInstallPath $transaction.directory ('failed-environment-' + [Guid]::NewGuid().ToString('N')))
    }
    foreach ($name in @('distribution-manifest.json','quest3d-install.json')) {
        Copy-Quest3DAtomic (Get-Quest3DInstallPath $transaction.directory ('metadata/' + $name)) (Get-Quest3DInstallPath $transaction.root $name)
    }
    $transaction.journal.phase = 'rolled-back'
    Write-Quest3DJson $transaction.path $transaction.journal
    return @{restored=$true;release=$transaction.journal.old_release;backup=$transaction.directory}
}

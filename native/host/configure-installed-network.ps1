<# Explicit installed-app action; no automatic elevation, profile change or discovery exception. #>
[CmdletBinding()]
param([switch]$Apply,[switch]$Remove,[switch]$Status,
    [ValidatePattern('^[0-9a-f]{32}$')][string]$OperationId,[string]$ReportPath)

function Assert-Quest3DNetworkPath([string]$Path) {
    $current=[IO.Path]::GetFullPath($Path)
    while($current) {
        try { if([IO.File]::GetAttributes($current) -band [IO.FileAttributes]::ReparsePoint){throw 'Network action cannot follow a reparse point.'} }
        catch {
            $cause=$_.Exception
            while($cause.InnerException){$cause=$cause.InnerException}
            if($cause -isnot [IO.FileNotFoundException] -and $cause -isnot [IO.DirectoryNotFoundException]){throw}
        }
        $parent=[IO.Path]::GetDirectoryName($current)
        if(!$parent -or $parent -eq $current){break}
        $current=$parent
    }
}
function Get-Quest3DNetworkPath([string]$Root,[string]$Relative) {
    if([string]::IsNullOrWhiteSpace($Relative) -or $Relative -match '[:\\\x00]' -or $Relative.StartsWith('/') -or
        $Relative -match '(^|/)(\.{1,2}|[^/]*[. ])(/|$)'){throw 'Invalid installed network path.'}
    foreach($part in $Relative.Split('/')) {
        if(!$part -or $part -match '^(?i:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?$'){throw 'Reserved installed network path.'}
    }
    $fullRoot=[IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    $path=[IO.Path]::GetFullPath((Join-Path $fullRoot $Relative))
    if(!$path.StartsWith($fullRoot+'\',[StringComparison]::OrdinalIgnoreCase)){throw 'Installed network path escaped root.'}
    Assert-Quest3DNetworkPath $path
    return $path
}
function Read-Quest3DNetworkJson([string]$Path,[long]$Limit=1048576) {
    Assert-Quest3DNetworkPath $Path
    $info=Get-Item -LiteralPath $Path -Force -ErrorAction Stop
    if($info.PSIsContainer -or $info.Length -gt $Limit){throw 'Invalid network action metadata file.'}
    return (Get-Content -LiteralPath $Path -Raw -Encoding UTF8 -ErrorAction Stop | ConvertFrom-Json -ErrorAction Stop)
}
function Get-Quest3DNetworkInstallation([string]$Root) {
    $rootPath=[IO.Path]::GetFullPath($Root).TrimEnd('\','/')
    if(!$rootPath -or $rootPath.Length -le [IO.Path]::GetPathRoot($rootPath).TrimEnd('\','/').Length -or
        $rootPath.IndexOfAny([char[]]"#`"`r`n") -ge 0){throw 'Invalid installed network root.'}
    Assert-Quest3DNetworkPath $rootPath
    $manifestPath=Get-Quest3DNetworkPath $rootPath 'distribution-manifest.json'
    $manifest=Read-Quest3DNetworkJson $manifestPath 8388608
    $owner=Read-Quest3DNetworkJson (Get-Quest3DNetworkPath $rootPath 'quest3d-install.json')
    $manifestSha=(Get-FileHash -LiteralPath $manifestPath -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
    if($manifest.schema -ne 1 -or !$manifest.files -or [string]$manifest.release -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$' -or
        $owner.product -cne 'Quest3D Desktop' -or $owner.completed -ne $true -or $owner.completed -isnot [bool] -or
        $owner.release -cne $manifest.release -or [string]$owner.package_manifest_sha256 -notmatch '^[a-fA-F0-9]{64}$' -or $owner.package_manifest_sha256 -ine $manifestSha){throw 'Network action requires a completed owned installation.'}
    $files=@{}
    foreach($entry in $manifest.files.PSObject.Properties) {
        $null=Get-Quest3DNetworkPath $rootPath $entry.Name
        if($files.ContainsKey($entry.Name) -or [string]$entry.Value.sha256 -notmatch '^[a-fA-F0-9]{64}$' -or
            ($entry.Value.bytes -isnot [int] -and $entry.Value.bytes -isnot [long]) -or $entry.Value.bytes -lt 0){throw 'Invalid network installation payload manifest.'}
        $files[$entry.Name]=$entry.Value
    }
    $config=Read-Quest3DNetworkJson (Get-Quest3DNetworkPath $rootPath 'config/distribution.json')
    $expectedHash='77c950b526ba6b944589b8697cbaaa76b26955e3ae2e412a4cfba7bc93626b63'
    if($config.schema -ne 1 -or $config.host_sha256 -cne $expectedHash -or
        [string]$config.host_runtime -cnotmatch '^artifacts/host/runtime-[A-Za-z0-9_-]+$'){throw 'Unrecognized installed network host.'}
    $exeRelative=$config.host_runtime+'/sunshine.exe'
    foreach($relative in @('native/host/configure-installed-network.ps1','.tools/desktop/powershell/pwsh.exe','config/distribution.json',$exeRelative)) {
        $file=Get-Quest3DNetworkPath $rootPath $relative;$entry=$files[$relative]
        if(!$entry -or !(Test-Path -LiteralPath $file -PathType Leaf) -or (Get-Item -LiteralPath $file).Length -ne $entry.bytes -or
            (Get-FileHash -LiteralPath $file -Algorithm SHA256 -ErrorAction Stop).Hash -ine $entry.sha256){throw 'Installed network action file checksum mismatch.'}
    }
    $program=Get-Quest3DNetworkPath $rootPath $exeRelative
    if((Get-FileHash -LiteralPath $program -Algorithm SHA256 -ErrorAction Stop).Hash -ine $expectedHash){throw 'Unexpected installed host executable.'}
    $sha=[Security.Cryptography.SHA256]::Create()
    try{$key=([BitConverter]::ToString($sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($program.ToLowerInvariant())))).Replace('-','').Substring(0,12)}finally{$sha.Dispose()}
    return @{root=$rootPath;program=$program;key=$key;manifest_sha256=$manifestSha;rules=@(
        @{name="Quest3D-$key-TCP";protocol='TCP';ports=@(47984,47989,48010)},
        @{name="Quest3D-$key-UDP";protocol='UDP';ports=@(47998,47999,48000)})}
}
function Get-Quest3DNetworkRows {
    # Query once without -Name: absence is an empty exact-name selection.
    # Provider/access errors stop the operation, including removal.
    return @(Get-NetFirewallRule -PolicyStore PersistentStore -ErrorAction Stop)
}
function Get-Quest3DNetworkSnapshot([array]$Rows,[hashtable]$Rule,[string]$Program,[string]$Key) {
    $found=@($Rows | Where-Object {$_.Name -ieq $Rule.name})
    if(!$found.Count){return $null}
    $apps=@($found | Get-NetFirewallApplicationFilter -ErrorAction Stop)
    if($found.Count -ne 1 -or $found[0].Description -cne "Quest3D installation $Key | $Program" -or
        $apps.Count -ne 1 -or $apps[0].Program -ine $Program){throw 'A different rule owns this name. No conflicting rule was changed.'}
    $ports=@($found | Get-NetFirewallPortFilter -ErrorAction Stop)
    $addresses=@($found | Get-NetFirewallAddressFilter -ErrorAction Stop)
    if($ports.Count -ne 1 -or $addresses.Count -ne 1){throw 'Owned rule filters could not be verified.'}
    return @{rule=$found[0];ports=$ports[0];addresses=$addresses[0]}
}
function Test-Quest3DNetworkScope([hashtable]$Snapshot,[hashtable]$Rule) {
    if(!$Snapshot){return $false}
    $row=$Snapshot.rule;$ports=$Snapshot.ports;$addresses=$Snapshot.addresses
    $protocol=if($Rule.protocol -eq 'TCP'){'6'}else{'17'}
    return ([string]$ports.Protocol -in @($Rule.protocol,$protocol) -and
        (@($ports.LocalPort | ForEach-Object {[string]$_} | Sort-Object) -join ',') -ceq (@($Rule.ports | ForEach-Object {[string]$_} | Sort-Object) -join ',') -and
        (@($ports.RemotePort) -join ',') -ieq 'Any' -and (@($addresses.RemoteAddress) -join ',') -ieq 'LocalSubnet' -and
        (@($addresses.LocalAddress) -join ',') -ieq 'Any' -and [string]$row.Profile -in @('Private','2') -and
        [string]$row.Direction -ieq 'Inbound' -and [string]$row.Action -ieq 'Allow' -and [string]$row.Enabled -ieq 'True' -and
        [string]$row.EdgeTraversalPolicy -ieq 'Block')
}
function Get-Quest3DNetworkActual([array]$Rows,[string]$Program,[string]$Key,[array]$Rules,[bool]$Delete) {
    $actual=@();$verified=$true
    foreach($spec in $Rules) {
        $snapshot=Get-Quest3DNetworkSnapshot $Rows $spec $Program $Key
        $scope=Test-Quest3DNetworkScope $snapshot $spec
        $actual += @{name=$spec.name;present=($null -ne $snapshot);scope_verified=[bool]$scope}
        if(($Delete -and $snapshot) -or (!$Delete -and !$scope)){$verified=$false}
    }
    return @{verified=$verified;rules=$actual}
}
function Update-Quest3DInstalledNetworkRules([string]$Program,[string]$Key,[array]$Rules,[bool]$Delete) {
    $result=[ordered]@{created=@();removed=@();kept=@();rolled_back=@();rollback_errors=@();errors=@();actual_rules=@();verified_after=$false;requested_applied=$false;partial=$false}
    $existing=@{}
    try {
        $rows=Get-Quest3DNetworkRows
        foreach($spec in $Rules) {
            $snapshot=Get-Quest3DNetworkSnapshot $rows $spec $Program $Key
            if(!$snapshot){continue}
            if(!$Delete -and !(Test-Quest3DNetworkScope $snapshot $spec)){throw 'An owned rule has different settings. Remove it explicitly before recreating.'}
            $existing[$spec.name]=$snapshot.rule
        }
        foreach($spec in $Rules) {
            if($existing.ContainsKey($spec.name)) {
                if($Delete){$existing[$spec.name] | Remove-NetFirewallRule -ErrorAction Stop;$result.removed += $spec.name}else{$result.kept += $spec.name}
            } elseif(!$Delete) {
                $null=New-NetFirewallRule -PolicyStore PersistentStore -Name $spec.name -DisplayName ('Quest3D LAN '+$spec.protocol) -Description "Quest3D installation $Key | $Program" -Group "Quest3D-$Key" -Direction Inbound -Action Allow -Enabled True -Profile Private -Program $Program -Protocol $spec.protocol -LocalPort $spec.ports -RemoteAddress LocalSubnet -EdgeTraversalPolicy Block -ErrorAction Stop
                $result.created += $spec.name
            }
        }
        $actual=Get-Quest3DNetworkActual (Get-Quest3DNetworkRows) $Program $Key $Rules $Delete
        $result.actual_rules=$actual.rules;$result.verified_after=[bool]$actual.verified;$result.requested_applied=[bool]$actual.verified
        if(!$actual.verified){throw 'Requested firewall state did not verify after the operation.'}
        return [pscustomobject]$result
    } catch {
        $failure=$_;$result.errors += $_.Exception.Message
        foreach($name in $result.created) {
            try {
                $spec=@($Rules | Where-Object {$_.name -ceq $name})[0]
                $snapshot=Get-Quest3DNetworkSnapshot (Get-Quest3DNetworkRows) $spec $Program $Key
                if($snapshot){$snapshot.rule | Remove-NetFirewallRule -ErrorAction Stop}
                $result.rolled_back += $name
            } catch{$result.rollback_errors += $name;$result.errors += $_.Exception.Message}
        }
        try {
            $actual=Get-Quest3DNetworkActual (Get-Quest3DNetworkRows) $Program $Key $Rules $Delete
            $result.actual_rules=$actual.rules;$result.verified_after=[bool]$actual.verified
        } catch{$result.errors += $_.Exception.Message;$result.verified_after=$false}
        $result.partial=($result.removed.Count -gt 0 -or $result.rollback_errors.Count -gt 0)
        $failure.Exception.Data['Quest3DNetworkResult']=[pscustomobject]$result
        throw $failure
    }
}
function Initialize-Quest3DNetworkReceipt([string]$Root,[string]$Id,[string]$RequestedPath,[bool]$StatusOnly=$false) {
    if($Id -cnotmatch '^[0-9a-f]{32}$'){throw 'A fresh GUID32 operation ID is required.'}
    $expected=Get-Quest3DNetworkPath $Root ('config/network-operations/'+$Id+'/result.json')
    if($RequestedPath -and [IO.Path]::GetFullPath($RequestedPath) -ine $expected){throw 'Network receipt must use this operation inside the installation config directory.'}
    if(Test-Path -LiteralPath $expected){throw 'Network operation receipt already exists. Use a new operation ID.'}
    $journal=$null
    if(!$StatusOnly){$journal=Get-Quest3DNetworkPath $Root 'config/network-rules.json'}
    if($journal -and (Test-Path -LiteralPath $journal) -and !(Test-Path -LiteralPath $journal -PathType Leaf)){throw 'Network journal must be a regular file.'}
    $directory=[IO.Path]::GetDirectoryName($expected)
    [void][IO.Directory]::CreateDirectory($directory);Assert-Quest3DNetworkPath $directory
    $stream=[IO.File]::Open($expected,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    if($StatusOnly){return @{path=$expected;journal=$null;stream=$stream}}
    try {
        $probe=Get-Quest3DNetworkPath $Root ('config/.network-write-probe-'+$Id)
        $probeCreated=$false
        try {
            $test=[IO.File]::Open($probe,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
            $probeCreated=$true
            try{$test.WriteByte(0);$test.Flush()}finally{$test.Dispose()}
        } finally{if($probeCreated -and (Test-Path -LiteralPath $probe)){Remove-Item -LiteralPath $probe -ErrorAction Stop}}
        if(Test-Path -LiteralPath $journal){$test=[IO.File]::Open($journal,[IO.FileMode]::Open,[IO.FileAccess]::Write,[IO.FileShare]::None);$test.Dispose()}
        return @{path=$expected;journal=$journal;stream=$stream}
    } catch{$stream.Dispose();throw}
}
function Write-Quest3DNetworkReceipt([hashtable]$Paths,[object]$Value) {
    function Write-Result {
        Assert-Quest3DNetworkPath $Paths.path
        $bytes=[Text.Encoding]::UTF8.GetBytes(($Value | ConvertTo-Json -Depth 10))
        $Paths.stream.Position=0;$Paths.stream.SetLength(0);$Paths.stream.Write($bytes,0,$bytes.Length);$Paths.stream.Flush()
    }
    try {
        Write-Result
        try{if($Paths.journal){Assert-Quest3DNetworkPath $Paths.journal;[IO.File]::WriteAllText($Paths.journal,($Value | ConvertTo-Json -Depth 10),[Text.UTF8Encoding]::new($false))}}
        catch{$Value.success=$false;$Value.outcome='failed';$Value.error='Rules were inspected, but the compatibility journal could not be saved.';$Value.errors += $_.Exception.Message;Write-Result;throw}
    } finally{$Paths.stream.Dispose()}
}
function Get-Quest3DInstalledNetworkStatus([hashtable]$Installation) {
    $result=[ordered]@{schema=1;operation_id=$null;action='status';outcome='status';mode='status';read_only=$true;program=$Installation.program;updated=(Get-Date).ToString('o');success=$false;known=$false;verified_after=$false;error=$null;partial=$false;requested_applied=$false;apply_satisfied=$false;owned_rules_absent=$false;rules_state='unknown';rules=@();errors=@();diagnostics_errors=@();diagnostics=@{
        connection_profiles=@();firewall_profiles=@();enabled_app_block_rules='unknown';block_diagnostic_scope='exact_program_only';effective_policy_scope='partial';discovery_policy='unknown'}}
    try {
        $actual=Get-Quest3DNetworkActual (Get-Quest3DNetworkRows) $Installation.program $Installation.key $Installation.rules $false
        $result.rules=$actual.rules;$result.known=$true;$result.success=$true;$result.verified_after=$true
        $result.apply_satisfied=[bool]$actual.verified;$result.requested_applied=[bool]$actual.verified
        $result.owned_rules_absent=(@($actual.rules | Where-Object {$_.present}).Count -eq 0)
        $result.rules_state=if($actual.verified){'applied'}elseif($result.owned_rules_absent){'absent'}else{'not_applied'}
    } catch{$result.error=$_.Exception.Message;$result.errors += $_.Exception.Message}
    try{$result.diagnostics.connection_profiles=@(Get-NetConnectionProfile -ErrorAction Stop | ForEach-Object {[string]$_.NetworkCategory})}catch{$result.errors += 'Connection profiles could not be inspected.';$result.diagnostics_errors += 'Connection profiles could not be inspected.'}
    try{$result.diagnostics.firewall_profiles=@(Get-NetFirewallProfile -ErrorAction Stop | ForEach-Object {@{profile=[string]$_.Name;enabled=[string]$_.Enabled}})}catch{$result.errors += 'Firewall profiles could not be inspected.';$result.diagnostics_errors += 'Firewall profiles could not be inspected.'}
    try {
        $blocks=@(Get-NetFirewallRule -PolicyStore ActiveStore -ErrorAction Stop | Where-Object {[string]$_.Enabled -ieq 'True' -and [string]$_.Direction -ieq 'Inbound' -and [string]$_.Action -ieq 'Block'})
        $count=0
        foreach($row in $blocks){if(@($row | Get-NetFirewallApplicationFilter -ErrorAction Stop | Where-Object {$_.Program -ieq $Installation.program}).Count){$count++}}
        $result.diagnostics.enabled_app_block_rules=$count
    } catch{$result.errors += 'Effective app block rules could not be fully inspected.';$result.diagnostics_errors += 'Effective app block rules could not be fully inspected.'}
    return [pscustomobject]$result
}

if($MyInvocation.InvocationName -eq '.'){return}
$ErrorActionPreference='Stop'
if(@(@($Apply,$Remove,$Status) | Where-Object {$_}).Count -gt 1){throw 'Choose Apply, Remove or Status.'}
$TaskInstallation=Get-Quest3DNetworkInstallation ([IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..')))
if($Status) {
    if($ReportPath -and !$OperationId){throw 'A status report requires its fresh operation ID.'}
    $TaskStatus=Get-Quest3DInstalledNetworkStatus $TaskInstallation
    if($OperationId) {
        $TaskStatus.operation_id=$OperationId
        $TaskStatusReceipt=Initialize-Quest3DNetworkReceipt $TaskInstallation.root $OperationId $ReportPath $true
        Write-Quest3DNetworkReceipt $TaskStatusReceipt $TaskStatus
    }
    $TaskStatus | ConvertTo-Json -Depth 10;return
}
if(!$Apply -and !$Remove){@{mode='plan';program=$TaskInstallation.program;profile='Private';remote_address='LocalSubnet';rules=$TaskInstallation.rules;admin_required_for_changes=$true;management_port_included=$false;discovery_exception_included=$false} | ConvertTo-Json -Depth 5;return}
$TaskPrincipal=[Security.Principal.WindowsPrincipal]::new([Security.Principal.WindowsIdentity]::GetCurrent())
if(!$TaskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)){throw 'Approve the Windows administrator prompt for this explicit network action.'}
if(!$OperationId){$OperationId=[Guid]::NewGuid().ToString('N')}
$TaskReceipt=Initialize-Quest3DNetworkReceipt $TaskInstallation.root $OperationId $ReportPath
$TaskAction=if($Remove){'remove'}else{'apply'}
$TaskValue=[ordered]@{schema=1;operation_id=$OperationId;action=$TaskAction;success=$false;outcome='failed';error=$null;program=$TaskInstallation.program;updated=(Get-Date).ToString('o');profile='Private';remote_address='LocalSubnet';verified_after=$false;requested_applied=$false;partial=$false;errors=@();result=$null}
$TaskFailed=$false
try {
    $TaskResult=Update-Quest3DInstalledNetworkRules $TaskInstallation.program $TaskInstallation.key $TaskInstallation.rules ([bool]$Remove)
    $TaskValue.result=$TaskResult;$TaskValue.success=$true;$TaskValue.outcome=if($Remove){'removed'}else{'applied'}
    $TaskValue.verified_after=$TaskResult.verified_after;$TaskValue.requested_applied=$TaskResult.requested_applied
} catch {
    $TaskFailed=$true;$TaskValue.error=$_.Exception.Message;$TaskException=$_.Exception
    while($TaskException -and !$TaskException.Data.Contains('Quest3DNetworkResult')){$TaskException=$TaskException.InnerException}
    if($TaskException){$TaskValue.result=$TaskException.Data['Quest3DNetworkResult']}
    if($TaskValue.result){$TaskValue.verified_after=$TaskValue.result.verified_after;$TaskValue.partial=$TaskValue.result.partial;$TaskValue.errors=$TaskValue.result.errors}else{$TaskValue.errors=@($TaskValue.error)}
}
Write-Quest3DNetworkReceipt $TaskReceipt $TaskValue
if($TaskFailed){throw $TaskValue.error}
Write-Output 'Quest3D private LAN rule state verified. Public networks, management and discovery policy were not changed.'

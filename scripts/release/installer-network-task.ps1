<# Tracked installer network tasks. Does not elevate or change rules when loaded. #>
. (Join-Path $PSScriptRoot 'installation-lifecycle.ps1')

function Get-Quest3DNetworkExecutables([string]$Root) {
    $installed = Get-Quest3DOwnedInstall $Root
    if (!$installed.owner.completed) { throw 'Finish the installation before changing network rules.' }
    $transaction = Get-Quest3DTransaction $installed.root
    if ($transaction -and $transaction.journal.phase -notin @('committed','rolled-back')) {
        throw 'Restore the interrupted update before changing network rules.'
    }
    $required = @('native/host/configure-installed-network.ps1', '.tools/desktop/powershell/pwsh.exe', 'scripts/release/installer-network-task.ps1', 'config/distribution.json')
    $paths = @{}
    foreach ($name in $required) {
        if (!$installed.package.files.ContainsKey($name)) { throw 'Update this installation before using tracked network setup.' }
        $path = Get-Quest3DInstallPath $installed.root $name
        $entry = $installed.package.files[$name]
        if (!(Test-Path -LiteralPath $path -PathType Leaf) -or (Get-Item -LiteralPath $path).Length -ne $entry.bytes -or
            (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $entry.sha256) {
            throw ('Network setup file changed; preserved: ' + $name)
        }
        $paths[$name] = $path
    }
    $configuration = Get-Content -LiteralPath $paths['config/distribution.json'] -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($configuration.schema -ne 1 -or $configuration.host_runtime -isnot [string] -or $configuration.host_sha256 -isnot [string] -or
        $configuration.host_sha256 -cnotmatch '^[a-f0-9]{64}$') { throw 'Invalid installed host configuration.' }
    $hostName = $configuration.host_runtime + '/sunshine.exe'
    $hostPath = Get-Quest3DInstallPath $installed.root $hostName
    if (!$installed.package.files.ContainsKey($hostName)) { throw 'Installed host is not part of the owned package.' }
    $hostEntry = $installed.package.files[$hostName]
    if ($hostEntry.sha256 -ine $configuration.host_sha256 -or !(Test-Path -LiteralPath $hostPath -PathType Leaf) -or
        (Get-Item -LiteralPath $hostPath).Length -ne $hostEntry.bytes -or
        (Get-FileHash -LiteralPath $hostPath -Algorithm SHA256).Hash -ine $configuration.host_sha256) { throw 'Installed host changed; preserved.' }
    return [pscustomobject]@{ root=$installed.root; shell=$paths[$required[1]]; helper=$paths[$required[0]]; program=$hostPath }
}

function New-Quest3DNetworkRequest([string]$Root, [string]$Action) {
    if ($Action -cnotin @('apply','remove','status')) { throw 'Unsupported network action.' }
    $tools = Get-Quest3DNetworkExecutables $Root
    if ($Action -ceq 'remove') { Assert-Quest3DInstallStopped $tools.root }
    $operation = [Guid]::NewGuid().ToString('N')
    $parent = Get-Quest3DInstallPath $tools.root 'config/network-operations'
    [void](New-Item -ItemType Directory -Path $parent -Force)
    $folder = Get-Quest3DInstallPath $tools.root ('config/network-operations/' + $operation)
    [void](New-Item -ItemType Directory -Path $folder)
    $report = Get-Quest3DInstallPath $tools.root ('config/network-operations/' + $operation + '/result.json')
    if (Test-Path -LiteralPath $report) { throw 'A network result already exists. Start a new request.' }
    return [pscustomobject]@{ schema=1; root=$tools.root; operation_id=$operation; action=$Action; report_path=$report; expected_program=$tools.program }
}

function Get-Quest3DNetworkReportPath($Request) {
    if ($Request.schema -ne 1 -or $Request.operation_id -cnotmatch '^[a-f0-9]{32}$' -or $Request.action -cnotin @('apply','remove','status')) {
        throw 'Invalid network request.'
    }
    $path = Get-Quest3DInstallPath $Request.root ('config/network-operations/' + $Request.operation_id + '/result.json')
    if (![StringComparer]::OrdinalIgnoreCase.Equals($path, $Request.report_path)) { throw 'Network report path does not match this request.' }
    return $path
}

function Start-Quest3DNetworkRequest($Request) {
    $path = Get-Quest3DNetworkReportPath $Request
    if (Test-Path -LiteralPath $path) { throw 'A network result already exists. Start a new request.' }
    # Recheck actual installed files immediately before asking Windows to elevate.
    $tools = Get-Quest3DNetworkExecutables $Request.root
    if (![StringComparer]::OrdinalIgnoreCase.Equals($tools.program,$Request.expected_program)) { throw 'The installed host changed after this request.' }
    if ($Request.action -ceq 'remove') { Assert-Quest3DInstallStopped $tools.root }
    $flag = if ($Request.action -ceq 'apply') { '-Apply' } elseif ($Request.action -ceq 'remove') { '-Remove' } else { '-Status' }
    $arguments = '-NoProfile -File "' + $tools.helper + '" ' + $flag + ' -OperationId "' + $Request.operation_id + '" -ReportPath "' + $path + '"'
    if ($Request.action -ceq 'status') {
        return Start-Process -FilePath $tools.shell -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput ($path + '.output.txt') -RedirectStandardError ($path + '.error.txt')
    }
    return Start-Process -FilePath $tools.shell -ArgumentList $arguments -Verb RunAs -WindowStyle Hidden -PassThru
}

function Test-Quest3DUacCancelled($ErrorRecord) {
    $cause = $ErrorRecord.Exception
    while ($cause.InnerException) { $cause = $cause.InnerException }
    return $cause -is [ComponentModel.Win32Exception] -and $cause.NativeErrorCode -eq 1223
}

function Read-Quest3DNetworkResult($Request, [int]$ExitCode) {
    try {
        $path = Get-Quest3DNetworkReportPath $Request
        if (!(Test-Path -LiteralPath $path -PathType Leaf)) { throw 'No result was written for this request. Network setup is not confirmed.' }
        if ((Get-Item -LiteralPath $path).Length -gt 1048576) { throw 'Network result is too large.' }
        $report = Get-Content -LiteralPath $path -Raw -Encoding UTF8 | ConvertFrom-Json
        $tools = Get-Quest3DNetworkExecutables $Request.root
        $expected = if ($Request.action -ceq 'apply') { 'applied' } elseif ($Request.action -ceq 'remove') { 'removed' } else { 'status' }
        if ($report.schema -ne 1 -or $report.operation_id -cne $Request.operation_id -or $report.action -cne $Request.action) {
            throw 'The result belongs to another request. Network setup is not confirmed.'
        }
        if (![StringComparer]::OrdinalIgnoreCase.Equals($tools.program,$Request.expected_program) -or
            $report.program -isnot [string] -or ![StringComparer]::OrdinalIgnoreCase.Equals($tools.program,$report.program)) {
            throw 'The result belongs to another installed host. Network setup is not confirmed.'
        }
        if ($Request.action -ceq 'status' -and $ExitCode -eq 0 -and $report.outcome -ceq 'status' -and
            $report.known -is [bool] -and !$report.known -and $report.success -is [bool] -and !$report.success -and
            $report.verified_after -is [bool] -and !$report.verified_after -and $report.partial -is [bool] -and !$report.partial -and
            $report.requested_applied -is [bool] -and !$report.requested_applied -and $report.apply_satisfied -is [bool] -and !$report.apply_satisfied -and
            $report.owned_rules_absent -is [bool] -and !$report.owned_rules_absent -and $report.rules_state -ceq 'unknown') {
            return [pscustomobject]@{ accepted=$false; trusted_unknown=$true; report=$report; error='The current user could not verify these application rules. Administrator verification is required.' }
        }
        if ($ExitCode -ne 0 -or $report.success -isnot [bool] -or !$report.success -or
            $report.verified_after -isnot [bool] -or !$report.verified_after -or $report.outcome -cne $expected -or
            $report.partial -isnot [bool] -or $report.partial -or $report.requested_applied -isnot [bool] -or
            ($Request.action -cne 'status' -and !$report.requested_applied)) {
            $reason = if ($report.PSObject.Properties.Name -contains 'error' -and $report.error) { [string]$report.error } else { 'Windows did not confirm the complete network change.' }
            throw $reason
        }
        if ($Request.action -ceq 'status' -and ($report.known -isnot [bool] -or !$report.known -or
            $report.apply_satisfied -isnot [bool] -or $report.owned_rules_absent -isnot [bool] -or
            ($report.apply_satisfied -and $report.owned_rules_absent))) {
            throw 'Windows could not verify these application rules. No administrator task or retirement was started.'
        }
        if ($Request.action -ceq 'status') {
            $state = if ($report.apply_satisfied) { 'applied' } elseif ($report.owned_rules_absent) { 'absent' } else { 'not_applied' }
            if ($report.rules_state -cne $state -or $report.requested_applied -ne $report.apply_satisfied) { throw 'The status flags are inconsistent. No administrator task or retirement was started.' }
        }
        return [pscustomobject]@{ accepted=$true; trusted_unknown=$false; report=$report; error=$null }
    } catch {
        return [pscustomobject]@{ accepted=$false; trusted_unknown=$false; report=$null; error=$_.Exception.Message }
    }
}

function Use-Quest3DNetworkRetirementReceipt([string]$Root, [string]$OperationId, [string]$ReceiptAction) {
    if ($ReceiptAction -cnotin @('remove','status')) { throw 'Unsupported retirement receipt.' }
    $tools = Get-Quest3DNetworkExecutables $Root
    $path = Get-Quest3DInstallPath $tools.root ('config/network-operations/' + $OperationId + '/result.json')
    $request = [pscustomobject]@{schema=1;root=$tools.root;operation_id=$OperationId;action=$ReceiptAction;report_path=$path;expected_program=$tools.program}
    $receipt = Read-Quest3DNetworkResult $request 0
    if (!$receipt.accepted) { throw ('Network cleanup is not confirmed. Application retained: ' + $receipt.error) }
    if ($ReceiptAction -ceq 'status' -and !$receipt.report.owned_rules_absent) { throw 'Application rules are still present. Application retained.' }
    $written = [IO.File]::GetLastWriteTimeUtc($path)
    $receiptHash = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    $now = [DateTime]::UtcNow
    if ($written -lt $now.AddMinutes(-5) -or $written -gt $now.AddMinutes(1)) { throw 'Network receipt expired. Retry removal from the install UI; application retained.' }
    $consumed = Get-Quest3DInstallPath $tools.root ('config/network-operations/' + $OperationId + '/retirement-consumed.json')
    if (Test-Path -LiteralPath $consumed) { throw 'Network receipt was already used. Start a new removal request; application retained.' }
    # Reinspect current rules as the original Windows user. A status receipt alone
    # may skip UAC only while this new readback still proves both rules absent.
    $currentRequest = New-Quest3DNetworkRequest $tools.root 'status'
    $child = Start-Quest3DNetworkRequest $currentRequest
    try {
        $child.WaitForExit()
        $current = Read-Quest3DNetworkResult $currentRequest $child.ExitCode
    } finally { $child.Dispose() }
    if ($current.accepted) {
        if (!$current.report.owned_rules_absent) { throw 'Application rules were added again. Retry removal from the install UI; application retained.' }
    } elseif ($ReceiptAction -cne 'remove' -or !$current.trusted_unknown) {
        throw ('Current rule state could not confirm retirement. Retry from the install UI; application retained: ' + $current.error)
    }
    Assert-Quest3DNoReparse $path
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ine $receiptHash -or [IO.File]::GetLastWriteTimeUtc($path) -lt [DateTime]::UtcNow.AddMinutes(-5)) {
        throw 'Network receipt changed or expired during verification. Retry from the install UI; application retained.'
    }
    # A fresh verified admin removal can be used when the original user's rule
    # query is unavailable. Record that limited readback scope explicitly.
    $value = @{schema=1;operation_id=$OperationId;receipt_action=$ReceiptAction;consumed_utc=[DateTime]::UtcNow.ToString('o');
        readback_operation_id=$currentRequest.operation_id;readback_known=[bool]$current.accepted;
        readback_owned_rules_absent=([bool]$current.accepted -and [bool]$current.report.owned_rules_absent)}
    Assert-Quest3DNoReparse $consumed
    $stream = [IO.File]::Open($consumed,[IO.FileMode]::CreateNew,[IO.FileAccess]::Write,[IO.FileShare]::None)
    try {
        $bytes = [Text.UTF8Encoding]::new($false).GetBytes(($value | ConvertTo-Json -Depth 5))
        $stream.Write($bytes,0,$bytes.Length)
    } finally { $stream.Dispose() }
    return [pscustomobject]$value
}

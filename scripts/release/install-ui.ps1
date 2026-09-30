[CmdletBinding()]
param([switch]$SelfTest, [string]$PreviewImage)
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'installation-lifecycle.ps1')
. (Join-Path $PSScriptRoot 'installer-network-task.ps1')
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$packageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
# Release a native current-directory handle when this is the installed management UI.
[Environment]::CurrentDirectory = [IO.Path]::GetTempPath()
$form = New-Object Windows.Forms.Form
$form.Text = 'Quest3D Desktop 설치'
$form.Size = New-Object Drawing.Size(700, 580)
$form.MinimumSize = New-Object Drawing.Size(700, 580)
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Malgun Gothic', 10)
$heading = New-Object Windows.Forms.Label
$heading.Text = 'PC에서 시작하고, Quest에서 연결하세요'
$heading.Font = New-Object Drawing.Font('Malgun Gothic', 16, [Drawing.FontStyle]::Bold)
$heading.SetBounds(24, 24, 640, 40)
$description = New-Object Windows.Forms.Label
$description.Text = "처음 한 번 Python, GPU 라이브러리와 AI 모델을 다운로드합니다.`r`n설치 후 AI는 PC에서 실행됩니다. NVIDIA Turing(sm75) GPU와 인터넷 연결이 필요합니다."
$description.SetBounds(24, 75, 640, 56)
$locationLabel = New-Object Windows.Forms.Label
$locationLabel.Text = '설치 폴더'
$locationLabel.SetBounds(24, 145, 100, 24)
$location = New-Object Windows.Forms.TextBox
$location.Text = Join-Path $env:LOCALAPPDATA 'Quest3D Desktop'
$location.SetBounds(24, 175, 520, 28)
$browse = New-Object Windows.Forms.Button
$browse.Text = '폴더 선택'
$browse.SetBounds(554, 173, 108, 32)
$shortcuts = New-Object Windows.Forms.CheckBox
$shortcuts.Text = '바탕화면과 시작 메뉴에 바로가기 만들기'
$shortcuts.Checked = $true
$shortcuts.SetBounds(24, 218, 520, 28)
$update = New-Object Windows.Forms.CheckBox
$update.Text = '기존 설치 업데이트 · 설정·페어링 유지'
$update.SetBounds(24, 247, 638, 28)
$update.Enabled = $false
$status = New-Object Windows.Forms.Label
$status.Text = '설치를 누르면 준비를 시작합니다. 다운로드는 수 GB이며 시간이 걸릴 수 있습니다.'
$status.SetBounds(24, 283, 638, 44)
$progress = New-Object Windows.Forms.ProgressBar
$progress.Minimum = 0
$progress.Maximum = 6
$progress.SetBounds(24, 333, 638, 24)
$detail = New-Object Windows.Forms.Label
$detail.Text = 'Windows 서비스·자동 시작·방화벽·브라우저 설정을 변경하지 않습니다.'
$detail.SetBounds(24, 369, 638, 40)
$install = New-Object Windows.Forms.Button
$install.Text = '설치'
$install.SetBounds(24, 421, 118, 38)
$launch = New-Object Windows.Forms.Button
$launch.Text = '앱 실행'
$launch.Enabled = $false
$launch.SetBounds(154, 421, 118, 38)
$network = New-Object Windows.Forms.Button
$network.Text = '연결 허용'
$network.Enabled = $false
$network.SetBounds(284, 421, 118, 38)
$networkTip = New-Object Windows.Forms.ToolTip
$networkTip.SetToolTip($network, "사설망(Private) · 로컬 서브넷(LocalSubnet) · 앱 스트리밍만 허용`r`n필요한 경우 Windows 관리자 승인 · 공용망·관리 페이지 제외")
$logs = New-Object Windows.Forms.Button
$logs.Text = '설치 로그'
$logs.Enabled = $false
$logs.SetBounds(414, 421, 118, 38)
$close = New-Object Windows.Forms.Button
$close.Text = '닫기'
$close.SetBounds(544, 421, 118, 38)
$rollback = New-Object Windows.Forms.Button
$rollback.Text = '이전 버전 복원'
$rollback.Enabled = $false
$rollback.SetBounds(24, 471, 160, 36)
$remove = New-Object Windows.Forms.Button
$remove.Text = '앱 제거 · 보관'
$remove.Enabled = $false
$remove.SetBounds(196, 471, 160, 36)
$form.Controls.AddRange(@($heading, $description, $locationLabel, $location, $browse, $shortcuts, $update, $status, $progress, $detail, $install, $launch, $network, $logs, $close, $rollback, $remove))
$script:installProcess = $null
$script:logFolder = $null
$script:installedRoot = $null
$script:operation = 'install'
$script:maintenanceMode = $false
$script:busy = $false
$script:networkRequest = $null
$script:pendingMaintenance = $null
$script:maintenanceRoot = $null
$script:requestedNetworkAction = $null
$script:adminStatusVerification = $false
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 750
function Refresh-Quest3DInstallChoice {
    if ($script:busy -or $script:installProcess) { return }
    $update.Enabled = $false
    $update.Checked = $false
    $install.Text = '설치'
    $install.Enabled = $true
    $rollback.Enabled = $false
    $remove.Enabled = $false
    $launch.Enabled = $false
    $network.Enabled = $false
    try {
        $owned = Get-Quest3DOwnedInstall $location.Text
        $script:installedRoot = $owned.root
        $remove.Enabled = $true
        $launch.Enabled = $owned.owner.completed -and (Test-Path -LiteralPath (Join-Path $owned.root '.venv/Scripts/pythonw.exe'))
        $network.Enabled = $owned.owner.completed -and $owned.package.files.ContainsKey('scripts/release/installer-network-task.ps1')
        $backup = Get-Quest3DTransaction $owned.root
        $rollback.Enabled = $null -ne $backup -and $backup.journal.phase -ne 'rolled-back'
        $packageManifest = Join-Path $packageRoot 'distribution-manifest.json'
        if ($owned.root -ieq $packageRoot) {
            $script:maintenanceMode = $true
            $install.Enabled = $false
            $detail.Text = '설치 관리 · 새 버전은 새 ZIP을 압축 해제한 뒤 같은 설치 폴더를 선택'
        } elseif ((Get-FileHash -LiteralPath $packageManifest).Hash -ine $owned.package.hash) {
            $update.Enabled = $true
            $update.Checked = $true
            $install.Text = '업데이트'
            $detail.Text = '기존 앱 종료 필요 · 실패 시 이전 버전 복원 · 설정·페어링 보존'
        } else {
            $install.Text = '설치 복구'
            $detail.Text = '같은 버전 설치 재시도 · 개인 설정·페어링 보존'
        }
    } catch {
        $script:installedRoot = $null
        $launch.Enabled = $false
        $network.Enabled = $false
        $remove.Enabled = $false
        $rollback.Enabled = $false
        # A crash can leave the new manifest with the old owner record. A valid
        # journal still proves ownership for rollback; do not offer app launch.
        try {
            $backup = Get-Quest3DTransaction $location.Text
            if ($backup -and $backup.journal.phase -notin @('committed','rolled-back')) {
                $script:installedRoot = $backup.root
                $rollback.Enabled = $true
                $install.Text = '중단된 업데이트 복구'
                $detail.Text = '이전 버전 복원 후 다시 업데이트 · 설정·페어링 보존'
                if ($backup.root -ieq $packageRoot) { $install.Enabled = $false }
            }
        } catch { }
    }
}
$location.Add_TextChanged({ Refresh-Quest3DInstallChoice })
if (Test-Path -LiteralPath (Join-Path $packageRoot 'quest3d-install.json') -PathType Leaf) { $location.Text = $packageRoot }
Refresh-Quest3DInstallChoice
$browse.Add_Click({
    $dialog = New-Object Windows.Forms.FolderBrowserDialog
    $dialog.Description = '비어 있는 Quest3D 전용 설치 폴더를 선택하세요.'
    $dialog.SelectedPath = $location.Text
    if ($dialog.ShowDialog($form) -eq [Windows.Forms.DialogResult]::OK) { $location.Text = $dialog.SelectedPath }
    $dialog.Dispose()
})
function Set-Quest3DInstallerBusy([bool]$Busy) {
    $script:busy = $Busy
    $close.Enabled = !$Busy
    $logs.Enabled = $null -ne $script:logFolder
    if ($Busy) {
        foreach ($control in @($install,$browse,$location,$shortcuts,$update,$rollback,$remove,$launch,$network)) { $control.Enabled = $false }
    } else {
        $browse.Enabled = $true; $location.Enabled = $true; $shortcuts.Enabled = $true
        $savedDetail = $detail.Text
        Refresh-Quest3DInstallChoice
        $detail.Text = $savedDetail
    }
}
function New-Quest3DInstallerLog {
    $path = Join-Path ([IO.Path]::GetTempPath()) ('Quest3D-Install-' + [Guid]::NewGuid().ToString('N'))
    Assert-Quest3DNoReparse $path
    [void](New-Item -ItemType Directory -Path $path)
    return $path
}
function Start-Quest3DMaintenanceWorker([string]$Action, [string]$Root, [string]$NetworkOperationId, [string]$NetworkReceiptAction='remove') {
    $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot ($Action + '.ps1')) + '" -Root "' + $Root + '" -ReportPath "' + (Join-Path $script:logFolder 'result.json') + '"'
    if ($NetworkOperationId) { $arguments += ' -NetworkOperationId "' + $NetworkOperationId + '" -NetworkReceiptAction "' + $NetworkReceiptAction + '"' }
    $shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
    # This worker stays in the original user's context, including their shortcuts.
    $script:installProcess = Start-Process -FilePath $shell -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:logFolder 'output.txt') -RedirectStandardError (Join-Path $script:logFolder 'error.txt')
    $script:operation = $Action
    $script:maintenanceRoot = $Root
    $progress.Style = [Windows.Forms.ProgressBarStyle]::Marquee
    $status.Text = if ($Action -eq 'rollback') { '이전 버전 복원 중' } else { '앱 방화벽 상태 확인 · 앱 제거·복구 보관 중' }
    Set-Quest3DInstallerBusy $true
    $timer.Start()
}
function Start-Quest3DInstallerNetwork([string]$Action, [string]$Root, [bool]$RetireAfter) {
    if ($script:busy) { return }
    try {
        $script:logFolder = New-Quest3DInstallerLog
        if ($RetireAfter) { Assert-Quest3DInstallStopped $Root }
        $script:networkRequest = New-Quest3DNetworkRequest $Root 'status'
        $script:requestedNetworkAction = $Action
        $script:pendingMaintenance = if ($RetireAfter) { $Root } else { $null }
        $script:operation = 'network-status'
        $progress.Style = [Windows.Forms.ProgressBarStyle]::Marquee
        $status.Text = '앱 방화벽 상태 확인 중'
        $detail.Text = '이미 허용되었거나 제거할 규칙이 없으면 관리자 확인을 생략합니다.'
        Set-Quest3DInstallerBusy $true
        $script:installProcess = Start-Quest3DNetworkRequest $script:networkRequest
        $timer.Start()
    } catch {
        $cancelled = Test-Quest3DUacCancelled $_
        $status.Text = if ($cancelled) { 'Windows 관리자 승인 취소 · 작업 미실행' } else { '연결 설정을 시작하지 못했습니다' }
        $detail.Text = if ($cancelled) { '앱과 기존 설정 보존 · 필요할 때 다시 시도' } else { $_.Exception.Message }
        if ($script:logFolder) { Write-Quest3DJson (Join-Path $script:logFolder 'start-error.json') @{cancelled=$cancelled;error=$_.Exception.Message} }
        $script:networkRequest = $null; $script:pendingMaintenance = $null
        Set-Quest3DInstallerBusy $false
    }
}
function Confirm-Quest3DNetworkChange([string]$Action) {
    if ($Action -cne 'remove') { throw '앱 제거 확인은 remove 작업에만 사용합니다.' }
    $message = '앱 소유 방화벽만 정리합니다. 취소하면 앱을 보존합니다. 이후 현재 사용자 권한으로 앱과 데이터를 복구용 보관합니다. Windows 관리자 확인이 이어집니다.'
    if ($script:adminStatusVerification) { $message = '현재 사용자 권한으로 앱 규칙을 확인하지 못했습니다. 관리자 권한으로 소유권과 상태를 다시 확인합니다. ' + $message }
    return [Windows.Forms.MessageBox]::Show($form,$message,'Quest3D 연결 설정',[Windows.Forms.MessageBoxButtons]::OKCancel,[Windows.Forms.MessageBoxIcon]::Information) -eq [Windows.Forms.DialogResult]::OK
}
function Start-Quest3DInstallerNetworkChange([string]$Root) {
    try {
        $action = $script:requestedNetworkAction
        # The connection button requests Apply; only Windows needs approval.
        if ($action -ceq 'remove' -and !(Confirm-Quest3DNetworkChange $action)) {
            $status.Text = '연결 설정 취소 · 규칙 변경 미실행'
            $detail.Text = '앱과 기존 설정 보존'
            $script:pendingMaintenance = $null
            Set-Quest3DInstallerBusy $false
            return
        }
        $script:networkRequest = New-Quest3DNetworkRequest $Root $action
        $script:operation = 'network-' + $action
        $progress.Style = [Windows.Forms.ProgressBarStyle]::Marquee
        $status.Text = if ($script:adminStatusVerification) { '앱 규칙 재확인 · Windows 관리자 승인 대기' } else { 'Windows 관리자 승인 대기' }
        $detail.Text = if ($action -ceq 'apply') { '사설망·로컬 서브넷 · 앱 스트리밍만 허용 · 취소 시 변경 미실행' } else { '승인을 취소하면 규칙 변경과 앱 제거를 시작하지 않습니다.' }
        if ($script:adminStatusVerification) { $detail.Text = "현재 사용자 조회 미완료 · 관리자 권한으로 앱 규칙 재확인`r`n" + $detail.Text }
        $status.Refresh()
        $detail.Refresh()
        $script:installProcess = Start-Quest3DNetworkRequest $script:networkRequest
        $status.Text = if ($action -ceq 'apply') { '연결 허용·적용 결과 확인 중' } else { '앱 방화벽 정리·결과 확인 중' }
        $timer.Start()
    } catch {
        $cancelled = Test-Quest3DUacCancelled $_
        $status.Text = if ($cancelled) { 'Windows 관리자 승인 취소 · 규칙 변경 미실행' } else { '연결 설정을 시작하지 못했습니다' }
        $detail.Text = if ($cancelled) { '앱과 기존 설정 보존 · 필요할 때 다시 시도' } else { $_.Exception.Message }
        Write-Quest3DJson (Join-Path $script:logFolder 'start-error.json') @{cancelled=$cancelled;error=$_.Exception.Message}
        $script:networkRequest = $null; $script:pendingMaintenance = $null
        Set-Quest3DInstallerBusy $false
    }
}
$install.Add_Click({
    if ($script:busy) { return }
    try {
        $destination = [IO.Path]::GetFullPath($location.Text)
        if ($destination.IndexOfAny([char[]]"#`"`r`n") -ge 0) { throw '설치 경로에 #, 따옴표, 줄바꿈을 사용할 수 없습니다. 다른 폴더를 선택해 주세요.' }
        $script:logFolder = New-Quest3DInstallerLog
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'install.ps1') + '" -Destination "' + $destination.TrimEnd('\') + '"'
        if (!$shortcuts.Checked) { $arguments += ' -NoShortcuts' }
        if ($update.Checked) { $arguments += ' -Update' }
        $shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        $script:installProcess = Start-Process -FilePath $shell -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:logFolder 'output.txt') -RedirectStandardError (Join-Path $script:logFolder 'error.txt')
        $script:installedRoot = $destination
        $script:operation = 'install'
        $progress.Style = [Windows.Forms.ProgressBarStyle]::Blocks
        Set-Quest3DInstallerBusy $true
        $status.Text = '설치 파일을 확인하고 있습니다...'
        $detail.Text = '설치 중에는 창을 최소화해도 됩니다. 완료될 때까지 기다려 주세요.'
        $timer.Start()
    } catch { [void][Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, '설치를 시작하지 못했습니다') }
})
$timer.Add_Tick({
    if (!$script:installProcess) { return }
    $text = if (Test-Path -LiteralPath (Join-Path $script:logFolder 'output.txt')) { Get-Content -LiteralPath (Join-Path $script:logFolder 'output.txt') -Tail 12 -ErrorAction SilentlyContinue } else { @() }
    $joined = $text -join "`n"
    $matches = [regex]::Matches($joined, '\[([1-6])/6\]')
    if ($matches.Count) {
        $step = [int]$matches[$matches.Count - 1].Groups[1].Value
        $progress.Value = $step - 1
        $steps = @('', '설치 파일 검사', '프로그램 파일 복사', 'Python 준비', 'GPU 라이브러리 다운로드·설치', 'AI 모델 다운로드·검증', '바로가기 생성')
        $status.Text = "$step / 6 · $($steps[$step])"
    }
    $script:installProcess.Refresh()
    if (!$script:installProcess.HasExited) { return }
    $script:installProcess.WaitForExit()
    $timer.Stop()
    $exitCode = $script:installProcess.ExitCode
    $script:installProcess.Dispose()
    $script:installProcess = $null
    if ($script:operation -in @('network-status','network-apply','network-remove')) {
        $request = $script:networkRequest
        $receipt = Read-Quest3DNetworkResult $request $exitCode
        $script:networkRequest = $null
        $progress.Style = [Windows.Forms.ProgressBarStyle]::Blocks
        if ($request.action -ceq 'status' -and $receipt.trusted_unknown) {
            try {
                Write-Quest3DJson (Join-Path $script:logFolder 'status-receipt.json') $receipt.report
                $script:adminStatusVerification = $true
                Start-Quest3DInstallerNetworkChange $request.root
            } catch {
                $status.Text = '관리자 검증을 시작하지 못했습니다 · 앱 보존'
                $detail.Text = $_.Exception.Message
                $script:pendingMaintenance = $null
                Set-Quest3DInstallerBusy $false
            } finally { $script:adminStatusVerification = $false }
            return
        }
        if (!$receipt.accepted) {
            $status.Text = if ($request.action -ceq 'status') { '앱 방화벽 조회 미완료 · 변경 미실행' } else { '연결 설정 미완료 · 앱과 개인 데이터 보존' }
            $detail.Text = $receipt.error
            $script:pendingMaintenance = $null
            Set-Quest3DInstallerBusy $false
            return
        }
        try {
            # Retain this exact receipt before retirement moves its installed path.
            Write-Quest3DJson (Join-Path $script:logFolder ($request.action + '-receipt.json')) $receipt.report
            if ($request.action -ceq 'status') {
                if ($script:requestedNetworkAction -ceq 'apply' -and $receipt.report.apply_satisfied) {
                    $progress.Value = 6
                    $status.Text = '이미 연결 허용 · 앱 규칙 확인'
                    $detail.Text = '관리자 확인 생략 · PC와 Quest에서 연결'
                    Set-Quest3DInstallerBusy $false
                    return
                }
                if ($script:requestedNetworkAction -ceq 'remove' -and $receipt.report.owned_rules_absent) {
                    $root = $script:pendingMaintenance
                    $script:pendingMaintenance = $null
                    Start-Quest3DMaintenanceWorker 'uninstall' $root $request.operation_id 'status'
                    return
                }
                Start-Quest3DInstallerNetworkChange $request.root
                return
            }
            if ($script:pendingMaintenance) {
                $root = $script:pendingMaintenance
                $script:pendingMaintenance = $null
                Start-Quest3DMaintenanceWorker 'uninstall' $root $request.operation_id
                return
            }
            $progress.Value = 6
            $status.Text = '연결 허용 완료 · 실제 규칙 확인'
            $detail.Text = 'PC와 Quest를 같은 사설 네트워크에 연결한 뒤 Scan → Pair → Connect'
        } catch {
            $status.Text = '방화벽 정리 확인 · 앱 제거는 미완료'
            $detail.Text = '앱과 개인 데이터 보존 · ' + $_.Exception.Message
        }
        Set-Quest3DInstallerBusy $false
        return
    }
    if ($script:operation -ne 'install') {
        $progress.Style = [Windows.Forms.ProgressBarStyle]::Blocks
        $resultPath = Join-Path $script:logFolder 'result.json'
        $result = $null; $validResult = $false
        try {
            if (Test-Path -LiteralPath $resultPath -PathType Leaf) {
                $result = Get-Content -LiteralPath $resultPath -Raw -Encoding UTF8 | ConvertFrom-Json
                $validResult = $exitCode -eq 0 -and $result.success -is [bool] -and $result.success -and $result.action -ceq $script:operation -and [StringComparer]::OrdinalIgnoreCase.Equals($result.root,$script:maintenanceRoot)
            }
        } catch { $result = $null }
        if ($validResult) {
            $progress.Value = 6
            $status.Text = if ($script:operation -eq 'rollback') { '이전 버전 복원 완료 · 설정·페어링 보존' } else { '앱 제거 완료 · 데이터와 프로그램은 복구용으로 보관' }
            $detail.Text = if ($script:operation -eq 'rollback') { '앱 실행 후 PC 시작 → Quest Connect' } else { '보관 위치 · ' + $result.archive + ' · 저장공간은 자동 회수하지 않음' }
        } else {
            $status.Text = '완료하지 못했습니다 · 실행 중인 앱과 설치 소유권 확인'
            $detail.Text = if ($result -and $result.PSObject.Properties.Name -contains 'error') { $result.error } else { '도구 오류 또는 결과 확인 실패 · 설치 로그 확인' }
        }
        Set-Quest3DInstallerBusy $false
        return
    }
    if ($exitCode -eq 0) {
        $progress.Value = 6
        $status.Text = '설치 완료. 앱 실행 → PC 시작 → Quest에서 Pair/Connect 순서로 사용하세요.'
        $detail.Text = '처음 연결할 때는 PC 앱에 Quest 화면의 PIN을 입력합니다.'
        Set-Quest3DInstallerBusy $false
    } else {
        $status.Text = '설치를 완료하지 못했습니다. 설치 로그의 마지막 오류를 확인해 주세요.'
        $detail.Text = '기존 파일을 자동 삭제하지 않습니다. 인터넷·디스크·NVIDIA 드라이버를 확인한 뒤 다시 시도할 수 있습니다.'
        Set-Quest3DInstallerBusy $false
    }
})
function Start-Quest3DMaintenance([string]$Action) {
    if ($Action -notin @('rollback','uninstall') -or $script:busy) { return }
    try {
        if ($Action -eq 'rollback') {
            $backup = Get-Quest3DTransaction $location.Text
            if (!$backup -or $backup.journal.phase -eq 'rolled-back') { throw '복원할 이전 버전이 없습니다.' }
            $maintenanceRoot = $backup.root
        } else { $maintenanceRoot = (Get-Quest3DOwnedInstall $location.Text).root }
        Assert-Quest3DInstallStopped $maintenanceRoot
        if ($Action -eq 'uninstall') {
            Start-Quest3DInstallerNetwork 'remove' $maintenanceRoot $true
        } else {
            $script:logFolder = New-Quest3DInstallerLog
            Start-Quest3DMaintenanceWorker 'rollback' $maintenanceRoot ''
        }
    } catch { [void][Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, '설치 관리') }
}
$rollback.Add_Click({ Start-Quest3DMaintenance 'rollback' })
$remove.Add_Click({ Start-Quest3DMaintenance 'uninstall' })
$logs.Add_Click({ if ($script:logFolder) { Start-Process explorer.exe -ArgumentList ('"' + $script:logFolder + '"') } })
$network.Add_Click({
    if ($script:busy) { return }
    Start-Quest3DInstallerNetwork 'apply' $script:installedRoot $false
})
$launch.Add_Click({
    if ($script:busy) { return }
    try {
        Start-Process -FilePath (Join-Path $script:installedRoot '.venv/Scripts/pythonw.exe') -ArgumentList ('-m quest3d.desktop --root "' + $script:installedRoot.TrimEnd('\') + '"') -WorkingDirectory $script:installedRoot -WindowStyle Hidden
    } catch { [void][Windows.Forms.MessageBox]::Show($form, $_.Exception.Message, '앱 실행 오류') }
})
$close.Add_Click({ $form.Close() })
$form.Add_FormClosing({
    param($sender, $eventArgs)
    if ($script:busy -or ($script:installProcess -and !$script:installProcess.HasExited)) {
        $eventArgs.Cancel = $true
        [void][Windows.Forms.MessageBox]::Show($form, '설정 작업이 진행 중입니다. 완료될 때까지 기다려 주세요. 창을 최소화할 수 있습니다.', '작업 진행 중')
    }
})
if ($SelfTest) {
    if (!$form.Controls.Contains($install) -or !$form.Controls.Contains($status) -or !$form.Controls.Contains($update) -or !$form.Controls.Contains($rollback) -or !$form.Controls.Contains($remove)) { throw 'Installer UI state invalid.' }
    if (!$networkTip.GetToolTip($network).Contains('Private') -or !$networkTip.GetToolTip($network).Contains('LocalSubnet')) { throw 'Network scope tooltip missing.' }
    Write-Output 'Installer UI construction passed; no installation started.'
    Write-Output ('Installer UI state: rollback=' + $rollback.Enabled + '; launch=' + $launch.Enabled + '; remove=' + $remove.Enabled)
    if ($PreviewImage) {
        $form.ShowInTaskbar = $false
        $form.StartPosition = 'Manual'
        $form.Location = New-Object Drawing.Point(-32000, -32000)
        $form.Show()
        [Windows.Forms.Application]::DoEvents()
        $bitmap = New-Object Drawing.Bitmap($form.Width, $form.Height)
        $form.DrawToBitmap($bitmap, (New-Object Drawing.Rectangle(0, 0, $form.Width, $form.Height)))
        $bitmap.Save([IO.Path]::GetFullPath($PreviewImage), [Drawing.Imaging.ImageFormat]::Png)
        $bitmap.Dispose()
    }
} else { [void]$form.ShowDialog() }
$timer.Dispose()
$networkTip.Dispose()
$form.Dispose()

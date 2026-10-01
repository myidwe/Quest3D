[CmdletBinding()]
param([switch]$SelfTest)
$ErrorActionPreference = 'Stop'
if ($PSVersionTable.PSVersion.Major -eq 5) {
    foreach ($module in @('Utility', 'Management')) {
        Import-Module (Join-Path $PSHOME ('Modules/Microsoft.PowerShell.' + $module + '/Microsoft.PowerShell.' + $module + '.psd1')) -ErrorAction Stop
    }
}
Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[Windows.Forms.Application]::EnableVisualStyles()
$form = New-Object Windows.Forms.Form
$form.Text = 'Sterevi · Quest 앱 설치'
$form.Size = New-Object Drawing.Size(680, 530)
$form.MinimumSize = $form.Size
$form.StartPosition = 'CenterScreen'
$form.Font = New-Object Drawing.Font('Malgun Gothic', 10)
$intro = New-Object Windows.Forms.Label
$intro.Text = "1. Quest 개발자 모드 · USB 데이터 케이블 연결`r`n2. 헤드셋에서 USB 디버깅 허용`r`n3. Google Platform Tools의 adb.exe 선택 → 기기 검색"
$intro.SetBounds(22, 22, 620, 78)
$link = New-Object Windows.Forms.LinkLabel
$link.Text = 'Google 공식 Platform Tools 다운로드 안내'
$link.SetBounds(22, 108, 620, 26)
$path = New-Object Windows.Forms.TextBox
$path.SetBounds(22, 148, 482, 28)
$packageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$known = Get-Command adb.exe -ErrorAction SilentlyContinue
$bundled = Join-Path $packageRoot 'platform-tools/adb.exe'
if (Test-Path -LiteralPath $bundled -PathType Leaf) { $path.Text = $bundled }
elseif ($known) { $path.Text = $known.Source }
$browse = New-Object Windows.Forms.Button
$browse.Text = 'adb.exe 선택'
$browse.SetBounds(514, 145, 128, 34)
$device = New-Object Windows.Forms.ComboBox
$device.DropDownStyle = 'DropDownList'
$device.DisplayMember = 'label'
$device.SetBounds(22, 198, 620, 28)
$status = New-Object Windows.Forms.TextBox
$status.Multiline = $true
$status.ReadOnly = $true
$status.ScrollBars = 'Vertical'
$status.Text = '기기 검색 후 설치할 Quest 선택. 기존 앱 데이터 보존'
$status.SetBounds(22, 247, 620, 132)
$check = New-Object Windows.Forms.Button
$check.Text = '기기 검색'
$check.SetBounds(22, 420, 148, 38)
$install = New-Object Windows.Forms.Button
$install.Text = 'Quest에 설치'
$install.SetBounds(183, 420, 148, 38)
$install.Enabled = $false
$close = New-Object Windows.Forms.Button
$close.Text = '닫기'
$close.SetBounds(494, 420, 148, 38)
$form.Controls.AddRange(@($intro, $link, $path, $browse, $device, $status, $check, $install, $close))
$script:worker = $null
$script:logDir = $null
$timer = New-Object Windows.Forms.Timer
$timer.Interval = 500
$link.Add_LinkClicked({ Start-Process 'https://developer.android.com/tools/releases/platform-tools' })
$browse.Add_Click({
    $dialog = New-Object Windows.Forms.OpenFileDialog
    $dialog.Filter = 'Android Debug Bridge (adb.exe)|adb.exe'
    if ($dialog.ShowDialog($form) -eq 'OK') { $path.Text = $dialog.FileName }
    $dialog.Dispose()
})
function Start-QuestInstall([bool]$SearchOnly) {
    try {
        $adbPath = [IO.Path]::GetFullPath($path.Text)
        if ($adbPath.IndexOfAny([char[]]"`"`r`n") -ge 0 -or !(Test-Path -LiteralPath $adbPath -PathType Leaf)) { throw '올바른 adb.exe를 선택해 주세요.' }
        $script:logDir = Join-Path ([IO.Path]::GetTempPath()) ('Quest3D-QuestInstall-' + [Guid]::NewGuid().ToString('N'))
        [void](New-Item -ItemType Directory -Path $script:logDir)
        $arguments = '-NoProfile -ExecutionPolicy Bypass -File "' + (Join-Path $PSScriptRoot 'install-quest.ps1') + '" -Adb "' + $adbPath + '"'
        if ($SearchOnly) { $arguments += ' -ListDevices' }
        else {
            if (!$device.SelectedItem -or !$device.SelectedItem.quest -or $device.SelectedItem.state -ne 'device') { throw '검색된 목록에서 연결·승인된 Quest를 선택해 주세요.' }
            $arguments += ' -Serial "' + $device.SelectedItem.serial + '"'
        }
        $script:searching = $SearchOnly
        if ($SearchOnly) { $device.Items.Clear(); $install.Enabled = $false }
        $shell = Join-Path $env:SystemRoot 'System32/WindowsPowerShell/v1.0/powershell.exe'
        $script:worker = Start-Process -FilePath $shell -ArgumentList $arguments -WindowStyle Hidden -PassThru -RedirectStandardOutput (Join-Path $script:logDir 'output.txt') -RedirectStandardError (Join-Path $script:logDir 'error.txt')
        $status.Text = if ($SearchOnly) { '기기와 APK 확인 중' } else { '선택한 Quest에 설치 중 · 기존 데이터 보존' }
        $install.Enabled = $false; $check.Enabled = $false; $browse.Enabled = $false; $path.Enabled = $false; $device.Enabled = $false
        $timer.Start()
    } catch { $status.Text = $_.Exception.Message }
}
$check.Add_Click({ Start-QuestInstall $true })
$install.Add_Click({ Start-QuestInstall $false })
$timer.Add_Tick({
    $script:worker.Refresh()
    if (!$script:worker.HasExited) { return }
    $script:worker.WaitForExit()
    $code = $script:worker.ExitCode
    $script:worker.Dispose(); $script:worker = $null
    $timer.Stop()
    $output = Get-Content -LiteralPath (Join-Path $script:logDir 'output.txt') -Raw -ErrorAction SilentlyContinue
    $errors = Get-Content -LiteralPath (Join-Path $script:logDir 'error.txt') -Raw -ErrorAction SilentlyContinue
    if ($code -eq 0 -and $script:searching) {
        try {
            $result = $output | ConvertFrom-Json
            $device.Items.Clear()
            foreach ($entry in $result.devices) {
                $label = if ($entry.state -eq 'unauthorized') { 'USB 디버깅 승인 대기' } elseif ($entry.state -eq 'offline') { '오프라인' } elseif ($entry.quest) { $entry.model } else { 'Quest 이외 기기' }
                $entry | Add-Member -MemberType NoteProperty -Name label -Value ($label + ' · …' + $entry.serial.Substring([Math]::Max(0, $entry.serial.Length - 4)))
                [void]$device.Items.Add($entry)
            }
            $ready = @($result.devices | Where-Object { $_.quest -and $_.state -eq 'device' })
            if ($ready.Count -eq 1) { $device.SelectedItem = $ready[0] }
            $status.Text = if ($ready.Count -gt 0) { '검색 완료 · 설치할 Quest를 선택한 뒤 Quest에 설치' } elseif (@($result.devices | Where-Object { $_.state -eq 'unauthorized' }).Count) { 'USB 디버깅 승인 대기 · 헤드셋에서 허용 후 다시 검색' } elseif (@($result.devices | Where-Object { $_.state -eq 'offline' }).Count) { '기기 오프라인 · USB 재연결 후 다시 검색' } else { 'Quest 없음 · 개발자 모드와 USB 데이터 케이블 확인' }
        } catch { $status.Text = '기기 목록 확인 실패 · USB 연결 확인 후 다시 검색'; $device.Items.Clear() }
    } else {
        if ($script:searching) { $device.Items.Clear() }
        $status.Text = if ($code -eq 0) { $output } else { "완료하지 못했습니다.`r`n$output`r`n$errors" }
    }
    $check.Enabled = $true; $browse.Enabled = $true; $path.Enabled = $true; $device.Enabled = $true
    $install.Enabled = ($device.SelectedItem -and $device.SelectedItem.quest -and $device.SelectedItem.state -eq 'device')
})
$device.Add_SelectedIndexChanged({ if (!$script:worker) { $install.Enabled = ($device.SelectedItem -and $device.SelectedItem.quest -and $device.SelectedItem.state -eq 'device') } })
$path.Add_TextChanged({ if (!$script:worker) { $device.Items.Clear(); $install.Enabled = $false } })
$close.Add_Click({ $form.Close() })
$form.Add_FormClosing({ param($sender,$eventArgs)
    if ($script:worker -and !$script:worker.HasExited) { $eventArgs.Cancel = $true; $status.Text = '설치/확인이 진행 중입니다. 완료될 때까지 기다려 주세요.' }
})
if ($SelfTest) { Write-Output 'Quest installer UI constructed. No ADB command or installation performed.' }
else { [void]$form.ShowDialog() }
$timer.Dispose(); $form.Dispose()

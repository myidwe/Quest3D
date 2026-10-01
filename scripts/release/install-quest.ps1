<# Installs only the APK named and hashed in quest-install.json. Never deletes app data. #>
[CmdletBinding()]
param([string]$Adb, [string]$Serial, [switch]$CheckOnly, [switch]$ListDevices)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest
if ($PSVersionTable.PSVersion.Major -eq 5) {
    foreach ($module in @('Utility', 'Management')) {
        Import-Module (Join-Path $PSHOME ('Modules/Microsoft.PowerShell.' + $module + '/Microsoft.PowerShell.' + $module + '.psd1')) -ErrorAction Stop
    }
}
$packageRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$manifest = Get-Content -LiteralPath (Join-Path $packageRoot 'quest-install.json') -Raw | ConvertFrom-Json
if ($manifest.schema -ne 1 -or $manifest.apk -cne 'Quest3D-Quest.apk') { throw 'Unsupported Quest installer manifest.' }
if ($manifest.package -cnotin @('app.questto3d.client.debug', 'app.questto3d.client')) { throw 'Unsupported Quest package.' }
if ($manifest.sha256 -cnotmatch '^[a-f0-9]{64}$' -or $manifest.preserve_data -ne $true) { throw 'Invalid APK integrity or update policy.' }
$apk = Join-Path $packageRoot $manifest.apk
if ((Get-FileHash -LiteralPath $apk).Hash -ine $manifest.sha256) { throw 'APK checksum mismatch.' }
if (!$Adb) {
    $bundled = Join-Path $packageRoot 'platform-tools/adb.exe'
    if (Test-Path -LiteralPath $bundled) { $Adb = $bundled }
    else {
        $command = Get-Command adb.exe -ErrorAction SilentlyContinue
        if ($command) { $Adb = $command.Source }
    }
}
if (!$Adb -or !(Test-Path -LiteralPath $Adb -PathType Leaf)) {
    throw 'Google Platform Tools 압축을 해제한 뒤 adb.exe를 선택해 주세요. 설치 안내에 공식 다운로드 링크가 있습니다.'
}
$rows = @(& $Adb devices)
if ($LASTEXITCODE -ne 0) { throw 'ADB 기기 검색 실패. USB 케이블과 Platform Tools를 확인해 주세요.' }
$devices = @($rows | Where-Object { $_ -match '^\S+\s+(device|unauthorized|offline)\b' } | ForEach-Object {
    $columns = $_ -split '\s+'
    [pscustomobject]@{ serial = $columns[0]; state = $columns[1] }
})
if ($ListDevices) {
    $results = @($devices | ForEach-Object {
        $deviceModel = ''
        if ($_.state -eq 'device') {
            $deviceModel = (& $Adb -s $_.serial shell getprop ro.product.model) -join ''
            if ($LASTEXITCODE -ne 0) { $deviceModel = '' }
        }
        [pscustomobject]@{ serial = $_.serial; state = $_.state; model = $deviceModel.Trim(); quest = ($deviceModel -match 'Quest') }
    })
    [pscustomobject]@{ devices = $results } | ConvertTo-Json -Depth 4 -Compress
    return
}
$connected = @($devices | Where-Object { $_.state -eq 'device' } | ForEach-Object { $_.serial })
if (!$Serial) {
    if (!$connected.Count -and @($devices | Where-Object { $_.state -eq 'unauthorized' }).Count) { throw 'USB 디버깅 승인 대기. Quest를 착용해 허용을 누른 뒤 다시 검색해 주세요.' }
    if (!$connected.Count -and @($devices | Where-Object { $_.state -eq 'offline' }).Count) { throw '기기가 오프라인 상태입니다. USB를 다시 연결한 뒤 검색해 주세요.' }
    if ($connected.Count -eq 0) { throw '연결된 기기 없음. 개발자 모드와 USB 데이터 케이블을 확인해 주세요.' }
    if ($connected.Count -gt 1) { throw '여러 기기가 연결되어 있습니다. 설치할 Quest를 선택해 주세요.' }
    $Serial = $connected[0]
}
if ($Serial -notmatch '^[A-Za-z0-9._:-]+$') { throw 'Invalid ADB device identifier.' }
if ($Serial -notin $connected) { throw '선택한 기기가 연결·승인되지 않았습니다. Quest에서 USB 디버깅 허용 후 다시 검색해 주세요.' }
$model = & $Adb -s $Serial shell getprop ro.product.model
if ($LASTEXITCODE -ne 0 -or ($model -join '') -notmatch 'Quest') { throw '선택한 기기가 Quest로 확인되지 않았습니다. 헤드셋을 다시 선택해 주세요.' }
if ($manifest.PSObject.Properties.Name -contains 'version_code') {
    if ($manifest.version_code -isnot [int] -or $manifest.version_code -lt 1) { throw 'Invalid APK version code.' }
    $packageInfo = (& $Adb -s $Serial shell dumpsys package $manifest.package) -join "`n"
    if ($LASTEXITCODE -ne 0) { throw '설치된 앱 버전 확인 실패. USB 연결을 확인해 주세요.' }
    $versionMatch = [regex]::Match($packageInfo, '\bversionCode=(\d+)\b')
    if ($versionMatch.Success -and [long]$versionMatch.Groups[1].Value -gt $manifest.version_code) { throw '더 최신 앱이 설치되어 있습니다. 이전 버전으로 덮어쓰지 않습니다.' }
}
Write-Output "$($model -join '') · APK 확인 완료"
if ($CheckOnly) { return }
& $Adb -s $Serial install -r --no-streaming $apk
if ($LASTEXITCODE -ne 0) { throw '설치 실패. 기존 앱과 데이터는 보존했습니다. 서명 불일치라면 해당 앱과 같은 서명의 배포판이 필요합니다. 자동으로 앱을 삭제하지 않습니다.' }
Write-Output '설치 완료 · Quest 앱 → 알 수 없는 출처 → Quest3D. PC 시작 후 Quest에서 Scan Network → Pair → Connect. 업데이트 후에는 기존 PC의 Connect를 누릅니다.'

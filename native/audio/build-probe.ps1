$ErrorActionPreference = 'Stop'
$audioRoot = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$audioVs = & 'C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe' -latest -products '*' -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 -property installationPath
if (-not $audioVs) { throw 'Visual Studio C++ build tools are required' }
Import-Module (Join-Path $audioVs 'Common7\Tools\Microsoft.VisualStudio.DevShell.dll')
Enter-VsDevShell -VsInstallPath $audioVs -SkipAutomaticLocation -DevCmdArguments '-arch=x64 -host_arch=x64'
$audioDist = Join-Path $PSScriptRoot 'dist'
New-Item -ItemType Directory -Path $audioDist -Force | Out-Null
Push-Location $audioDist
try {
    cl /nologo /std:c++20 /EHsc /W4 /DUNICODE /D_UNICODE /utf-8 (Join-Path $PSScriptRoot 'audio_probe.cpp') /Fe:audio_probe.exe /link ole32.lib uuid.lib propsys.lib
    if ($LASTEXITCODE -ne 0) { throw 'Read-only audio probe build failed' }
} finally { Pop-Location }

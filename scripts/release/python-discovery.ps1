<# Read-only discovery. Never relocates a registered Windows Python install. #>
function Test-Quest3DPythonRuntime([string]$Candidate) {
    if (!$Candidate -or !(Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }
    if ($Candidate -match '(?i)[\\/]Microsoft[\\/]WindowsApps[\\/]') { return $false }
    & $Candidate -I -c "import sys,tkinter,struct; assert sys.version_info[:3] == (3,12,6); assert struct.calcsize('P') == 8; assert tkinter.TkVersion >= 8.6" 2>$null
    return $LASTEXITCODE -eq 0
}

function Select-Quest3DPythonRuntime([object[]]$Candidates, [bool]$RegisteredPython312) {
    foreach ($candidate in $Candidates) {
        if (Test-Quest3DPythonRuntime ([string]$candidate)) {
            return @{path=[IO.Path]::GetFullPath([string]$candidate); install_allowed=$false; reused=$true}
        }
    }
    # Running the same per-user MSI with a new TargetDir may relocate an
    # existing Python 3.12 and break unrelated virtual environments.
    if ($RegisteredPython312) {
        throw 'Existing Python 3.12 registration was preserved. Repair it or select a compatible Python 3.12.6 with -Python; automatic relocation is blocked.'
    }
    return @{path=$null; install_allowed=$true; reused=$false}
}

function Get-Quest3DExistingPythonRuntime {
    $candidates = [Collections.Generic.List[string]]::new()
    foreach ($command in @(Get-Command python.exe -CommandType Application -All -ErrorAction SilentlyContinue)) {
        if ($command.Path) { $candidates.Add($command.Path) }
    }
    $registered = $false
    foreach ($root in @('HKCU:\Software\Python\PythonCore', 'HKLM:\Software\Python\PythonCore',
                         'HKCU:\Software\WOW6432Node\Python\PythonCore', 'HKLM:\Software\WOW6432Node\Python\PythonCore')) {
        foreach ($version in @(Get-ChildItem -LiteralPath $root -ErrorAction SilentlyContinue |
                               Where-Object PSChildName -Like '3.12*')) {
            $registered = $true
            $key = Get-Item -LiteralPath (Join-Path $version.PSPath 'InstallPath') -ErrorAction SilentlyContinue
            if (!$key) { continue }
            $exe = $key.GetValue('ExecutablePath')
            $directory = $key.GetValue('')
            if ($exe) { $candidates.Add([string]$exe) }
            elseif ($directory) { $candidates.Add((Join-Path ([string]$directory) 'python.exe')) }
        }
    }
    return Select-Quest3DPythonRuntime @($candidates | Select-Object -Unique) $registered
}

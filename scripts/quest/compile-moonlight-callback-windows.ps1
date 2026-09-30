$ErrorActionPreference = 'Stop'
$project = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '../..'))
$compilerDirectory = Join-Path $project 'native/host/tools/msys64/ucrt64/bin'
$originalPath = $env:PATH
try {
    $env:PATH = "$compilerDirectory;$originalPath"
    $source = Join-Path $project 'artifacts/quest/moonlight-callback-drain/source'
    $output = Join-Path $project 'artifacts/quest/moonlight-callback-drain/Connection.windows.o'
    & (Join-Path $compilerDirectory 'gcc.exe') -std=c11 -O2 -Wall -Wextra -Werror -Wno-unused-parameter -DHAS_SOCKLEN_T -DLC_DEBUG "-I$source/src" "-I$source/enet/include" -c "$source/src/Connection.c" -o $output
    if ($LASTEXITCODE -ne 0) { throw "Moonlight Windows object compilation failed: $LASTEXITCODE" }
    Get-FileHash -LiteralPath $output -Algorithm SHA256
} finally {
    $env:PATH = $originalPath
}

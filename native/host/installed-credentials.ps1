# Recovery is allowed only while the owning setup journal is incomplete. This file
# never reads or changes an already configured host's launch/pairing state.
function Test-Quest3DInstalledCredentialFile([string]$Path, [PSCredential]$Account) {
    if (!(Test-Path -LiteralPath $Path -PathType Leaf)) { return $false }
    $TaskInfo = Get-Item -LiteralPath $Path -Force
    if (($TaskInfo.Attributes -band [IO.FileAttributes]::ReparsePoint) -or $TaskInfo.Length -gt 65536) { return $false }
    $TaskPlain = $null
    $TaskBytes = $null
    try {
        $TaskValue = [IO.File]::ReadAllText($Path) | ConvertFrom-Json -AsHashtable -ErrorAction Stop
        if ($TaskValue -isnot [System.Collections.IDictionary] -or $TaskValue.username -isnot [string] -or
            $TaskValue.password -isnot [string] -or $TaskValue.salt -isnot [string]) { return $false }
        if (![StringComparer]::OrdinalIgnoreCase.Equals($TaskValue.username, $Account.UserName) -or
            $TaskValue.salt -cnotmatch '^[A-Za-z0-9!%&()=\-]{16}$' -or $TaskValue.password -cnotmatch '^[0-9A-F]{64}$') { return $false }
        $TaskPlain = $Account.GetNetworkCredential().Password
        if ([string]::IsNullOrEmpty($TaskPlain)) { return $false }
        $TaskBytes = [Text.Encoding]::UTF8.GetBytes($TaskPlain + $TaskValue.salt)
        $TaskHash = [Security.Cryptography.SHA256]::HashData($TaskBytes)
        # Sunshine httpcommon.cpp uses util::hex(crypto::hash(password + salt)).
        # utility.h's default hex formatter reverses the digest bytes and uses A-F.
        [Array]::Reverse($TaskHash)
        return [StringComparer]::Ordinal.Equals([Convert]::ToHexString($TaskHash), $TaskValue.password)
    } catch { return $false }
    finally {
        if ($TaskBytes) { [Array]::Clear($TaskBytes, 0, $TaskBytes.Length) }
        $TaskPlain = $null
    }
}

function Initialize-Quest3DInstalledCredentials {
    param(
        [Parameter(Mandatory)][string]$AccessPath,
        [Parameter(Mandatory)][string]$CredentialsPath,
        [Parameter(Mandatory)][string]$Executable,
        [Parameter(Mandatory)][string]$WorkingDirectory,
        [Parameter(Mandatory)][string]$ConfigPath,
        [Parameter(Mandatory)][string]$LogPath,
        [switch]$OwnedSetup
    )
    if (!$OwnedSetup) { throw 'Credential recovery requires an owned incomplete setup journal.' }
    foreach ($TaskPath in @($AccessPath, $CredentialsPath)) {
        $TaskAncestor = [IO.Path]::GetFullPath($TaskPath)
        while ($TaskAncestor) {
            if ((Test-Path -LiteralPath $TaskAncestor) -and ((Get-Item -LiteralPath $TaskAncestor -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Credential recovery cannot follow a reparse point. Existing data was preserved.'
            }
            $TaskAncestor = Split-Path -Parent $TaskAncestor
        }
        if (Test-Path -LiteralPath $TaskPath) {
            $TaskInfo = Get-Item -LiteralPath $TaskPath -Force
            if ($TaskInfo.PSIsContainer -or ($TaskInfo.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
                throw 'Credential recovery cannot use a directory or reparse point. Existing data was preserved.'
            }
        }
    }
    $TaskAccount = $null
    $TaskPassword = $null
    $TaskBackup = $null
    try {
        if (Test-Path -LiteralPath $AccessPath) {
            try { $TaskAccount = Import-Clixml -LiteralPath $AccessPath -ErrorAction Stop }
            catch { throw 'The saved Windows account cannot be decrypted by this user. Existing credentials were preserved.' }
            if ($TaskAccount -isnot [PSCredential] -or $TaskAccount.UserName -cne 'quest3d' -or
                $TaskAccount.GetNetworkCredential().Password -cnotmatch '^[0-9A-F]{64}$') {
                throw 'The saved setup account is invalid. Existing credentials were preserved.'
            }
        } else {
            $TaskPassword = [Convert]::ToHexString([Security.Cryptography.RandomNumberGenerator]::GetBytes(32))
            $TaskAccount = [PSCredential]::new('quest3d', (ConvertTo-SecureString $TaskPassword -AsPlainText -Force))
            $TaskTemp = $AccessPath + '.setup-' + [guid]::NewGuid().ToString('N')
            try {
                $TaskAccount | Export-Clixml -LiteralPath $TaskTemp
                [IO.File]::Move($TaskTemp, $AccessPath, $false)
            } finally { if (Test-Path -LiteralPath $TaskTemp) { Remove-Item -LiteralPath $TaskTemp } }
        }
        if (Test-Quest3DInstalledCredentialFile $CredentialsPath $TaskAccount) {
            return [pscustomobject]@{ regenerated = $false; backup = $null; verified = $true }
        }
        if (Test-Path -LiteralPath $CredentialsPath) {
            # Sunshine refuses --creds when existing JSON is malformed. Preserve the
            # failed file, then use the SAME saved DPAPI account to regenerate it.
            $TaskBackup = $CredentialsPath + '.failed-' + [guid]::NewGuid().ToString('N')
            [IO.File]::Move($CredentialsPath, $TaskBackup, $false)
        }
        $TaskPassword = $TaskAccount.GetNetworkCredential().Password
        Invoke-Quest3DCommand $Executable $WorkingDirectory @($ConfigPath, '--creds', $TaskAccount.UserName, $TaskPassword) $LogPath
        if (!(Test-Quest3DInstalledCredentialFile $CredentialsPath $TaskAccount)) {
            throw 'Host credential creation did not produce a matching account. The saved Windows account and failed-file backup were preserved.'
        }
        return [pscustomobject]@{ regenerated = $true; backup = $TaskBackup; verified = $true }
    } finally {
        $TaskPassword = $null
        if ($TaskAccount -is [PSCredential]) { $TaskAccount.Password.Dispose() }
    }
}

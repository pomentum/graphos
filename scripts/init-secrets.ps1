# init-secrets.ps1 — create graphos local secrets on first run (Windows).
#
# PowerShell equivalent of init-secrets.sh. Writes into
# services/graphos/deploy/secrets/ (gitignored):
#   graphos_pw.txt       random password for the database superuser (init only)
#   graphos_app_pw.txt   random password for graphos_app, the role graphos-api uses
#   master_key.txt       the master key graphos-api seals its signing keys and ID
#                        tokens with (GRAPHOS_MASTER_KEY_FILE); keep a copy safe
#
# Idempotent: never overwrites an existing file.

$ErrorActionPreference = 'Stop'

$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$secretsDir = Resolve-Path (Join-Path $here '..\services\graphos\deploy\secrets')

function New-Secret {
    # 32 random alphanumerics.
    $bytes = New-Object byte[] 48
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $b64 = [Convert]::ToBase64String($bytes)
    return ($b64 -replace '[^A-Za-z0-9]', '').Substring(0, 32)
}

function Ensure-Password([string]$name) {
    $target = Join-Path $secretsDir $name
    if ((Test-Path $target) -and ((Get-Item $target).Length -gt 0)) {
        Write-Host "ok: $name already present"
        return
    }
    # No trailing newline: postgres reads the file as-is.
    [System.IO.File]::WriteAllText($target, (New-Secret))
    Write-Host "created: $name"
}

Ensure-Password 'graphos_pw.txt'
Ensure-Password 'graphos_app_pw.txt'

$masterKey = Join-Path $secretsDir 'master_key.txt'
if ((Test-Path $masterKey) -and ((Get-Item $masterKey).Length -gt 0)) {
    Write-Host 'ok: master_key.txt already present'
} else {
    $bytes = New-Object byte[] 32
    [System.Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    # One key per line, "<version>:<base64 of 32 bytes>" (graphos.vault).
    [System.IO.File]::WriteAllText($masterKey, "1:$([Convert]::ToBase64String($bytes))`n")
    Write-Host 'created: master_key.txt (back it up: graphos cannot read what it sealed without it)'
}
# graphos's own first admin is user "graphos", password "graphos", to be
# changed at the first sign-in (or set GRAPHOS_ADMIN_PASSWORD_FILE).

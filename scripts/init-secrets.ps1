# init-secrets.ps1 — create graphos local secrets on first run (Windows).
#
# PowerShell equivalent of init-secrets.sh. Writes into
# services/graphos/deploy/secrets/ (gitignored):
#   graphos_pw.txt       random password for the database superuser (init only)
#   graphos_app_pw.txt   random password for graphos_app, the role graphos-api uses
#   graphos_tokens.json  one random admin token (add consumer tokens here)
#   admin_token.txt      the same admin token
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

function Ensure-Tokens {
    $target = Join-Path $secretsDir 'graphos_tokens.json'
    if ((Test-Path $target) -and ((Get-Item $target).Length -gt 0)) {
        Write-Host "ok: graphos_tokens.json already present"
        return
    }
    $token = "tok_" + (New-Secret)
    $json = "{`n  `"$token`": {`"consumer`": `"admin`", `"tenants`": [], `"admin`": true}`n}`n"
    [System.IO.File]::WriteAllText($target, $json)
    [System.IO.File]::WriteAllText((Join-Path $secretsDir 'admin_token.txt'), $token)
    Write-Host "created: graphos_tokens.json with one admin token (also in admin_token.txt)"
}

Ensure-Password 'graphos_pw.txt'
Ensure-Password 'graphos_app_pw.txt'
Ensure-Tokens

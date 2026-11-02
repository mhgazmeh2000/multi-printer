$content = @'
# make-env.ps1
# Creates a new .env in the current folder with freshly generated secrets.
# Secrets are generated on this machine and are never printed or sent anywhere.
#
# Modes (run once per machine):
#   -Mode lan    : direct access over HTTP. Port 5053, TRUST_PROXY=0.
#                  CAPTCHA stays ON for the admin-panel choice, but direct connections
#                  from the LAN networks in -BypassNetworks skip it.
#   -Mode domain : behind nginx. Port 5050, TRUST_PROXY=1. CAPTCHA follows the admin panel.
#
# Usage (run inside the multi-printer folder):
#   powershell -ExecutionPolicy Bypass -File .\make-env.ps1 -Mode lan
#   powershell -ExecutionPolicy Bypass -File .\make-env.ps1 -Mode domain

param(
    [ValidateSet("domain", "lan")]
    [string]$Mode = "domain",
    [string]$Python = "python",
    [string]$BypassNetworks = "172.16.0.0/24,172.16.8.0/24,172.16.10.0/24,172.16.15.0/24,172.16.25.0/24,172.16.27.0/24,172.16.31.0/24,172.16.32.0/24,172.16.50.0/24,172.16.61.0/24",
    [string]$TrustedProxies = "172.16.25.50"
)

$ErrorActionPreference = "Stop"

$secretKey = (& $Python -c 'import secrets; print(secrets.token_hex(32))').Trim()
$altchaKey = (& $Python -c 'import secrets; print(secrets.token_urlsafe(48))').Trim()
if ([string]::IsNullOrEmpty($secretKey) -or [string]::IsNullOrEmpty($altchaKey)) {
    throw "Could not generate secrets. Check that Python ($Python) runs."
}

$pgPassword = Read-Host "PostgreSQL password for user postgres"
if ([string]::IsNullOrEmpty($pgPassword)) {
    throw "PostgreSQL password cannot be empty."
}

if ($Mode -eq "lan") {
    $port     = "5053"
    $trust    = "0"
    $bypass   = $BypassNetworks
    $proxies  = $TrustedProxies
} else {
    $port     = "5050"
    $trust    = "1"
    $bypass   = ""
    $proxies  = ""
}

if (Test-Path ".env") {
    Copy-Item ".env" ".env.bak" -Force
    Write-Host "Existing .env was saved as .env.bak. Copy any other keys from it (Google OAuth, SMTP)."
}

$lines = @(
    "ENVIRONMENT=production",
    "FLASK_PORT=$port",
    "TRUST_PROXY=$trust",
    "SECRET_KEY=$secretKey",
    "",
    "DB_BACKEND=postgresql",
    "PG_DSN=postgresql://postgres:$pgPassword@127.0.0.1:5432/printguard",
    "",
    "CAPTCHA_PROVIDER=altcha",
    "CAPTCHA_ENABLED=true",
    "ALTCHA_ENABLED=true",
    "",
    "ALTCHA_HMAC_SECRET=$altchaKey",
    "ALTCHA_COST=5000",
    "ALTCHA_CHALLENGE_TTL_SECONDS=300",
    "",
    "RECAPTCHA_SITE_KEY=",
    "RECAPTCHA_SECRET_KEY=",
    "",
    "CAPTCHA_BYPASS_NETWORKS=$bypass",
    "CAPTCHA_TRUSTED_PROXIES=$proxies"
)

$path = Join-Path (Get-Location) ".env"
[System.IO.File]::WriteAllLines($path, $lines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Created .env (mode: $Mode)."
Write-Host "Next: restart the app from a NEW cmd window, then test the challenge URL."
'@
Set-Content -Path "make-env.ps1" -Value $content -Encoding UTF8
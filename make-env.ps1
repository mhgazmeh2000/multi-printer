# make-env.ps1
# Creates a new .env in the current folder with freshly generated secrets.
# The secrets are generated on this machine and are never printed or sent anywhere.
#
# Usage (run inside the multi-printer folder):
#   powershell -ExecutionPolicy Bypass -File .\make-env.ps1 -Mode domain
#   powershell -ExecutionPolicy Bypass -File .\make-env.ps1 -Mode lan
#
# -Mode domain : behind nginx, CAPTCHA ON  (port 5050, TRUST_PROXY=1)
# -Mode lan    : direct access over HTTP, CAPTCHA OFF (port 5053, TRUST_PROXY=0)

param(
    [ValidateSet("domain", "lan")]
    [string]$Mode = "domain"
)

$ErrorActionPreference = "Stop"

$secretKey  = (python -c 'import secrets; print(secrets.token_hex(32))').Trim()
$altchaKey  = (python -c 'import secrets; print(secrets.token_urlsafe(48))').Trim()
$pgPassword = Read-Host "PostgreSQL password for user postgres"

if ($Mode -eq "domain") {
    $port    = "5050"
    $trust   = "1"
    $captcha = "true"
} else {
    $port    = "5053"
    $trust   = "0"
    $captcha = "false"
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
    "CAPTCHA_ENABLED=$captcha",
    "ALTCHA_ENABLED=true",
    "",
    "ALTCHA_HMAC_SECRET=$altchaKey",
    "ALTCHA_COST=5000",
    "ALTCHA_CHALLENGE_TTL_SECONDS=300",
    "",
    "RECAPTCHA_SITE_KEY=",
    "RECAPTCHA_SECRET_KEY="
)

$path = Join-Path (Get-Location) ".env"
[System.IO.File]::WriteAllLines($path, $lines, (New-Object System.Text.UTF8Encoding($false)))

Write-Host "Created .env (mode: $Mode)."
Write-Host "Next: restart the app from a NEW cmd window, then test the challenge URL."

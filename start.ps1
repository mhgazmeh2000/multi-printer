# =============================================================================
# @file:         start.ps1
# @brief:        PowerShell launcher that starts PrintGuard on a chosen port.
# @details:      Responsibilities: find Python, ask for the port, free it if busy, check the
#                database (starting PostgreSQL from E:\pgsql when it is off), open the browser
#                and run run.py. Same steps and messages as the former start.bat.
# @dependencies: python or py, run.py, tools\auto_migrate.py, PostgreSQL pg_ctl (optional)
# @notes:        Saved as UTF-8 with BOM so Windows PowerShell 5.1 reads Persian text.
#                Started by start.bat (ASCII launcher), which keeps the window open at the end.
# =============================================================================

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $Host.UI.RawUI.WindowTitle = 'PrintGuard - Runner' } catch { }

$ProjectDir = $PSScriptRoot
Push-Location -LiteralPath $ProjectDir
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'
$EnvFile = Join-Path $ProjectDir '.env'
$Rule = '  ' + ('=' * 60)

function Read-EnvValue {
    # Returns the last value of $Key in .env, or an empty string.
    param([string]$Key)
    $value = ''
    if (-not (Test-Path -LiteralPath $EnvFile)) { return $value }
    foreach ($line in (Get-Content -LiteralPath $EnvFile -Encoding UTF8)) {
        if ($line.StartsWith(';')) { continue }
        $eq = $line.IndexOf('=')
        if ($eq -lt 1) { continue }
        if ($line.Substring(0, $eq) -ieq $Key) { $value = $line.Substring($eq + 1).Trim('"') }
    }
    return $value
}

function Test-PortListening {
    # Returns $true when a LISTENING line for the given port exists in netstat output.
    param([int]$Port)
    $hits = netstat -ano 2>$null | Select-String -SimpleMatch ":$Port " | Where-Object { $_.Line -match 'LISTENING' }
    return [bool]$hits
}

function Invoke-Quiet {
    # Runs a command without showing its output and returns the exit code.
    param([string]$Exe, [string[]]$Arguments)
    $code = 1
    try {
        & $Exe @Arguments *> $null
        $code = $LASTEXITCODE
    } catch { }
    return $code
}

Write-Host ''
Write-Host $Rule
Write-Host '   PrintGuard - Multi Printer Monitoring System'
Write-Host $Rule
Write-Host ''

# ---------------------------------------------------------------------------
# [1/4] Python interpreter
# ---------------------------------------------------------------------------

Write-Host ' [1/4] بررسی مفسر پایتون...'
$PyCmd = ''
$venvPython = Join-Path (Join-Path (Join-Path $ProjectDir '.venv') 'Scripts') 'python.exe'
if (Test-Path -LiteralPath $venvPython) {
    $PyCmd = $venvPython
} elseif (Get-Command 'python' -ErrorAction SilentlyContinue) {
    $PyCmd = 'python'
} elseif (Get-Command 'py' -ErrorAction SilentlyContinue) {
    $PyCmd = 'py'
}
if (-not $PyCmd) {
    Write-Host '  [ERROR] مفسر پایتون پیدا نشد!'
    Pop-Location
    exit 1
}
$verWords = ((& $PyCmd --version 2>&1) | Out-String).Trim() -split '\s+'
$pyVersion = if ($verWords.Count -ge 2) { $verWords[1] } else { '' }
Write-Host " [OK] پایتون: $pyVersion"

Get-ChildItem -LiteralPath $ProjectDir -Recurse -Filter '*.pyc' -ErrorAction SilentlyContinue |
    Remove-Item -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# [2/4] Web port
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [2/4] پیکربندی پورت وب‌سرور...'
$defaultPort = '5050'
$fromEnv = Read-EnvValue 'FLASK_PORT'
if ($fromEnv) { $defaultPort = $fromEnv }

$userPort = Read-Host "  Port [$defaultPort]"
if (-not $userPort) { $userPort = $defaultPort }
$env:FLASK_PORT = $userPort
Write-Host " [OK] پورت انتخابی: $userPort"

# Free the port if another process is listening on it.
$busyLines = netstat -aon 2>$null | Select-String -SimpleMatch ":$userPort " | Where-Object { $_.Line -match 'LISTENING' }
$ownerPids = @($busyLines | ForEach-Object { (($_.Line.Trim()) -split '\s+')[-1] } | Sort-Object -Unique)
foreach ($ownerPid in $ownerPids) {
    if ($ownerPid -and $ownerPid -ne '0') {
        Write-Host "        آزادسازی پورت $userPort با بستن PID $ownerPid ..."
        Invoke-Quiet 'taskkill' @('/F', '/PID', $ownerPid) | Out-Null
    }
}
Write-Host " [OK] پورت $userPort آماده است."

# ---------------------------------------------------------------------------
# [3/4] Database
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [3/4] بررسی اتصال پایگاه‌داده...'
$dbBackend = Read-EnvValue 'DB_BACKEND'
if (-not $dbBackend) { $dbBackend = 'sqlite' }
$pgDsn = Read-EnvValue 'PG_DSN'

if ($dbBackend -ieq 'postgresql') {
    Write-Host '        نوع پایگاه‌داده: PostgreSQL'
    if (-not (Test-PortListening 5432)) {
        Write-Host '        پایگاه‌داده خاموش است؛ در حال استارت خودکار از E:\pgsql ...'
        $pgCtl = 'E:\pgsql\binaries\bin\pg_ctl.exe'
        if (Test-Path -LiteralPath $pgCtl) {
            Invoke-Quiet $pgCtl @('-D', 'E:\pgsql\data', 'start') | Out-Null
            Start-Sleep -Seconds 3
        }
    }

    $env:PRINTGUARD_DSN = $pgDsn
    $connectCode = 'import os, psycopg; conn=psycopg.connect(os.environ[''PRINTGUARD_DSN''], connect_timeout=3); conn.close()'
    if ((Invoke-Quiet $PyCmd @('-c', $connectCode)) -ne 0) {
        Write-Host '  [ERROR] اتصال به پایگاه‌داده PostgreSQL برقرار نشد!'
        Pop-Location
        exit 1
    }
    Write-Host ' [OK] اتصال به PostgreSQL پایدار است.'

    $env:ALEMBIC_DB_URL = $pgDsn
    Invoke-Quiet $PyCmd @('-m', 'alembic', 'upgrade', 'head') | Out-Null
    & $PyCmd 'tools\auto_migrate.py' | Out-Host
} else {
    Write-Host '        نوع پایگاه‌داده: SQLite (logs.db)'
}

# ---------------------------------------------------------------------------
# [4/4] Start the web application
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [4/4] راه‌اندازی سامانه مانیتورینگ...'
Write-Host $Rule
Write-Host "  داشبورد وب: http://localhost:$userPort/"
Write-Host '  توقف برنامه: کلیدهای Ctrl + C'
Write-Host $Rule
Write-Host ''

try { $Host.UI.RawUI.WindowTitle = "PrintGuard [Port: $userPort]" } catch { }

# Open the dashboard in the browser after a short delay (runs in a background job).
$dashboardUrl = "http://localhost:$userPort/"
Start-Job -ScriptBlock {
    param($url)
    Start-Sleep -Seconds 2
    Start-Process $url
} -ArgumentList $dashboardUrl | Out-Null

& $PyCmd 'run.py' | Out-Host

Write-Host ''
Write-Host ' سامانه PrintGuard متوقف شد.'
Pop-Location
exit 0

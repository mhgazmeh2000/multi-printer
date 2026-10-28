# =============================================================================
# @file:         setup.ps1
# @brief:        PowerShell implementation of the PrintGuard Windows setup (7 steps).
# @details:      Responsibilities: create the .venv, install dependencies, find PostgreSQL
#                tools, validate .env, create the database, apply Alembic, check logs.db.
#                Same steps, repair attempts and messages as the former setup.bat.
#                Started by setup.bat (ASCII launcher).
# @dependencies: python or py, pip, requirements.txt, PostgreSQL tools, alembic, .env
# @notes:        Saved as UTF-8 with BOM so Windows PowerShell 5.1 reads Persian text.
#                Known behaviours kept from setup.bat are marked with "KEPT:" comments.
# =============================================================================

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }
try { $Host.UI.RawUI.WindowTitle = 'PrintGuard - Self-Healing Setup and Provisioning' } catch { }

$ProjectDir   = $PSScriptRoot
try {
    Push-Location -LiteralPath $ProjectDir -ErrorAction Stop
} catch {
    # Same message and exit code as the old pushd check.
    Write-Host '[خطا] دسترسی به پوشه پروژه امکان‌پذیر نیست.'
    exit 1
}
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

$VenvDir      = Join-Path $ProjectDir '.venv'
$PyCmd        = Join-Path (Join-Path $VenvDir 'Scripts') 'python.exe'
$SetupErr     = Join-Path $ProjectDir 'setup_error.txt'
$TempLog      = Join-Path $ProjectDir '~setup_temp.log'
$LogsDb       = Join-Path $ProjectDir 'logs.db'
$EnvFile      = Join-Path $ProjectDir '.env'
$Requirements = Join-Path $ProjectDir 'requirements.txt'
$AlembicIni   = Join-Path $ProjectDir 'alembic.ini'
$Rule         = '  ' + ('=' * 60)

Remove-Item -LiteralPath $SetupErr, $TempLog -Force -ErrorAction SilentlyContinue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Invoke-Quiet {
    # Runs a command, stores its combined output in $TempLog, and returns the exit code.
    param([string]$Exe, [string[]]$Arguments)
    $code = 1
    try {
        $output = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
    } catch {
        $output = $_.Exception.Message
    }
    ($output | Out-String) | Set-Content -LiteralPath $TempLog -Encoding UTF8
    return $code
}

function Invoke-Live {
    # Runs a command with its output shown on the console, and returns the exit code.
    param([string]$Exe, [string[]]$Arguments)
    $code = 1
    try {
        & $Exe @Arguments | Out-Host
        $code = $LASTEXITCODE
    } catch {
        Write-Host $_.Exception.Message
    }
    return $code
}

function Find-SystemPython {
    # Returns the first Python launcher found on PATH ('python' or 'py'), or $null.
    if (Get-Command 'python' -ErrorAction SilentlyContinue) { return 'python' }
    if (Get-Command 'py' -ErrorAction SilentlyContinue) { return 'py' }
    return $null
}

function Read-EnvValue {
    # Returns the last value of $Key in .env, or an empty string (same rule as the old for /f loop).
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

function Write-EnvFile {
    # Writes the standard .env (used by repair attempts 1 and 2 of step 4).
    $lines = @(
        'DB_BACKEND=postgresql',
        'PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard',
        'FLASK_PORT=5050'
    )
    [System.IO.File]::WriteAllLines($EnvFile, [string[]]$lines, (New-Object System.Text.UTF8Encoding($false)))
}

function Find-PgBin {
    # Returns the folder that contains createdb.exe, or an empty string.
    foreach ($candidate in @('E:\pgsql\binaries\bin', 'E:\pgsql\binaries')) {
        if (Test-Path -LiteralPath (Join-Path $candidate 'createdb.exe')) { return $candidate }
    }
    # KEPT: as in setup.bat, the last existing version in this list wins (14 over 18).
    $found = ''
    foreach ($v in 18, 17, 16, 15, 14) {
        $p = "C:\Program Files\PostgreSQL\$v\bin"
        if (Test-Path -LiteralPath (Join-Path $p 'createdb.exe')) { $found = $p }
    }
    return $found
}

function Add-SetupError {
    # Appends one line to setup_error.txt.
    param([string]$Text)
    Add-Content -LiteralPath $SetupErr -Value $Text -Encoding UTF8
}

function Add-LogToError {
    # Appends the last command output (TEMP_LOG) to setup_error.txt.
    if (Test-Path -LiteralPath $TempLog) {
        Get-Content -LiteralPath $TempLog -Encoding UTF8 | Add-Content -LiteralPath $SetupErr -Encoding UTF8
    }
}

function Exit-Fatal {
    # Prints the failure banner, shows the last 10 error lines and exits with code 1.
    if (Test-Path -LiteralPath $TempLog) { Remove-Item -LiteralPath $TempLog -Force -ErrorAction SilentlyContinue }
    Write-Host ''
    Write-Host $Rule
    Write-Host '   ❌ عملیات نصب متوقف شد (پس از ۲ بار تلاش برای تعمیر خودکار)'
    Write-Host $Rule
    Write-Host '   متن کامل خطا و لاگ سیستمی در فایل زیر ذخیره شد:'
    Write-Host "   $SetupErr"
    Write-Host $Rule
    Write-Host ''
    if (Test-Path -LiteralPath $SetupErr) {
        Get-Content -LiteralPath $SetupErr -Encoding UTF8 | Select-Object -Last 10
    }
    Write-Host ''
    Pop-Location
    exit 1
}

# ---------------------------------------------------------------------------
# Banner
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host $Rule
Write-Host '   PrintGuard - راه‌اندازی و پیکربندی خودکار با قابلیت خودترمیمی'
Write-Host $Rule
Write-Host ''

# ---------------------------------------------------------------------------
# Step 1: Python interpreter and virtual environment .venv
# ---------------------------------------------------------------------------

Write-Host ' [1/7] بررسی پایتون و ایجاد محیط مجازی (.venv)...'
$repairCount = 0
$step1Ok = $false
while ($true) {
    $pySystem = Find-SystemPython
    if (-not $pySystem) {
        Set-Content -LiteralPath $SetupErr -Value '[خطا] مفسر پایتون روی ویندوز یافت نشد.' -Encoding UTF8
    }
    if ((-not (Test-Path -LiteralPath $PyCmd)) -and $pySystem) {
        Invoke-Quiet $pySystem @('-m', 'venv', $VenvDir) | Out-Null
    }
    if ((Invoke-Quiet $PyCmd @('--version')) -eq 0) { $step1Ok = $true; break }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] محیط مجازی با مشکل مواجه شد. در حال اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: حذف پوشه .venv و بازسازی ...'
        if (Test-Path -LiteralPath $VenvDir) { Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue }
        if ($pySystem) { Invoke-Quiet $pySystem @('-m', 'venv', '--clear', $VenvDir) | Out-Null }
    } else {
        Write-Host '        - اقدام تعمیر ۲: پاک‌سازی کش و ساخت مجدد ...'
        Get-ChildItem -LiteralPath $ProjectDir -Recurse -Filter '*.pyc' -ErrorAction SilentlyContinue |
            Remove-Item -Force -ErrorAction SilentlyContinue
        Invoke-Quiet 'py' @('-3', '-m', 'venv', $VenvDir) | Out-Null
    }
}
if (-not $step1Ok) {
    Add-SetupError '[خطا در گام ۱] مفسر پایتون یا محیط مجازی قابل بازیابی نیست.'
    Add-LogToError
    Exit-Fatal
}
$verWords = ((& $PyCmd --version 2>&1) | Out-String).Trim() -split '\s+'
$pyVersion = if ($verWords.Count -ge 2) { $verWords[1] } else { '' }
Write-Host " [OK] محیط مجازی آماده شد (پایتون: $pyVersion)"

# ---------------------------------------------------------------------------
# Step 2: Python dependencies, with live download output
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [2/7] نصب و ارزیابی کتابخانه‌های پروژه (نمایش زنده دانلود)...'
$repairCount = 0
Write-Host '        در حال نصب پکیج‌ها (لطفاً شکیبا باشید، پیشرفت در ادامه نمایش می‌یابد)...'
Write-Host '        ------------------------------------------------------------'
Invoke-Live $PyCmd @('-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check') | Out-Null
Invoke-Live $PyCmd @('-m', 'pip', 'install', '-r', $Requirements, '--prefer-binary', '--disable-pip-version-check') | Out-Null
Invoke-Live $PyCmd @('-m', 'pip', 'install', 'psycopg[binary]>=3.1', '--prefer-binary', '--disable-pip-version-check') | Out-Null
Write-Host '        ------------------------------------------------------------'
$step2Ok = $false
while ($true) {
    if ((Invoke-Quiet $PyCmd @('-c', 'import flask, psycopg, alembic')) -eq 0) { $step2Ok = $true; break }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] برخی پکیج‌ها ناقص نصب شده‌اند. در حال اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: پاک‌سازی کش pip و تلاش مجدد با timeout بالاتر ...'
        Invoke-Quiet $PyCmd @('-m', 'pip', 'cache', 'purge') | Out-Null
        Invoke-Live $PyCmd @('-m', 'pip', 'install', '-r', $Requirements, '--default-timeout=100') | Out-Null
        Invoke-Live $PyCmd @('-m', 'pip', 'install', 'psycopg[binary]>=3.1', '--default-timeout=100') | Out-Null
    } else {
        Write-Host '        - اقدام تعمیر ۲: نصب مجدد اجباری با --force-reinstall ...'
        Invoke-Live $PyCmd @('-m', 'pip', 'install', '-r', $Requirements, '--force-reinstall', '--no-cache-dir') | Out-Null
    }
}
if (-not $step2Ok) {
    Add-SetupError '[خطا در گام ۲] نصب وابستگی‌ها شکست خورد.'
    Add-LogToError
    Exit-Fatal
}
Write-Host ' [OK] کلیه کتابخانه‌ها با موفقیت نصب و تأیید شدند.'

# ---------------------------------------------------------------------------
# Step 3: PostgreSQL binaries createdb.exe / psql.exe
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [3/7] کشف هوشمند باینری‌های PostgreSQL...'
$repairCount = 0
$pgBin = Find-PgBin
$pgFound = $false
while ($true) {
    if ($pgBin) {
        $env:PATH = "$pgBin;$env:PATH"
        if ((Invoke-Quiet (Join-Path $pgBin 'createdb.exe') @('--version')) -eq 0) { $pgFound = $true; break }
    }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] باینری‌ها در مسیرهای پیش‌فرض یافت نشدند. اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: جستجوی پوشه E:\pgsql برای یافتن createdb.exe ...'
        $hit = Get-ChildItem -LiteralPath 'E:\pgsql' -Recurse -Filter 'createdb.exe' -ErrorAction SilentlyContinue | Select-Object -First 1
        if ($hit) { $pgBin = $hit.DirectoryName }
    } else {
        Write-Host '        - اقدام تعمیر ۲: بررسی PATH جاری سیستم ...'
        $cmdInfo = Get-Command 'createdb.exe' -ErrorAction SilentlyContinue
        if ($cmdInfo) { $pgBin = Split-Path -Parent $cmdInfo.Source }
    }
}
if ($pgFound) {
    Write-Host " [OK] باینری‌های PostgreSQL یافت شدند: $pgBin"
} else {
    Write-Host '  [توجه] ابزار خط فرمان createdb یافت نشد؛ ساخت دیتابیس با درایور پایتون انجام می‌شود.'
}

# ---------------------------------------------------------------------------
# Step 4: .env file
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [4/7] پیکربندی و اعتبارسنجی فایل تنظیمات (.env)...'
$repairCount = 0
$step4Ok = $false
$pgDsn = ''
while ($true) {
    $pgDsn = Read-EnvValue 'PG_DSN'
    if ($pgDsn) { $step4Ok = $true; break }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] فایل .env ناقص یا ناموجود است. اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: ایجاد فایل .env با مقادیر استاندارد ...'
    } else {
        Write-Host '        - اقدام تعمیر ۲: بازنویسی فایل .env با کدگذاری استاندارد UTF-8 ...'
    }
    Write-EnvFile
}
if (-not $step4Ok) {
    Add-SetupError '[خطا در گام ۴] تنظیم فایل .env ممکن نیست.'
    Exit-Fatal
}
Write-Host " [OK] فایل .env تأیید شد: $pgDsn"

# ---------------------------------------------------------------------------
# Step 5: PostgreSQL service and the printguard database (Collation C)
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [5/7] بررسی سرویس دیتابیس و ساخت پایگاه‌داده printguard...'
$repairCount = 0
$step5Ok = $false
$env:PRINTGUARD_DSN = $pgDsn
$connectCode = 'import os, psycopg; conn=psycopg.connect(os.environ[''PRINTGUARD_DSN''], connect_timeout=3); conn.close()'
while ($true) {
    if ((Invoke-Quiet $PyCmd @('-c', $connectCode)) -eq 0) { $step5Ok = $true; break }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] اتصال به دیتابیس برقرار نشد. در حال اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: بررسی وضعیت سرویس‌های PostgreSQL در ویندوز ...'
        Get-Service -Name '*postgres*' -ErrorAction SilentlyContinue |
            Where-Object { $_.Status -ne 'Running' } |
            ForEach-Object { Start-Service -Name $_.Name -ErrorAction SilentlyContinue }
        if ($pgBin) {
            $env:PGPASSWORD = '123456'
            Invoke-Quiet (Join-Path $pgBin 'createdb.exe') @('-h', '127.0.0.1', '-p', '5432', '-U', 'postgres', '-E', 'UTF8', '-l', 'C', '--lc-collate=C', '--lc-ctype=C', 'printguard') | Out-Null
        }
    } else {
        Write-Host '        - اقدام تعمیر ۲: ساخت مستقیم دیتابیس printguard از داخل پایتون با Collation C ...'
        $env:PRINTGUARD_ADMIN_DSN = 'postgresql://postgres:123456@127.0.0.1:5432/postgres'
        $env:PRINTGUARD_SQL = "CREATE DATABASE printguard ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'"
        $createCode = 'import os, psycopg; conn=psycopg.connect(os.environ[''PRINTGUARD_ADMIN_DSN''], autocommit=True); cur=conn.cursor(); cur.execute(os.environ[''PRINTGUARD_SQL'']); conn.close()'
        Invoke-Quiet $PyCmd @('-c', $createCode) | Out-Null
    }
}
if (-not $step5Ok) {
    Add-SetupError '[خطا در گام ۵] برقراری اتصال به PostgreSQL یا ساخت دیتابیس شکست خورد.'
    Add-LogToError
    Exit-Fatal
}
Write-Host ' [OK] پایگاه‌داده printguard آماده و پاسخگو است.'

# ---------------------------------------------------------------------------
# Step 6: Alembic schema
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [6/7] اعمال ساختار جداول با Alembic...'
$repairCount = 0
$step6Ok = $false
$alembicHead = ''
while ($true) {
    # KEPT: the URL is reset to the .env value at the start of every attempt (as in setup.bat).
    $env:ALEMBIC_DB_URL = $pgDsn
    $alembicHead = ''
    if ((Invoke-Quiet $PyCmd @('-m', 'alembic', 'upgrade', 'head')) -eq 0) {
        $currentLines = @(& $PyCmd -m alembic current 2>$null)
        $firstLine = $currentLines | Where-Object { $_ -and ([string]$_).Trim() } | Select-Object -First 1
        if ($firstLine) { $alembicHead = (([string]$firstLine).Trim() -split '\s+')[0] }
    }
    if ($alembicHead) { $step6Ok = $true; break }
    if ($repairCount -ge 2) { break }
    $repairCount++
    Write-Host "        [هشدار] اعمال اسکیمای Alembic با مشکل مواجه شد. اجرای تعمیر $repairCount از 2..."
    if ($repairCount -eq 1) {
        Write-Host '        - اقدام تعمیر ۱: تنظیم فرمت صریح postgresql+psycopg ...'
        # KEPT: as in setup.bat, this override is reset at the start of the next attempt.
        $env:ALEMBIC_DB_URL = $pgDsn.Replace('postgresql://', 'postgresql+psycopg://')
    } else {
        Write-Host '        - اقدام تعمیر ۲: انطباق سرشاخه با alembic.ini ...'
        Invoke-Quiet $PyCmd @('-m', 'alembic', '--config', $AlembicIni, 'stamp', 'head') | Out-Null
        Invoke-Quiet $PyCmd @('-m', 'alembic', '--config', $AlembicIni, 'upgrade', 'head') | Out-Null
    }
}
if (-not $step6Ok) {
    Add-SetupError '[خطا در گام ۶] اعمال اسکیما با Alembic ناموفق بود.'
    Add-LogToError
    Exit-Fatal
}
Write-Host " [OK] ساختار ۱۱ جدول با موفقیت ایجاد شد (نسخه اسکیما: $alembicHead)."

function Show-DiagnosisIssues {
    # Prints every issue from logs.db.diagnosis.json (written by diagnose_data_quality.py).
    $diagPath = Join-Path $ProjectDir 'logs.db.diagnosis.json'
    if (-not (Test-Path -LiteralPath $diagPath)) {
        Write-Host '        (فایل گزارش تشخیصی ساخته نشد؛ جزئیات در دسترس نیست.)'
        return
    }
    try {
        $report = Get-Content -LiteralPath $diagPath -Encoding UTF8 -Raw | ConvertFrom-Json
        foreach ($tbl in $report.tables.PSObject.Properties) {
            foreach ($issue in @($tbl.Value.issues)) {
                if ($issue) { Write-Host "        - [$($tbl.Name)] $($issue.message)" }
            }
        }
    } catch {
        Write-Host "        (خواندن گزارش تشخیصی ممکن نبود: $($_.Exception.Message))"
    }
}

# ---------------------------------------------------------------------------
# Step 7: logs.db health check and optional migration
# ---------------------------------------------------------------------------

Write-Host ''
Write-Host ' [7/7] بررسی پایگاه‌داده منبع (logs.db) و ارزیابی کیفیت داده...'
if (-not (Test-Path -LiteralPath $LogsDb)) {
    Write-Host '        فایل logs.db وجود ندارد؛ پایگاه‌داده نو و آماده برای ثبت داده‌های جدید است.'
} else {
    $dbSize = (Get-Item -LiteralPath $LogsDb).Length
    Write-Host "        فایل logs.db پیدا شد (حجم: $dbSize بایت)."
    $repairCount = 0
    $diagOk = $false
    while ($true) {
        if ((Invoke-Quiet $PyCmd @('tools\diagnose_data_quality.py', '--db', 'logs.db', '--quiet')) -eq 0) { $diagOk = $true; break }
        # Repairs are exhausted: stop here. The failure is reported below and setup continues (option A).
        if ($repairCount -ge 2) { break }
        $repairCount++
        Write-Host "        [هشدار] بررسی کیفیت داده‌های logs.db نیاز به توجه دارد. تعمیر خودکار $repairCount از 2..."
        if ($repairCount -eq 1) {
            Write-Host '        - اقدام تعمیر ۱: اجرای PRAGMA integrity_check روی SQLite ...'
            Invoke-Quiet $PyCmd @('-c', "import sqlite3; sqlite3.connect('logs.db').execute('PRAGMA integrity_check;')") | Out-Null
        } else {
            Write-Host '        - اقدام تعمیر ۲: بازسازی ایندکس‌ها با PRAGMA reindex ...'
            Invoke-Quiet $PyCmd @('-c', "import sqlite3; sqlite3.connect('logs.db').execute('REINDEX;')") | Out-Null
        }
    }
    if ($diagOk) {
        Write-Host ' [OK] ارزیابی سلامت دیتابیس منبع تأیید شد.'
    } else {
        Write-Host ' [هشدار] بررسی کیفیت داده‌های logs.db پس از ۲ بار تعمیر همچنان ناموفق بود. ادامه با هشدار:'
        Show-DiagnosisIssues
    }
    Write-Host ''
    $answer = Read-Host '  آیا مایلید مهاجرت کامل داده‌های logs.db به PostgreSQL همین حالا انجام شود؟ (Y/N) [Y]'
    if (-not $answer) { $answer = 'Y' }
    if ($answer -ieq 'Y') {
        Write-Host '        در حال مهاجرت و تبدیل زمان به فرمت استاندارد UTC Z ...'
        $migrateCode = Invoke-Live $PyCmd @('tools\migrate_sqlite_to_pg.py', '--source', 'logs.db', '--target', $pgDsn, '--convert-naive-timestamps', '--verify')
        if ($migrateCode -ne 0) {
            Write-Host '  [هشدار] مهاجرت داده‌ها متوقف شد؛ برای اجرای اختصاصی از migrate.bat استفاده نمایید.'
        } else {
            Write-Host ' [OK] کلیه داده‌ها با موفقیت کامل به PostgreSQL منتقل شدند.'
        }
    } else {
        Write-Host '        مهاجرت داده‌ها نادیده گرفته شد (می‌توانید بعداً migrate.bat را اجرا کنید).'
    }
}

# ---------------------------------------------------------------------------
# Finish
# ---------------------------------------------------------------------------

if (Test-Path -LiteralPath $TempLog) { Remove-Item -LiteralPath $TempLog -Force -ErrorAction SilentlyContinue }
Write-Host ''
Write-Host $Rule
Write-Host '   ✔ راه‌اندازی و بررسی هفت‌گانه با موفقیت کامل پایان یافت!'
Write-Host '   اکنون برای شروع پایش پرینترها، start.bat را اجرا کنید.'
Write-Host $Rule
Write-Host ''
Pop-Location
exit 0

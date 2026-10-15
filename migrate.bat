@echo off
setlocal EnableDelayedExpansion

chcp 65001 >nul
title PrintGuard - Database Migration (SQLite to PostgreSQL)

echo.
echo  ============================================================
echo   PrintGuard - مهاجرت ایمن پایگاه‌داده به PostgreSQL
echo  ============================================================
echo.

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "ERROR_LOG=migration_error.txt"
set "SUCCESS_LOG=migration_success.txt"
set "TEMP_LOG=~migration_temp.log"

:: اضافه کردن باینری‌های درایو E به PATH
if exist "E:\pgsql\binaries\bin" set "PATH=E:\pgsql\binaries\bin;!PATH!"
if exist "E:\pgsql\binaries" set "PATH=E:\pgsql\binaries;!PATH!"

if exist "!ERROR_LOG!" del /f /q "!ERROR_LOG!" >nul 2>&1
if exist "!TEMP_LOG!" del /f /q "!TEMP_LOG!" >nul 2>&1

:: ۱. انتخاب مفسر پایتون
set "PY_CMD="
if exist ".venv\Scripts\python.exe" set "PY_CMD=.venv\Scripts\python.exe"
if not defined PY_CMD where python >nul 2>&1 && set "PY_CMD=python"
if not defined PY_CMD where py >nul 2>&1 && set "PY_CMD=py"

if not defined PY_CMD (
    echo [خطا] مفسر پایتون یافت نشد. > "!ERROR_LOG!"
    echo  [خطا] پایتون یافت نشد!
    pause & exit /b 1
)

:: ۲. بررسی وجود فایل پایگاه‌داده مبدأ (logs.db)
if not exist "logs.db" (
    echo [خطا] فایل دیتابیس logs.db در این پوشه یافت نشد. > "!ERROR_LOG!"
    echo  [خطا] فایل logs.db پیدا نشد!
    pause & exit /b 1
)

for %%F in (logs.db) do set "DB_SIZE=%%~zF"
echo  [1/5] دیتابیس مبدأ: logs.db (حجم: !DB_SIZE! بایت)

:: ۳. تهیه نسخه پشتیبان در درایو E
echo.
echo  [2/5] تهیه نسخه پشتیبان از logs.db...
set "BACKUP_DIR="
if exist "E:\" (
    if not exist "E:\pgbackup" mkdir "E:\pgbackup" >nul 2>&1
    set "BACKUP_DIR=E:\pgbackup"
) else (
    if not exist "backups" mkdir "backups" >nul 2>&1
    set "BACKUP_DIR=backups"
)

for /f "tokens=2 delims==" %%I in ('wmic os get localdatetime /value 2^>nul') do set "DT=%%I"
set "BACKUP_FILE=!BACKUP_DIR!\logs_backup_!DT:~0,8!_!DT:~8,6!.db"

copy "logs.db" "!BACKUP_FILE!" >nul 2>&1
if errorlevel 1 (
    echo [هشدار] کپی پشتیبان انجام نشد، ادامه با احتیاط...
) else (
    echo  [OK] نسخه پشتیبان ذخیره شد در: !BACKUP_FILE!
)

:: ۴. خواندن متغیر PG_DSN از .env
echo.
echo  [3/5] بررسی اتصال به PostgreSQL...
set "PG_DSN="
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="PG_DSN" set "PG_DSN=%%~b"
    )
)

if not defined PG_DSN (
    set "PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard"
)

:: تست اتصال پایتون به دیتابیس مقصد
"!PY_CMD!" -c "import psycopg; conn=psycopg.connect('!PG_DSN!', connect_timeout=3); conn.close()" > "!TEMP_LOG!" 2>&1
if errorlevel 1 (
    echo [خطا] ارتباط با دیتابیس PostgreSQL برقرار نشد. > "!ERROR_LOG!"
    type "!TEMP_LOG!" >> "!ERROR_LOG!"
    echo  [خطا] اتصال به PostgreSQL برقرار نشد! جزئیات در migration_error.txt
    pause & exit /b 1
)
echo  [OK] اتصال به پایگاه‌داده printguard تأیید شد.

:: ۵. اجرای آزمایشی (Dry-Run)
echo.
echo  [4/5] ارزیابی اولیه داده‌ها (Dry-Run)...
"!PY_CMD!" tools\migrate_sqlite_to_pg.py --source logs.db --target "!PG_DSN!" --dry-run > "!TEMP_LOG!" 2>&1
set "DRY_EXIT=!ERRORLEVEL!"

if not "!DRY_EXIT!"=="3" if not "!DRY_EXIT!"=="0" (
    echo [خطا در تست Dry-Run] > "!ERROR_LOG!"
    type "!TEMP_LOG!" >> "!ERROR_LOG!"
    echo  [خطا] تست Dry-Run ناموفق بود. جزئیات در migration_error.txt
    pause & exit /b 1
)
echo  [OK] ساختار جداول آماده انتقال است.

:: ۶. انتقال قطعی داده‌ها با تصحیح ساعت به UTC Z
echo.
echo  [5/5] در حال انتقال قطعی داده‌ها و همگام‌سازی توالی‌ها...
echo        (تبدیل خودکار ساعت محلی تهران به UTC Z و محاسبه چک‌سام)
echo.

"!PY_CMD!" tools\migrate_sqlite_to_pg.py --source logs.db --target "!PG_DSN!" --convert-naive-timestamps --verify > "!TEMP_LOG!" 2>&1
if errorlevel 1 (
    echo [خطا در انتقال داده‌ها] > "!ERROR_LOG!"
    type "!TEMP_LOG!" >> "!ERROR_LOG!"
    echo  [خطا] انتقال داده‌ها با مشکل مواجه شد!
    echo.
    echo  --- گزیده‌ای از خطا: ---
    powershell -NoProfile -Command "Get-Content '!ERROR_LOG!' | Select-Object -Last 10"
    echo.
    echo  متن کامل در migration_error.txt ذخیره شد.
    pause & exit /b 1
)

type "!TEMP_LOG!" > "!SUCCESS_LOG!"
del /f /q "!TEMP_LOG!" >nul 2>&1

:: تنظیم DB_BACKEND در .env
powershell -NoProfile -Command "(Get-Content .env) -replace '^DB_BACKEND=.*', 'DB_BACKEND=postgresql' | Set-Content .env"

echo.
echo  ============================================================
echo   ✔ مهاجرت پایگاه‌داده با موفقیت کامل به پایان رسید!
echo  ============================================================
echo   - کلیه جداول و شمارنده‌ها به PostgreSQL منتقل شدند.
echo   - برچسب‌های زمانی به فرمت استاندارد UTC Z کسر و ثبت شدند.
echo   - فایل .env با موفقیت روی DB_BACKEND=postgresql تنظیم شد.
echo   - گزارش در فایل migration_success.txt ذخیره شد.
echo  ============================================================
echo   اکنون می‌توانید با اجرای start.bat سامانه را اجرا نمایید.
echo  ============================================================
echo.

pause
exit /b 0
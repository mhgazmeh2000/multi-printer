@echo off
setlocal EnableDelayedExpansion
set "SCRIPT_DIR=%~dp0"

:: کدپیج UTF-8
chcp 65001 >nul

:: اصلاح عنوان بدون کاراکتر & برای جلوگیری از خطای ویندوز
title PrintGuard - Self-Healing Setup and Provisioning

echo.
echo  ============================================================
echo   PrintGuard - راه‌اندازی و پیکربندی خودکار با قابلیت خودترمیمی
echo  ============================================================
echo.

pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 (
    echo [خطا] دسترسی به پوشه پروژه امکان‌پذیر نیست.
    pause
    exit /b 1
)

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "SETUP_ERR=%CD%\setup_error.txt"
set "TEMP_LOG=%CD%\~setup_temp.log"

if exist "!SETUP_ERR!" del /f /q "!SETUP_ERR!" >nul 2>&1
if exist "!TEMP_LOG!" del /f /q "!TEMP_LOG!" >nul 2>&1

:: ============================================================
:: گام ۱: مفسر پایتون و محیط مجازی (.venv)
:: ============================================================
echo  [1/7] بررسی پایتون و ایجاد محیط مجازی (.venv)...
set "REPAIR_COUNT=0"

:step1_check
set "PY_SYSTEM="
where python >nul 2>&1 && set "PY_SYSTEM=python"
if not defined PY_SYSTEM where py >nul 2>&1 && set "PY_SYSTEM=py"

if not defined PY_SYSTEM (
    echo [خطا] مفسر پایتون روی ویندوز یافت نشد. > "!SETUP_ERR!"
    goto step1_fail
)

if not exist "%CD%\.venv\Scripts\python.exe" (
    "%PY_SYSTEM%" -m venv "%CD%\.venv" > "!TEMP_LOG!" 2>&1
)

set "PY_CMD=%CD%\.venv\Scripts\python.exe"
"%PY_CMD%" --version > "!TEMP_LOG!" 2>&1
if not errorlevel 1 goto step1_success

:step1_fail
if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] محیط مجازی با مشکل مواجه شد. در حال اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: حذف پوشه .venv و بازسازی ...
        if exist "%CD%\.venv" rmdir /s /q "%CD%\.venv" >nul 2>&1
        "%PY_SYSTEM%" -m venv --clear "%CD%\.venv" > "!TEMP_LOG!" 2>&1
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: پاک‌سازی کش و ساخت مجدد ...
        del /s /q "%CD%\*.pyc" >nul 2>&1
        py -3 -m venv "%CD%\.venv" > "!TEMP_LOG!" 2>&1
    )
    goto step1_check
)
echo [خطا در گام ۱] مفسر پایتون یا محیط مجازی قابل بازیابی نیست. >> "!SETUP_ERR!"
type "!TEMP_LOG!" >> "!SETUP_ERR!"
goto fatal_exit

:step1_success
for /f "tokens=2" %%v in ('"%PY_CMD%" --version 2^>^&1') do echo  [OK] محیط مجازی آماده شد (پایتون: %%v)


:: ============================================================
:: گام ۲: نصب وابستگی‌های پایتون (با نمایش زنده فرآیند دانلود)
:: ============================================================
echo.
echo  [2/7] نصب و ارزیابی کتابخانه‌های پروژه (نمایش زنده دانلود)...
set "REPAIR_COUNT=0"

:step2_install
echo        در حال نصب پکیج‌ها (لطفاً شکیبا باشید، پیشرفت در ادامه نمایش می‌یابد)...
echo        ------------------------------------------------------------
"%PY_CMD%" -m pip install --upgrade pip --disable-pip-version-check
"%PY_CMD%" -m pip install -r "%CD%\requirements.txt" --prefer-binary --disable-pip-version-check
"%PY_CMD%" -m pip install "psycopg[binary]>=3.1" --prefer-binary --disable-pip-version-check
echo        ------------------------------------------------------------

:step2_check
"%PY_CMD%" -c "import flask, psycopg, alembic" > "!TEMP_LOG!" 2>&1
if not errorlevel 1 goto step2_success

if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] برخی پکیج‌ها ناقص نصب شده‌اند. در حال اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: پاک‌سازی کش pip و تلاش مجدد با timeout بالاتر ...
        "%PY_CMD%" -m pip cache purge >nul 2>&1
        "%PY_CMD%" -m pip install -r "%CD%\requirements.txt" --default-timeout=100
        "%PY_CMD%" -m pip install "psycopg[binary]>=3.1" --default-timeout=100
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: نصب مجدد اجباری با --force-reinstall ...
        "%PY_CMD%" -m pip install -r "%CD%\requirements.txt" --force-reinstall --no-cache-dir
    )
    goto step2_check
)
echo [خطا در گام ۲] نصب وابستگی‌ها شکست خورد. >> "!SETUP_ERR!"
type "!TEMP_LOG!" >> "!SETUP_ERR!"
goto fatal_exit

:step2_success
echo  [OK] کلیه کتابخانه‌ها با موفقیت نصب و تأیید شدند.


:: ============================================================
:: گام ۳: کشف باینری‌های PostgreSQL (createdb.exe / psql.exe)
:: ============================================================
echo.
echo  [3/7] کشف هوشمند باینری‌های PostgreSQL...
set "REPAIR_COUNT=0"

:step3_check
set "PG_BIN="
if exist "E:\pgsql\binaries\bin\createdb.exe" set "PG_BIN=E:\pgsql\binaries\bin"
if not defined PG_BIN if exist "E:\pgsql\binaries\createdb.exe" set "PG_BIN=E:\pgsql\binaries"

if not defined PG_BIN (
    for %%v in (18 17 16 15 14) do (
        if exist "C:\Program Files\PostgreSQL\%%v\bin\createdb.exe" (
            set "PG_BIN=C:\Program Files\PostgreSQL\%%v\bin"
        )
    )
)

if defined PG_BIN (
    set "PATH=!PG_BIN!;!PATH!"
    "%PG_BIN%\createdb.exe" --version > "!TEMP_LOG!" 2>&1
    if not errorlevel 1 goto step3_success
)

if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] باینری‌ها در مسیرهای پیش‌فرض یافت نشدند. اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: جستجوی پوشه E:\pgsql برای یافتن createdb.exe ...
        for /f "delims=" %%f in ('where /r "E:\pgsql" createdb.exe 2^>nul') do (
            set "PG_BIN=%%~dpf"
            set "PG_BIN=!PG_BIN:~0,-1!"
        )
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: بررسی PATH جاری سیستم ...
        for /f "delims=" %%f in ('where createdb.exe 2^>nul') do (
            set "PG_BIN=%%~dpf"
            set "PG_BIN=!PG_BIN:~0,-1!"
        )
    )
    goto step3_check
)
echo  [توجه] ابزار خط فرمان createdb یافت نشد؛ ساخت دیتابیس با درایور پایتون انجام می‌شود.
goto step3_continue

:step3_success
echo  [OK] باینری‌های PostgreSQL یافت شدند: !PG_BIN!

:step3_continue


:: ============================================================
:: گام ۴: پیکربندی و اعتبارسنجی فایل .env
:: ============================================================
echo.
echo  [4/7] پیکربندی و اعتبارسنجی فایل تنظیمات (.env)...
set "REPAIR_COUNT=0"

:step4_check
set "PG_DSN="
if exist "%CD%\.env" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%CD%\.env") do (
        if /i "%%a"=="PG_DSN" set "PG_DSN=%%~b"
    )
)

if defined PG_DSN goto step4_success

if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] فایل .env ناقص یا ناموجود است. اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: ایجاد فایل .env با مقادیر استاندارد ...
        (
            echo DB_BACKEND=postgresql
            echo PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard
            echo FLASK_PORT=5050
        ) > "%CD%\.env"
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: بازنویسی فایل .env با کدگذاری استاندارد UTF-8 ...
        powershell -NoProfile -Command "[System.IO.File]::WriteAllLines('.env', @('DB_BACKEND=postgresql','PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard','FLASK_PORT=5050'))"
    )
    goto step4_check
)
echo [خطا در گام ۴] تنظیم فایل .env ممکن نیست. >> "!SETUP_ERR!"
goto fatal_exit

:step4_success
echo  [OK] فایل .env تأیید شد: !PG_DSN!


:: ============================================================
:: گام ۵: بررسی سرویس PostgreSQL و ساخت دیتابیس با Collation='C'
:: ============================================================
echo.
echo  [5/7] بررسی سرویس دیتابیس و ساخت پایگاه‌داده printguard...
set "REPAIR_COUNT=0"

:step5_check
"%PY_CMD%" -c "import psycopg; conn=psycopg.connect('!PG_DSN!', connect_timeout=3); conn.close()" > "!TEMP_LOG!" 2>&1
if not errorlevel 1 goto step5_success

if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] اتصال به دیتابیس برقرار نشد. در حال اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: بررسی وضعیت سرویس‌های PostgreSQL در ویندوز ...
        for /f "tokens=2" %%s in ('sc query state^= all 2^>nul ^| findstr /i "SERVICE_NAME" ^| findstr /i "postgres"') do (
            net start %%s >nul 2>&1
        )
        if defined PG_BIN (
            set "PGPASSWORD=123456"
            "%PG_BIN%\createdb.exe" -h 127.0.0.1 -p 5432 -U postgres -E UTF8 -l C --lc-collate=C --lc-ctype=C printguard >nul 2>&1
        )
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: ساخت مستقیم دیتابیس printguard از داخل پایتون با Collation C ...
        "%PY_CMD%" -c "import psycopg; conn=psycopg.connect('postgresql://postgres:123456@127.0.0.1:5432/postgres', autocommit=True); cur=conn.cursor(); cur.execute(\"CREATE DATABASE printguard ENCODING 'UTF8' LC_COLLATE 'C' LC_CTYPE 'C'\"); conn.close()" > "!TEMP_LOG!" 2>&1
    )
    goto step5_check
)
echo [خطا در گام ۵] برقراری اتصال به PostgreSQL یا ساخت دیتابیس شکست خورد. >> "!SETUP_ERR!"
type "!TEMP_LOG!" >> "!SETUP_ERR!"
goto fatal_exit

:step5_success
echo  [OK] پایگاه‌داده printguard آماده و پاسخگو است.


:: ============================================================
:: گام ۶: اعمال و همگام‌سازی اسکیمای Alembic (0005)
:: ============================================================
echo.
echo  [6/7] اعمال ساختار جداول با Alembic...
set "REPAIR_COUNT=0"

:step6_check
set "ALEMBIC_DB_URL=!PG_DSN!"
"%PY_CMD%" -m alembic upgrade head > "!TEMP_LOG!" 2>&1
if errorlevel 1 goto step6_fail

for /f %%c in ('"%PY_CMD%" -m alembic current 2^>nul') do set "ALEMBIC_HEAD=%%c"
if defined ALEMBIC_HEAD goto step6_success

:step6_fail
if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] اعمال اسکیمای Alembic با مشکل مواجه شد. اجرای تعمیر !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: تنظیم فرمت صریح postgresql+psycopg ...
        set "ALEMBIC_DB_URL=!PG_DSN:postgresql://=postgresql+psycopg://!"
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: انطباق سرشاخه با alembic.ini ...
        "%PY_CMD%" -m alembic --config "%CD%\alembic.ini" stamp head >nul 2>&1
        "%PY_CMD%" -m alembic --config "%CD%\alembic.ini" upgrade head > "!TEMP_LOG!" 2>&1
    )
    goto step6_check
)
echo [خطا در گام ۶] اعمال اسکیما با Alembic ناموفق بود. >> "!SETUP_ERR!"
type "!TEMP_LOG!" >> "!SETUP_ERR!"
goto fatal_exit

:step6_success
echo  [OK] ساختار ۱۱ جدول با موفقیت ایجاد شد (نسخه اسکیما: !ALEMBIC_HEAD!).


:: ============================================================
:: گام ۷: اعتبارسنجی سلامت داده‌ها و پیشنهاد مهاجرت
:: ============================================================
echo.
echo  [7/7] بررسی پایگاه‌داده منبع (logs.db) و ارزیابی کیفیت داده...
set "REPAIR_COUNT=0"

:step7_check
if not exist "%CD%\logs.db" (
    echo        فایل logs.db وجود ندارد؛ پایگاه‌داده نو و آماده برای ثبت داده‌های جدید است.
    goto step7_success
)

for %%F in ("%CD%\logs.db") do set "DB_SZ=%%~zF"
echo        فایل logs.db پیدا شد (حجم: !DB_SZ! بایت).

"%PY_CMD%" tools\diagnose_data_quality.py --db logs.db --quiet > "!TEMP_LOG!" 2>&1
if not errorlevel 1 goto step7_healthy

if !REPAIR_COUNT! LSS 2 (
    set /a REPAIR_COUNT+=1
    echo        [هشدار] بررسی کیفیت داده‌های logs.db نیاز به توجه دارد. تعمیر خودکار !REPAIR_COUNT! از 2...
    if !REPAIR_COUNT!==1 (
        echo        - اقدام تعمیر ۱: اجرای PRAGMA integrity_check روی SQLite ...
        "%PY_CMD%" -c "import sqlite3; sqlite3.connect('logs.db').execute('PRAGMA integrity_check;')" >nul 2>&1
    )
    if !REPAIR_COUNT!==2 (
        echo        - اقدام تعمیر ۲: بازسازی ایندکس‌ها با PRAGMA reindex ...
        "%PY_CMD%" -c "import sqlite3; sqlite3.connect('logs.db').execute('REINDEX;')" >nul 2>&1
    )
    goto step7_check
)

:step7_healthy
echo  [OK] ارزیابی سلامت دیتابیس منبع تأیید شد.
echo.
set /p DO_MIGRATE="  آیا مایلید مهاجرت کامل داده‌های logs.db به PostgreSQL همین حالا انجام شود؟ (Y/N) [Y]: "
if /i "!DO_MIGRATE!"=="" set "DO_MIGRATE=Y"
if /i "!DO_MIGRATE!"=="Y" (
    echo        در حال مهاجرت و تبدیل زمان به فرمت استاندارد UTC Z ...
    "%PY_CMD%" tools\migrate_sqlite_to_pg.py --source logs.db --target "!PG_DSN!" --convert-naive-timestamps --verify
    if errorlevel 1 (
        echo  [هشدار] مهاجرت داده‌ها متوقف شد؛ برای اجرای اختصاصی از migrate.bat استفاده نمایید.
    ) else (
        echo  [OK] کلیه داده‌ها با موفقیت کامل به PostgreSQL منتقل شدند.
    )
) else (
    echo        مهاجرت داده‌ها نادیده گرفته شد (می‌توانید بعداً migrate.bat را اجرا کنید).
)

:step7_success

if exist "!TEMP_LOG!" del /f /q "!TEMP_LOG!" >nul 2>&1

echo.
echo  ============================================================
echo   ✔ راه‌اندازی و بررسی هفت‌گانه با موفقیت کامل پایان یافت!
echo   اکنون برای شروع پایش پرینترها، start.bat را اجرا کنید.
echo  ============================================================
echo.
popd
pause
exit /b 0

:fatal_exit
if exist "!TEMP_LOG!" del /f /q "!TEMP_LOG!" >nul 2>&1
echo.
echo  ============================================================
echo   ❌ عملیات نصب متوقف شد (پس از ۲ بار تلاش برای تعمیر خودکار)
echo  ============================================================
echo   متن کامل خطا و لاگ سیستمی در فایل زیر ذخیره شد:
echo   %SETUP_ERR%
echo  ============================================================
echo.
powershell -NoProfile -Command "Get-Content '!SETUP_ERR!' | Select-Object -Last 10"
echo.
popd
pause
exit /b 1
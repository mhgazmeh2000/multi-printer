@echo off
setlocal EnableDelayedExpansion

chcp 65001 >nul
title PrintGuard - Runner

echo.
echo  ============================================================
echo   PrintGuard - Multi Printer Monitoring System
echo  ============================================================
echo.

cd /d "%~dp0"

set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"

:: --- ۱. مفسر پایتون ---
echo  [1/4] بررسی مفسر پایتون...
set "PY_CMD="
if exist ".venv\Scripts\python.exe" set "PY_CMD=.venv\Scripts\python.exe"
if not defined PY_CMD where python >nul 2>&1 && set "PY_CMD=python"
if not defined PY_CMD where py >nul 2>&1 && set "PY_CMD=py"

if not defined PY_CMD (
    echo  [ERROR] مفسر پایتون پیدا نشد!
    pause & exit /b 1
)
for /f "tokens=2" %%v in ('!PY_CMD! --version 2^>^&1') do echo  [OK] پایتون: %%v

del /s /q *.pyc >nul 2>&1

:: --- ۲. دریافت پورت دلخواه ---
echo.
echo  [2/4] پیکربندی پورت وب‌سرور...
set "DEFAULT_PORT=5050"
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="FLASK_PORT" set "DEFAULT_PORT=%%~b"
    )
)

set "USER_PORT=!DEFAULT_PORT!"
set /p USER_PORT="  Port [!DEFAULT_PORT!]: "
if "!USER_PORT!"=="" set "USER_PORT=!DEFAULT_PORT!"
set "FLASK_PORT=!USER_PORT!"
echo  [OK] پورت انتخابی: !USER_PORT!

:: آزادسازی پورت در صورت اشغال بودن
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":%USER_PORT% " ^| findstr "LISTENING"') do (
    if not "%%a"=="0" (
        echo        آزادسازی پورت %USER_PORT% با بستن PID %%a ...
        taskkill /F /PID %%a >nul 2>&1
    )
)
echo  [OK] پورت %USER_PORT% آماده است.

:: --- ۳. بررسی پایگاه‌داده (با استارت خودکار E:\pgsql در صورت خاموش بودن) ---
echo.
echo  [3/4] بررسی اتصال پایگاه‌داده...
set "DB_BACKEND=sqlite"
set "PG_DSN="
if exist ".env" (
    for /f "usebackq tokens=1,* delims==" %%a in (".env") do (
        if /i "%%a"=="DB_BACKEND" set "DB_BACKEND=%%~b"
        if /i "%%a"=="PG_DSN"     set "PG_DSN=%%~b"
    )
)

if /i "!DB_BACKEND!"=="postgresql" (
    echo        نوع پایگاه‌داده: PostgreSQL

    :: اگر پورت ۵۴۳۲ روشن نبود، استارت خودکار با pg_ctl
    netstat -ano 2>nul | findstr ":5432 " | findstr "LISTENING" >nul 2>&1
    if errorlevel 1 (
        echo        پایگاه‌داده خاموش است؛ در حال استارت خودکار از E:\pgsql ...
        if exist "E:\pgsql\binaries\bin\pg_ctl.exe" (
            "E:\pgsql\binaries\bin\pg_ctl.exe" -D "E:\pgsql\data" start >nul 2>&1
            timeout /t 3 /nobreak >nul 2>&1
        )
    )

    :: اعتبارسنجی اتصال
    !PY_CMD! -c "import psycopg; conn=psycopg.connect('!PG_DSN!', connect_timeout=3); conn.close()" >nul 2>&1
    if errorlevel 1 (
        echo  [ERROR] اتصال به پایگاه‌داده PostgreSQL برقرار نشد!
        pause & exit /b 1
    )
    echo  [OK] اتصال به PostgreSQL پایدار است.

    set "ALEMBIC_DB_URL=!PG_DSN!"
    !PY_CMD! -m alembic upgrade head >nul 2>&1
    !PY_CMD! tools\auto_migrate.py
) else (
    echo        نوع پایگاه‌داده: SQLite (logs.db)
)

:: --- ۴. اجرای پروژه ---
echo.
echo  [4/4] راه‌اندازی سامانه مانیتورینگ...
echo  ============================================================
echo   داشبورد وب: http://localhost:%USER_PORT%/
echo   توقف برنامه: کلیدهای Ctrl + C
echo  ============================================================
echo.

title PrintGuard [Port: %USER_PORT%]

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 2; Start-Process 'http://localhost:%USER_PORT%/'"

!PY_CMD! run.py

echo.
echo  سامانه PrintGuard متوقف شد.
pause
exit /b 0
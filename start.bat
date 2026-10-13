@echo off
setlocal
set "SCRIPT_DIR=%~dp0"

title PrintGuard - Starting...

echo.
echo  ============================================================
echo   PrintGuard - Multi Printer Monitoring
echo  ============================================================
echo.

:: --- Navigate to script directory ---
:: pushd maps UNC to a drive letter (e.g. V:) which avoids the
:: parentheses problem in UNC paths like "workspace-... (1)"
pushd "%SCRIPT_DIR%" >nul 2>&1
if errorlevel 1 goto folder_error

:: IMPORTANT: After pushd, %CD% is the mapped drive path (clean, no UNC).
:: Always use %CD% (not %SCRIPT_DIR%) to avoid UNC + parentheses issues.

:: --- Step 1: Check Python ---
echo  [1/5] Checking Python...
set "PY_CMD="

:: Priority 1: venv
if exist "%CD%\.venv\Scripts\python.exe" set "PY_CMD=%CD%\.venv\Scripts\python.exe"
if defined PY_CMD goto python_found

:: Priority 2: py launcher
where py >nul 2>&1
if errorlevel 1 goto skip_py
set "PY_CMD=py"
:skip_py
if defined PY_CMD goto python_found

:: Priority 3: python in PATH
where python >nul 2>&1
if errorlevel 1 goto skip_python
set "PY_CMD=python"
:skip_python
if defined PY_CMD goto python_found

:: Priority 4: common install locations
if exist "%LOCALAPPDATA%\Programs\Python\Python314\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python314\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python313\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python313\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python312\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python312\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python311\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python311\python.exe"
if defined PY_CMD goto python_found
if exist "%LOCALAPPDATA%\Programs\Python\Python310\python.exe" set "PY_CMD=%LOCALAPPDATA%\Programs\Python\Python310\python.exe"
if defined PY_CMD goto python_found
if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe" set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python314\python.exe"
if defined PY_CMD goto python_found
if exist "%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe" set "PY_CMD=%USERPROFILE%\AppData\Local\Programs\Python\Python313\python.exe"
if defined PY_CMD goto python_found

goto python_error

:python_found
"%PY_CMD%" --version >nul 2>&1
if errorlevel 1 goto python_error
for /f "tokens=2" %%v in ('"%PY_CMD%" --version 2^>^&1') do echo  [OK] Python %%v

:: --- Step 2: Clean cache ---
echo  [2/5] Cleaning cache...
for /d /r "%CD%" %%d in (__pycache__) do rd /s /q "%%d" 2>nul
del /s /q "%CD%\*.pyc" 2>nul
echo  [OK] Cache cleared

:: --- Step 3: Dependencies ---
echo  [3/5] Checking dependencies...
"%PY_CMD%" -m pip install -r "%CD%\requirements.txt" -q --disable-pip-version-check 2>nul
echo  [OK] Dependencies ready

:: --- Step 4: Port ---
echo  [4/5] Port configuration...
set "USER_PORT=5050"
set /p USER_PORT="  Port [5050]: "
if "%USER_PORT%"=="" set USER_PORT=5050
set FLASK_PORT=%USER_PORT%
echo  [OK] Port: %USER_PORT%

:: --- Step 5: Kill old processes ---
echo  [5/5] Cleaning old processes on port %USER_PORT%...
for /f "tokens=5" %%a in ('netstat -aon 2^>nul ^| findstr ":%USER_PORT% " ^| findstr "LISTENING"') do (
    echo        Killing PID %%a
    taskkill /F /PID %%a >nul 2>&1
)
echo  [OK] Port %USER_PORT% ready

:: --- Launch ---
echo.
echo  ============================================================
echo   Launching on http://localhost:%USER_PORT%/
echo   Press Ctrl+C to stop
echo  ============================================================
echo.

:: --- Step 6: Database availability, migration & check ---
:: اولویت متغیرها: متغیر محیطی شل > فایل .env > پیش‌فرض (sqlite)
set "ENV_DB_BACKEND=%DB_BACKEND%"
set "ENV_PG_DSN=%PG_DSN%"
set "DB_BACKEND="
if exist "%CD%\.env" for /f "usebackq tokens=1,* delims==" %%a in ("%CD%\.env") do (
    if /i "%%a"=="DB_BACKEND" if not defined ENV_DB_BACKEND set "DB_BACKEND=%%~b"
    if /i "%%a"=="PG_DSN"     if not defined ENV_PG_DSN     set "PG_DSN=%%~b"
)
if not defined DB_BACKEND set "DB_BACKEND=sqlite"

echo  [DB] Checking database (DB_BACKEND=%DB_BACKEND%)...
if /i "%DB_BACKEND%"=="postgresql" (
    :: ۱) تشخیص خودکار سرویس PostgreSQL در حال اجرا (مستقل از نسخه 16، 17، 18 و ...)
    set "PG_SERVICE="
    for /f "tokens=2" %%s in ('sc query ^| findstr /i "SERVICE_NAME" ^| findstr /i "postgres"') do (
        if not defined PG_SERVICE set "PG_SERVICE=%%s"
    )
    if not defined PG_SERVICE (
        set "PG_STOPPED_SERVICE="
        for /f "tokens=2" %%s in ('sc query state^= all ^| findstr /i "SERVICE_NAME" ^| findstr /i "postgres"') do (
            if not defined PG_STOPPED_SERVICE set "PG_STOPPED_SERVICE=%%s"
        )
        if defined PG_STOPPED_SERVICE (
            echo  [DB] ERROR: PostgreSQL service %PG_STOPPED_SERVICE% is stopped.
            echo  [DB] خطا: سرویس PostgreSQL با نام %PG_STOPPED_SERVICE% متوقف است.
            echo  [DB] Fix: net start %PG_STOPPED_SERVICE%
            echo  [DB] راه‌حل: net start %PG_STOPPED_SERVICE%
        ) else (
            echo  [DB] ERROR: No running PostgreSQL service found on this machine.
            echo  [DB] خطا: هیچ سرویس فعال PostgreSQL در این سیستم یافت نشد.
            echo  [DB] Fix: Ensure PostgreSQL is installed and its service is running.
            echo  [DB] راه‌حل: بررسی کنید که PostgreSQL نصب و سرویس آن فعال باشد.
        )
        popd
        pause
        exit /b 1
    )
    echo  [DB] PostgreSQL service '%PG_SERVICE%' is RUNNING / سرویس فعال است

    :: ۲) بررسی وجود DSN
    if not defined PG_DSN (
        echo  [DB] ERROR: PG_DSN is not set - neither environment nor .env.
        echo  [DB] خطا: متغیر PG_DSN تنظیم نشده است - نه در محیط، نه در .env.
        echo  [DB] Example: PG_DSN=postgresql://postgres:123456@127.0.0.1:5432/printguard
        popd
        pause
        exit /b 1
    )

    :: ۳) اجرای اسکیما و مایگریشن‌های Alembic
    echo  [DB] Applying Alembic schema migrations...
    echo  [DB] در حال اعمال اسکیمای پایگاه‌داده با Alembic...
    set "ALEMBIC_DB_URL=%PG_DSN%"
    "%PY_CMD%" -m alembic upgrade head
    if errorlevel 1 (
        echo  [DB] ERROR: Alembic schema migration failed!
        echo  [DB] خطا: اعمال اسکیمای Alembic با خطا مواجه شد.
        popd
        pause
        exit /b 1
    )
    echo  [DB] Alembic schema is ready / اسکیمای دیتابیس آماده است

    :: ۴) اجرای مهاجرت خودکار و امن داده‌ها با بررسی ایمنی
    echo  [DB] Checking data auto-migration (tools\auto_migrate.py)...
    echo  [DB] در حال بررسی و مهاجرت امن داده‌ها...
    "%PY_CMD%" "%CD%\tools\auto_migrate.py"
    if errorlevel 1 (
        echo  [DB] ERROR: Auto-migration failed or was stopped by safety checks!
        echo  [DB] خطا: مهاجرت خودکار داده‌ها متوقف شد یا توسط بررسی امنیتی لغو شد!
        popd
        pause
        exit /b 1
    )
    echo  [DB] PostgreSQL is fully ready / PostgreSQL کاملاً آماده است
) else (
    if not exist "%CD%\logs.db" (
        echo  [DB] SQLite file logs.db does not exist; it will be created.
        echo  [DB] فایل logs.db وجود ندارد، ساخته می‌شود
    ) else (
        echo  [DB] SQLite file logs.db is present / فایل logs.db موجود است
    )
)

title PrintGuard [Port %USER_PORT%]

start "" powershell -NoProfile -WindowStyle Hidden -Command "Start-Sleep 3; Start-Process 'http://localhost:%USER_PORT%/'"

"%PY_CMD%" run.py

echo.
echo  PrintGuard stopped.
popd
pause
exit /b 0

:folder_error
echo [ERROR] Cannot access project folder.
pause
exit /b 1

:python_error
echo.
echo  [ERROR] Python not found or not working!
echo.
echo  -----------------------------------------------------------
echo  Diagnosis:
echo    The file .venv\Scripts\python.exe exists but may be broken.
echo    This happens when Python was uninstalled or moved.
echo.
echo  Fix (choose one):
echo.
echo    Option A - Reinstall Python (recommended):
echo      1. Download Python 3.10+: https://www.python.org/downloads/
echo      2. Run installer and CHECK "Add Python to PATH"
echo      3. Delete the .venv folder, then run start.bat again
echo.
echo    Option B - Use system Python:
echo      Open CMD and run:
echo        python --version
echo      If that works, delete the .venv folder and rerun start.bat
echo.
echo    Option C - Recreate venv (if Python is installed):
echo      Open CMD in this folder and run:
echo        rmdir /s /q .venv
echo        python -m venv .venv
echo        .venv\Scripts\pip install -r requirements.txt
echo.
echo  -----------------------------------------------------------
echo  Debug info: check %CD%\.venv\Scripts\python.exe
echo.
pause
exit /b 1

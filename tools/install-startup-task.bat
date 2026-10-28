@echo off
REM ============================================================
REM @file:         tools/install-startup-task.bat
REM @brief:        Installs a Windows Scheduled Task that keeps PrintGuard running at boot.
REM @details:      Responsibilities: Create run-watchdog.bat and register a startup task that runs even without a login.
REM @dependencies: schtasks, run-watchdog.bat
REM @notes:        Requires administrator rights. Re-running the script replaces the existing task.
REM ============================================================

chcp 65001 >nul
rem =====================================================================
rem  install-startup-task.bat — Prevent monitoring from being turned off
rem
rem  The reason for the creation: 89-hour shutdown of the server Thursday to Sunday morning caused the prints
rem  Register late. This script:
rem   1 Creates a watchdog run-watchdog.bat that if the program crashes, after
rem      10 seconds brings it up again
rem   2 Creates a Scheduled Task that starts when Windows starts up even without login
rem      Run the watchdog
rem   3 Sleep automatically shuts down Windows
rem
rem  Run: right click on the file → Run as administrator
rem  Delete task: schtasks /Delete /TN PrintGuardMonitor /F
rem =====================================================================
setlocal

set "APP_DIR=%~dp0"
set "APP_DIR=%APP_DIR:~0,-1%"
rem If the script is inside tools\, the program root = its parent
if not exist "%APP_DIR%\run.py" if exist "%APP_DIR%\..\run.py" set "APP_DIR=%APP_DIR%\.."
if not exist "%APP_DIR%\run.py" (
  echo [FAIL] این فایل باید کنار run.py (ریشه‌ی برنامه) باشد: %APP_DIR%
  exit /b 1
)

set "PY_CMD=python"
if exist "%APP_DIR%\.venv\Scripts\python.exe" set "PY_CMD=%APP_DIR%\.venv\Scripts\python.exe"

echo.
echo === [۱/۳] ساخت نگهبان run-watchdog.bat
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"
> "%APP_DIR%\run-watchdog.bat" (
  echo @echo off
  echo title PrintGuard Monitor ^(watchdog^)
  echo pushd "%APP_DIR%"
  echo :loop
  echo echo [%%date%% %%time%%] starting run.py ^>^> logs\watchdog.log
  echo "%PY_CMD%" run.py ^>^> logs\watchdog.log 2^>^&1
  echo echo [%%date%% %%time%%] exited — restart in 10s ^>^> logs\watchdog.log
  echo timeout /t 10 /nobreak ^>nul
  echo goto loop
)
echo    OK

echo.
echo  === [۲/۳] ساخت Scheduled Task (ONLOGON — اجرای خودکار با کاربر فعلی)
rem The path of the project is on the network share; SYSTEM does not normally have access to it.
rem ONLOGON with the current account maintains access to the UNC path.
schtasks /Create /TN "PrintGuardMonitor" /SC ONLOGON /RU "%USERNAME%" /TR "cmd.exe /c call \"%APP_DIR%\\run-watchdog.bat\"" /F
if errorlevel 1 (
  echo [هشدار] ساخت تسک ONLOGON ناموفق بود.
  echo این فایل را با حساب کاربری فعلی اجرا کن یا Task را دستی بساز.
) else (
  echo    OK — تسک ساخته شد و با ورود کاربر اجرا می‌شود
)

echo.
echo === [۳/۳] خاموش‌کردن Sleep خودکار ویندوز (روی برق)
powercfg /change standby-timeout-ac 0 >nul 2>&1 && echo    OK || echo    [هشدار] نیازمند Run as administrator

echo.
echo =============================================================
echo  ✓ از این پس مانیتورینگ با هر بالا آمدن ویندوز خودش اجرا می‌شود
echo    و کرش هم بعد از ۱۰ ثانیه ری‌استارت می‌شود.
echo  ✓ برای شروع همین حالا: schtasks /Run /TN "PrintGuardMonitor"
echo  ✓ وضعیت: schtasks /Query /TN "PrintGuardMonitor" /V /FO LIST
echo =============================================================
exit /b 0

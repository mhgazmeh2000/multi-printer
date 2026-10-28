@echo off
REM ============================================================
REM @file:         tools/install-weekly-audit.bat
REM @brief:        Installs a weekly Scheduled Task that audits log and counter consistency.
REM @details:      Responsibilities: Register the Saturday 08:00 audit and append its output to logs\audit-history.log.
REM @dependencies: schtasks, tools/audit_pages.py
REM @notes:        Requires administrator rights. Run the audit manually with schtasks /Run /TN PrintGuardWeeklyAudit.
REM ============================================================

chcp 65001 >nul
rem =====================================================================
rem  install-weekly-audit.bat — Automatic weekly log/counter consistency audit
rem
rem  Creates a Scheduled Task that audits the last 7 days every Saturday at 8 AM
rem  Run and append the output to logs\audit-history.log.
rem
rem  Run: right click on the file → Run as administrator
rem  Run the audit manually: schtasks /Run /TN PrintGuardWeeklyAudit
rem  Delete task: schtasks /Delete /TN PrintGuardWeeklyAudit /F
rem =====================================================================
setlocal

set "APP_DIR=%~dp0"
set "APP_DIR=%APP_DIR:~0,-1%"
rem If the script is inside tools\, the program root = its parent
if not exist "%APP_DIR%\run.py" if exist "%APP_DIR%\..\run.py" set "APP_DIR=%APP_DIR%\.."
if not exist "%APP_DIR%\tools\audit_pages.py" (
  echo [FAIL] این فایل باید کنار tools\audit_pages.py ^(ریشه‌ی برنامه^) باشد
  exit /b 1
)
set "PY_CMD=python"
if exist "%APP_DIR%\.venv\Scripts\python.exe" set "PY_CMD=%APP_DIR%\.venv\Scripts\python.exe"
if not exist "%APP_DIR%\logs" mkdir "%APP_DIR%\logs"

echo === [۱/۲] ساخت اجراکننده‌ی run-weekly-audit.bat
> "%APP_DIR%\run-weekly-audit.bat" (
  echo @echo off
  echo title PrintGuard Weekly Audit
  echo cd /d "%APP_DIR%"
  echo echo [%%date%% %%time%%] weekly audit start ^>^> logs\audit-history.log
  echo "%PY_CMD%" tools\audit_pages.py --days 7 --md audit-weekly.md ^>^> logs\audit-history.log 2^>^&1
  echo echo [%%date%% %%time%%] done ^>^> logs\audit-history.log
)
echo    OK

echo === [۲/۲] ساخت Scheduled Task هفتگی
schtasks /Create /TN "PrintGuardWeeklyAudit" /SC WEEKLY /D SAT /ST 08:00 /RU SYSTEM /RL HIGHEST /F /TR "'%APP_DIR%\run-weekly-audit.bat'"
if errorlevel 1 (echo [FAIL] ساخت تسک ناموفق ^(Run as administrator؟^) & exit /b 1)

echo.
echo ✓ تسک هفتگی ساخته شد: هر شنبه ۰۸:۰۰ ممیزی ۷روز اخیر
echo ✓ گزارش: audit-weekly.md  ^|  تاریخچه‌ی اجراها: logs\audit-history.log
echo ✓ اجرای همین حالا: schtasks /Run /TN "PrintGuardWeeklyAudit"
exit /b 0

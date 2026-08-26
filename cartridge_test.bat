@echo off
setlocal EnableExtensions
REM ============================================================================
REM  cartridge_test.bat -- guided diagnostics for automatic cartridge-change
REM  detection ( Canon refill case, but works for every brand ).
REM
REM  Usage:   cartridge_test.bat <printer_ip> [brand] [extra_log_file]
REM  Example: cartridge_test.bat 172.16.0.43 canon
REM
REM  What it does (3 steps):
REM    STEP 1: run tools\probe_cartridge_id.py + tools\diagnose_printer.py
REM    STEP 2: read manual_override + newest cartridge events from logs.db
REM    STEP 3: search service/watchdog log files for this device IP
REM
REM  Output files produced next to this script (send them back):
REM    probe_identity_<ip>.md , diagnose_<ip>.md , probe_web_<ip>_*.html
REM ============================================================================

set "IP=%~1"
set "BRAND=%~2"
if "%BRAND%"=="" set "BRAND=canon"
set "EXTRA_LOG=%~3"

echo ============================================================================
echo   CARTRIDGE-CHANGE DIAGNOSTICS  ^|  ip=%IP% brand=%BRAND%
echo   folder: %CD%
echo ============================================================================

if "%IP%"=="" (
    echo.
    echo  ERROR: printer IP is required.
    echo  Usage: %~nx0 ^<printer_ip^> [brand] [extra_log_file]
    echo  brand: canon ^(default^), hp, brother, toshiba
    echo.
    pause
    exit /b 1
)

REM ---- locate python (project venv first, then system python) --------------
set "PY="
if exist "venv\Scripts\python.exe" set "PY=venv\Scripts\python.exe"
if not defined PY (
    where python >nul 2>nul && set "PY=python"
)
if not defined PY (
    echo  ERROR: Python not found. Activate the project venv or install Python.
    pause
    exit /b 1
)
echo using python: %PY%

REM ============================================================================
echo.
echo === STEP 1/3: probe identity ^+ full diagnose ============================
if not exist "tools\probe_cartridge_id.py" (
    echo  ERROR: tools\probe_cartridge_id.py not found!
    echo  ^>^> The project code is outdated -- update it first, then re-run.
    pause
    exit /b 1
)
%PY% tools\probe_cartridge_id.py %IP% --brand %BRAND%
if errorlevel 1 echo  [warn] probe exited with an error (see lines above^)
%PY% tools\diagnose_printer.py %IP%
if errorlevel 1 echo  [warn] diagnose exited with an error (see lines above^)

REM ============================================================================
echo.
echo === STEP 2/3: manual_override + cartridge events from logs.db ============
if not exist "tools\cartridge_test_step2.py" (
    echo  [warn] tools\cartridge_test_step2.py not found -- skipped
) else (
    %PY% tools\cartridge_test_step2.py %IP% logs.db
    if errorlevel 1 echo  [warn] step2 reported a problem (see lines above^)
)

REM ============================================================================
echo.
echo === STEP 3/3: search service / watchdog logs for %IP% ===================
set "LOGCANDIDATES=printer-monitor.log monitor.log run.log run-watchdog.log watchdog.log service.log console.log app.log out.log toner_report.txt"
set "ANYLOG="
for %%F in (%LOGCANDIDATES%) do (
    if exist "%%~F" (
        set "ANYLOG=1"
        echo   -- %%~F :
        findstr /I /N /C:"%IP% candidate" /C:"cartridge" /C:"CARTRIDGE_CHANGED" /C:"REFILL" "%%~F" 2>nul || echo      ^(no matches^)
    )
)
if not "%EXTRA_LOG%"=="" (
    if exist "%EXTRA_LOG%" (
        set "ANYLOG=1"
        echo   -- %EXTRA_LOG% :
        findstr /I /N /C:"%IP% candidate" /C:"cartridge" /C:"CARTRIDGE_CHANGED" /C:"REFILL" "%EXTRA_LOG%" 2>nul || echo      ^(no matches^)
    ) else (
        echo   [warn] extra log file not found: %EXTRA_LOG%
    )
)
if not defined ANYLOG (
    echo   [info] No known log file found next to the project.
    echo   If the service runs via run-watchdog.bat with a console window,
    echo   just copy/paste the last ~50 console lines of the service window.
    echo   Or re-run: %~nx0 %IP% %BRAND% ^<path\to\your\logfile^>
)

REM ============================================================================
echo.
echo === DONE ==================================================================
echo  Please send back all of the following:
echo    1) this console output ^(copy/paste^)
echo    2) probe_identity_%IP%.md
echo    3) diagnose_%IP%.md
echo    4) probe_web_%IP%_*.html ^(if any were created^)
echo ============================================================================
pause
endlocal

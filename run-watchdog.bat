@echo off
REM ============================================================
REM @file:         run-watchdog.bat
REM @brief:        Windows watchdog loop that restarts PrintGuard if it crashes.
REM @details:      Responsibilities: Run run.py in a loop, logging to logs\watchdog.log, and restart it 10 seconds after it exits.
REM @dependencies: python, run.py
REM @notes:        None.
REM ============================================================

setlocal
pushd "%~dp0"
:loop
"python" run.py >> logs\watchdog.log 2>&1
 timeout /t 10 /nobreak >nul
goto loop

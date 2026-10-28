@echo off
REM ============================================================
REM @file:    start.bat
REM @brief:   Launcher for start.ps1. The start logic lives in start.ps1.
REM @notes:   Keep this file ASCII only. cmd.exe mis-parses non-ASCII text
REM           inside batch files; Persian text is in start.ps1 (UTF-8 with BOM).
REM ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1"
set "START_RC=%ERRORLEVEL%"
echo.
pause
exit /b %START_RC%

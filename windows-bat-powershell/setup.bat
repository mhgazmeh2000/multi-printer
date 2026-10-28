@echo off
REM ============================================================
REM @file:    setup.bat
REM @brief:   Launcher for setup.ps1. The setup logic lives in setup.ps1.
REM @notes:   Keep this file ASCII only. cmd.exe mis-parses non-ASCII text
REM           inside batch files; Persian text is in setup.ps1 (UTF-8 with BOM).
REM ============================================================
chcp 65001 >nul
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
set "SETUP_RC=%ERRORLEVEL%"
echo.
pause
exit /b %SETUP_RC%

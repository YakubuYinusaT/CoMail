@echo off
rem Double-click to run Cohandle on this machine. No server, no bill.
title Cohandle
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on this PC.
  echo Install it from python.org, tick "Add python.exe to PATH", then run this again.
  pause
  exit /b 1
)

python scripts\desktop.py
echo.
pause

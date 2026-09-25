@echo off
rem Double-click to connect Zoho. Nothing sends as a result of this.
rem
rem It runs from this file's own folder, so it does not matter where you
rem start it from. Running scripts\zoho_setup.py by hand from the wrong
rem directory is the one way to make this fail.
title Cohandle - connect Zoho
cd /d "%~dp0"

where python >nul 2>&1
if errorlevel 1 (
  echo Python was not found on this PC.
  echo Install it from python.org, tick "Add python.exe to PATH", then run this again.
  echo.
  pause
  exit /b 1
)

python scripts\zoho_setup.py
echo.
pause

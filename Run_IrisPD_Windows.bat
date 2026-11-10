@echo off
setlocal
cd /d "%~dp0"
title IrisPD - Setup and launch
where py >nul 2>nul
if errorlevel 1 (
  echo Python launcher not found. Install 64-bit Python 3.11 from https://www.python.org/downloads/
  echo Ensure the Python launcher is installed, then run this file again.
  pause
  exit /b 1
)
py -3.11 -c "import sys; assert sys.maxsize == 9223372036854775807" >nul 2>nul
if errorlevel 1 (
  echo IrisPD requires 64-bit Python 3.11. Install it and try again.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  echo Creating local Python virtual environment...
  py -3.11 -m venv .venv
  if errorlevel 1 goto :error
)
if not exist ".venv\installed.ok" (
  echo Installing local dependencies on first launch...
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 goto :error
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto :error
  echo ok>".venv\installed.ok"
)
".venv\Scripts\python.exe" pd_app.py
if errorlevel 1 goto :error
exit /b 0
:error
echo.
echo Setup or startup failed. Review the error above.
pause
exit /b 1

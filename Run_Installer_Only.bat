@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 exit /b 1
py -3.11 -c "import sys; assert sys.maxsize == 9223372036854775807" >nul 2>nul
if errorlevel 1 exit /b 1
if not exist ".venv\Scripts\python.exe" (
  py -3.11 -m venv .venv
  if errorlevel 1 exit /b 1
)
if not exist ".venv\installed.ok" (
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  if errorlevel 1 exit /b 1
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 exit /b 1
  echo ok>".venv\installed.ok"
)
exit /b 0

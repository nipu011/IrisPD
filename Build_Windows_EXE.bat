@echo off
setlocal
cd /d "%~dp0"
title IrisPD - Build Windows EXE
call Run_Installer_Only.bat
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m pip install "pyinstaller>=6.10,<7"
if errorlevel 1 goto :error
".venv\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onedir --windowed ^
  --name IrisPD ^
  --icon irispd_icon.ico ^
  --add-data "irispd_icon.png;." ^
  --add-data "IrisPD_Help.pdf;." ^
  --collect-all mediapipe ^
  pd_app.py
if errorlevel 1 goto :error
echo.
echo Built executable: dist\IrisPD\IrisPD.exe
echo Share the WHOLE dist\IrisPD directory, not only the EXE.
pause
exit /b 0
:error
echo Build failed. Review the messages above.
pause
exit /b 1

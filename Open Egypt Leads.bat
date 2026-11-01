@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\pythonw.exe" (
  echo First-time setup...
  call "%~dp0start.bat"
  exit /b 0
)

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

start "" ".venv\Scripts\pythonw.exe" -m app.desktop
exit /b 0

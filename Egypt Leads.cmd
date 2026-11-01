@echo off
setlocal
cd /d "%~dp0"

REM Prefer the reliable Python desktop window (same UI as the .exe)
if exist ".venv\Scripts\pythonw.exe" (
  if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul
  start "" ".venv\Scripts\pythonw.exe" -m app.desktop
  exit /b 0
)

if exist "dist\EgyptLeads\EgyptLeads.exe" (
  start "" "dist\EgyptLeads\EgyptLeads.exe"
  exit /b 0
)

echo First-time setup needed.
call "%~dp0start.bat"

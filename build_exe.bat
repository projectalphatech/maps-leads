@echo off
setlocal
cd /d "%~dp0"

echo Building EgyptLeads desktop app...
echo.

if not exist ".venv\Scripts\python.exe" (
  echo Run start.bat once first to create the environment.
  pause
  exit /b 1
)

".venv\Scripts\python.exe" -m pip install -q pyinstaller aiosqlite

if exist "build" rmdir /s /q "build" >nul 2>&1
if exist "dist\EgyptLeads" rmdir /s /q "dist\EgyptLeads" >nul 2>&1

".venv\Scripts\pyinstaller.exe" --noconfirm EgyptLeads.spec

if errorlevel 1 (
  echo Build failed.
  pause
  exit /b 1
)

echo.
echo Done: dist\EgyptLeads\EgyptLeads.exe
echo.
pause

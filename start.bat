@echo off
setlocal
cd /d "%~dp0"

echo ============================================
echo   Egypt Lead Scraper
echo ============================================
echo.

if not exist ".venv\Scripts\python.exe" (
  echo [1/3] Creating virtual environment...
  python -m venv .venv
  if errorlevel 1 (
    echo Install Python 3.11+ from python.org first.
    pause
    exit /b 1
  )
  echo [2/3] Installing dependencies...
  ".venv\Scripts\python.exe" -m pip install -e .
  echo [3/3] Installing Chromium...
  ".venv\Scripts\python.exe" -m playwright install chromium
  echo.
  echo Setup done.
)

if not exist ".env" if exist ".env.example" copy ".env.example" ".env" >nul

echo Opening the desktop app window...
start "" ".venv\Scripts\pythonw.exe" -m app.desktop
echo.
echo Tip: run build_exe.bat once to create dist\EgyptLeads.exe
echo Then you can double-click EgyptLeads.exe like a normal app.
timeout /t 3 >nul
exit /b 0

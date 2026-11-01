@echo off
setlocal
cd /d "%~dp0"

echo Stopping Egypt Lead Scraper...

taskkill /F /IM EgyptLeads.exe >nul 2>&1

for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8787" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)

echo Stopped.
timeout /t 2 >nul

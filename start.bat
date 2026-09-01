@echo off
cd /d "%~dp0"
echo.
echo Starting YouTube Downloader...
echo.

echo Stopping old servers on port 5050...
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":5050" ^| findstr "LISTENING"') do (
  taskkill /F /PID %%a >nul 2>&1
)
timeout /t 2 /nobreak >nul

netsh advfirewall firewall show rule name="YouTube Downloader 5050" >nul 2>&1
if errorlevel 1 (
  echo.
  echo WARNING: Firewall rule not found.
  echo Other PCs cannot connect until you run add-firewall.bat as Administrator.
  echo.
)

python app.py
pause

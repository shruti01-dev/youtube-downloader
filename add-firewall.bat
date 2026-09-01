@echo off
cd /d "%~dp0"
echo.
echo ========================================
echo   YouTube Downloader - LAN Setup
echo ========================================
echo.

netsh advfirewall firewall delete rule name="YouTube Downloader 5050" >nul 2>&1
netsh advfirewall firewall add rule name="YouTube Downloader 5050" dir=in action=allow protocol=TCP localport=5050 profile=any enable=yes

if errorlevel 1 (
  echo [FAILED] Could not add firewall rule.
  echo Right-click this file and choose "Run as administrator".
  echo.
  pause
  exit /b 1
)

echo [OK] Firewall rule added for TCP port 5050.
echo Other PCs on the same Wi-Fi can now connect.
echo.
pause

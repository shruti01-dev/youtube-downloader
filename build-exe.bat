@echo off
cd /d "%~dp0"
echo.
echo Building YouTube Downloader desktop app ...
echo.

python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -U pyinstaller pywebview
python -m PyInstaller --noconfirm YouTubeDownloader.spec

if exist "dist\YouTube Downloader\YouTube Downloader.exe" (
  echo.
  echo DONE!
  echo App folder: dist\YouTube Downloader\
  echo.
  echo To make a Setup.exe that people can install:
  echo   build-installer.bat
  echo.
) else (
  echo Build failed.
)

pause

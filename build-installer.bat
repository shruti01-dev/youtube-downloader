@echo off
setlocal
cd /d "%~dp0"

echo.
echo Building YouTube Downloader desktop installer ...
echo.

python -m pip install -U pip
python -m pip install -r requirements.txt
python -m pip install -U pyinstaller pywebview
python -m PyInstaller --noconfirm --clean YouTubeDownloader.spec
if errorlevel 1 exit /b 1

set ISCC=
if exist "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist "%ProgramFiles%\Inno Setup 6\ISCC.exe" set "ISCC=%ProgramFiles%\Inno Setup 6\ISCC.exe"
if exist "%LocalAppData%\Programs\Inno Setup 6\ISCC.exe" set "ISCC=%LocalAppData%\Programs\Inno Setup 6\ISCC.exe"
if "%ISCC%"=="" (
  echo Inno Setup not found. Install with:
  echo   winget install --id JRSoftware.InnoSetup -e
  echo Then run this file again.
  exit /b 1
)

"%ISCC%" "installer\youtube_downloader.iss"
echo.
echo Installer: installer_output\YouTube-Downloader-Setup.exe
echo Users can install this like a normal Windows app.
echo Downloaded videos save in Downloads\YouTube Downloader.
endlocal

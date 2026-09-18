# YouTube Downloader

Desktop app to download YouTube videos and playlists.

## Download

### Windows (available now)

[Download YouTube Downloader for Windows](https://github.com/shruti01-dev/youtube-downloader/releases/latest/download/YouTube-Downloader-Setup.exe)

1. Download the Setup file
2. Run it → Next → Install (no admin password)
3. Open **YouTube Downloader** from the desktop
4. Paste a YouTube link and download

Videos save in the PC **Downloads** folder as H.264 MP4.

### Mac

Coming next. Not available yet.

## Run from source

```bash
pip install -r requirements.txt
python desktop_app.py
```

## Build Windows installer

1. Install [Inno Setup 6](https://jrsoftware.org/isinfo.php) if needed: `winget install --id JRSoftware.InnoSetup -e`
2. Double-click `build-installer.bat`

Output:

```text
installer_output/YouTube-Downloader-Setup.exe
```

## Notes

- ffmpeg is included in the Windows app
- YouTube changes often, so rebuild a new Setup.exe after updating `yt-dlp` if downloads start failing

import os
import random
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from flask import Flask, after_this_request, abort, jsonify, request, send_from_directory
from yt_dlp import YoutubeDL


def resource_root():
    """Bundled files (templates) — inside the .exe extract folder when frozen."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def app_data_root():
    """Writable folder next to the .exe (or project folder in dev)."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


RESOURCE_ROOT = resource_root()
ROOT = app_data_root()
PORT = int(os.environ.get("PORT", "5050"))
CLIENT_ID_RE = re.compile(r"^[a-f0-9-]{36}$", re.I)
OUR_FILE_RE = re.compile(
    r"(?:\[([A-Za-z0-9_-]{11})\]|-([A-Za-z0-9_-]{11}))\.(mp4|mp3)$",
    re.I,
)
# On free cloud hosts, keep files only briefly then send to the user device.
CLOUD_FILE_TTL_SEC = int(os.environ.get("CLOUD_FILE_TTL_SEC", "900"))


def windows_downloads_dir():
    """Real Windows Downloads folder, not a folder inside this project."""
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\Shell Folders",
        ) as key:
            path = winreg.QueryValueEx(key, "{374DE290-123F-4565-9164-39C4925E467B}")[0]
            if path:
                folder = Path(path)
                if folder.is_dir():
                    return folder
    except OSError:
        pass
    folder = Path.home() / "Downloads"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def state_dir():
    """Internal files (archive). Keep these out of the user's Downloads folder."""
    folder = ROOT / ".appdata"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


DOWNLOAD_DIR = (
    (windows_downloads_dir() / "YouTube Downloader")
    if os.name == "nt"
    and not bool(
        os.environ.get("RENDER")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("FLY_APP_NAME")
        or os.environ.get("KOYEB_APP_ID")
    )
    else (ROOT / "downloads")
)
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)
STATE_DIR = state_dir()
CLIENTS_DIR = STATE_DIR / "clients"
CLIENTS_DIR.mkdir(parents=True, exist_ok=True)

FFMPEG_CANDIDATES = [
    ROOT / "tools",
    RESOURCE_ROOT / "tools",
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "Microsoft/WinGet/Packages/Gyan.FFmpeg_Microsoft.Winget.Source_8wekyb3d8bbwe/ffmpeg-9.0.1-full_build/bin",
    Path(r"C:\ffmpeg\bin"),
]


def find_ffmpeg():
    for folder in FFMPEG_CANDIDATES:
        if (folder / "ffmpeg.exe").exists() or (folder / "ffmpeg").exists():
            return str(folder)
    found = shutil.which("ffmpeg")
    if found:
        return str(Path(found).parent)
    try:
        import imageio_ffmpeg

        exe = imageio_ffmpeg.get_ffmpeg_exe()
        if exe and Path(exe).is_file():
            return str(Path(exe).parent)
    except Exception:
        pass
    return None


def ffmpeg_bin():
    if FFMPEG_DIR:
        for name in ("ffmpeg.exe", "ffmpeg"):
            path = Path(FFMPEG_DIR) / name
            if path.is_file():
                return str(path)
    return shutil.which("ffmpeg")


def is_windows_n():
    if os.name != "nt":
        return False
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"SOFTWARE\Microsoft\Windows NT\CurrentVersion",
        ) as key:
            edition = str(winreg.QueryValueEx(key, "EditionID")[0] or "")
            return edition.upper().endswith("N")
    except OSError:
        return False


def make_playable_mp4(path):
    """Re-encode to H.264 Main + AAC when Windows can use Photos/Films & TV."""
    src = Path(path)
    if src.suffix.lower() != ".mp4" or not src.is_file():
        return src
    # Windows N has no media codecs. Re-encoding will not make Photos play, and
    # leftover .tmp.mp4 files in Downloads look broken if opened mid-convert.
    if is_windows_n():
        return src
    exe = ffmpeg_bin()
    if not exe:
        return src
    tmp = STATE_DIR / (src.stem + ".tmp.mp4")
    cmd = [
        exe, "-y", "-i", str(src),
        "-c:v", "libx264", "-profile:v", "main", "-level", "4.0",
        "-pix_fmt", "yuv420p", "-preset", "veryfast", "-crf", "23",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-movflags", "+faststart", "-f", "mp4", str(tmp),
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True)
        if tmp.is_file() and tmp.stat().st_size > 0:
            tmp.replace(src)
    except (OSError, subprocess.CalledProcessError):
        safe_unlink(tmp)
    safe_unlink(tmp)
    return src


def open_with_player(path):
    """Windows N has no Media Player/Photos codecs. Open in Chrome/Edge instead."""
    path = Path(path)
    if os.name == "nt" and path.suffix.lower() in {".mp4", ".mp3", ".m4a", ".webm"}:
        browsers = []
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            browsers.extend([
                Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe",
                Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
            ])
        for browser in browsers:
            if browser.is_file():
                subprocess.Popen([str(browser), path.resolve().as_uri()], shell=False)
                return
        for base in (
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        ):
            player = Path(base) / "Windows Media Player" / "wmplayer.exe"
            if player.is_file():
                subprocess.Popen([str(player), str(path)], shell=False)
                return
    os.startfile(path)


def find_cookies_file():
    """Optional YouTube cookies for cloud hosts blocked by bot checks."""
    candidates = [
        os.environ.get("COOKIES_FILE", "").strip(),
        "/etc/secrets/cookies.txt",
        str(ROOT / "cookies.txt"),
        str(RESOURCE_ROOT / "cookies.txt"),
    ]
    for item in candidates:
        if not item:
            continue
        path = Path(item)
        if path.is_file() and path.stat().st_size > 0:
            return str(path)
    return None


def friendly_ytdlp_error(text):
    lower = (text or "").lower()
    if "sign in to confirm" in lower or "not a bot" in lower or "cookies" in lower:
        return "YouTube blocked this download (bot check). Wait a bit, then try another link."
    if "rate-limited" in lower or "try again later" in lower:
        return "YouTube rate-limited this IP. Wait a while, then try again."
    if "page needs to be reloaded" in lower:
        return "YouTube blocked this request. Wait a few seconds, then try the same link again."
    if "video is not available" in lower or "video unavailable" in lower or "this video is unavailable" in lower:
        return (
            "YouTube says this video is not available. "
            "It may be private, removed, region-blocked, or the link is incomplete. "
            "Open the video in Chrome first, then paste that full link."
        )
    if "private video" in lower:
        return "This video is private, so it cannot be downloaded."
    if "members only" in lower or "members-only" in lower:
        return "This video is members-only, so it cannot be downloaded."
    if "age" in lower and "restrict" in lower:
        return "This video is age-restricted, so it cannot be downloaded on this PC."
    if "requested format is not available" in lower:
        return "Could not get a playable MP4 for this video. Try 720p or another link."
    if "ffmpeg" in lower:
        return "Could not finish the MP4 file. Close the app, open it again, and retry."
    clean = re.sub(r"^ERROR:\s*(\[youtube\]\s*)?", "", text or "", flags=re.I).strip()
    return (clean or "Download failed")[:200]


FFMPEG_DIR = find_ffmpeg()
if FFMPEG_DIR:
    os.environ["PATH"] = FFMPEG_DIR + os.pathsep + os.environ.get("PATH", "")

app = Flask(__name__)
app.config["TEMPLATES_AUTO_RELOAD"] = True
app.jinja_env.auto_reload = True
jobs = {}
jobs_lock = threading.Lock()


@app.after_request
def add_lan_headers(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Client-Id"
    return response


@app.route("/api/<path:_path>", methods=["OPTIONS"])
@app.route("/health", methods=["OPTIONS"])
def cors_preflight(_path=None):
    return "", 204

def h264_format(max_height=None):
    """Prefer H.264 + AAC so the file plays on Windows, phones, and TVs."""
    h = f"[height<={max_height}]" if max_height else ""
    return (
        f"bestvideo{h}[vcodec^=avc1]+bestaudio[ext=m4a]/"
        f"bestvideo{h}[vcodec^=avc1]+bestaudio/"
        f"best{h}[ext=mp4][vcodec^=avc1]/"
        f"best{h}[vcodec^=avc1]/"
        f"best{h}[ext=mp4]/"
        f"bestvideo{h}[vcodec!*=av01][vcodec!*=vp9]+bestaudio/"
        f"best{h}/best"
    )


QUALITY_MAP = {
    "best": h264_format(),
    "1080": h264_format(1080),
    "720": h264_format(720),
    "480": h264_format(480),
    "audio": "ba/b",
}


def new_job(client_id):
    return {
        "status": "queued",
        "percent": 0,
        "speed": "",
        "eta": "",
        "title": "",
        "message": "Starting…",
        "files": [],
        "error": "",
        "skipped": 0,
        "ok_count": 0,
        "current": 0,
        "total": 0,
        "new_files": [],
        "client_id": client_id,
    }


class JobLogger:
    def __init__(self, job_id):
        self.job_id = job_id

    def debug(self, msg):
        pass

    def info(self, msg):
        pass

    def warning(self, msg):
        text = str(msg)
        if "rate-limited" in text.lower() or "try again later" in text.lower():
            with jobs_lock:
                jobs[self.job_id]["message"] = "YouTube rate-limited this IP. Waiting before next video…"

    def error(self, msg):
        text = str(msg)
        with jobs_lock:
            job = jobs[self.job_id]
            job["error"] = text[:300]
            if "rate-limited" in text.lower() or "try again later" in text.lower():
                job["message"] = "YouTube rate limit. Pausing, then skipping this video…"
            elif "unavailable" in text.lower():
                job["skipped"] += 1
                job["message"] = "Skipping unavailable video…"
            else:
                job["message"] = friendly_ytdlp_error(text)


def is_cloud_host():
    return bool(
        os.environ.get("RENDER")
        or os.environ.get("RAILWAY_ENVIRONMENT")
        or os.environ.get("FLY_APP_NAME")
        or os.environ.get("KOYEB_APP_ID")
    )


def safe_unlink(path):
    try:
        Path(path).unlink(missing_ok=True)
    except OSError:
        pass


def cleanup_cloud_files():
    """Delete old temp videos on cloud so free disk stays free."""
    if not is_cloud_host():
        return
    now = time.time()
    roots = [CLIENTS_DIR]
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                age = now - path.stat().st_mtime
                if age >= CLOUD_FILE_TTL_SEC:
                    safe_unlink(path)
            except OSError:
                continue


def cloud_cleanup_loop():
    while True:
        try:
            cleanup_cloud_files()
        except Exception:
            pass
        time.sleep(120)


def is_local_request():
    # On cloud (Render etc.) every visitor is remote — never treat as host PC.
    if is_cloud_host():
        return False
    addr = (request.remote_addr or "").replace("::ffff:", "")
    if addr in ("127.0.0.1", "::1"):
        return True
    try:
        if addr == get_lan_ip():
            return True
    except OSError:
        pass
    return False


def read_client_id():
    return (
        request.headers.get("X-Client-Id")
        or request.args.get("client_id")
        or request.cookies.get("client_id")
        or ""
    ).strip()


def request_client_id():
    if is_local_request():
        return "local-pc"
    client_id = read_client_id()
    if CLIENT_ID_RE.match(client_id):
        return client_id
    return None


def client_dir_for(client_id):
    if client_id == "local-pc":
        return DOWNLOAD_DIR
    folder = CLIENTS_DIR / client_id
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def require_client_id():
    client_id = request_client_id()
    if not client_id:
        return None, (jsonify({"ok": False, "error": "Missing or invalid client id."}), 401)
    return client_id, None


def list_client_files(client_dir):
    files = []
    seen_ids = {}
    if not client_dir.is_dir():
        return files
    for f in client_dir.iterdir():
        if not f.is_file() or not is_listable_download(f.name):
            continue
        try:
            stat = f.stat()
            entry = {"name": f.name, "size": stat.st_size, "mtime": stat.st_mtime}
            vid_match = OUR_FILE_RE.search(f.name)
            if vid_match:
                vid = vid_match.group(1) or vid_match.group(2)
                prev = seen_ids.get(vid)
                if prev and (prev["mtime"] > entry["mtime"] or prev["size"] >= entry["size"]):
                    continue
                seen_ids[vid] = entry
            else:
                files.append(entry)
        except OSError:
            continue
    files.extend(seen_ids.values())
    files.sort(key=lambda x: x["mtime"], reverse=True)
    return files


def progress_hook(job_id):
    def hook(d):
        with jobs_lock:
            job = jobs[job_id]
            total_videos = job.get("total") or 0
            current_video = job.get("current") or 0
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                downloaded = d.get("downloaded_bytes") or 0
                video_pct = round(downloaded / total * 100, 1) if total else 0
                job["status"] = "downloading"
                if total_videos > 1 and current_video > 0:
                    job["percent"] = round(
                        ((current_video - 1) / total_videos) * 100 + (video_pct / total_videos),
                        1,
                    )
                else:
                    job["percent"] = video_pct
                job["speed"] = d.get("_speed_str") or ""
                job["eta"] = d.get("_eta_str") or ""
                job["title"] = d.get("info_dict", {}).get("title") or job["title"]
                job["message"] = f"Downloading {job['title'] or 'video'}…"
            elif d.get("status") == "finished":
                job["message"] = "Merging / finishing file…"
                filename = d.get("filename")
                if filename:
                    job["files"].append(os.path.basename(filename))

    return hook


def base_opts(job_id, quality, mode, client_dir):
    fmt = QUALITY_MAP.get(quality, QUALITY_MAP["best"])
    name = (
        "%(playlist_index)03d - %(title).80s-%(id)s.%(ext)s"
        if mode == "playlist"
        else "%(title).80s-%(id)s.%(ext)s"
    )
    opts = {
        "format": fmt,
        "outtmpl": str(client_dir / name),
        "merge_output_format": "mp3" if quality == "audio" else "mp4",
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": mode == "playlist",
        "noprogress": True,
        "restrictfilenames": True,
        "windowsfilenames": True,
        "continuedl": True,
        "retries": 10,
        "fragment_retries": 10,
        "keepvideo": False,
        "socket_timeout": 30,
        "sleep_interval_requests": 1.0,
        "logger": JobLogger(job_id),
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        },
    }
    if mode == "playlist":
        opts["sleep_interval"] = 2
        opts["max_sleep_interval"] = 6
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    elif shutil.which("ffmpeg"):
        opts["ffmpeg_location"] = str(Path(shutil.which("ffmpeg")).parent)
    cookies = find_cookies_file()
    if cookies:
        opts["cookiefile"] = cookies
    have_ffmpeg = bool(opts.get("ffmpeg_location") or shutil.which("ffmpeg"))
    if quality == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif have_ffmpeg:
        opts["postprocessors"] = [
            {"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"},
        ]
        opts["postprocessor_args"] = {
            "Merger+ffmpeg": ["-movflags", "+faststart"],
            "VideoRemuxer+ffmpeg": ["-movflags", "+faststart"],
        }
    return opts


VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def clean_youtube_url(url):
    """Fix paste issues (like German ß) and keep a normal YouTube link."""
    text = (url or "").strip().replace("ß", "ss").replace("ẞ", "SS")
    parsed = urlparse(text)
    host = (parsed.netloc or "").lower()
    qs = parse_qs(parsed.query)
    playlist = (qs.get("list") or [""])[0]
    vid = ""
    if "youtu.be" in host:
        vid = parsed.path.strip("/").split("/")[0]
    else:
        vid = (qs.get("v") or [""])[0]
    if VIDEO_ID_RE.match(vid):
        out = f"https://www.youtube.com/watch?v={vid}"
        if playlist:
            out += f"&list={playlist}"
        return out
    if playlist and "youtube" in host:
        return f"https://www.youtube.com/playlist?list={playlist}"
    return text


def force_single_video_url(url):
    parsed = urlparse(url)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs.pop("list", None)
    qs.pop("index", None)
    qs.pop("start_radio", None)
    query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=query))


def has_video_id(url):
    parsed = urlparse(url)
    host = (parsed.netloc or "").lower()
    if "youtu.be" in host:
        return bool(parsed.path.strip("/"))
    qs = parse_qs(parsed.query)
    return bool((qs.get("v") or [""])[0])


def has_playlist_id(url):
    parsed = urlparse(url)
    if "/playlist" in (parsed.path or "").lower():
        return bool((parse_qs(parsed.query).get("list") or [""])[0])
    qs = parse_qs(parsed.query)
    return bool((qs.get("list") or [""])[0])


def validate_link_mode(url, mode):
    video = has_video_id(url)
    playlist = has_playlist_id(url)
    if mode == "single":
        if not video:
            return (
                False,
                "This link is not a single video. Select Playlist to download a playlist.",
            )
        return True, ""
    if mode == "playlist":
        if not playlist:
            return (
                False,
                "This link is not a playlist. Select One video to download a single video.",
            )
        return True, ""
    return False, "Invalid mode."


INTERMEDIATE_FILE_RE = re.compile(r"\.f\d+\.|\.part\.|\.ytdl|\.temp\.|\.frag\.", re.I)
FINAL_EXTENSIONS = {".mp4", ".mp3"}


def is_listable_download(filename):
    if filename == "archive.txt":
        return False
    if filename.endswith(".part"):
        return False
    if INTERMEDIATE_FILE_RE.search(filename):
        return False
    if Path(filename).suffix.lower() not in FINAL_EXTENSIONS:
        return False
    # Only show this app's files, not everything in the user's Downloads folder.
    return bool(OUR_FILE_RE.search(filename))


def listable_names(client_dir):
    if not client_dir.is_dir():
        return set()
    return {
        f.name
        for f in client_dir.iterdir()
        if f.is_file() and is_listable_download(f.name)
    }


def playlist_video_url(entry):
    if not entry:
        return None
    vid = entry.get("id") or entry.get("url")
    if not vid:
        return None
    if str(vid).startswith("http"):
        return vid
    return f"https://www.youtube.com/watch?v={vid}"


def run_download(job_id, url, mode, quality, client_id):
    client_dir = client_dir_for(client_id)
    before_names = listable_names(client_dir)
    try:
        if mode == "playlist":
            list_opts = {
                "extract_flat": True,
                "skip_download": True,
                "quiet": True,
                "no_warnings": True,
                "ignoreerrors": True,
                "sleep_interval_requests": 1.5,
                "logger": JobLogger(job_id),
            }
            with YoutubeDL(list_opts) as ydl:
                info = ydl.extract_info(url, download=False)
            title = (info or {}).get("title") or "Playlist"
            entries = [e for e in ((info or {}).get("entries") or []) if e]
            with jobs_lock:
                jobs[job_id]["title"] = title
                jobs[job_id]["status"] = "downloading"
                jobs[job_id]["total"] = len(entries)
                jobs[job_id]["current"] = 0
                jobs[job_id]["message"] = f"Playlist: {title} ({len(entries)} videos)"

            opts = base_opts(job_id, quality, "playlist", client_dir)
            opts["noplaylist"] = True
            with YoutubeDL(opts) as ydl:
                for i, entry in enumerate(entries, start=1):
                    video_url = playlist_video_url(entry)
                    if not video_url:
                        continue
                    with jobs_lock:
                        job = jobs[job_id]
                        job["current"] = i
                        total = job.get("total") or len(entries)
                        job["percent"] = round(((i - 1) / total) * 100, 1) if total else 0
                        job["message"] = f"Video {i}/{total}: {entry.get('title') or video_url}"
                    outtmpl = str(client_dir / f"{i:03d} - %(title).80s-%(id)s.%(ext)s")
                    opts["outtmpl"] = outtmpl
                    ydl.params["outtmpl"] = {"default": outtmpl}
                    before_video = listable_names(client_dir)
                    try:
                        ydl.download([video_url])
                        added = listable_names(client_dir) - before_video
                        with jobs_lock:
                            if added:
                                jobs[job_id]["ok_count"] += 1
                            else:
                                jobs[job_id]["skipped"] += 1
                            total = jobs[job_id].get("total") or len(entries)
                            jobs[job_id]["percent"] = round((i / total) * 100, 1) if total else 100
                    except Exception as exc:
                        err = str(exc)
                        with jobs_lock:
                            jobs[job_id]["skipped"] += 1
                            jobs[job_id]["message"] = friendly_ytdlp_error(err)
                        if "rate-limited" in err.lower() or "try again later" in err.lower():
                            wait = 90
                            with jobs_lock:
                                jobs[job_id]["message"] = f"Rate limited. Waiting {wait}s then continuing…"
                            time.sleep(wait)
                        continue
                    time.sleep(random.uniform(3, 8))
        else:
            opts = base_opts(job_id, quality, "single", client_dir)
            opts["noplaylist"] = True
            opts["ignoreerrors"] = False
            with YoutubeDL(opts) as ydl:
                with jobs_lock:
                    jobs[job_id]["status"] = "downloading"
                    jobs[job_id]["message"] = "Downloading video…"
                ydl.download([url])

        new_files = sorted(
            listable_names(client_dir) - before_names,
            key=lambda n: (client_dir / n).stat().st_mtime,
            reverse=True,
        )
        if quality != "audio":
            with jobs_lock:
                jobs[job_id]["message"] = "Making a Windows-playable MP4…"
            playable = []
            for name in new_files:
                src = client_dir / name
                if src.suffix.lower() == ".mp4":
                    make_playable_mp4(src)
                if src.is_file() and is_listable_download(src.name):
                    playable.append(src.name)
            new_files = playable or new_files
        listed = sorted(
            listable_names(client_dir),
            key=lambda n: (client_dir / n).stat().st_mtime,
            reverse=True,
        )
        with jobs_lock:
            job = jobs[job_id]
            job["files"] = listed[:40]
            job["new_files"] = new_files
            job["ok_count"] = len(new_files)
            job["percent"] = 100
            if new_files:
                job["status"] = "done"
                job["message"] = f"Finished. Saved {len(new_files)} file(s) to Downloads."
            elif not (job.get("error") or "").strip():
                job["status"] = "done"
                job["message"] = "Already saved in your Downloads folder."
            else:
                job["status"] = "error"
                detail = (job.get("error") or job.get("message") or "").strip()
                job["message"] = friendly_ytdlp_error(detail)
    except Exception as exc:
        err = str(exc)
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err
            jobs[job_id]["message"] = friendly_ytdlp_error(err)


def get_lan_ip():
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.connect(("8.8.8.8", 80))
        ip = sock.getsockname()[0]
        sock.close()
        return ip
    except OSError:
        return "127.0.0.1"


def network_info():
    lan = get_lan_ip()
    return {
        "port": PORT,
        "local_url": f"http://127.0.0.1:{PORT}",
        "lan_url": f"http://{lan}:{PORT}",
        "lan_ip": lan,
    }


@app.get("/")
def index():
    html = (RESOURCE_ROOT / "templates" / "index.html").read_text(encoding="utf-8")
    resp = app.make_response(html)
    resp.headers["Content-Type"] = "text/html; charset=utf-8"
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    resp.headers["Pragma"] = "no-cache"
    return resp


@app.get("/api/client-id")
def api_client_id():
    if is_local_request():
        return jsonify({"ok": True, "client_id": "local-pc", "is_local": True})
    existing = read_client_id()
    if CLIENT_ID_RE.match(existing):
        client_id = existing
    else:
        client_id = str(uuid.uuid4())
    resp = jsonify({"ok": True, "client_id": client_id, "is_local": False})
    resp.set_cookie("client_id", client_id, max_age=31536000, samesite="Lax", path="/")
    return resp


@app.get("/api/network")
def api_network():
    return jsonify({"ok": True, **network_info()})


@app.get("/health")
def health():
    return jsonify({"ok": True, "message": "Server is running", **network_info()})


@app.post("/api/download")
def start_download():
    client_id, err = require_client_id()
    if err:
        return err

    data = request.get_json(silent=True) or {}
    url = clean_youtube_url((data.get("url") or "").strip())
    mode = data.get("mode") or "single"
    quality = data.get("quality") or "best"
    if not re.search(r"youtube\.com|youtu\.be", url, re.I):
        return jsonify({"ok": False, "error": "Please paste a valid YouTube video or playlist link."}), 400
    if mode not in ("single", "playlist"):
        return jsonify({"ok": False, "error": "Invalid mode."}), 400
    if quality not in QUALITY_MAP:
        return jsonify({"ok": False, "error": "Invalid quality."}), 400

    ok, link_err = validate_link_mode(url, mode)
    if not ok:
        return jsonify({"ok": False, "error": link_err}), 400

    job_id = os.urandom(8).hex()
    if mode == "single":
        url = force_single_video_url(url)
        vid = (parse_qs(urlparse(url).query).get("v") or [""])[0]
        if not VIDEO_ID_RE.match(vid):
            return jsonify({
                "ok": False,
                "error": "This YouTube link looks incomplete. Open the video in Chrome, copy the full link, and paste it again.",
            }), 400
    with jobs_lock:
        jobs[job_id] = new_job(client_id)
    threading.Thread(
        target=run_download,
        args=(job_id, url, mode, quality, client_id),
        daemon=True,
    ).start()
    return jsonify({"ok": True, "job_id": job_id})


@app.get("/api/status/<job_id>")
def job_status(job_id):
    client_id, err = require_client_id()
    if err:
        return err
    with jobs_lock:
        job = jobs.get(job_id)
        if not job:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        if job.get("client_id") != client_id:
            return jsonify({"ok": False, "error": "Job not found"}), 404
        payload = {k: v for k, v in job.items() if k != "client_id"}
        return jsonify({"ok": True, **payload})


@app.get("/downloads/<client_id>/<path:filename>")
def get_file(client_id, filename):
    req_client_id, err = require_client_id()
    if err:
        return err
    if client_id != req_client_id:
        return jsonify({"ok": False, "error": "Forbidden"}), 403
    client_dir = client_dir_for(client_id).resolve()
    safe_name = Path(filename).name
    path = (client_dir / safe_name).resolve()
    if path.parent != client_dir or not path.is_file():
        abort(404)
    as_attachment = request.args.get("download") == "1"
    delete_after = as_attachment and (
        is_cloud_host() or request.args.get("purge") == "1"
    )

    if delete_after:
        @after_this_request
        def _purge(response):
            # After the file is sent to the user's PC/phone, free server storage.
            if response.status_code < 400:
                safe_unlink(path)
            return response

    return send_from_directory(
        client_dir,
        safe_name,
        as_attachment=as_attachment,
        mimetype="video/mp4" if path.suffix.lower() == ".mp4" else None,
        conditional=True,
    )


@app.post("/api/purge-file")
def purge_file():
    """Optional: delete a prepared file from cloud after the user saved it."""
    client_id, err = require_client_id()
    if err:
        return err
    if not is_cloud_host() and client_id == "local-pc":
        return jsonify({"ok": True, "skipped": True})
    data = request.get_json(silent=True) or {}
    name = Path(data.get("name") or "").name
    client_dir = client_dir_for(client_id).resolve()
    path = (client_dir / name).resolve()
    if path.parent != client_dir:
        return jsonify({"ok": False, "error": "Invalid file"}), 400
    safe_unlink(path)
    return jsonify({"ok": True})


@app.get("/api/files")
def list_files():
    client_id, err = require_client_id()
    if err:
        return err
    client_dir = client_dir_for(client_id)
    files = list_client_files(client_dir)
    return jsonify({
        "ok": True,
        "files": files,
        "client_id": client_id,
        "is_local": client_id == "local-pc",
        "is_cloud": is_cloud_host(),
        "folder": str(client_dir.resolve()) if client_id == "local-pc" else "",
    })


@app.post("/api/open-file")
def open_file():
    client_id, err = require_client_id()
    if err:
        return err
    if client_id != "local-pc":
        return jsonify({"ok": False, "error": "Only available on the host PC."}), 403
    data = request.get_json(silent=True) or {}
    name = Path(data.get("name") or "").name
    client_dir = client_dir_for(client_id).resolve()
    path = (client_dir / name).resolve()
    if path.parent != client_dir or not path.is_file():
        return jsonify({"ok": False, "error": "File not found"}), 404
    if os.name != "nt":
        return jsonify({"ok": False, "error": "Use Save to download the file."}), 403
    open_with_player(path)
    return jsonify({"ok": True})


@app.get("/api/open-folder")
@app.post("/api/open-folder")
def open_folder():
    client_id, err = require_client_id()
    if err:
        return err
    if client_id != "local-pc":
        return jsonify({"ok": False, "error": "Only available on the host PC."}), 403
    folder = str(client_dir_for(client_id).resolve())
    if os.name != "nt":
        return jsonify({"ok": True, "folder": folder})
    subprocess.Popen(["explorer", folder], shell=False)
    return jsonify({"ok": True, "folder": folder})


if __name__ == "__main__":
    info = network_info()
    print("YouTube Downloader running")
    print("On this PC:  " + info["local_url"])
    print("On your LAN: " + info["lan_url"])
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)


# Under gunicorn (Render), start cleanup once when the app module loads.
if is_cloud_host():
    threading.Thread(target=cloud_cleanup_loop, daemon=True).start()

import os
import random
import re
import shutil
import socket
import subprocess
import threading
import time
import uuid
from pathlib import Path

from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

from flask import Flask, abort, jsonify, request, send_from_directory
from yt_dlp import YoutubeDL

ROOT = Path(__file__).resolve().parent
DOWNLOAD_DIR = ROOT / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
CLIENTS_DIR = DOWNLOAD_DIR / "clients"
CLIENTS_DIR.mkdir(exist_ok=True)
PORT = int(os.environ.get("PORT", "5050"))
CLIENT_ID_RE = re.compile(r"^[a-f0-9-]{36}$", re.I)

FFMPEG_CANDIDATES = [
    ROOT / "tools",
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
    return None


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
    """Prefer H.264 MP4 so videos play in Films & TV and on phones."""
    h = f"[height<={max_height}]" if max_height else ""
    return (
        f"bestvideo{h}[vcodec^=avc1][ext=mp4]+bestaudio[ext=m4a]/"
        f"bestvideo{h}[vcodec^=avc1]+bestaudio/"
        f"best{h}[vcodec^=avc1]/"
        f"bestvideo{h}+bestaudio/best{h}/best"
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
            if "rate-limited" in text.lower() or "try again later" in text.lower():
                job["message"] = "YouTube rate limit. Pausing, then skipping this video…"
            elif "unavailable" in text.lower():
                job["skipped"] += 1
                job["message"] = "Skipping unavailable video…"
            else:
                job["message"] = text[:180]


def is_local_request():
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
            vid_match = re.search(r"\[([^\]]+)\]\.", f.name)
            if vid_match:
                vid = vid_match.group(1)
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
        "%(playlist_index)03d - %(title).80s [%(id)s].%(ext)s"
        if mode == "playlist"
        else "%(title).80s [%(id)s].%(ext)s"
    )
    opts = {
        "format": fmt,
        "outtmpl": str(client_dir / name),
        "merge_output_format": "mp3" if quality == "audio" else "mp4",
        "progress_hooks": [progress_hook(job_id)],
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": True,
        "restrictfilenames": False,
        "windowsfilenames": True,
        "continuedl": True,
        "retries": 5,
        "fragment_retries": 5,
        "download_archive": str(client_dir / "archive.txt"),
        "keepvideo": False,
        "socket_timeout": 30,
        "sleep_interval_requests": 1.5,
        "sleep_interval": 2,
        "max_sleep_interval": 6,
        "logger": JobLogger(job_id),
    }
    if FFMPEG_DIR:
        opts["ffmpeg_location"] = FFMPEG_DIR
    if quality == "audio":
        opts["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    else:
        opts["postprocessor_args"] = {
            "Merger+ffmpeg": ["-movflags", "+faststart"],
        }
    return opts


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
FINAL_EXTENSIONS = {".mp4", ".mp3", ".webm", ".mkv", ".m4a", ".opus"}


def is_listable_download(filename):
    if filename == "archive.txt":
        return False
    if filename.endswith(".part"):
        return False
    if INTERMEDIATE_FILE_RE.search(filename):
        return False
    return Path(filename).suffix.lower() in FINAL_EXTENSIONS


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
                    outtmpl = str(client_dir / f"{i:03d} - %(title).80s [%(id)s].%(ext)s")
                    opts["outtmpl"] = outtmpl
                    ydl.params["outtmpl"] = {"default": outtmpl}
                    try:
                        ydl.download([video_url])
                        with jobs_lock:
                            jobs[job_id]["ok_count"] += 1
                            total = jobs[job_id].get("total") or len(entries)
                            jobs[job_id]["percent"] = round((i / total) * 100, 1) if total else 100
                    except Exception as exc:
                        err = str(exc)
                        with jobs_lock:
                            jobs[job_id]["skipped"] += 1
                            jobs[job_id]["message"] = err[:180]
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
            with YoutubeDL(opts) as ydl:
                with jobs_lock:
                    jobs[job_id]["status"] = "downloading"
                    jobs[job_id]["message"] = "Downloading video…"
                ydl.download([url])
                with jobs_lock:
                    jobs[job_id]["ok_count"] += 1

        with jobs_lock:
            job = jobs[job_id]
            job["status"] = "done"
            job["percent"] = 100
            skipped = job.get("skipped") or 0
            ok_count = job.get("ok_count") or 0
            job["message"] = f"Finished. Saved {ok_count} file(s), skipped {skipped}."
            job["files"] = [f.name for f in client_dir.iterdir() if f.is_file() and is_listable_download(f.name)][-40:]
    except Exception as exc:
        err = str(exc)
        with jobs_lock:
            jobs[job_id]["status"] = "error"
            jobs[job_id]["error"] = err
            if "rate-limited" in err.lower() or "try again later" in err.lower():
                jobs[job_id]["message"] = (
                    "YouTube blocked too many requests from this IP (about 1 hour). "
                    "Wait, then try again. Already-saved videos will be skipped."
                )
            else:
                jobs[job_id]["message"] = "Download failed"


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
    html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
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
    url = (data.get("url") or "").strip()
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
    return send_from_directory(client_dir, safe_name, as_attachment=as_attachment)


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
    os.startfile(path)
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

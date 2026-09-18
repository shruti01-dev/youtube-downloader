"""
Desktop launcher — same UI as the web app, inside a native window.
"""
import os
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from app import PORT, STATE_DIR, app


def wait_for_server(url, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, TimeoutError, OSError):
            time.sleep(0.2)
    return False


def port_free(port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def run_server():
    app.run(host="127.0.0.1", port=PORT, debug=False, threaded=True, use_reloader=False)


def find_chrome():
    for base in (
        os.environ.get("ProgramFiles", r"C:\Program Files"),
        os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
        os.environ.get("LOCALAPPDATA", ""),
    ):
        if not base:
            continue
        for rel in (
            Path("Google") / "Chrome" / "Application" / "chrome.exe",
            Path("Microsoft") / "Edge" / "Application" / "msedge.exe",
        ):
            path = Path(base) / rel
            if path.is_file():
                return str(path)
    return None


def start_chrome_app(chrome, url):
    """Own Chrome profile so this window stays open (normal Chrome exits immediately)."""
    profile = STATE_DIR / "chrome-app"
    profile.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen(
        [
            chrome,
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-extensions",
            f"--app={url}",
            "--window-size=1180,780",
        ],
        shell=False,
    )


def start_webview(url):
    try:
        import webview
    except ImportError:
        print("Missing dependency: pywebview")
        print("Install with: pip install pywebview")
        sys.exit(1)
    webview.create_window(
        "YouTube Downloader",
        url,
        width=1180,
        height=780,
        min_size=(900, 600),
    )
    webview.start()


def main():
    url = f"http://127.0.0.1:{PORT}"

    if port_free(PORT):
        thread = threading.Thread(target=run_server, daemon=True)
        thread.start()
        if not wait_for_server(f"{url}/health"):
            print("Could not start local server.")
            sys.exit(1)
    else:
        if not wait_for_server(f"{url}/health", timeout=3):
            print(f"Port {PORT} is busy. Close the other app and try again.")
            sys.exit(1)

    chrome = find_chrome()
    if chrome:
        proc = start_chrome_app(chrome, url)
        time.sleep(1.5)
        if proc.poll() is None:
            proc.wait()
            return

    start_webview(url)


if __name__ == "__main__":
    main()

"""Check GitHub for a newer release and install it.

.exe build : download the release's DeskrawlTracker.zip, unpack it, and let a small batch file wait
             for the tracker to close, copy the new files over the install folder and start it again.
source     : "git pull --ff-only" in a git checkout, otherwise the release's source zip copied over.
Files the tracker writes (config, histories, captures, cache) are not in the zip and stay untouched.
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import paths
from version import REPO, VERSION

API = f"https://api.github.com/repos/{REPO}/releases/latest"
UA = {"User-Agent": f"DeskrawlTracker/{VERSION}", "Accept": "application/vnd.github+json"}


def parse(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:3]) or (0,)


def latest() -> dict | None:
    """{version, tag, notes, zip_url, source_url, page} of the newest release, None if unreachable."""
    try:
        with urllib.request.urlopen(urllib.request.Request(API, headers=UA), timeout=10) as r:
            d = json.load(r)
    except Exception:
        return None
    asset = next((a for a in d.get("assets", []) if a.get("name", "").lower().endswith(".zip")), None)
    return {"version": d.get("tag_name", "").lstrip("v"), "tag": d.get("tag_name", ""), "notes": d.get("body") or "",
            "zip_url": asset["browser_download_url"] if asset else None, "zip_size": asset["size"] if asset else 0,
            "source_url": d.get("zipball_url"), "page": d.get("html_url")}


def is_newer(info) -> bool:
    return bool(info) and parse(info["version"]) > parse(VERSION)


def _download(url, dest, progress=None, size=0):
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, size or int(r.headers.get("Content-Length") or 0))


def _run_detached(bat_path):
    # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP: one hidden console that tasklist/findstr/robocopy share.
    # (Without a console - DETACHED_PROCESS - every command opened its own window and "find" waited.)
    flags = 0x08000000 | 0x00000200
    subprocess.Popen(["cmd", "/c", bat_path], creationflags=flags, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _copy_bat(work, src, target, restart_cmd):
    """Batch file: wait for this process to end, copy src over target, start the tracker again."""
    pid = os.getpid()
    bat = os.path.join(work, "update.bat")
    with open(bat, "w", encoding="ascii", errors="replace") as f:
        f.write("@echo off\r\n"
                ":wait\r\n"
                f'tasklist /FI "PID eq {pid}" /NH | findstr /C:" {pid} " >nul && (ping -n 2 127.0.0.1 >nul & goto wait)\r\n'
                f'robocopy "{src}" "{target}" /E /R:5 /W:1 /NFL /NDL /NJH /NJS /NP >nul\r\n'
                f"cd /d \"{target}\"\r\n"
                f"{restart_cmd}\r\n"
                f'rmdir /s /q "{work}"\r\n')
    return bat


def install(info, progress=None) -> str:
    """Prepare the update; returns "restart" when the tracker has to close now, else a message."""
    work = tempfile.mkdtemp(prefix="deskrawl_update_")
    if paths.FROZEN:
        if not info.get("zip_url"):
            return "The release has no DeskrawlTracker.zip."
        z = os.path.join(work, "update.zip")
        _download(info["zip_url"], z, progress, info.get("zip_size", 0))
        with zipfile.ZipFile(z) as zf:
            zf.extractall(os.path.join(work, "new"))
        new = os.path.join(work, "new")
        inner = [d for d in os.listdir(new) if os.path.isdir(os.path.join(new, d))]
        src = os.path.join(new, inner[0]) if len(inner) == 1 and not os.path.exists(os.path.join(new, "DeskrawlTracker.exe")) else new
        target = os.path.dirname(os.path.abspath(sys.executable))
        _run_detached(_copy_bat(work, src, target, 'start "" "DeskrawlTracker.exe"'))
        return "restart"
    target = os.path.dirname(os.path.abspath(__file__))
    restart = 'start "" "Start Deskrawl Tracker.bat"'
    if os.path.isdir(os.path.join(target, ".git")):
        r = subprocess.run(["git", "-C", target, "pull", "--ff-only"], capture_output=True, text=True,
                           creationflags=0x08000000)  # CREATE_NO_WINDOW
        if r.returncode != 0:
            return "git pull failed: " + (r.stderr or r.stdout).strip()[:300]
        pip = os.path.join(target, ".venv", "Scripts", "python.exe")
        if os.path.exists(pip):  # new packages, if requirements changed
            subprocess.run([pip, "-m", "pip", "install", "-q", "-r", os.path.join(target, "requirements.txt")],
                           creationflags=0x08000000)
        with open(os.path.join(work, "restart.bat"), "w") as f:
            f.write(f'@echo off\r\nping -n 3 127.0.0.1 >nul\r\ncd /d "{target}"\r\n{restart}\r\n')
        _run_detached(os.path.join(work, "restart.bat"))
        return "restart"
    if not info.get("source_url"):
        return "No source archive in the release."
    z = os.path.join(work, "source.zip")
    _download(info["source_url"], z, progress)
    with zipfile.ZipFile(z) as zf:
        zf.extractall(os.path.join(work, "new"))
    new = os.path.join(work, "new")
    src = os.path.join(new, os.listdir(new)[0])
    _run_detached(_copy_bat(work, src, target, restart))
    return "restart"

"""Check GitHub for a newer release and install it.

.exe build : download the release's DeskrawlTracker.zip, unpack it, and let a small batch file wait
             for the tracker to close, copy the new files over the install folder and start it again.
source     : "git pull --ff-only" in a git checkout, otherwise the release's source zip copied over.
Files the tracker writes (config, histories, captures, cache) are not in the zip and stay untouched.

Checks before anything is replaced: the download comes from this repository's release, has the size and
SHA-256 that GitHub lists for the asset, is a complete zip with DeskrawlTracker.exe inside, and the install
folder is writable. If copying fails anyway, the batch file leaves update_failed.txt for the next start.
"""
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

import paths
from version import REPO, VERSION

API = f"https://api.github.com/repos/{REPO}/releases/latest"
DOWNLOAD_PREFIX = f"https://github.com/{REPO}/releases/download/"
FAILED_NOTE = paths.user("update_failed.txt")
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
    digest = (asset or {}).get("digest") or ""  # "sha256:<hex>", listed by GitHub for every uploaded asset
    return {"version": d.get("tag_name", "").lstrip("v"), "tag": d.get("tag_name", ""), "notes": d.get("body") or "",
            "zip_url": asset["browser_download_url"] if asset else None, "zip_size": asset["size"] if asset else 0,
            "zip_sha256": digest.split(":", 1)[1].lower() if digest.startswith("sha256:") else "",
            "source_url": d.get("zipball_url"), "page": d.get("html_url")}


def is_newer(info) -> bool:
    return bool(info) and parse(info["version"]) > parse(VERSION)


class UpdateError(Exception):
    pass


def _download(url, dest, progress=None, size=0) -> tuple:
    """(bytes, sha256 hex) of the downloaded file."""
    req = urllib.request.Request(url, headers={"User-Agent": UA["User-Agent"]})
    h = hashlib.sha256()
    with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
        done = 0
        while True:
            chunk = r.read(1 << 16)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            done += len(chunk)
            if progress:
                progress(done, size or int(r.headers.get("Content-Length") or 0))
    return done, h.hexdigest()


def _unzip(z, dest):
    try:
        with zipfile.ZipFile(z) as zf:
            bad = zf.testzip()
            if bad:
                raise UpdateError(f"The download is damaged ({bad}). Please try again.")
            zf.extractall(dest)
    except zipfile.BadZipFile:
        raise UpdateError("The download is not a valid zip file. Please try again.")


def _check_writable(folder):
    test = os.path.join(folder, ".update_write_test")
    try:
        with open(test, "w") as f:
            f.write("")
        os.remove(test)
    except OSError:
        raise UpdateError(f"No write access to {folder}. Move the tracker to a folder of your own "
                          f"(e.g. Documents) or install the update by hand from the release page.")


def pending_failure() -> str:
    """Message left by a failed copy step of the last update ("" if none); read once."""
    try:
        with open(FAILED_NOTE, encoding="utf-8", errors="replace") as f:
            msg = f.read().strip()
        os.remove(FAILED_NOTE)
        return msg or "The last update could not copy all files."
    except OSError:
        return ""


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
                f'if errorlevel 8 (echo Copying the update failed, robocopy exit code %errorlevel%. '
                f'Files were left in {work} > "{FAILED_NOTE}" & set KEEP=1)\r\n'
                f"cd /d \"{target}\"\r\n"
                f"{restart_cmd}\r\n"
                f'if not defined KEEP rmdir /s /q "{work}"\r\n')  # last line: the batch file itself lives in work
    return bat


def install(info, progress=None) -> str:
    """Prepare the update; returns "restart" when the tracker has to close now, else a message."""
    work = tempfile.mkdtemp(prefix="deskrawl_update_")
    if paths.FROZEN:
        if not info.get("zip_url"):
            return "The release has no DeskrawlTracker.zip."
        if not info["zip_url"].startswith(DOWNLOAD_PREFIX):
            return "The release download is not from the tracker's GitHub repository - update stopped."
        target = os.path.dirname(os.path.abspath(sys.executable))
        try:
            _check_writable(target)
            z = os.path.join(work, "update.zip")
            got, sha = _download(info["zip_url"], z, progress, info.get("zip_size", 0))
            if info.get("zip_size") and got != info["zip_size"]:
                raise UpdateError(f"Download incomplete ({got / 1e6:.1f} of {info['zip_size'] / 1e6:.1f} MB). "
                                  f"Please try again.")
            if info.get("zip_sha256") and sha != info["zip_sha256"]:
                raise UpdateError("The download does not match the release checksum - update stopped.")
            new = os.path.join(work, "new")
            _unzip(z, new)
            inner = [d for d in os.listdir(new) if os.path.isdir(os.path.join(new, d))]
            src = os.path.join(new, inner[0]) if len(inner) == 1 and not os.path.exists(os.path.join(new, "DeskrawlTracker.exe")) else new
            if not os.path.isfile(os.path.join(src, "DeskrawlTracker.exe")):
                raise UpdateError("The release zip has no DeskrawlTracker.exe - update stopped.")
        except UpdateError as e:
            shutil.rmtree(work, ignore_errors=True)
            return str(e)
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
    new = os.path.join(work, "new")
    try:
        _check_writable(target)
        _download(info["source_url"], z, progress)
        _unzip(z, new)
    except UpdateError as e:
        shutil.rmtree(work, ignore_errors=True)
        return str(e)
    src = os.path.join(new, os.listdir(new)[0])
    _run_detached(_copy_bat(work, src, target, restart))
    return "restart"

"""Where files live.

RES_DIR  - files shipped with the tracker (data/*.json, legendaries.json). Inside the .exe build
           this is the unpacked bundle, so it is read-only.
USER_DIR - files the tracker writes (config, histories, captures). Next to the .exe or the scripts;
           if that folder is not writable (e.g. Program Files) %LOCALAPPDATA%\\DeskrawlTracker.
"""
import os
import sys

FROZEN = bool(getattr(sys, "frozen", False))
_HERE = os.path.dirname(os.path.abspath(__file__))
RES_DIR = getattr(sys, "_MEIPASS", _HERE)


def _user_dir() -> str:
    base = os.path.dirname(os.path.abspath(sys.executable)) if FROZEN else _HERE
    try:
        test = os.path.join(base, ".write_test")
        with open(test, "w") as f:
            f.write("")
        os.remove(test)
        return base
    except OSError:
        alt = os.path.join(os.environ.get("LOCALAPPDATA", base), "DeskrawlTracker")
        os.makedirs(alt, exist_ok=True)
        return alt


USER_DIR = _user_dir()


def res(*parts) -> str:
    return os.path.join(RES_DIR, *parts)


def user(*parts) -> str:
    return os.path.join(USER_DIR, *parts)


# default location of the game's log; the first-run setup lets the player pick another one
DEFAULT_LOG = os.path.join(os.environ.get("USERPROFILE", ""), "AppData", "LocalLow",
                           "First Day Games", "Deskrawl", "Game.log")

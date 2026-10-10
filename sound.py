"""Sounds the tracker plays (Legendary drop).

Built in: data/legendary.wav (a chime made by tools/make_sounds.py). Your own sounds: .mp3 or .wav files in the
folder "sounds" next to the tracker (paths.user). They are yours and stay on your PC - the folder is not part
of the repository or the download.

Playback uses the Windows media control interface (winmm, MCI), which plays MP3 and WAV without extra
packages; it runs asynchronously, a new sound stops the previous one.
"""
import ctypes
import os
import threading

import errlog
import paths

BUILTIN = "Chime (built in)"
EXTS = (".mp3", ".wav")
_lock = threading.Lock()
_ALIAS = "deskrawl_sound"


def folder() -> str:
    return paths.user("sounds")


def own_sounds() -> list:
    """File names of the sounds in the sounds folder."""
    try:
        return sorted(f for f in os.listdir(folder()) if f.lower().endswith(EXTS))
    except OSError:
        return []


def choices() -> list:
    return [BUILTIN] + own_sounds()


def path_of(choice: str) -> str:
    if choice and choice != BUILTIN:
        p = os.path.join(folder(), choice)
        if os.path.isfile(p):
            return p
    return paths.res("data", "legendary.wav")


def _mci(cmd: str) -> int:
    return ctypes.windll.winmm.mciSendStringW(cmd, None, 0, None)


def play(choice: str):
    """Play a sound (BUILTIN or a file name from the sounds folder) without waiting for it."""
    p = path_of(choice)
    with _lock:
        try:
            _mci(f"close {_ALIAS}")
            kind = "mpegvideo" if p.lower().endswith(".mp3") else "waveaudio"
            err = _mci(f'open "{p}" type {kind} alias {_ALIAS}')
            if err == 0:
                err = _mci(f"play {_ALIAS} from 0")
            if err:
                import winsound  # fallback: the built-in chime
                winsound.PlaySound(paths.res("data", "legendary.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            errlog.report("sound", "legendary sound could not be played")

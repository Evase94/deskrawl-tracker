"""Error log: tracker_errors.log next to the other user files (the .exe has no console).

The file is capped at 1 MB with two older copies, so a worker that fails every frame cannot fill the disk.
Background loops call report() with a key: the same problem is written at most once a minute.
"""
import logging
import logging.handlers
import os
import sys
import threading
import time

import paths

ERROR_LOG = paths.user("tracker_errors.log")
log = logging.getLogger("tracker")

_last: dict = {}
_lock = threading.Lock()


def setup():
    """File handler + hooks for uncaught exceptions in the main thread and in worker threads."""
    if log.handlers:
        return
    try:
        h = logging.handlers.RotatingFileHandler(ERROR_LOG, maxBytes=1_000_000, backupCount=2, encoding="utf-8",
                                                 delay=True)
    except OSError:
        return
    h.setFormatter(logging.Formatter("--- %(asctime)s %(levelname)s %(threadName)s\n%(message)s",
                                     "%Y-%m-%d %H:%M:%S"))
    log.addHandler(h)
    log.setLevel(logging.INFO)
    log.propagate = False
    sys.excepthook = uncaught
    threading.excepthook = lambda a: uncaught(a.exc_type, a.exc_value, a.exc_traceback)


def uncaught(exc_type, exc, tb):
    log.error("uncaught exception", exc_info=(exc_type, exc, tb))


def report(key: str, msg: str, every_s: float = 60.0, exc_info=True):
    """Log msg with the current exception; repeats of the same key within every_s are only counted."""
    now = time.time()
    with _lock:
        last, skipped = _last.get(key, (0.0, 0))
        if now - last < every_s:
            _last[key] = (last, skipped + 1)
            return
        _last[key] = (now, 0)
    if skipped:
        msg += f" (+{skipped} more since the last entry)"
    log.error(msg, exc_info=exc_info)


def path() -> str:
    return ERROR_LOG if os.path.exists(ERROR_LOG) else ""

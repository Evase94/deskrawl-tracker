"""Crash-safe JSON files for config and statistics.

write_json: the data goes to <file>.tmp first and replaces the file in one step (os.replace), so a crash or
power cut while saving leaves either the old or the new file, never half of one. Before the first save
of a session the current file is copied to <file>.bak.
read_json: an unreadable file is kept as <file>.broken-<time> and the .bak is used instead.
"""
import json
import os
import shutil
import threading
import time

from errlog import log

_backed_up: set = set()
_lock = threading.Lock()


def read_json(path, default=None):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        log.error(f"cannot read {path}", exc_info=True)
    broken = f"{path}.broken-{time.strftime('%Y%m%d-%H%M%S')}"
    try:
        os.replace(path, broken)
    except OSError:
        broken = path
    bak = path + ".bak"
    try:
        with open(bak, encoding="utf-8") as f:
            data = json.load(f)
        log.warning(f"{os.path.basename(path)} was damaged - restored from {os.path.basename(bak)}, "
                    f"damaged file kept as {os.path.basename(broken)}")
        return data
    except Exception:
        log.error(f"no usable backup for {path}", exc_info=os.path.exists(bak))
        return default


def write_json(path, data, **dump_args) -> bool:
    """True when saved. Thread-safe; failures are written to the error log."""
    tmp = path + ".tmp"
    with _lock:
        try:
            if path not in _backed_up and os.path.exists(path):
                shutil.copy2(path, path + ".bak")
                _backed_up.add(path)
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, **dump_args)
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(5):  # a virus scanner may hold the old file for a moment
                try:
                    os.replace(tmp, path)
                    return True
                except PermissionError:
                    if attempt == 4:
                        raise
                    time.sleep(0.1)
        except Exception:
            log.error(f"cannot save {path}", exc_info=True)
            try:
                os.remove(tmp)
            except OSError:
                pass
            return False

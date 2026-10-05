"""
Deskrawl Tracker - live EXP/h, Gold/h, runs/h from Game.log plus OCR-based DPS meter.

Run:  python deskrawl_tracker.py
Test OCR offline against a recorded clip:  python deskrawl_tracker.py --video clip.mp4
"""
import argparse
import csv
import ctypes
import ctypes.wintypes
import json
import os
import queue
import re
import sys
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime

import tkinter as tk
from tkinter import ttk

import paths
import setup_dialog
import item_ocr  # first: loads onnxruntime before WinRT/winocr (avoids a crash)
import item_eval
import stages
import copy
import item_quality
import ui_theme as ui
import numpy as np
import game_input
from game_capture import GameCapture

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels everywhere
except Exception:
    pass

LOG_PATH = paths.DEFAULT_LOG
APP_DIR = paths.USER_DIR  # everything the tracker writes
CONFIG_PATH = os.path.join(APP_DIR, "tracker_config.json")
RUNS_CSV = os.path.join(APP_DIR, "runs_history.csv")

ROLLING_WINDOW_S = 15 * 60
LIVE_DPS_WINDOW_S = 5.0


# --------------------------------------------------------------------------- helpers

def num(s: str) -> float:
    """Parse numbers like '1,45' (German decimal) or '56124'."""
    return float(s.replace(",", "."))


def fmt(v: float, decimals: int = 0) -> str:
    if v is None:
        return "-"
    a = abs(v)
    if a >= 1e9:
        return f"{v / 1e9:.2f}B"
    if a >= 1e6:
        return f"{v / 1e6:.2f}M"
    if a >= 1e4:
        return f"{v / 1e3:.1f}k"
    return f"{v:,.{decimals}f}".replace(",", ".")


def fmt_dur(s: float) -> str:
    if s is None:
        return "-"
    s = int(s)
    h, r = divmod(s, 3600)
    m, sec = divmod(r, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except Exception:
        pass


# --------------------------------------------------------------------------- log parsing

RE_START = re.compile(r"run/start ok(?: \((?P<mode>\w+)\))? — runId=\S+:(?P<id>[0-9a-f]+), "
                      r"difficulty=(?P<diff>\w+), waves=(?P<waves>\d+), plannedItems=(?P<planned>\d+), "
                      r"finds\(server/local\): mf (?P<mf>[\d,]+)/\S+, gf (?P<gf>[\d,]+)/\S+, xp (?P<xpm>[\d,]+)/")
RE_COMMIT = re.compile(r"run/commit ok — runId=\S+:(?P<id>[0-9a-f]+), collected=(?P<collected>\d+), "
                       r"granted=(?P<g1>\d+)\+(?P<g2>\d+) stacks, gold=(?P<gold>\d+), xp=(?P<xp>\d+) "
                       r"\(level (?P<level>\d+)\)")
RE_LOGIN = re.compile(r"\[cloud\] login — name='(?P<name>[^']*)' hero=(?P<hero>\w+) lvl=(?P<lvl>\d+) "
                      r"xp=(?P<xp>\d+).*?map:(?P<map>\w+).*?gold=(?P<gold>\d+)")
RE_PLACED = re.compile(r"\[TransparentWindow\] placed \((?P<x>-?\d+),(?P<y>-?\d+)\) (?P<w>\d+)x(?P<h>\d+)")


@dataclass
class Run:
    run_id: str
    start: float | None = None
    end: float | None = None
    difficulty: str = "?"
    waves: int = 0
    mode: str = ""
    mf: float = 0
    gf: float = 0
    xpm: float = 0
    xp: int = 0
    gold: int = 0
    items: int = 0
    level: int = 0
    damage: float = 0.0
    hits: int = 0
    crits: int = 0
    peak_dps: float = 0.0
    death: str = ""          # "", "vermutet" or "bestätigt"
    stage_name: str = ""     # from the in-game log panel ("The Cinder Crown: 6")
    stage_guess: str = ""    # assumed from the previous run while the log panel was not read
    aggregated: bool = False  # already counted in the persistent per-stage statistics
    game_seconds: int | None = None  # clear time from the in-game log panel
    death_info: dict = field(default_factory=dict)

    @property
    def duration(self):
        if self.start is None:
            return None
        return (self.end or time.time()) - self.start

    @property
    def avg_dps(self):
        d = self.duration
        return self.damage / d if d and d > 0 and self.damage else None


# XP needed to go from level L to L+1. Derived from Game.log level-ups (login lines give the
# exact XP inside the level, run commits give XP per run and the new level). Midpoints of the
# observed bounds; levels outside the table use a quadratic fit. Accurate to a few percent.
XP_SEED = {43: 5612000, 44: 6238000, 45: 6871000, 46: 7728000, 47: 8564000, 48: 9466000,
           49: 10628000, 50: 11872000, 51: 12490000, 52: 13412000, 53: 14209000, 54: 15245000,
           55: 16128000, 56: 17046000, 57: 18094000, 58: 19201000, 59: 20431000}
XP_FIT = (9789.55, -72921.87, -9463242.14)  # a*L^2 + b*L + c


class XpModel:
    """Tracks XP inside the current level and learns level requirements from level-ups."""

    def __init__(self, learned: dict | None = None):
        # learned: {level: [lo, hi]} - tightest bounds seen for that level's requirement
        self.learned = {int(k): list(v) for k, v in (learned or {}).items()}
        self.char = None
        self.level = 0
        self.xp = None        # XP inside the current level
        self.exact = False    # True while no level-up happened since the last login line
        self.changed = False  # learned bounds changed -> caller persists them

    def required(self, level: int) -> int:
        if level in self.learned:
            lo, hi = self.learned[level]
            return int((lo + hi) / 2)
        if level in XP_SEED:
            return XP_SEED[level]
        a, b, c = XP_FIT
        return max(int(a * level * level + b * level + c), 1000)

    def login(self, char: str, level: int, xp: int):
        self.char, self.level, self.xp, self.exact = char, level, xp, True

    def run(self, gain: int, new_level: int):
        if self.xp is None:
            self.level = new_level
            return
        if new_level == self.level:
            self.xp += gain
            return
        if new_level == self.level + 1:
            lo, hi = self.xp, self.xp + gain  # requirement lies inside the crossing run
            if self.exact:
                old = self.learned.get(self.level)
                nb = [max(lo, old[0]), min(hi, old[1])] if old else [lo, hi]
                if nb[0] <= nb[1] and nb != old:
                    self.learned[self.level] = nb
                    self.changed = True
            req = min(max(self.required(self.level), lo), hi)
            self.xp = self.xp + gain - req
            self.exact = False  # carry-over is an estimate now
        else:  # several levels at once (should not happen) - restart from zero
            self.xp, self.exact = 0, False
        self.level = new_level


class GoldAudit:
    """Gold that never shows up in run commits (auto-sold items, merchant sales).

    The log only reports gold per run. Comparing two readings of the account balance with the
    run gold committed in between gives the extra gold. Intervals where the balance grew less
    than the run gold contain spending (blacksmith, gambling...) and are skipped - their extra
    gold cannot be separated.
    """
    TOLERANCE = 50  # commit vs. on-screen timing jitter

    def __init__(self):
        self.reset()

    def reset(self):
        self.last = None        # (t, balance, committed_run_gold)
        self.extra = 0
        self.measured_s = 0.0
        self.skipped = 0
        self.balance = None
        self.balance_t = None

    def observe(self, t: float, balance: int, committed: int):
        self.balance, self.balance_t = balance, t
        if self.last is not None:
            t0, b0, c0 = self.last
            if balance == b0 and committed == c0:
                return  # nothing happened; keep the older anchor
            extra = (balance - b0) - (committed - c0)
            if extra >= -self.TOLERANCE:
                self.extra += max(extra, 0)
                self.measured_s += t - t0
            else:
                self.skipped += 1
        self.last = (t, balance, committed)

    @property
    def rate_h(self):
        return self.extra / self.measured_s * 3600 if self.measured_s >= 120 else None


class GameState:
    """All tracked data. Mutated by the log thread and OCR thread under self.lock."""

    def __init__(self):
        self.lock = threading.RLock()
        self.reset()
        self.hero = "-"
        self.char_name = "-"
        self.level = 0
        self.map = "-"
        self.window_rect = None  # (x, y, w, h) of the game window from the log
        self.backlog = {"runs": 0, "xp": 0, "gold": 0, "items": 0}
        self.xpm = XpModel(load_config().get("xp_learned"))
        self.gold_audit = GoldAudit()
        self.committed_gold = 0  # run gold of all live commits (for GoldAudit)
        self.log_seen = None     # entries of the in-game log panel at the last read
        self.sold: list = []     # (t, item, gold, rarity) from the in-game log panel
        self.drops: list = []    # (t, item, rarity, action) items named in the log panel that were not sold
        # last sale seen as a toast; while recent (or right after start) log "Sold" lines are skipped
        self.last_toast = time.time()
        self.stage_name = ""     # "The Cinder Crown: 6"
        self.stage_key = None    # (difficulty, waves) of the run that stage_name came from
        self.run_committed = threading.Event()  # wakes the PanelReader

    def reset(self):
        with getattr(self, "lock", threading.RLock()):
            self.session_start = time.time()
            self.runs: list[Run] = []
            self.by_id: dict[str, Run] = {}
            self.current: Run | None = None
            self.hits: deque = deque()  # (t, value, crit) - all OCR hits of this session
            if hasattr(self, "gold_audit"):
                self.gold_audit.reset()
                self.sold = []
                self.drops = []

    # ---- log events -------------------------------------------------------
    def handle_line(self, line: str, live: bool, now: float):
        if "run/" in line:
            m = RE_START.search(line)
            if m:
                if not live:
                    return
                r = Run(run_id=m["id"], start=now, difficulty=m["diff"], waves=int(m["waves"]),
                        mode=m["mode"] or "", mf=num(m["mf"]), gf=num(m["gf"]), xpm=num(m["xpm"]))
                with self.lock:
                    self.by_id[r.run_id] = r
                    self.current = r
                return
            m = RE_COMMIT.search(line)
            if m:
                xp, gold, items, lvl = int(m["xp"]), int(m["gold"]), int(m["collected"]), int(m["level"])
                with self.lock:
                    self.level = lvl
                    self.xpm.run(xp, lvl)
                    if not live:
                        self.backlog["runs"] += 1
                        self.backlog["xp"] += xp
                        self.backlog["gold"] += gold
                        self.backlog["items"] += items
                        return
                    self.committed_gold += gold
                    r = self.by_id.get(m["id"]) or Run(run_id=m["id"])
                    r.end, r.xp, r.gold, r.items, r.level = now, xp, gold, items, lvl
                    self.runs.append(r)
                    if self.current is r:
                        self.current = None
                    self._check_death(r)
                    if self.stage_name and self.stage_key == (r.difficulty, r.waves):
                        r.stage_guess = self.stage_name  # same settings as the last known stage
                    self.run_committed.set()
                append_run_csv(r)
                if r.death:
                    append_death_csv(r, self.char_name)
                return
        if "[cloud] login" in line:
            m = RE_LOGIN.search(line)
            if m:
                with self.lock:
                    self.char_name, self.hero = m["name"], m["hero"].replace("Hero", "")
                    self.level, self.map = int(m["lvl"]), m["map"]
                    self.xpm.login(m["name"], int(m["lvl"]), int(m["xp"]))
            return
        if "[TransparentWindow] placed" in line:
            m = RE_PLACED.search(line)
            if m:
                self.window_rect = (int(m["x"]), int(m["y"]), int(m["w"]), int(m["h"]))

    # ---- deaths ------------------------------------------------------------
    # The log has no death entry: a run that ends in death is committed like any other, just with
    # less XP and earlier. Compare against recent runs on the same stage; a death text seen on
    # screen during the run (DeathWatcher) confirms it.
    DEATH_XP_RATIO = 0.6
    DEATH_DUR_RATIO = 0.75

    def mark_death_seen(self, t: float, text: str, extra: dict | None = None, max_age: float = 15):
        with self.lock:
            r = self.current or (self.runs[-1] if self.runs else None)
            if r is None or (r.end and t - r.end > max_age):
                return
            if extra:
                r.death_info.update(extra)
            r.death_info.setdefault("screen", text)
            r.death_info.setdefault("seen_at", t)
            if r.end and r.death != "bestätigt":  # death screen showed up after the commit
                self._check_death(r)
                append_death_csv(r, self.char_name)

    def _stage_refs(self, r: Run) -> list:
        return [o for o in self.runs if o is not r and not o.death and o.difficulty == r.difficulty
                and o.waves == r.waves and o.start and o.end and o.xp][-10:]

    def _check_death(self, r: Run):
        refs = self._stage_refs(r)
        info = r.death_info
        med = lambda xs: sorted(xs)[len(xs) // 2]
        if len(refs) >= 3:
            mx, md = med([o.xp for o in refs]), med([o.duration for o in refs])
            info["xp_ratio"] = r.xp / mx if mx else None
            info["dur_ratio"] = r.duration / md if (md and r.start) else None
            info["ref_runs"] = len(refs)
            if info["xp_ratio"] is not None and r.waves:
                # XP comes from kills, so the XP share approximates the share of waves cleared
                info["est_wave"] = min(r.waves, max(1, int(info["xp_ratio"] * r.waves) + 1))
        suspicious = (info.get("xp_ratio") is not None and info["xp_ratio"] < self.DEATH_XP_RATIO
                      and (info.get("dur_ratio") is None or info["dur_ratio"] < self.DEATH_DUR_RATIO))
        if "screen" in info:
            r.death = "bestätigt"
        elif suspicious:
            r.death = "vermutet"
        if r.death:
            info["dps_before"] = r.avg_dps
            info["peak_dps"] = r.peak_dps or None
            info["level"] = r.level

    def death_stats(self) -> dict:
        with self.lock:
            deaths = [r for r in self.runs if r.death]
            span = self.stats()["span"]  # same time base as EXP/h etc.
            return {"n": len(deaths), "confirmed": sum(r.death == "bestätigt" for r in deaths),
                    "per_h": len(deaths) / span * 3600, "rate": len(deaths) / len(self.runs) if self.runs else 0,
                    "list": deaths}

    # ---- in-game log panel ---------------------------------------------------
    @staticmethod
    def _norm(e: dict) -> str:
        return re.sub(r"[^a-z0-9]", "", e["text"].lower())

    def _overlap(self, old: list, new: list) -> int:
        """Largest k with old[-k:] == new[:k] (fuzzy): entries already seen at the last read."""
        import difflib
        eq = lambda a, b: a == b or difflib.SequenceMatcher(None, a, b).ratio() > 0.88
        for k in range(min(len(old), len(new)), 0, -1):
            if all(eq(old[len(old) - k + i], new[i]) for i in range(k)):
                return k
        return 0

    def ingest_log(self, entries: list, t: float):
        """Apply entries of the in-game log panel that were not there at the previous read."""
        if not entries:
            return
        with self.lock:
            norm = [self._norm(e) for e in entries]
            if self.log_seen is None:  # first read: older than the tool, only remember them
                self.log_seen = norm
                for e in entries:
                    if e["type"] == "clear":
                        self.stage_name = f"{e['stage']}: {e['n']}"
                return
            fresh = entries[self._overlap(self.log_seen, norm):]
            self.log_seen = norm
            # newest clear belongs to the newest committed run without a stage, and so on backwards
            # (runs that ended in death have no clear entry)
            open_runs = [o for o in reversed(self.runs) if o.end and not o.stage_name and o.death != "bestätigt"
                         and t - o.end < 1800]
            for e, r in zip([e for e in reversed(fresh) if e["type"] == "clear"], open_runs):
                r.stage_name, r.game_seconds = f"{e['stage']}: {e['n']}", e["seconds"]
                if r.start is None:  # tool started mid-run: the clear time gives the start
                    r.start = r.end - e["seconds"]
                if r is self.runs[-1] or self.stage_key is None:
                    self.stage_key = (r.difficulty, r.waves)
            for e in fresh:
                if e["type"] == "clear":
                    self.stage_name = f"{e['stage']}: {e['n']}"
                elif e["type"] == "sold":
                    if t - self.last_toast > 600:  # toasts are the primary source; log only as fallback
                        self._add_sale(t, e, "log")
                elif e["type"] == "item":
                    self._add_drop(t, e, "log")
                elif e["type"] == "killed":
                    info = {k: e[k] for k in ("killer", "killer_level", "source", "damage", "element")}
                    self.mark_death_seen(t, e["text"], info, max_age=180)

    def _add_sale(self, t: float, e: dict, source: str):
        # the same sale can be seen as a toast and later in the log panel: same item for the same
        # gold from the *other* source within a minute is that one sale (two toasts are two sales)
        key = (re.sub(r"[^a-z]", "", e["item"].lower()), e["gold"])
        self._sale_keys = [x for x in getattr(self, "_sale_keys", []) if t - x[0] <= 60]
        for i, (t0, k, src) in enumerate(self._sale_keys):
            if k == key and src != source:
                del self._sale_keys[i]
                return
        self._sale_keys.append((t, key, source))
        self.sold.append((t, e["item"], e["gold"], e.get("rarity", "?")))

    def _add_drop(self, t: float, e: dict, source: str):
        """Non-sale item events (kept drops, gems, runes, keys...); same cross-source rule as sales."""
        key = (re.sub(r"[^a-z]", "", e["item"].lower()), -1)
        self._sale_keys = [x for x in getattr(self, "_sale_keys", []) if t - x[0] <= 60]
        for i, (t0, k, src) in enumerate(self._sale_keys):
            if k == key and src != source:
                del self._sale_keys[i]
                return
        self._sale_keys.append((t, key, source))
        self.drops.append((t, e["item"], drop_category(e), e.get("action", "")))

    def add_toast_drop(self, t: float, e: dict):
        with self.lock:
            self._add_drop(t, e, "toast")

    def add_toast_sale(self, t: float, e: dict):
        with self.lock:
            self.last_toast = t
            self._add_sale(t, e, "toast")

    def sold_stats(self, window_s: float | None = None) -> dict:
        with self.lock:
            now = time.time()
            sold = [x for x in self.sold if not window_s or x[0] >= now - window_s]
            return {"n": len(sold), "gold": sum(x[2] for x in sold)}

    def drop_stats(self) -> dict:
        """Per rarity: seen in the log panel, how many were sold, gold from sales."""
        with self.lock:
            out = {}
            for _, _, gold, rar in self.sold:
                d = out.setdefault(rar, {"n": 0, "sold": 0, "gold": 0})
                d["n"] += 1
                d["sold"] += 1
                d["gold"] += gold
            for _, _, rar, _ in self.drops:
                out.setdefault(rar, {"n": 0, "sold": 0, "gold": 0})["n"] += 1
            recent = sorted([(t, i, r, "verkauft", g) for t, i, g, r in self.sold]
                            + [(t, i, r, a or "behalten", None) for t, i, r, a in self.drops])[-60:]
            return {"by_rarity": out, "recent": recent}

    # ---- OCR events --------------------------------------------------------
    def observe_gold(self, t: float, balance: int):
        with self.lock:
            self.gold_audit.observe(t, balance, self.committed_gold)

    def add_hit(self, t: float, value: float, crit: bool):
        with self.lock:
            self.hits.append((t, value, crit))
            r = self.current
            if r is not None and r.start is not None and t >= r.start:
                r.damage += value
                r.hits += 1
                r.crits += int(crit)
                r.peak_dps = max(r.peak_dps, self.live_dps(t))

    def live_dps(self, now: float | None = None) -> float:
        now = now or time.time()
        with self.lock:
            cutoff = now - LIVE_DPS_WINDOW_S
            while self.hits and self.hits[0][0] < now - 3600:
                self.hits.popleft()
            total = sum(v for t, v, _ in reversed(self.hits) if t >= cutoff)
        return total / LIVE_DPS_WINDOW_S

    # ---- aggregates --------------------------------------------------------
    def level_eta(self) -> dict | None:
        """Time to next level, from the XP rate of recent runs on the current stage."""
        with self.lock:
            xm = self.xpm
            if xm.xp is None or not xm.level:
                return None
            need = xm.required(xm.level)
            left = max(need - xm.xp, 0)
            known = [r for r in self.runs if r.difficulty != "?"]  # runs seen from their start
            ref = self.current or (known[-1] if known else None)
            out = {"level": xm.level, "xp": xm.xp, "need": need, "left": left, "exact": xm.exact,
                   "stage": None, "runs_left": None, "eta_stage": None}
            if ref is None:
                return out
            same = [r for r in self.runs if r.difficulty == ref.difficulty and r.waves == ref.waves
                    and r.start and r.end and r.xp and not r.death][-10:]
            out["stage"] = f"{ref.difficulty} · {ref.waves} Waves"
            if same:
                xp_run = sum(r.xp for r in same) / len(same)
                cycle = (same[-1].end - same[0].start) / len(same) if len(same) > 1 else same[0].duration
                out["xp_run"] = xp_run
                out["n_runs"] = len(same)
                out["runs_left"] = left / xp_run
                out["eta_stage"] = out["runs_left"] * cycle  # cycle includes downtime between runs
            return out

    def stats(self, window_s: float | None = None) -> dict:
        now = time.time()
        with self.lock:
            timed = [r for r in self.runs if r.start]  # a run already going at tool start has no start time
            runs = timed
            if window_s:
                runs = [r for r in runs if r.end and r.end >= now - window_s]
            first_starts = [r.start for r in timed]
            if self.current and self.current.start:
                first_starts.append(self.current.start)
            t0 = min(first_starts) if first_starts else self.session_start
            if window_s:
                t0 = max(t0, now - window_s)
            span = max(now - t0, 1.0)
            xp = sum(r.xp for r in runs)
            gold = sum(r.gold for r in runs)
            items = sum(r.items for r in runs)
            durs = [r.duration for r in runs if r.start and r.end]
            dmg_runs = [r for r in runs if r.start and r.end and r.damage]
            return {
                "span": span, "runs": len(runs), "xp": xp, "gold": gold, "items": items,
                "xp_h": xp / span * 3600, "gold_h": gold / span * 3600,
                "runs_h": len(runs) / span * 3600, "items_h": items / span * 3600,
                "avg_run": sum(durs) / len(durs) if durs else None,
                "avg_xp_run": xp / len(runs) if runs else None,
                "avg_gold_run": gold / len(runs) if runs else None,
                "avg_dps": (sum(r.damage for r in dmg_runs) / sum(r.duration for r in dmg_runs)) if dmg_runs else None,
            }


DEATHS_CSV = os.path.join(APP_DIR, "deaths_log.csv")


def append_death_csv(r: Run, char: str):
    i = r.death_info
    new = not os.path.exists(DEATHS_CSV)
    try:
        with open(DEATHS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["time", "status", "char", "level", "difficulty", "waves", "est_wave", "duration_s",
                            "xp", "xp_ratio", "dur_ratio", "gold", "items", "avg_dps", "peak_dps", "screen_text",
                            "run_id", "stage", "killer", "killer_level", "source", "damage", "element"])
            w.writerow([datetime.fromtimestamp(r.end).isoformat(timespec="seconds"), r.death, char, r.level,
                        r.difficulty, r.waves, i.get("est_wave", ""), round(r.duration, 1) if r.start else "",
                        r.xp, round(i["xp_ratio"], 2) if i.get("xp_ratio") is not None else "",
                        round(i["dur_ratio"], 2) if i.get("dur_ratio") is not None else "", r.gold, r.items,
                        int(r.avg_dps or 0), int(r.peak_dps), i.get("screen", ""), r.run_id, r.stage_name,
                        i.get("killer", ""), i.get("killer_level", ""), i.get("source", ""), i.get("damage", ""),
                        i.get("element", "")])
    except Exception:
        pass


def drop_category(e: dict) -> str:
    """Bucket for the drop statistics: equipment by rarity, gems by tier, other loot by kind."""
    info = item_ocr.classify_drop(e.get("item", ""))
    if info["kind"] == "gem":
        return f"Edelstein Stufe {info['tier'] or '?'}"
    if info["kind"] == "rune":
        return {"set": "Set-Rune", "ability": "Fähigkeits-Rune", "attribute": "Attribut-Rune"}.get(info.get("rune_type"), "Rune")
    return {"key": "Schatzschlüssel", "shard": "Soul Shard", "boss_material": "Boss-Material", "skull": "Schädel",
            "ore": "Erz", "plant": "Pflanze"}.get(info["kind"], e.get("rarity", "?"))


def append_run_csv(r: Run):
    new = not os.path.exists(RUNS_CSV)
    try:
        with open(RUNS_CSV, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f, delimiter=";")
            if new:
                w.writerow(["end_time", "run_id", "difficulty", "waves", "duration_s", "xp", "gold", "items",
                            "level", "mf", "gf", "xp_mult", "ocr_damage", "ocr_avg_dps", "ocr_peak_dps"])
            w.writerow([datetime.fromtimestamp(r.end).isoformat(timespec="seconds"), r.run_id, r.difficulty,
                        r.waves, round(r.duration, 1) if r.start else "", r.xp, r.gold, r.items, r.level,
                        r.mf, r.gf, r.xpm, int(r.damage), int(r.avg_dps or 0), int(r.peak_dps)])
    except Exception:
        pass


class LogTailer(threading.Thread):
    """Polls Game.log, timestamps new lines on arrival. Handles truncation / game restart."""

    def __init__(self, state: GameState, path: str = LOG_PATH):
        super().__init__(daemon=True)
        self.state, self.path = state, path
        self.pos = 0
        self.buf = ""
        self.first = True
        self.status = "waiting for log"

    def set_path(self, path: str):
        """Follow another log file (chosen in the setup); its history is read without live events."""
        self.path, self.pos, self.buf, self.first = path, 0, "", True

    def run(self):
        while True:
            try:
                size = os.path.getsize(self.path)
                if size < self.pos:  # game restarted -> new log
                    self.pos, self.buf = 0, ""
                if size > self.pos:
                    with open(self.path, "r", encoding="utf-8", errors="replace") as f:
                        f.seek(self.pos)
                        data = f.read()
                        self.pos = f.tell()
                    self.buf += data
                    *lines, self.buf = self.buf.split("\n")
                    now = time.time()
                    for ln in lines:
                        self.state.handle_line(ln, live=not self.first, now=now)
                self.first = False
                self.status = "log ok"
            except FileNotFoundError:
                self.status = "Game.log not found"
            except Exception as e:  # keep tailing on transient errors (file locked etc.)
                self.status = f"log error: {e}"
            time.sleep(0.5)


# --------------------------------------------------------------------------- OCR DPS meter

RE_DMG = re.compile(r"^\d{1,3}(?:[,.]\d{3})+$|^\d{2,9}$")


@dataclass
class Track:
    x: float
    y: float
    h: float
    first: float
    last: float
    readings: Counter = field(default_factory=Counter)
    crit_votes: int = 0
    seen: int = 0
    y0: float = 0.0
    formatted: bool = False


def digits_similar(a: str, b: str) -> bool:
    if a == b or a in b or b in a:
        return True
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        return sum(c1 != c2 for c1, c2 in zip(a, b)) <= 2
    return False


class DamageOCR:
    """Turns screenshots into unique damage hits.

    Floating numbers live ~1 s and drift upward, so each one is seen in several frames.
    Detections are grouped into tracks by position + similar digits; a track is counted once,
    after it disappears, with its most frequent reading. Long-lived motionless tracks are
    static text (not damage) and get dropped.
    """

    def __init__(self, on_hit, min_value: int = 10):
        import numpy as np  # noqa: F401  (fail early if missing)
        import cv2  # noqa: F401
        import winocr  # noqa: F401
        self.on_hit = on_hit
        self.min_value = min_value
        self.tracks: list[Track] = []
        self.static_spots: list = []  # (until, digits, x, y): UI numbers seen standing still
        self.recent_values: deque = deque(maxlen=200)
        self.rejected = 0
        self.scale = 1.0

    def preprocess(self, bgr):
        import cv2
        import numpy as np
        hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
        b, g, r = [c.astype(np.int16) for c in cv2.split(bgr)]
        white = (hsv[..., 2] > 200) & (hsv[..., 1] < 60)
        yellow = (r > 200) & (g > 170) & (b < 140)
        mask = (white | yellow).astype(np.uint8) * 255
        h = bgr.shape[0]
        self.scale = 2.0 if h < 500 else 1.0
        if self.scale != 1.0:
            mask = cv2.resize(mask, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_NEAREST)
        return cv2.cvtColor(255 - mask, cv2.COLOR_GRAY2BGR), yellow

    def detect(self, bgr):
        import winocr
        img, yellow = self.preprocess(bgr)
        res = winocr.recognize_cv2_sync(img, "en")
        out = []
        for line in res.get("lines", []):
            words = line.get("words", [])
            ltext = line.get("text", "")
            if re.search(r"[A-Za-z]{3,}", ltext.replace("XP", "")):
                continue  # damage numbers stand alone; numbers inside text are UI (log, panels)
            i = 0
            while i < len(words):
                w = words[i]
                text = w["text"].strip()
                br = w["bounding_rect"]
                x, y, ww, hh = br["x"], br["y"], br["width"], br["height"]
                # glue split leading digit: "1 74,435"
                if re.fullmatch(r"\d{1,2}", text) and i + 1 < len(words):
                    nb = words[i + 1]["bounding_rect"]
                    nt = words[i + 1]["text"].strip()
                    if re.fullmatch(r"\d{1,3}(?:[,.]\d{3})+", nt) and nb["x"] - (x + ww) < hh * 0.6:
                        text = text + nt
                        ww = nb["x"] + nb["width"] - x
                        i += 1
                i += 1
                if text.startswith("+") or "XP" in ltext[ltext.find(text):ltext.find(text) + len(text) + 4]:
                    continue
                if not RE_DMG.match(text):
                    continue
                digits = re.sub(r"\D", "", text)
                if int(digits) < self.min_value:
                    continue
                s = self.scale
                cx, cy, ch = (x + ww / 2) / s, (y + hh / 2) / s, hh / s
                y0, y1 = int(y / s), int((y + hh) / s)
                x0, x1 = int(x / s), int((x + ww) / s)
                patch = yellow[max(y0, 0):y1, max(x0, 0):x1]
                crit = bool(patch.size) and patch.mean() > 0.08
                formatted = bool(re.fullmatch(r"\d{1,3}(?:,\d{3})+", text))
                out.append((digits, cx, cy, ch, crit, formatted))
        return out

    def feed(self, bgr, now: float):
        dets = self.detect(bgr)
        for digits, cx, cy, ch, crit, formatted in dets:
            best = None
            for tr in self.tracks:
                if now - tr.last > 0.8:
                    continue
                if abs(cx - tr.x) > max(ch * 2.5, 40):
                    continue
                if not (tr.y - ch * 4 - 30 <= cy <= tr.y + ch * 0.8 + 5):
                    continue
                top = tr.readings.most_common(1)[0][0]
                if digits_similar(digits, top):
                    best = tr
                    break
            if best is None:
                best = Track(x=cx, y=cy, h=ch, first=now, last=now, y0=cy)
                self.tracks.append(best)
            best.x, best.y, best.last = cx, cy, now
            best.readings[digits] += 1
            best.crit_votes += 1 if crit else -1
            best.seen += 1
            best.formatted = best.formatted or formatted
        self.flush(now)
        return dets

    # Measured on recorded gameplay: damage numbers stay on screen <= ~0.5 s and barely move.
    # Anything visible longer is UI (panels, tooltips, gold counter) and must not count.
    MAX_LIFETIME_S = 1.2
    STATIC_MEMORY_S = 60.0
    OUTLIER_FACTOR = 25.0

    def _known_static(self, digits: str, x: float, y: float, now: float) -> bool:
        self.static_spots = [sp for sp in self.static_spots if sp[0] > now]
        return any(d == digits and abs(x - sx) < 15 and abs(y - sy) < 15 for _, d, sx, sy in self.static_spots)

    def flush(self, now: float, force: bool = False):
        keep = []
        for tr in self.tracks:
            if not force and now - tr.last < 0.6:
                keep.append(tr)
                continue
            # most frequent reading; ties -> longest (partial reads lose digits)
            value = max(tr.readings.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
            if tr.last - tr.first > self.MAX_LIFETIME_S:
                # UI number: remember the spot, it may flicker back as a "new" short track
                self.static_spots.append((now + self.STATIC_MEMORY_S, value, tr.x, tr.y))
                continue
            if self._known_static(value, tr.x, tr.y, now):
                continue
            # single sightings only count when cleanly read as "123,456"
            if not (tr.seen >= 2 or tr.formatted):
                continue
            v = float(value)
            if len(self.recent_values) >= 20:
                med = sorted(self.recent_values)[len(self.recent_values) // 2]
                if v > med * self.OUTLIER_FACTOR:  # misread (merged numbers, UI) - not a hit
                    self.rejected += 1
                    continue
            self.recent_values.append(v)
            self.on_hit(tr.first, v, tr.crit_votes > 0)
        self.tracks = keep


class OCRWorker(threading.Thread):
    """Live DPS: grabs the game window directly (works while covered) and feeds DamageOCR."""

    def __init__(self, state: GameState, capture: GameCapture, get_region):
        super().__init__(daemon=True)
        self.state = state
        self.capture = capture
        self.get_region = get_region  # fractions (x, y, w, h) of the game client area
        self.enabled = threading.Event()
        self.status = "DPS-OCR aus"
        self.fps = 0.0
        self.ocr = None
        self.suspend_until = 0.0

    def suspend(self, seconds: float):
        """Ignore the screen for a while (auto-opened panels show numbers that are not damage)."""
        self.suspend_until = max(self.suspend_until, time.time() + seconds)

    def run(self):
        try:
            self.ocr = DamageOCR(on_hit=self.state.add_hit)
        except Exception as e:
            self.status = f"OCR nicht verfügbar: {e}"
            return
        last = time.time()
        while True:
            if not self.enabled.is_set():
                self.status = "DPS-OCR aus"
                if self.ocr.tracks:
                    self.ocr.flush(time.time(), force=True)
                self.enabled.wait(1.0)
                continue
            t = time.time()
            if t < self.suspend_until:
                self.ocr.tracks = []  # drop half-seen numbers instead of counting them
                time.sleep(0.2)
                continue
            try:
                hwnd = self.capture.find()
                u = ctypes.windll.user32
                sig = (bool(hwnd and u.IsIconic(hwnd)), game_input.is_foreground(hwnd),
                       bool(hwnd and game_input.is_hidden(hwnd)))
                if sig != getattr(self, "_win_sig", sig):
                    self.suspend(2.0)  # window state changed: next frames may be stale or half-drawn
                self._win_sig = sig
                if t < self.suspend_until:
                    self.ocr.tracks = []
                    time.sleep(0.2)
                    continue
                frame = self.capture.grab()
                if frame is None:
                    self.status = self.capture.status
                    time.sleep(1.0)
                    continue
                H, W = frame.shape[:2]
                fx, fy, fw, fh = self.get_region()
                x0, y0 = int(fx * W), int(fy * H)
                x1, y1 = int((fx + fw) * W), int((fy + fh) * H)
                self.ocr.feed(np.ascontiguousarray(frame[y0:y1, x0:x1]), t)
                dt = time.time() - last
                last = time.time()
                self.fps = 0.8 * self.fps + 0.2 * (1 / dt if dt > 0 else 0)
                self.status = f"DPS-OCR an · {self.fps:.1f} fps"
            except Exception as e:
                self.status = f"OCR Fehler: {e}"
                time.sleep(1.0)
            time.sleep(max(0.0, 0.15 - (time.time() - t)))


class GoldWatcher(threading.Thread):
    """Every few seconds, OCRs the whole game window: reads the gold balance when the inventory
    footer is visible, and looks for a death screen text."""

    INTERVAL_S = 5.0

    def __init__(self, state: GameState, capture: GameCapture):
        super().__init__(daemon=True)
        self.state, self.capture = state, capture

    def run(self):
        while True:
            time.sleep(self.INTERVAL_S)
            try:
                frame = self.capture.grab()
                if frame is None:
                    continue
                lines = item_ocr.ocr_windows(frame)
                gold = item_ocr.find_gold(lines)
                if gold is not None:
                    self.state.observe_gold(time.time(), gold)
                death = item_ocr.find_death_text(lines)
                if death:
                    self.state.mark_death_seen(time.time(), death)
                hdr = item_ocr.find_log_header(lines)
                if hdr is not None:
                    self.state.ingest_log(item_ocr.read_log_panel(frame, hdr), time.time())
            except Exception:
                pass


class PanelReader(threading.Thread):
    """After every run: open inventory (gold balance) and log panel (stage, sales, drops, deaths)
    in the game, read them, and close what was opened.

    Deskrawl only reacts to real keys while it has focus (tested: faked focus messages are ignored),
    so in the background mode "always" briefly takes focus. While the game is hidden by the tracker
    the panels are left open; they only close when a death restarts the stage, so focus is taken
    only after a death.
    """
    MODES = {"always": "mit Fokuswechsel", "fg": "nur Vordergrund", "off": "Aus"}

    def __init__(self, state: GameState, capture: GameCapture, ocr: OCRWorker, cfg: dict):
        super().__init__(daemon=True)
        self.state, self.capture, self.ocr, self.cfg = state, capture, ocr, cfg
        self.status = ""
        self.lock = threading.Lock()
        self.focus_until = 0.0  # focus switch in progress until this time

    def run(self):
        while True:
            self.state.run_committed.wait()
            self.state.run_committed.clear()
            time.sleep(1.5)  # let the game write its log entry
            try:
                with self.lock:
                    self.read_once()
            except Exception as e:
                self.status = f"Panel-Fehler: {e}"

    def _look(self):
        frame = self.capture.grab()
        lines = item_ocr.ocr_windows(frame) if frame is not None else []
        return frame, lines, item_ocr.find_gold(lines) is not None, item_ocr.find_log_header(lines) is not None

    def _press(self, keys: list, want_open: bool, mode: str):
        """Press keys; returns (how, prev_focus) with how in fg/bg/focus or None if nothing worked."""
        hwnd = self.capture.find()
        if game_input.is_foreground(hwnd):
            game_input.press_keys(hwnd, keys, allow_focus_switch=False)
            return "fg", None
        if mode == "always":
            self.focus_until = time.time() + 5
            prev = game_input.press_keys(hwnd, keys, allow_focus_switch=True)
            if prev is not None:
                return "focus", prev
        return None, None

    def _keys(self):
        return self.cfg.get("panel_keys") or {"inventory": "I", "log": "C"}

    def read_once(self):
        mode = self.cfg.get("auto_panels", "always")
        mode = mode if mode in self.MODES else "always"
        frame, lines, inv, log = self._look()
        if frame is None:
            self.status = "Panels: " + self.capture.status
            return
        keys = []
        if mode != "off":
            if not inv:
                keys.append(self._keys()["inventory"])
            if not log:
                keys.append(self._keys()["log"])
        how, prev = None, None
        if keys:
            self.ocr.suspend(5.0)
            how, prev = self._press(keys, True, mode)
            if how is None:
                self.status = "Panels: Deskrawl nicht im Vordergrund – übersprungen"
                keys = []
            else:
                time.sleep(0.5)
                frame, lines, _, _ = self._look()
        try:
            now = time.time()
            gold = item_ocr.find_gold(lines)
            if gold is not None:
                self.state.observe_gold(now, gold)
            hdr = item_ocr.find_log_header(lines)
            if hdr is not None and frame is not None:
                self.state.ingest_log(item_ocr.read_log_panel(frame, hdr), now)
            if keys:
                label = {"fg": "Vordergrund", "focus": "Fokuswechsel"}[how]
                self.status = f"Panels gelesen {datetime.now().strftime('%H:%M:%S')} ({label})"
        finally:
            if keys:
                hwnd = self.capture.find()
                if not self.cfg.get("game_hidden"):  # visible game: close what we opened
                    game_input.press_keys(hwnd, keys, allow_focus_switch=(how == "focus"))
                game_input.restore_focus(prev)
                self.focus_until = time.time() + 1
                self.ocr.suspend(1.0)


# --------------------------------------------------------------------------- hotkeys

VK = {f"F{i}": 0x6F + i for i in range(1, 13)}
WM_HOTKEY = 0x0312


class ToastWatcher(threading.Thread):
    """Reads the pop-ups bottom-left ("Sold [..] for 228 gold", "Obtained [..]") a few times per second.

    Pop-ups stack and stay visible across several reads. Each read gives a multiset of pop-up texts;
    a pop-up counts when more copies of it are visible than in any of the last reads (so a pop-up
    missed by OCR in one read is not counted again, and two identical drops still count twice)."""

    INTERVAL_S = 0.4
    MEMORY = 4  # reads (~1.6 s)

    def __init__(self, state: GameState, capture: GameCapture):
        super().__init__(daemon=True)
        self.state, self.capture = state, capture
        self.history: deque = deque(maxlen=self.MEMORY)
        self.count = 0

    @staticmethod
    def _key(e):
        return re.sub(r"[^a-z0-9]", "", f"{e.get('action', '')}{e['item']}{e.get('gold', '')}".lower())

    def _canon(self, key):
        """Map an OCR variant onto a key seen recently (same pop-up, slightly different reading)."""
        import difflib
        for h in self.history:
            for k in h:
                if k == key or difflib.SequenceMatcher(None, k, key).ratio() > 0.88:
                    return k
        return key

    def run(self):
        while True:
            time.sleep(self.INTERVAL_S)
            try:
                frame = self.capture.grab()
                if frame is None:
                    continue
                entries = item_ocr.read_toasts(frame)
                now_counts, by_key = Counter(), {}
                for e in entries:
                    k = self._canon(self._key(e))
                    now_counts[k] += 1
                    by_key.setdefault(k, e)
                for k, n in now_counts.items():
                    new = n - max((h.get(k, 0) for h in self.history), default=0)
                    for _ in range(max(new, 0)):
                        self.count += 1
                        e = by_key[k]
                        if e["type"] == "sold":
                            self.state.add_toast_sale(time.time(), e)
                        else:
                            self.state.add_toast_drop(time.time(), e)
                self.history.append(now_counts)
            except Exception:
                pass


class ClickThroughGuard(threading.Thread):
    """While the game is hidden, keep it click-through. Deskrawl itself makes its panels
    clickable when the cursor is over them; invisible panels would then block the desktop.
    Polls every 10 ms, so the window is back to click-through before a click lands."""

    def __init__(self, capture: GameCapture, cfg: dict):
        super().__init__(daemon=True)
        self.capture, self.cfg = capture, cfg
        self.fixes = 0

    def run(self):
        while True:
            if self.cfg.get("game_hidden"):
                hwnd = self.capture.find()
                if hwnd and game_input.ensure_click_through(hwnd):
                    self.fixes += 1
                time.sleep(0.01)
            else:
                time.sleep(0.25)


class HotkeyThread(threading.Thread):
    """Global hotkeys (RegisterHotKey) - they fire while the game has focus."""

    def __init__(self, bindings: dict, out: queue.Queue):
        super().__init__(daemon=True)
        self.bindings = bindings  # {"F8": "item", ...}
        self.out = out
        self.failed = []

    def run(self):
        u = ctypes.windll.user32
        ids = {}
        for i, (key, action) in enumerate(self.bindings.items(), start=1):
            vk = VK.get(key.upper())
            if vk and u.RegisterHotKey(None, i, 0x4000, vk):  # MOD_NOREPEAT
                ids[i] = action
            else:
                self.failed.append(key)
        msg = ctypes.wintypes.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY and msg.wParam in ids:
                self.out.put(("hotkey", ids[msg.wParam]))


# --------------------------------------------------------------------------- region picker

class GameRegionPicker(tk.Toplevel):
    """Shows a snapshot of the game window; drag a rectangle over the area with damage numbers."""

    def __init__(self, master, frame, current, on_done):
        super().__init__(master)
        from PIL import Image, ImageTk
        self.title("DPS-Bereich wählen")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        H, W = frame.shape[:2]
        self.k = min(1100 / W, 650 / H, 1.0)
        img = Image.fromarray(frame[:, :, ::-1]).resize((int(W * self.k), int(H * self.k)))
        self.photo = ImageTk.PhotoImage(img)
        tk.Label(self, text="Rechteck über den Bereich ziehen, in dem Schadenszahlen erscheinen  ·  ESC = abbrechen",
                 bg=BG, fg=FG, font=("Segoe UI", 10)).pack(padx=8, pady=6)
        self.c = tk.Canvas(self, width=img.width, height=img.height, highlightthickness=0, cursor="crosshair")
        self.c.pack(padx=8, pady=(0, 8))
        self.c.create_image(0, 0, image=self.photo, anchor="nw")
        self.iw, self.ih = img.width, img.height
        fx, fy, fw, fh = current
        self.c.create_rectangle(fx * self.iw, fy * self.ih, (fx + fw) * self.iw, (fy + fh) * self.ih,
                                outline="#e0af68", width=2, dash=(4, 3))
        self.on_done = on_done
        self.start = None
        self.rect = None
        self.c.bind("<ButtonPress-1>", self._down)
        self.c.bind("<B1-Motion>", self._move)
        self.c.bind("<ButtonRelease-1>", self._up)
        self.bind("<Escape>", lambda e: self.destroy())
        self.focus_force()

    def _down(self, e):
        self.start = (e.x, e.y)
        self.rect = self.c.create_rectangle(e.x, e.y, e.x, e.y, outline="#00e5ff", width=3)

    def _move(self, e):
        if self.rect:
            self.c.coords(self.rect, *self.start, e.x, e.y)

    def _up(self, e):
        if not self.start:
            return
        x0, x1 = sorted((max(0, min(self.start[0], self.iw)), max(0, min(e.x, self.iw))))
        y0, y1 = sorted((max(0, min(self.start[1], self.ih)), max(0, min(e.y, self.ih))))
        self.destroy()
        if x1 - x0 > 20 and y1 - y0 > 15:
            self.on_done((x0 / self.iw, y0 / self.ih, (x1 - x0) / self.iw, (y1 - y0) / self.ih))


# --------------------------------------------------------------------------- UI

BG, PANEL, FG, MUTED, LINE = ui.BG, ui.PANEL, ui.FG, ui.MUTED, ui.LINE
C_XP = C_GOLD = C_ITEM = C_DPS = ui.ACCENT
C_GOOD, C_BAD, C_MEH, C_RUN = ui.GOOD, ui.BAD, ui.WARN, ui.GOOD
DEFAULT_REGION = (0.0, 0.42, 1.0, 0.52)  # lower part of the game window: the battle strip, full width
ITEMS_CSV = os.path.join(APP_DIR, "items_history.csv")
VERDICT_MARGIN = 2.0  # points; inside +/- margin counts as a sidegrade


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.cfg = load_config()
        self.events: queue.Queue = queue.Queue()
        self.state = GameState()
        self.tailer = LogTailer(self.state, self.cfg.get("log_path") or LOG_PATH)
        self.tailer.start()
        self.capture = GameCapture()
        self.region = tuple(self.cfg.get("game_region") or DEFAULT_REGION)
        self.ocr = OCRWorker(self.state, self.capture, lambda: self.region)
        self.ocr.start()
        GoldWatcher(self.state, self.capture).start()
        self.panels = PanelReader(self.state, self.capture, self.ocr, self.cfg)
        self.panels.start()
        ClickThroughGuard(self.capture, self.cfg).start()
        ToastWatcher(self.state, self.capture).start()
        self._last_item = None
        self.stage_stats = stages.StageStats()
        self.enemy_data = stages.load_enemies()
        self.char_stats = {k: tuple(v) for k, v in self.cfg.get("char_stats", {}).items()}
        self.char_stats_time = self.cfg.get("char_stats_time", "")
        self.item_history = []
        self.busy = False
        self.items_at_start = None
        self.hotkeys_cfg = self.cfg.get("hotkeys", {"F8": "item", "F9": "attributes", "F10": "hide"})
        if "hide" not in self.hotkeys_cfg.values():
            self.hotkeys_cfg["F10"] = "hide"
        self.boss_grace_until = 0.0
        self.hotkeys = HotkeyThread(self.hotkeys_cfg, self.events)
        self.hotkeys.start()
        self.hk = {v: k for k, v in self.hotkeys_cfg.items()}

        root.title("Deskrawl Tracker")
        root.configure(bg=BG)
        root.attributes("-topmost", self.cfg.get("topmost", True))
        if self.cfg.get("alpha", 1.0) < 1.0:
            root.attributes("-alpha", self.cfg["alpha"])
        geo = self.cfg.get("geometry", "")
        if geo and "-32000" not in geo:  # never restore a minimized position
            root.geometry(geo)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self._build()
        root.update_idletasks()
        self.hwnd = ctypes.windll.user32.GetParent(root.winfo_id())
        if self.cfg.get("ocr_on"):
            self.toggle_ocr()
        self._fill_char()
        self._fill_eval_tab()
        self.tick()
        self.poll_events()

    # -- layout --------------------------------------------------------------
    def _build(self):
        r = self.root
        ui.apply_style(r)
        head = tk.Frame(r, bg=BG)
        head.pack(fill="x", padx=14, pady=(10, 6))
        self.lbl_hero = tk.Label(head, bg=BG, fg=FG, font=("Bahnschrift SemiBold", 14), anchor="w")
        self.lbl_hero.pack(side="left")
        self.lbl_hero_sub = tk.Label(head, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_hero_sub.pack(side="left", padx=(10, 0), pady=(4, 0))
        dots = tk.Frame(head, bg=BG)
        dots.pack(side="right")
        self.dots = {}
        for key, label in (("log", "Log"), ("game", "Spiel"), ("dps", "DPS"), ("panels", "Panels"), ("keys", "Hotkeys")):
            d = ui.StatusDot(dots, label)
            d.pack(side="left", padx=(10, 0))
            self.dots[key] = d
        self.lbl_status = tk.Label(r, bg=BG, fg=C_MEH, font=ui.F_SMALL, anchor="e")  # transient messages

        self.nb = ui.SideNav(r)
        self.nb.pack(fill="both", expand=True)
        self.tab_farm = tk.Frame(self.nb, bg=BG)
        self.tab_items = tk.Frame(self.nb, bg=BG)
        self.tab_char = tk.Frame(self.nb, bg=BG)
        self.tab_w = tk.Frame(self.nb, bg=BG)
        self.tab_death = tk.Frame(self.nb, bg=BG)
        self.tab_drops = tk.Frame(self.nb, bg=BG)
        self.tab_stages = tk.Frame(self.nb, bg=BG)
        self.tab_gems = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_farm, text="Übersicht")
        self.nb.add(self.tab_stages, text="Stages")
        self.nb.add(self.tab_items, text="Items")
        self.nb.add(self.tab_drops, text="Drops")
        self.nb.add(self.tab_death, text="Tode")
        self.nb.add(self.tab_char, text="Charakter")
        self.nb.add(self.tab_gems, text="Edelsteine")
        self.nb.add(self.tab_w, text="Bewertung")
        self._build_farm(self.tab_farm)
        self._build_items(self.tab_items)
        self._build_char(self.tab_char)
        self._build_weights(self.tab_w)
        self._build_deaths(self.tab_death)
        self._build_drops(self.tab_drops)
        self._build_stages(self.tab_stages)
        self._build_gems(self.tab_gems)

        side = self.nb.bottom
        tk.Label(side, text="Steuerung", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=(0, 4))

        def ctl(text, cmd, tip):
            b = ui.button(side, text, cmd, small=True)
            b.configure(anchor="w", bg=PANEL)
            b.bind("<Leave>", lambda e: b.configure(bg=PANEL))
            b.pack(fill="x", padx=6, pady=1)
            ui.Tooltip(b, tip)
            return b

        self.btn_hide = ctl("", self.toggle_hide, "Deskrawl unsichtbar weiterlaufen lassen statt minimieren – "
                                                  "nur so kann der Tracker weiter mitlesen.")
        self.btn_ocr = ctl("", self.toggle_ocr, "Schadenszahlen im Spiel mitlesen (für DPS pro Run).")
        ctl("DPS-Bereich wählen", self.pick_region, "Bereich im Spielbild, in dem Schadenszahlen erscheinen.")
        self.btn_panels = ctl("", self.cycle_panels, "Nach jedem Run Inventar (I) und Log (C) öffnen und lesen. "
                                                     "„mit Fokuswechsel“ holt Deskrawl dafür kurz nach vorne.")
        self.btn_top = ctl("", self.toggle_topmost, "Tracker-Fenster immer im Vordergrund halten.")
        ctl("Log-Datei ändern", self.open_setup, "Pfad zur Game.log von Deskrawl wählen und prüfen, ob die "
                                                 "Windows-Texterkennung Englisch installiert ist.")
        ctl("Session zurücksetzen", self.reset, "Raten und Run-Liste der laufenden Session leeren. "
                                                "Stage-Statistik und Verläufe bleiben erhalten.")
        if setup_dialog.needs_setup(self.cfg):
            self.root.after(400, lambda: self.open_setup(first_run=True))
        self._update_panels_btn()
        self._update_hide_btn()
        self._update_top_btn()
        self.btn_ocr.configure(text="DPS-Messung: aus")

    def open_setup(self, first_run=False):
        setup_dialog.SetupDialog(self.root, self.cfg, self._log_chosen, first_run=first_run)

    def _log_chosen(self, path):
        save_config(self.cfg)
        if os.path.normcase(path) != os.path.normcase(self.tailer.path):
            self.tailer.set_path(path)

    def _btn(self, parent, text, cmd, side="left"):
        b = ui.button(parent, text, cmd)
        b.pack(side=side, padx=3)
        return b

    def _tree(self, parent, cols, height, icons=False):
        f = tk.Frame(parent, bg=PANEL)
        tree = ui.ZTree(f, columns=[c[0] for c in cols], show="tree headings" if icons else "headings",
                        height=height, style="Icons.Treeview" if icons else "Treeview")
        if icons:
            tree.column("#0", width=48, minwidth=48, stretch=False, anchor="center")
        for key, title, w, anchor in cols:
            tree.heading(key, text=title, anchor=anchor)
            tree.column(key, width=w, anchor=anchor, stretch=True)
        sb = ttk.Scrollbar(f, orient="vertical", command=tree.yview)

        def yset(first, last):
            # scrollbar only while there is something to scroll
            sb.set(first, last)
            if float(first) <= 0 and float(last) >= 1:
                sb.pack_forget()
            elif not sb.winfo_ismapped():
                sb.pack(side="right", fill="y", before=tree)

        tree.configure(yscrollcommand=yset)
        tree.pack(side="left", fill="both", expand=True)
        return f, tree

    def _section(self, parent, text, info_text=None):
        row = tk.Frame(parent, bg=BG)
        row.pack(fill="x", padx=14, pady=(10, 4))
        tk.Label(row, text=text, bg=BG, fg=MUTED, font=("Bahnschrift", 11)).pack(side="left")
        if info_text:
            ui.info(row, info_text).pack(side="left", padx=(6, 0))
        return row

    def _build_farm(self, p):
        # the one loud element: time to the next level
        lv = ui.card(p, fill="x", padx=14, pady=(14, 8))
        top = tk.Frame(lv, bg=PANEL)
        top.pack(fill="x", padx=16, pady=(12, 0))
        self.lbl_lvl_eta = tk.Label(top, text="-", bg=PANEL, fg=ui.ACCENT, font=ui.F_NUM_XL)
        self.lbl_lvl_eta.pack(side="left")
        self.lbl_lvl_title = tk.Label(top, text="bis zum nächsten Level", bg=PANEL, fg=FG, font=ui.F_BODY)
        self.lbl_lvl_title.pack(side="left", padx=(12, 0), pady=(12, 0))
        self.lbl_lvl_runs = tk.Label(top, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, justify="right")
        self.lbl_lvl_runs.pack(side="right", pady=(12, 0))
        self.lvl_bar = tk.Canvas(lv, height=12, bg=LINE, highlightthickness=0)
        self.lvl_bar.pack(fill="x", padx=16, pady=(8, 4))
        self.lbl_lvl_sub = tk.Label(lv, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_lvl_sub.pack(fill="x", padx=16, pady=(0, 12))

        self.lbl_run = tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_run.pack(fill="x", padx=16, pady=(0, 4))
        tiles = tk.Frame(p, bg=BG)
        tiles.pack(fill="x", padx=10)
        self.tiles = {}
        spec = [("xp_h", "EXP pro Stunde"), ("gold_h", "Gold pro Stunde"), ("runs_h", "Runs pro Stunde"),
                ("items_h", "Items pro Stunde"), ("run_dps", "Ø DPS pro Run"), ("deaths", "Tode pro Stunde")]
        for i, (key, title) in enumerate(spec):
            f = ui.card(tiles)
            f.grid(row=i // 3, column=i % 3, sticky="nsew", padx=4, pady=4)
            tiles.columnconfigure(i % 3, weight=1, uniform="t")
            tk.Label(f, text=title, bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(anchor="w", padx=12, pady=(10, 0))
            v = tk.Label(f, text="-", bg=PANEL, fg=FG, font=ui.F_NUM)
            v.pack(anchor="w", padx=12)
            sub = ui.autowrap(tk.Label(f, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, justify="left", anchor="w"))
            sub.pack(fill="x", padx=12, pady=(0, 10))
            self.tiles[key] = (v, sub)
        self.lbl_totals = ui.autowrap(tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_totals.pack(fill="x", padx=16, pady=(8, 0))
        self._section(p, "Letzte Runs", "„≈“ vor der Stage: angenommen vom vorherigen Run, weil das Log-Fenster "
                                         "für diesen Run nicht gelesen werden konnte.")
        cols = [("t", "Ende", 68, "e"), ("stage", "Stage", 170, "w"), ("diff", "Diff", 74, "w"),
                ("dur", "Zeit", 48, "e"), ("xp", "EXP", 64, "e"), ("gold", "Gold", 56, "e"), ("it", "Items", 44, "e"),
                ("dps", "Ø DPS", 62, "e")]
        f, self.tree = self._tree(p, cols, 8)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def _build_items(self, page):
        hint = (f"Im Spiel mit der Maus über ein Item fahren, bis der Vergleichs-Tooltip offen ist, dann "
                f"{self.hk.get('item', 'F8')} drücken. Für genaue Werte vorher den Charakter mit "
                f"{self.hk.get('attributes', 'F9')} einlesen.\n\nModus: worauf es dir gerade ankommt – Schaden, "
                f"Überleben, beides oder Farmen (Gold/Items/EXP).\nRoll-% hinter einem Stat: wie gut er gewürfelt "
                f"ist (0 % schlechtester, 100 % bester möglicher Wert).")
        hdr = ui.page_header(page, "Item-Vergleich", hint)
        self.var_mode = tk.StringVar(value=self.cfg.get("item_mode", "Ausgewogen"))
        cb = ttk.Combobox(hdr, textvariable=self.var_mode, values=list(item_eval.MODES), width=11, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", self._mode_changed)
        tk.Label(hdr, text="Modus", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(14, 6))
        self.var_upg = tk.StringVar(value=self.cfg.get("item_upgrade", "+0"))
        cbu = ttk.Combobox(hdr, textvariable=self.var_upg, values=["+0", "+3", "+4", "+5", "+7", "+10"], width=4,
                           state="readonly")
        cbu.pack(side="right")
        cbu.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("item_upgrade", self.var_upg.get()), self._mode_changed()))
        lab = tk.Label(hdr, text="Upgrade", bg=BG, fg=MUTED, font=ui.F_SMALL)
        lab.pack(side="right", padx=(0, 6))
        ui.Tooltip(lab, "Beide Items auf diese Upgrade-Stufe hochrechnen (das ersetzte Item wird als +0 angenommen).")

        sf = ui.ScrollFrame(page)
        sf.pack(fill="both", expand=True)
        p = sf.inner

        # 1. the two items, laid out like the game's comparison tooltip
        self.cards = tk.Frame(p, bg=BG)
        self.cards.pack(fill="x", padx=10, pady=(0, 4))
        self.cards.columnconfigure(0, weight=1, uniform="c")
        self.cards.columnconfigure(1, weight=1, uniform="c")
        self.card_new = ui.card(self.cards)
        self.card_new.grid(row=0, column=0, sticky="nsew", padx=4)
        self.card_old = ui.card(self.cards)
        self.card_old.grid(row=0, column=1, sticky="nsew", padx=4)
        tk.Label(self.card_new, text=f"{self.hk.get('item', 'F8')} im Spiel über einem Item drücken",
                 bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(padx=12, pady=20)

        # 2. stat differences
        self._section(p, "Unterschiede")
        cols = [("stat", "Stat", 170, "w"), ("d", "Änderung", 80, "e"), ("roll", "Roll", 55, "e"),
                ("eff", "Wirkung", 290, "w")]
        f, self.item_tree = self._tree(p, cols, 5)
        f.pack(fill="x", padx=14, pady=(0, 4))

        # 3. effects
        self.fx_head = self._section(p, "Effekte")
        self.lbl_fx = ui.autowrap(tk.Label(p, bg=BG, fg=C_MEH, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_fx.pack(fill="x", padx=16)

        # 4. damage / survival / income
        self._section(p, "Einstufung")
        met = tk.Frame(p, bg=BG)
        met.pack(fill="x", padx=10)
        self.lbl_m = {}
        for i, (key, title) in enumerate((("dps", "Schaden"), ("surv", "Überleben"), ("farm", "Ertrag"))):
            f = ui.card(met)
            f.grid(row=0, column=i, sticky="nsew", padx=4)
            met.columnconfigure(i, weight=1, uniform="m")
            tk.Label(f, text=title, bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(anchor="w", padx=12, pady=(8, 0))
            v = tk.Label(f, text="-", bg=PANEL, fg=FG, font=ui.F_NUM_M)
            v.pack(anchor="w", padx=12)
            sub = ui.autowrap(tk.Label(f, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, justify="left", anchor="w"))
            sub.pack(fill="x", padx=12, pady=(0, 8))
            self.lbl_m[key] = (v, sub)

        # 5. verdict
        self._section(p, "Urteil")
        vc = ui.card(p, fill="x", padx=14, pady=(0, 4))
        vrow = tk.Frame(vc, bg=PANEL)
        vrow.pack(fill="x", padx=14, pady=(10, 0))
        self.lbl_verdict = tk.Label(vrow, text="-", bg=PANEL, fg=FG, font=("Bahnschrift SemiBold", 18), anchor="w")
        self.lbl_verdict.pack(side="left")
        self.lbl_sure = tk.Label(vrow, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_sure.pack(side="left", padx=(12, 0), pady=(6, 0))
        self.lbl_reason = ui.autowrap(tk.Label(vc, text="", bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left"), 28)
        self.lbl_reason.pack(fill="x", padx=14)
        self.lbl_item = ui.autowrap(tk.Label(vc, text="", bg=PANEL, fg=C_MEH, font=ui.F_SMALL, anchor="w", justify="left"), 28)
        self.lbl_item.pack(fill="x", padx=14)
        self.lbl_quality = ui.autowrap(tk.Label(vc, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"), 28)
        self.lbl_quality.pack(fill="x", padx=14, pady=(2, 10))

        # 6. history
        self._section(p, "Zuletzt geprüft", "Anklicken zeigt das Item wieder oben an. Spaltenüberschrift anklicken "
                                            "sortiert.")
        cols = [("name", "Item", 210, "w"), ("dps", "Schaden", 92, "e"), ("surv", "Überleben", 100, "e"),
                ("v", "Urteil", 120, "w")]
        f, self.hist_tree = self._tree(p, cols, 6, icons=True)
        self.hist_tree.tag_configure("good", foreground=C_GOOD)
        self.hist_tree.tag_configure("bad", foreground=C_BAD)
        self.hist_tree.tag_configure("meh", foreground=C_MEH)
        self._hist_icons = []
        f.pack(fill="x", padx=14, pady=(0, 14))
        self.hist_tree.bind("<<TreeviewSelect>>", self._hist_select)
        self._icons = []

    def _fill_card(self, card, title, it, rolls, note="", gems=None):
        """One item drawn like the in-game tooltip. it: item_ocr.ItemData (None: only title + note)."""
        for w in card.winfo_children():
            w.destroy()
        tk.Label(card, text=title, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=12, pady=(8, 0))
        if it is None:
            ui.autowrap(tk.Label(card, text=note, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left")).pack(
                fill="x", padx=12, pady=(4, 10))
            return
        rarity, slot, ancient = item_quality.item_kind(it.type_line)
        rcol = ui.RARITY.get(rarity or "", FG)
        head = tk.Frame(card, bg=PANEL)
        head.pack(fill="x", padx=12, pady=(4, 6))
        box = tk.Frame(head, bg=rcol, width=58, height=58)
        box.pack(side="left")
        box.pack_propagate(False)
        ph = self._photo(it.icon, 54)
        tk.Label(box, image=ph or "", text="" if ph else "?", bg=PANEL, fg=MUTED).pack(expand=True, fill="both", padx=2, pady=2)
        nm = tk.Frame(head, bg=PANEL)
        nm.pack(side="left", fill="x", expand=True, padx=(10, 0))
        lab = tk.Label(nm, text=it.name if it.name_sure else f"{it.name} ?", bg=PANEL, fg=rcol,
                       font=("Bahnschrift SemiBold", 12), anchor="w", justify="left")
        ui.autowrap(lab).pack(fill="x")
        if not it.name_sure:
            ui.Tooltip(lab, f"Name nicht sicher erkannt (gelesen: \"{it.raw_name}\"). Die Werte betrifft das nicht.")
        tk.Label(nm, text=it.type_line, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x")
        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="x", padx=12)

        def line(st, col, show_roll):
            r = tk.Frame(body, bg=PANEL)
            r.pack(fill="x")
            unit = "%" if st.pct else ""
            txt = f"+{st.value:g}{unit} {st.name}"  # the change itself is in the "Unterschiede" table
            lab = tk.Label(r, text=txt + ("  ?" if not st.ok else ""), bg=PANEL, fg=col if st.ok else ui.WARN,
                           font=ui.F_SMALL, anchor="w")
            lab.pack(side="left")
            if not st.ok:
                ui.Tooltip(lab, f"Wert passt nicht zu Item-Level und Seltenheit – vermutlich falsch gelesen "
                                f"(OCR: \"{st.raw}\"). Wird trotzdem verwendet.")
            q = rolls.get(st.name) if show_roll else None
            if q:
                qc = ui.GOOD if q[1] >= 75 else (MUTED if q[1] >= 35 else ui.BAD)
                tk.Label(r, text=f"{q[1]:.0f} %", bg=PANEL, fg=qc, font=ui.F_SMALL).pack(side="right")

        # base values laid out like the game: Armor / Damage big, then "Speed ... DPS" on one row
        row = None
        for st in it.base:
            big = st.name in ("Armor", "Weapon Damage")
            label = {"Armor": "Armor", "Weapon Damage": "Waffenschaden", "Weapon Speed": "Speed",
                     "Weapon DPS": "DPS"}.get(st.name, st.name)
            if st.name != "Weapon DPS" or row is None:
                row = tk.Frame(body, bg=PANEL)
                row.pack(fill="x")
            side = "right" if st.name == "Weapon DPS" else "left"
            if st.diff is not None and side == "right":
                tk.Label(row, text=f"({st.diff:+g})", bg=PANEL, fg=ui.GOOD if st.diff > 0 else ui.BAD,
                         font=ui.F_SMALL).pack(side="right", padx=(6, 0))
            tk.Label(row, text=f"{st.value:g} {label}", bg=PANEL, fg=FG if st.ok else ui.WARN,
                     font=("Bahnschrift SemiBold", 13) if big else ui.F_SMALL, anchor="w").pack(side=side)
            if st.diff is not None and side == "left":
                tk.Label(row, text=f"({st.diff:+g})", bg=PANEL, fg=ui.GOOD if st.diff > 0 else ui.BAD,
                         font=ui.F_SMALL).pack(side="left", padx=(6, 0))
        for label, rows, col, roll in (("Primär", it.primary, "#a9b4ff", True), ("Sekundär", it.secondary, "#a9b4ff", True),
                                       ("Sockel", it.sockets, ui.GOOD, False)):
            if not rows and not (label == "Sockel" and it.empty_sockets):
                continue
            tk.Label(body, text=label, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", pady=(4, 0))
            for st in rows:
                line(st, col, roll)
            if label == "Sockel" and it.empty_sockets:
                fill = (gems or {}).get("fill")
                for _ in range(it.empty_sockets):
                    if fill:
                        t = f"leer → +{fill[2]:g}{'%' if fill[3] else ''} {fill[1]}"
                    else:
                        t = "leerer Sockel"
                    lab = tk.Label(body, text=t, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w")
                    lab.pack(fill="x")
                    if fill:
                        ui.Tooltip(lab, f"Vorschlag: {fill[0]} – bester Edelstein der gewählten Stufe (Tab "
                                        f"Edelsteine) für diesen Modus. Ist in der Bewertung schon eingerechnet.")
        for fx in it.effects:
            ui.autowrap(tk.Label(body, text=fx, bg=PANEL, fg=ui.RARITY["Legendary"], font=ui.F_SMALL, anchor="w",
                                 justify="left")).pack(fill="x", pady=(4, 0))
        req = it.req_level or (min(70, it.item_level // 10) if it.item_level else None)
        lvl = self.state.level or (self.char_stats.get("Level") or (0,))[0]
        foot = tk.Frame(card, bg=PANEL)
        foot.pack(fill="x", padx=12, pady=(6, 10))
        if req:
            tk.Label(foot, text=f"benötigt Level {req}", bg=PANEL, fg=ui.BAD if lvl and req > lvl else MUTED,
                     font=ui.F_SMALL).pack(side="right")
        if it.item_level:
            tk.Label(foot, text=f"Item-Level {it.item_level}", bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        else:
            tk.Label(foot, text="Item-Level nicht gelesen", bg=PANEL, fg=ui.WARN, font=ui.F_SMALL).pack(side="left")

    def _mode_changed(self, _=None):
        self._set_cfg("item_mode", self.var_mode.get())
        self._fill_gems()
        if self._last_item is not None:
            self._show_item(self._last_item)

    def _photo(self, bgr, size):
        if bgr is None:
            return None
        from PIL import Image, ImageTk
        img = Image.fromarray(bgr[:, :, ::-1]).resize((size, size))
        ph = ImageTk.PhotoImage(img)
        self._icons.append(ph)
        self._icons = self._icons[-6:]
        return ph

    def _build_char(self, p):
        key = self.hk.get("attributes", "F9")
        hdr = ui.page_header(p, "Charakter", f"Im Spiel das Charakterfenster mit dem Tab „Attributes“ öffnen und {key} "
                                             f"drücken. Dann nach unten scrollen und nochmal {key} drücken – die Werte "
                                             f"werden zusammengeführt. Nach jedem Ausrüstungswechsel neu einlesen.")
        self._btn(hdr, "Leeren", self.clear_char, side="right")
        b = ui.button(hdr, f"Einlesen ({key})", self.scan_attributes, accent=True)
        b.pack(side="right", padx=3)
        self.lbl_char = tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_char.pack(fill="x", padx=16)
        cols = [("stat", "Stat", 240, "w"), ("v", "Wert", 110, "e")]
        f, self.char_tree = self._tree(p, cols, 16)
        f.pack(fill="both", expand=True, padx=14, pady=(6, 12))

    RARITY_TAG = {"Common": "meh", "Uncommon": "blue", "Rare": "rare", "Legendary": "leg",
                  **{f"Edelstein Stufe {i}": "gem" for i in range(1, 7)},
                  "Set-Rune": "leg", "Fähigkeits-Rune": "rare", "Attribut-Rune": "blue", "Schatzschlüssel": "rare"}

    def _build_gems(self, p):
        g = item_quality.GEMS
        sockets = ", ".join(f"{k} {v}" for k, v in g.get("sockets", {}).items() if v)
        sc = g.get("socket_cost", {})
        sc_txt = (" + ".join(f"{v} {k}" for k, v in sc.items() if k not in ("gold", "per") and isinstance(v, (int, float)))
                  + (f" + {sc['gold'].replace('item level', 'Item-Level').replace(' x ', ' × ')} Gold" if isinstance(sc.get("gold"), str) else "")
                  ) if isinstance(sc, dict) else str(sc)
        top = ui.page_header(p, "Edelsteine", (
            f"Was ein Edelstein der gewählten Stufe in jedem Slot-Typ für deinen Charakter bringt (im Modus aus "
            f"dem Item-Vergleich). Steine ohne Wirkung sind ausgeblendet. ★ = bester Stein für den Slot-Typ.\n\n"
            f"Sockel pro Slot: {sockets}. Sockel ab Item-Level 300, Ancient ab 300 mit allen Sockeln.\n"
            f"Sockel hinzufügen: {sc_txt} pro Sockel.\nKombinieren: {g.get('combine', '?')}"))
        self._btn(top, "Neu berechnen", self._fill_gems, side="right")
        self.var_gem_tier = tk.StringVar(value=str(self.cfg.get("gem_tier", 3)))
        cb = ttk.Combobox(top, textvariable=self.var_gem_tier, values=[str(i) for i in range(1, 7)], width=3,
                          state="readonly")
        cb.pack(side="right", padx=(0, 10))
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("gem_tier", int(self.var_gem_tier.get())),
                                                    self._fill_gems()))
        tk.Label(top, text="Stufe", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(0, 6))
        self.lbl_gems = ui.autowrap(tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_gems.pack(fill="x", padx=16, pady=(0, 6))
        cols = [("slot", "Sockel in", 120, "w"), ("gem", "Edelstein", 90, "w"), ("bonus", "Bonus", 190, "w"),
                ("eff", "Wirkung", 200, "w")]
        f, self.gem_tree = self._tree(p, cols, 12)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self._fill_gems()

    def _unused_gem_text(self, p):
        g = item_quality.GEMS
        sockets = ", ".join(f"{k} {v}" for k, v in g.get("sockets", {}).items() if v)
        sc = g.get("socket_cost", {})
        sc_txt = (" + ".join(f"{v} {k}" for k, v in sc.items() if k not in ("gold", "per") and isinstance(v, (int, float)))
                  + (f" + {sc['gold'].replace('item level', 'Item-Level').replace(' x ', ' × ')} Gold" if isinstance(sc.get("gold"), str) else "")
                  ) if isinstance(sc, dict) else str(sc)
        tk.Label(p, bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w", justify="left", wraplength=640, text=(
            f"Wert = Wirkung eines Edelsteins der gewählten Stufe auf deinen Charakter im gewählten Item-Modus "
            f"(Tab Items). ★ = bester Stein für diesen Slot-Typ.\nSockel pro Slot: {sockets}. Sockel ab Item-Level 300; "
            f"Ancient ab 300 mit allen Sockeln. Sockel hinzufügen: {sc_txt} pro Sockel. "
            f"Kombinieren: {g.get('combine', '?')}")).pack(fill="x", padx=6, pady=(0, 6))
        self._fill_gems()

    def _fill_gems(self):
        tier = int(self.var_gem_tier.get())
        mode = self.cfg.get("item_mode", "Ausgewogen")
        ctx = self._eval_context()
        self.gem_tree.delete(*self.gem_tree.get_children())
        if not ctx.char:
            self.lbl_gems.configure(text="Charakter mit F9 einlesen, dann werden die Edelsteine bewertet.")
        else:
            self.lbl_gems.configure(text=f"Bewertet für {ctx.main}, {ctx.elem}-Schaden, Modus {mode}")
        label = {"weapon": "Waffe", "armor": "Helm/Brust/Hose", "accessory": "Ring/Kette"}
        rows = []
        for gem, group, stat, val, pct, name in item_quality.gem_options(tier):
            bonus = f"{stat} +{val:g}{'%' if pct else ''}"
            score, eff = -1e9, "-"
            if ctx.char:
                ev = item_eval.evaluate_deltas({stat: (float(val), pct)}, ctx, mode)
                score = ev.score
                parts = [f"{ev.dps_pct:+.1f} % Schaden" if abs(ev.dps_pct) >= 0.05 else "",
                         f"{ev.surv_pct:+.1f} % Überleben" if abs(ev.surv_pct) >= 0.05 else "",
                         f"{ev.farm_pct:+.1f} % Ertrag" if abs(ev.farm_pct) >= 0.05 else ""]
                eff = " · ".join(x for x in parts if x) or "kein Effekt"
            rows.append((group, score, gem, bonus, eff))
        order = {"weapon": 0, "armor": 1, "accessory": 2}
        rows.sort(key=lambda r: (order[r[0]], -r[1]))
        seen = set()
        for group, score, gem, bonus, eff in rows:
            if ctx.char and eff == "kein Effekt":
                continue  # no use for this character
            first = group not in seen and score > 0
            seen.add(group)
            self.gem_tree.insert("", "end", tags=("good",) if first else ("meh",) if score <= 0 else (),
                                 values=(label[group], ("★ " if first else "") + gem, bonus, eff))

    def _build_stages(self, p):
        ui.page_header(p, "Stages", "Alle Runs pro Stage und Schwierigkeit, über Neustarts hinweg. Gold inklusive "
                                    "Verkäufe. ★ = beste Stage für EXP bzw. Gold (ab 3 Runs).\n\nZeile anklicken: "
                                    "Gegner und ihre Schadensarten, Item-Level der Drops und eine Prognose für die "
                                    "nächste Schwierigkeit.")
        cols = [("stage", "Stage", 200, "w"), ("diff", "Diff", 58, "w"), ("runs", "Runs", 56, "e"),
                ("t", "Ø Zeit", 64, "e"), ("xp", "EXP/h", 75, "e"), ("gold", "Gold/h", 70, "e"),
                ("it", "Items/h", 66, "e"), ("dead", "Tode", 56, "e"), ("dps", "Ø DPS", 70, "e")]
        f, self.stage_tree = self._tree(p, cols, 9)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.stage_tree.tag_configure("best", foreground=C_GOOD)
        self.stage_tree.bind("<<TreeviewSelect>>", self._stage_select)
        det = ui.card(p, fill="x", padx=14, pady=(0, 12))
        self.lbl_stage_detail = ui.autowrap(tk.Label(det, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left",
                                                     text="Stage in der Liste anklicken für Details."), 24)
        self.lbl_stage_detail.pack(fill="x", padx=12, pady=10)
        self._stage_rows = []
        self._stage_sig = None

    def _aggregate_stages(self):
        """Count runs into the persistent stage statistics once their stage is known (or 2.5 min passed)."""
        st = self.state
        now = time.time()
        changed = False
        with st.lock:
            runs = list(st.runs)
            sold = list(st.sold)
        prev = None
        for r in runs:
            if r.aggregated or not r.start or not r.end:
                prev = r
                continue
            if not r.stage_name and now - r.end < 150:
                break  # wait for the log panel to name the stage
            stage = r.stage_name or r.stage_guess or f"Unbekannt ({r.waves} Waves)"
            cycle = r.end - prev.end if prev and prev.end and 0 < r.end - prev.end < r.duration * 1.5 + 60 else r.duration
            t0 = prev.end if prev and prev.end else r.start
            sold_gold = sum(g for t, _, g, _ in sold if t0 < t <= r.end + 10)
            self.stage_stats.add_run(stage, r.difficulty, cycle, r.xp, r.gold, sold_gold, r.items,
                                     r.death == "bestätigt" or r.death == "vermutet",
                                     r.damage, r.duration if r.damage else 0)
            r.aggregated = True
            changed = True
            prev = r
        if changed:
            self.stage_stats.save()
        rows = self.stage_stats.rows()
        sig = tuple((x["stage"], x["difficulty"], x["runs"]) for x in rows)
        if sig == self._stage_sig:
            return
        self._stage_sig = sig
        self._stage_rows = sorted(rows, key=lambda x: -x["xp_h"])
        best_xp = max((x["xp_h"] for x in rows if x["runs"] >= 3), default=None)
        best_gold = max((x["gold_h"] for x in rows if x["runs"] >= 3), default=None)
        self.stage_tree.delete(*self.stage_tree.get_children())
        for i, x in enumerate(self._stage_rows):
            star_x = " ★" if best_xp and x["xp_h"] == best_xp else ""
            star_g = " ★" if best_gold and x["gold_h"] == best_gold else ""
            self.stage_tree.insert("", "end", iid=str(i), tags=("best",) if star_x or star_g else (), values=(
                x["stage"], {"Nightmare": "NM", "Inferno": "Inf"}.get(x["difficulty"], x["difficulty"]), x["runs"],
                fmt_dur(x["avg_s"]), fmt(x["xp_h"]) + star_x,
                fmt(x["gold_h"]) + star_g, f"{x['items_h']:.1f}", f"{x['death_rate'] * 100:.0f} %",
                fmt(x["dps"]) if x["dps"] else "-"))

    def _stage_select(self, _=None):
        sel = self.stage_tree.selection()
        if not sel:
            return
        x = self._stage_rows[int(sel[0])]
        info = stages.stage_info(x["stage"], self.enemy_data)
        parts = [f"{x['stage']} · {x['difficulty']} · {x['runs']} Runs · Ø {fmt(x['xp_run'])} EXP/Run"]
        if info:
            prof = stages.damage_profile(info)
            lv = f"Level {info.get('level_min')}–{info.get('level_max')}" if info.get("level_min") else ""
            dmg = ", ".join(f"{k} {v * 100:.0f} %" for k, v in sorted(prof.items(), key=lambda kv: -kv[1])) or "unbekannt"
            dot = ", ".join(info.get("dot") or []) or "keine bekannt"
            parts.append(f"Gegner: {lv} · Schadensarten: {dmg} · Schaden über Zeit: {dot}"
                         + (f" · Boss: {info['boss']}" if info.get("boss") else ""))
            if info.get("enemies"):
                parts.append("Gegnertypen: " + ", ".join(info["enemies"][:12]))
            lo, hi = item_quality.drop_item_level(info.get("level_min") or 70, x["difficulty"])
            loot = [f"Drops: Item-Level {lo}–{hi} (tragbar ab Lvl {min(70, lo // 10)})"]
            if info.get("boss"):
                blvl = 70 if x["difficulty"] != "Normal" else (info.get("level_max") or 70)
                shards = item_quality.soul_shards(blvl, x["difficulty"])
                per_h = shards * 3600 / x["avg_s"] if x.get("avg_s") else 0
                loot.append(f"Boss {info['boss']}: {shards} Soul Shards pro Boss-Kill (≈ {per_h:.0f}/h)")
            parts.append(" · ".join(loot))
        else:
            parts.append("Gegnerdaten: für diese Stage nicht in data/enemies.json gefunden.")
        level = info.get("level_max") if info else None
        fc = stages.forecast(x, level)
        if fc:
            parts.append(
                f"Prognose {fc['difficulty']} (gleiche Ausrüstung): Gegner {fc['hp_factor']:.1f}× HP, "
                f"{fc['dmg_factor']:.2f}× Schaden → Run ≈ {fmt_dur(fc['run_s'])}, EXP/h ≈ {fmt(fc['xp_h'])} "
                f"(jetzt {fmt(x['xp_h'])}), Gold/h ≈ {fmt(fc['gold_h'])} (jetzt {fmt(x['gold_h'])}).")
            need = fc["dmg_factor"]
            parts.append(
                f"Um dort gleich sicher zu sein, bräuchtest du ≈ {need:.2f}× deine heutige effektive HP "
                f"(Toughness {fmt(self._toughness() or 0)} → ≈ {fmt((self._toughness() or 0) * need)})"
                + (f" · heute {x['death_rate'] * 100:.0f} % Tode" if x["runs"] else "") + "."
                + ("" if fc["stage_level_known"] else " Stage-Level unbekannt: Level-Anstieg auf 70 nicht eingerechnet."))
        self.lbl_stage_detail.configure(text="\n".join(parts))

    def _toughness(self):
        c = {k: v[0] for k, v in self.char_stats.items()}
        if not c:
            return None
        ctx = self._eval_context()
        return item_eval.defense(c, c, ctx)["toughness"]

    def _build_drops(self, p):
        hdr = ui.page_header(p, "Drops", "Quelle: Einblendungen unten links im Spiel („Sold …“, „Obtained …“), "
                                         "ersatzweise das Log-Fenster. Rarity aus der Farbe des Namens: weiß Common, "
                                         "blau Uncommon, gelb Rare, orange Legendary. Edelsteine nach Stufe "
                                         "(Raw Sphere 1 bis Radiant Octagon 6).")
        self.lbl_drops = tk.Label(hdr, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="e")
        self.lbl_drops.pack(side="right")
        cols = [("r", "Rarity", 120, "w"), ("n", "Anzahl", 70, "e"), ("h", "pro h", 70, "e"),
                ("pct", "Anteil", 70, "e"), ("sold", "verkauft", 70, "e"), ("gold", "Verkaufsgold", 100, "e")]
        f, self.drop_tree = self._tree(p, cols, 6)
        f.pack(fill="x", padx=14, pady=(0, 4))
        self._section(p, "Letzte Funde")
        cols = [("t", "Zeit", 62, "e"), ("item", "Item", 230, "w"), ("r", "Rarity", 90, "w"),
                ("a", "Aktion", 90, "w"), ("g", "Gold", 60, "e")]
        f, self.drop_list = self._tree(p, cols, 9)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        for tree in (self.drop_tree, self.drop_list):
            tree.tag_configure("blue", foreground=ui.RARITY["Uncommon"])
            tree.tag_configure("rare", foreground=ui.RARITY["Rare"])
            tree.tag_configure("leg", foreground=ui.RARITY["Legendary"])
            tree.tag_configure("gem", foreground=ui.RARITY["Gem"])
        self._drops_sig = None

    def _refresh_drops(self, span):
        d = self.state.drop_stats()
        sig = (len(d["recent"]), d["recent"][-1][0] if d["recent"] else 0)
        if sig == self._drops_sig:
            return
        self._drops_sig = sig
        br = d["by_rarity"]
        total = sum(v["n"] for v in br.values())
        self.lbl_drops.configure(text=f"{total} Funde in dieser Session" if total else "Noch keine Funde")
        self.drop_tree.delete(*self.drop_tree.get_children())
        for rar in sorted(br, key=lambda r: item_ocr.RARITY_ORDER.index(r) if r in item_ocr.RARITY_ORDER else 99):
            v = br[rar]
            self.drop_tree.insert("", "end", tags=(self.RARITY_TAG.get(rar, ""),), values=(
                rar, v["n"], f"{v['n'] / span * 3600:.1f}", f"{v['n'] / total * 100:.0f} %", v["sold"], fmt(v["gold"])))
        self.drop_list.delete(*self.drop_list.get_children())
        for t, item, rar, action, gold in reversed(d["recent"]):
            self.drop_list.insert("", "end", tags=(self.RARITY_TAG.get(rar, ""),), values=(
                datetime.fromtimestamp(t).strftime("%H:%M"), item, rar, action, fmt(gold) if gold else "-"))

    def _build_deaths(self, p):
        top = ui.page_header(p, "Tode", "Das Spiel schreibt Tode nur ins Log-Fenster. „bestätigt“: Todeszeile "
                                        "(„Killed by …“) oder Tod-Text gelesen. „vermutet“: Run brachte unter 60 % "
                                        "der üblichen EXP dieser Stage und endete früher (ab 3 Vergleichs-Runs). "
                                        "Wave ≈ aus dem EXP-Anteil geschätzt. Gespeichert in deaths_log.csv.")
        self.lbl_deaths = tk.Label(top, text="", bg=BG, fg=FG, font=("Bahnschrift SemiBold", 13), anchor="e")
        self.lbl_deaths.pack(side="right")
        self.lbl_deaths_sub = tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left")
        self.lbl_deaths_sub.pack(fill="x", padx=16)
        cols = [("t", "Zeit", 62, "e"), ("st", "Status", 74, "w"), ("stage", "Stage", 130, "w"),
                ("killer", "Getötet von", 150, "w"),
                ("wave", "Wave ≈", 60, "e"), ("dur", "Dauer", 56, "e"), ("xp", "EXP-Anteil", 76, "e"),
                ("dps", "Ø DPS", 66, "e")]
        f, self.death_tree = self._tree(p, cols, 10)
        f.pack(fill="both", expand=True, padx=14, pady=(6, 6))
        self.death_tree.bind("<<TreeviewSelect>>", self._death_select)
        det = ui.card(p, fill="x", padx=14, pady=(0, 12))
        self.lbl_death_detail = ui.autowrap(tk.Label(det, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left",
                                                     text="Tod in der Liste anklicken für Details."), 24)
        self.lbl_death_detail.pack(fill="x", padx=12, pady=10)
        self._death_rows = None

    def _refresh_deaths(self):
        d = self.state.death_stats()
        if d["n"] == 0:
            self.lbl_deaths.configure(text="Keine Tode in dieser Session", fg=C_GOOD)
        else:
            self.lbl_deaths.configure(text=f"{d['n']} {'Tod' if d['n'] == 1 else 'Tode'}, {d['per_h']:.1f} pro Stunde",
                                      fg=C_BAD)
        self.lbl_deaths_sub.configure(
            text=f"{d['confirmed']} bestätigt · {d['n'] - d['confirmed']} vermutet · "
                 f"{d['rate'] * 100:.1f} % der Runs")
        self.nb.tab(self.tab_death, text=f"Tode ({d['n']})" if d["n"] else "Tode")
        sig = (d["n"], sum(bool(r.death_info.get("killer")) for r in d["list"]))
        if sig == self._death_rows:
            return
        self._death_rows = sig
        self._death_list = d["list"]
        self.death_tree.delete(*self.death_tree.get_children())
        for idx, r in reversed(list(enumerate(d["list"]))):
            i = r.death_info
            xr = i.get("xp_ratio")
            self.death_tree.insert("", "end", iid=str(idx), tags=("bad" if r.death == "bestätigt" else "meh",), values=(
                datetime.fromtimestamp(r.end).strftime("%H:%M:%S"), r.death,
                r.stage_name or r.stage_guess or f"{r.difficulty} · {r.waves} W", i.get("killer", "-"),
                f"{i['est_wave']}/{r.waves}" if i.get("est_wave") else "-", fmt_dur(r.duration),
                f"{xr * 100:.0f} %" if xr is not None else "-", fmt(r.avg_dps) if r.damage else "-"))

    def _death_select(self, _):
        sel = self.death_tree.selection()
        if not sel:
            return
        r = self._death_list[int(sel[0])]
        i = r.death_info
        parts = [f"{datetime.fromtimestamp(r.end).strftime('%H:%M:%S')} · {r.death} · Lvl {i.get('level', r.level)}",
                 f"Stage: {r.difficulty}, {r.waves} Waves · gestorben ≈ Wave {i.get('est_wave', '?')}",
                 f"Run-Dauer {fmt_dur(r.duration)} ({(i.get('dur_ratio') or 0) * 100:.0f} % der üblichen) · "
                 f"EXP {fmt(r.xp)} ({(i.get('xp_ratio') or 0) * 100:.0f} % der üblichen, Basis {i.get('ref_runs', 0)} Runs)",
                 f"Gold {fmt(r.gold)} · Items {r.items} · MF {r.mf:g} GF {r.gf:g}"]
        if r.damage:
            parts.append(f"Ø DPS im Run {fmt(r.avg_dps)} · Peak {fmt(r.peak_dps)}")
        if i.get("killer"):
            parts.append(f"Getötet von {i['killer']} (Lv. {i.get('killer_level', '?')})"
                         + (f" · {i['source']}" if i.get("source") else "")
                         + f" · {i.get('damage', '?')} {i.get('element', '')} Damage")
        elif i.get("screen"):
            parts.append(f"Bildschirm: „{i['screen']}“")
        self.lbl_death_detail.configure(text="\n".join(parts))

    def _build_weights(self, p):
        top = ui.page_header(p, "Bewertung", "Grundlagen für den Item-Vergleich und die Edelsteine. Schaden und "
                                             "Überleben folgen den Spielformeln; hier legst du fest, was "
                                             "sich nicht berechnen lässt.")
        self._btn(top, "Standard wiederherstellen", self.reset_weights, side="right")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(row, text="Schadens-Element", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left", padx=(0, 6))
        self.var_elem = tk.StringVar(value=self.cfg.get("element", "Auto"))
        cb = ttk.Combobox(row, textvariable=self.var_elem, values=["Auto"] + item_eval.ELEMENTS, width=10,
                          state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("element", self.var_elem.get()), self._fill_eval_tab()))
        self.lbl_ctx = ui.autowrap(tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_ctx.pack(fill="x", padx=16, pady=(0, 2))
        self._section(p, "Legendäre Effekte", "Doppelklick auf „Eigener Wert“: z. B. „15“ = +15 % Schaden, "
                                              "„0/10“ = +10 % Überleben. Leer = automatische Bewertung.")
        cols = [("name", "Legendary", 180, "w"), ("slot", "Slot", 70, "w"), ("auto", "Bewertung", 250, "w"),
                ("own", "Eigener Wert", 90, "e")]
        f, self.leg_tree = self._tree(p, cols, 8)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.leg_tree.bind("<Double-1>", self._edit_legendary)
        self._section(p, "Sonstige Stats", "Stats außerhalb der Formeln. Doppelklick auf den Wert zum Ändern: "
                                           "wie viel Prozent Schaden, Überleben oder Ertrag ein Punkt (bzw. 1 %) bringt.")
        cols = [("stat", "Stat", 200, "w"), ("tgt", "wirkt auf", 100, "w"), ("per", "% pro Einheit", 100, "e")]
        f, self.w_tree = self._tree(p, cols, 5)
        f.pack(fill="both", expand=False, padx=14, pady=(0, 12))
        self.w_tree.bind("<Double-1>", self._edit_other)

    TARGET_LABEL = {"dps": "Schaden", "surv": "Überleben", "farm": "Ertrag"}

    def _eval_context(self) -> "item_eval.Context":
        char = {k: v[0] for k, v in self.char_stats.items()}
        deaths = self._death_history()
        weights, dot, enemy = {}, 0.2, None
        if len(deaths) >= 3:
            uni = 1 / len(item_eval.ELEMENTS)
            obs = {e: sum(1 for d in deaths if d["element"] == e) / len(deaths) for e in item_eval.ELEMENTS}
            weights = {e: 0.3 * uni + 0.7 * obs[e] for e in item_eval.ELEMENTS}
            dot = sum(1 for d in deaths if d["source"]) / len(deaths)
        lv = sorted(d["level"] for d in deaths[-10:] if d["level"])
        if lv:
            enemy = lv[len(lv) // 2]
        info = stages.stage_info(self.state.stage_name, getattr(self, "enemy_data", {}))
        if info:
            prof = stages.damage_profile(info)
            if prof and len(deaths) < 3:  # what the stage's enemies deal, until own deaths tell better
                uni = 1 / len(item_eval.ELEMENTS)
                weights = {e: 0.3 * uni + 0.7 * prof.get(e, 0.0) for e in item_eval.ELEMENTS}
            if enemy is None and info.get("level_max"):
                enemy = info["level_max"]
        other = {k: tuple(v) for k, v in self.cfg.get("other_stats", {}).items()}
        return item_eval.Context(char=char, hero=self.state.hero, level=self.state.level or int(char.get("Level", 0)),
                                 element=self.cfg.get("element", "Auto"), enemy_level=enemy, damage_weights=weights,
                                 dot_share=dot, other=other, overrides=self.cfg.get("legendary_values", {}),
                                 weapon=tuple(self.cfg["weapon"]) if self.cfg.get("weapon") else None,
                                 gem_tier=int(self.cfg.get("gem_tier", 3)))

    def _death_history(self) -> list:
        """Deaths with known killer: from deaths_log.csv (all sessions) and the running session."""
        out = []
        try:
            with open(DEATHS_CSV, encoding="utf-8") as f:
                for row in csv.DictReader(f, delimiter=";"):
                    if row.get("element"):
                        out.append({"element": row["element"].capitalize(), "source": row.get("source", ""),
                                    "level": int(row["killer_level"]) if row.get("killer_level", "").isdigit() else 0})
        except Exception:
            pass
        return out

    def _fill_eval_tab(self):
        ctx = self._eval_context()
        deaths = self._death_history()
        dw = ", ".join(f"{e} {w * 100:.0f} %" for e, w in sorted(ctx.damage_weights.items(), key=lambda kv: -kv[1])
                       if w >= 0.1) if ctx.damage_weights else "alle gleich (noch < 3 Tode, keine Gegnerdaten)"
        self.lbl_ctx.configure(text=(
            f"Hauptattribut: {ctx.main} ({ctx.hero or 'Klasse unbekannt'}) · Element: {ctx.elem} · "
            f"Gegner-Level: {ctx.enemy_level or str(ctx.level or '?') + ' (Heldenlevel)'} · "
            f"Schadensarten ({'aus ' + str(len(deaths)) + ' Toden' if len(deaths) >= 3 else 'Gegner der Stage ' + (self.state.stage_name or '?')}): "
            f"{dw} · DoT-Anteil {ctx.dot_share * 100:.0f} %\n"
            ""))
        self.leg_tree.delete(*self.leg_tree.get_children())
        ov = self.cfg.get("legendary_values", {})
        for L in item_eval.LEGENDARIES:
            _, txt, known, _ = item_eval._legendary_deltas(L, L["effect"], 1, item_eval.Context(char={}, element=ctx.elem), {})
            txt = txt.replace(" – im Tab Bewertung selbst bewerten", " – selbst bewerten →")
            own = ov.get(L["name"])
            own_txt = f"{own.get('dps', 0):g}/{own.get('surv', 0):g}" if own else ""
            self.leg_tree.insert("", "end", iid=L["name"], values=(L["name"], L["slot"], txt, own_txt),
                                 tags=(() if known or own else ("meh",)))
        self.w_tree.delete(*self.w_tree.get_children())
        other = {**item_eval.OTHER_DEFAULTS, **{k: tuple(v) for k, v in self.cfg.get("other_stats", {}).items()}}
        for k, (tgt, per) in other.items():
            self.w_tree.insert("", "end", iid=k, values=(k, self.TARGET_LABEL[tgt], f"{per:g}"))

    def _inline_edit(self, tree, iid, column, initial, on_commit):
        x, y, w, h = tree.bbox(iid, column)
        ent = tk.Entry(tree, bg=LINE, fg=FG, insertbackground=FG, relief="flat", justify="right")
        ent.insert(0, initial)
        ent.place(x=x, y=y, width=w, height=h)
        ent.focus_set()
        ent.select_range(0, "end")

        def commit(_=None):
            on_commit(ent.get().strip())
            ent.destroy()

        ent.bind("<Return>", commit)
        ent.bind("<FocusOut>", commit)
        ent.bind("<Escape>", lambda _: ent.destroy())

    def _edit_legendary(self, e):
        iid = self.leg_tree.identify_row(e.y)
        if not iid:
            return

        def done(text):
            ov = self.cfg.setdefault("legendary_values", {})
            if not text:
                ov.pop(iid, None)
            else:
                try:
                    parts = [float(x.replace(",", ".")) for x in text.split("/")]
                    ov[iid] = {"dps": parts[0], "surv": parts[1] if len(parts) > 1 else 0.0}
                except ValueError:
                    return
            save_config(self.cfg)
            self._fill_eval_tab()
            if self._last_item is not None:
                self._show_item(self._last_item)

        cur = self.leg_tree.item(iid, "values")[3]
        self._inline_edit(self.leg_tree, iid, "own", cur, done)

    def _edit_other(self, e):
        iid = self.w_tree.identify_row(e.y)
        if not iid:
            return
        tgt = (self.cfg.get("other_stats", {}).get(iid) or item_eval.OTHER_DEFAULTS.get(iid, ("dps", 0)))[0]

        def done(text):
            try:
                per = float(text.replace(",", "."))
            except ValueError:
                return
            self.cfg.setdefault("other_stats", {})[iid] = [tgt, per]
            save_config(self.cfg)
            self._fill_eval_tab()

        self._inline_edit(self.w_tree, iid, "per", self.w_tree.item(iid, "values")[2], done)

    # -- characters / weights ------------------------------------------------
    def _fill_char(self):
        self.char_tree.delete(*self.char_tree.get_children())
        order = ["Level"] + item_ocr.KNOWN_STATS
        for k in sorted(self.char_stats, key=lambda s: order.index(s) if s in order else 999):
            v, pct = self.char_stats[k]
            self.char_tree.insert("", "end", values=(k, f"{v:g}{'%' if pct else ''}"))
        n = len(self.char_stats)
        self.lbl_char.configure(text=f"{n} Werte · Stand {self.char_stats_time}" if n else "Noch nicht eingelesen")

    def clear_char(self):
        self.char_stats, self.char_stats_time = {}, ""
        self._save_char()
        self._fill_char()

    def _save_char(self):
        self.cfg["char_stats"] = {k: list(v) for k, v in self.char_stats.items()}
        self.cfg["char_stats_time"] = self.char_stats_time
        save_config(self.cfg)

    def _set_cfg(self, key, value):
        self.cfg[key] = value
        save_config(self.cfg)

    def reset_weights(self):
        for k in ("weights", "other_stats", "legendary_values"):
            self.cfg.pop(k, None)
        save_config(self.cfg)
        self._fill_eval_tab()

    # -- background jobs -----------------------------------------------------
    def _run_job(self, label, fn):
        if self.busy:
            return
        self.busy = True
        self.job_label = label

        def work():
            try:
                frame = self.capture.grab()
                if frame is None:
                    raise RuntimeError(self.capture.status)
                self.events.put(("done", label, fn(frame)))
            except Exception as e:
                self.events.put(("error", label, str(e)))

        threading.Thread(target=work, daemon=True).start()

    def scan_item(self):
        def job(frame):
            res = item_ocr.parse_tooltip(frame)
            item_ocr.save_debug(frame, res, os.path.join(APP_DIR, "captures"))
            return res
        self._run_job("item", job)

    def scan_attributes(self):
        self._run_job("attributes", item_ocr.parse_attributes)

    def poll_events(self):
        try:
            while True:
                ev = self.events.get_nowait()
                if ev[0] == "hotkey":
                    {"item": self.scan_item, "attributes": self.scan_attributes,
                     "hide": self.toggle_hide}.get(ev[1], lambda: None)()
                elif ev[0] == "done":
                    self.busy = False
                    if ev[1] == "item":
                        self._show_item(ev[2], record=True)
                    else:
                        self._merge_attributes(ev[2])
                elif ev[0] == "error":
                    self.busy = False
                    self.lbl_verdict.configure(text=f"Fehler: {ev[2]}", fg=C_BAD) if ev[1] == "item" else \
                        self.lbl_char.configure(text=f"Fehler: {ev[2]}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def _merge_attributes(self, stats: dict):
        if not stats:
            self.lbl_char.configure(text="Keine Attribute erkannt – ist das Charakterfenster offen?")
            self.nb.select(self.tab_char)
            return
        self.char_stats.update(stats)
        self.char_stats_time = datetime.now().strftime("%d.%m. %H:%M")
        self._save_char()
        self._fill_char()
        self._fill_gems()
        self._fill_eval_tab()
        self.lbl_char.configure(text=f"+{len(stats)} gelesen · {len(self.char_stats)} Werte · Stand {self.char_stats_time}")
        self.nb.select(self.tab_char)

    def _remember_weapon(self, res):
        """Damage and speed of the equipped weapon: needed to value flat Damage and other weapons."""
        for it in (getattr(res, "old_item", None), getattr(res, "new_item", None)):
            if it is None or (it is res.new_item and not (res.mode == "single" and it.equipped)):
                continue
            b = {st.name: st.value for st in it.base if st.ok}
            if "Weapon Damage" in b and "Weapon Speed" in b:
                w = [b["Weapon Damage"], b["Weapon Speed"]]
                if self.cfg.get("weapon") != w:
                    self._set_cfg("weapon", w)
                return

    def _show_item(self, res, record=False):
        self._last_item = res
        if record:
            self._remember_weapon(res)
        self.nb.select(self.tab_items)
        self.item_tree.delete(*self.item_tree.get_children())
        if res.mode == "none":
            self._fill_card(self.card_new, "Kein Item-Tooltip erkannt", None, {},
                            "Maus über dem Item halten, bis der Tooltip offen ist, dann Hotkey drücken.")
            for w in self.card_old.winfo_children():
                w.destroy()
            self.lbl_verdict.configure(text="-", fg=FG)
            self.lbl_sure.configure(text="")
            self.lbl_fx.configure(text="")
            return
        mode = self.var_mode.get()
        level = int(self.var_upg.get().lstrip("+") or 0)
        rarity, slot, ancient = item_quality.item_kind(res.type_line)
        n_new = 1 + item_quality.affix_count(rarity) if rarity else 99
        new_stats = res.new_stats[:n_new]
        o_rar = item_quality.item_kind(res.old_type_line)[0] or rarity
        old_stats = res.old_stats[:1 + item_quality.affix_count(o_rar) if o_rar else 99]
        res_eval = res
        if level and res.mode == "compare" and rarity:
            # both items upgraded to the same level: add each item's upgrade gains to the difference
            res_eval = copy.copy(res)
            dl = dict(res.deltas)
            for k, g in item_quality.upgrade_gain(new_stats, level, rarity).items():
                d, p_ = dl.get(k, (0.0, False))
                dl[k] = (d + g, p_ or bool((item_quality.AFFIX.get("affixes", {}).get(k) or {}).get("percent")))
            for k, g in item_quality.upgrade_gain(old_stats, level, o_rar).items():
                d, p_ = dl.get(k, (0.0, False))
                dl[k] = (d - g, p_)
            res_eval.deltas = dl
        ev = item_eval.evaluate(res_eval, self._eval_context(), mode)
        if getattr(res, "warnings", None):
            ev.confident = False
        color = {"ANLEGEN": C_GOOD, "SPÄTER ANLEGEN": C_GOOD, "NICHT ANLEGEN": C_BAD}.get(ev.verdict, C_MEH)
        if res.mode == "single":
            verdict, color = "KEIN VERGLEICH", C_MEH
        elif not self.char_stats:
            verdict, color = "CHARAKTER EINLESEN (F9)", C_MEH
        else:
            verdict = ev.verdict
        sure = "sicher" if ev.confident else "unsicher"
        nice = {"ANLEGEN": "Anlegen", "SPÄTER ANLEGEN": "Später anlegen", "NICHT ANLEGEN": "Nicht anlegen",
                "SEITWÄRTS": "Gleichwertig", "KEIN VERGLEICH": "Kein Vergleich",
                "CHARAKTER EINLESEN (F9)": "Charakter einlesen (F9)"}.get(verdict, verdict)
        self.lbl_verdict.configure(text=nice, fg=color)
        self.lbl_sure.configure(text=sure, fg=MUTED if ev.confident else C_MEH)
        if verdict in ("ANLEGEN", "SPÄTER ANLEGEN", "NICHT ANLEGEN", "SEITWÄRTS"):
            what = {"Schaden": "vor allem Schaden", "Überleben": "vor allem Überleben",
                    "Ausgewogen": "Schaden und Überleben gleich", "Farmen": "vor allem Gold, Items und EXP"}[mode]
            reason = f"Im Modus {mode} ({what} zählt) ändert sich dein Charakter insgesamt um {ev.score:+.1f} %."
            if verdict == "SEITWÄRTS":
                reason += " Das ist unter ±1 % – kein spürbarer Unterschied, behalte was dir lieber ist."
            elif verdict == "SPÄTER ANLEGEN":
                reason += f" Du kannst es erst ab Level {res.req_level} tragen."
        elif verdict == "KEIN VERGLEICH":
            reason = "Kein Vergleichs-Tooltip: entweder ist der Slot leer oder das Spiel zeigt nur dieses Item."
        else:
            reason = "Ohne eingelesenen Charakter lässt sich die Wirkung nicht berechnen."
        self.lbl_reason.configure(text=reason)

        q_new = item_quality.roll_report(new_stats, rarity, res.item_level) if rarity else {"stats": {}, "avg": None}
        q_old = item_quality.roll_report(old_stats, o_rar, res.old_item_level) if o_rar and res.old_item_level else None
        self._fill_card(self.card_new, "Neu", res.new_item, q_new["stats"], gems=ev.gems.get("new"))
        if res.mode == "compare":
            self.card_old.grid()
            self._fill_card(self.card_old, "Angelegt", res.old_item, q_old["stats"] if q_old else {},
                            gems=ev.gems.get("old"))
        else:
            self.card_old.grid_remove()

        def metric(key, val, sub):
            lab, sl = self.lbl_m[key]
            lab.configure(text=f"{val:+.1f} %" if val is not None else "-",
                          fg=FG if val is None or abs(val) < 0.05 else (C_GOOD if val > 0 else C_BAD))
            sl.configure(text=sub)

        has_char = bool(self.char_stats)
        metric("dps", ev.dps_pct if has_char else None, f"{ev.main}, {ev.element}" if has_char else "F9 nötig")
        metric("surv", ev.surv_pct if has_char else None,
               f"Toughness {fmt(ev.tough_old)} → {fmt(ev.tough_new)}" if has_char else "F9 nötig")
        metric("farm", ev.farm_pct if has_char else None,
               (", ".join(f"{k} {v:+.1f} %" for k, v in ev.farm.items() if abs(v) >= 0.05) or "keine Änderung")
               if has_char else "F9 nötig")

        warn = list(dict.fromkeys(ev.reasons)) + ["Lesefehler? " + w for w in getattr(res, "warnings", [])]
        self.lbl_item.configure(text="\n".join("⚠ " + w for w in warn))
        q_new = item_quality.roll_report(new_stats, rarity, res.item_level) if rarity else {"stats": {}, "avg": None}
        q_old = item_quality.roll_report(old_stats, o_rar, res.old_item_level) if o_rar and res.old_item_level else None
        qparts = []
        if q_new["avg"] is not None:
            qparts.append(f"Roll-Qualität neu: Ø {q_new['avg']:.0f} %" + (" (Ancient)" if ancient else "")
                          + (f" · ersetztes Item: Ø {q_old['avg']:.0f} % (iLvl {res.old_item_level})" if q_old and q_old["avg"] is not None else ""))
            if q_new["upgraded_or_gem"]:
                qparts.append("über Maximum (schon upgegradet oder Edelstein?): " + ", ".join(q_new["upgraded_or_gem"]))
        if rarity and res.item_level:
            tgt = max(level, 5)
            c = item_quality.upgrade_cost(tgt, res.item_level, rarity, slot, ancient)
            ores = ", ".join(f"{fmt(v)} {k}" for k, v in c["ores"].items())
            qparts.append(f"Upgrade auf +{tgt}: ≈ {fmt(c['gold'])} Gold, {ores}, Ø {c['attempts']:.1f} Versuche"
                          + (f", {c['boss'][1]}× {c['boss'][0]}" if c["boss"] else "")
                          + (f" · Sockel: {item_quality.sockets(slot)}" if slot and item_quality.sockets(slot) else ""))
        self.lbl_quality.configure(text="\n".join(qparts))
        fx = []
        for sign, item, effect, txt, known in ev.effects:
            head = "Neu" if sign > 0 else "Fällt weg"
            fx.append(f"{head}: {item} – {effect}\n      Bewertung: {txt}")
        if not fx:
            fx = ["Keine Effekte, die sich ändern." if res.mode == "compare" else "Keine Effekte."]
        self.lbl_fx.configure(text="\n".join(fx), fg=C_MEH if any(not k for *_, k in ev.effects) else MUTED)
        self.item_tree.configure(height=max(3, min(len(ev.rows), 16)))  # as tall as the item has stats
        for name, d, pct, txt, weighted in ev.rows:
            tag = "meh" if abs(weighted) < 0.05 else ("good" if weighted > 0 else "bad")
            rq = q_new["stats"].get(name)
            self.item_tree.insert("", "end", values=(name, f"{d:+.4g}{'%' if pct else ''}",
                                                     f"{rq[1]:.0f} %" if rq else "-", txt), tags=(tag,))
        if record:
            entry = (datetime.now(), res, ev)
            self.item_history.append(entry)
            tag = "good" if color == C_GOOD else ("bad" if color == C_BAD else "meh")
            icon = None
            if res.icon is not None:
                from PIL import Image, ImageTk
                icon = ImageTk.PhotoImage(Image.fromarray(res.icon[:, :, ::-1]).resize((30, 30)))
                self._hist_icons.append(icon)
            self.hist_tree.insert("", 0, iid=str(len(self.item_history) - 1), image=icon or "", values=(
                f"{res.name}  ·  {entry[0].strftime('%H:%M')}", f"{ev.dps_pct:+.1f} %", f"{ev.surv_pct:+.1f} %",
                nice + ("" if ev.confident else " ?")), tags=(tag,))
            self._append_item_csv(entry, mode, verdict)

    @staticmethod
    def _mode_formula(mode):
        w = item_eval.MODES[mode]
        return " + ".join(f"{v:g}×{n}" for n, v in (("Schaden", w["dps"]), ("Überleben", w["surv"]), ("Ertrag", w["farm"])))

    def _hist_select(self, _):
        sel = self.hist_tree.selection()
        if sel:
            self._show_item(self.item_history[int(sel[0])][1])

    def _append_item_csv(self, entry, mode, verdict):
        t, res, ev = entry
        new = not os.path.exists(ITEMS_CSV)
        try:
            with open(ITEMS_CSV, "a", newline="", encoding="utf-8") as f:
                w = csv.writer(f, delimiter=";")
                if new:
                    w.writerow(["time", "item", "type", "item_level", "req_level", "replaces", "mode", "verdict",
                                "confident", "dps_pct", "surv_pct", "gold_pct", "items_pct", "xp_pct", "deltas",
                                "effects_new", "effects_lost"])
                w.writerow([t.isoformat(timespec="seconds"), res.name, res.type_line, res.item_level, res.req_level,
                            res.old_name, mode, verdict, ev.confident, round(ev.dps_pct, 2), round(ev.surv_pct, 2),
                            round(ev.farm.get("Gold", 0), 2), round(ev.farm.get("Items", 0), 2),
                            round(ev.farm.get("EXP", 0), 2),
                            ", ".join(f"{k} {d:+g}{'%' if p else ''}" for k, (d, p) in res.deltas.items()),
                            " | ".join(res.effects_new), " | ".join(res.effects_lost)])
        except Exception:
            pass

    # -- actions -------------------------------------------------------------
    def toggle_ocr(self):
        if self.ocr.enabled.is_set():
            self.ocr.enabled.clear()
        else:
            self.ocr.enabled.set()
        on = self.ocr.enabled.is_set()
        self.btn_ocr.configure(text="DPS-Messung: an" if on else "DPS-Messung: aus", fg=C_RUN if on else FG)
        self.cfg["ocr_on"] = on

    def pick_region(self):
        frame = self.capture.grab()
        if frame is None:
            self.lbl_status.configure(text=self.capture.status)
            return

        def done(rect):
            self.region = rect
            self.cfg["game_region"] = list(rect)
            save_config(self.cfg)
        GameRegionPicker(self.root, frame, self.region, done)

    def reset(self):
        self.state.reset()
        self.tree.delete(*self.tree.get_children())
        self._runs_sig = None

    PANEL_MODES = PanelReader.MODES

    def cycle_panels(self):
        order = list(self.PANEL_MODES)
        cur = self.cfg.get("auto_panels", "always")
        cur = cur if cur in order else "always"
        self.cfg["auto_panels"] = order[(order.index(cur) + 1) % len(order)]
        save_config(self.cfg)
        self._update_panels_btn()

    def _update_panels_btn(self):
        mode = self.cfg.get("auto_panels", "always")
        mode = mode if mode in self.PANEL_MODES else "always"
        self.btn_panels.configure(text=f"Panels: {self.PANEL_MODES[mode]}", fg=FG)

    def toggle_hide(self):
        self.set_game_hidden(not self.cfg.get("game_hidden", False))
        self.boss_grace_until = time.time() + 2.5

    def set_game_hidden(self, hidden: bool):
        hwnd = self.capture.find()
        if not hwnd:
            self.lbl_status.configure(text="Deskrawl nicht gefunden")
            return
        self.cfg["game_hidden"] = hidden
        if hidden:
            game_input.hide_game(hwnd)
        else:
            game_input.show_game(hwnd)
            if ctypes.windll.user32.IsIconic(hwnd):
                ctypes.windll.user32.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
        save_config(self.cfg)
        self._update_hide_btn()

    def _update_hide_btn(self):
        hidden = self.cfg.get("game_hidden", False)
        key = self.hk.get("hide", "F10")
        self.btn_hide.configure(text=f"Deskrawl {'einblenden' if hidden else 'ausblenden'} ({key})",
                                fg=ui.ACCENT if hidden else FG)

    def _sync_game_window(self):
        """While hidden, keep the game invisible but not minimized (a minimized game is not
        rendered). Only F10 / the button show it again - Deskrawl brings itself to the front on
        its own (e.g. auto rerun), so focus is no sign that the user wants to see it."""
        u = ctypes.windll.user32
        hwnd = self.capture.find()
        if not hwnd:
            return
        now = time.time()
        if self.cfg.get("game_hidden"):
            if not game_input.is_hidden(hwnd) or u.IsIconic(hwnd):
                game_input.hide_game(hwnd)  # game re-applied its window setup, or was restarted
        elif u.IsIconic(hwnd) and now < self.boss_grace_until:
            u.ShowWindow(hwnd, 4)  # just shown via F10: make sure it is not left minimized

    def toggle_topmost(self):
        self.cfg["topmost"] = not self.cfg.get("topmost", True)
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._update_top_btn()

    def _update_top_btn(self):
        self.btn_top.configure(text="Immer im Vordergrund: " + ("an" if self.cfg.get("topmost", True) else "aus"))

    def close(self):
        if self.cfg.get("game_hidden"):  # never leave the game invisible without the tracker
            hwnd = self.capture.find()
            if hwnd:
                game_input.show_game(hwnd)
            self.cfg["game_hidden"] = False
        if self.root.state() == "normal":
            self.cfg["geometry"] = self.root.geometry()
        save_config(self.cfg)
        self.root.destroy()

    # -- refresh -------------------------------------------------------------
    def _keep_visible(self):
        # "Show desktop" (Win+D) minimizes even topmost windows; restore without stealing focus
        # and re-assert HWND_TOPMOST so other topmost windows cannot cover us.
        if not self.cfg.get("topmost", True):
            return
        try:
            u = ctypes.windll.user32
            hwnd = self.hwnd
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
            u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)
        except Exception:
            pass

    def _gold_audit_text(self):
        so = self.state.sold_stats()
        if so["n"]:
            src = "Verkaufs-Einblendung" if time.time() - self.state.last_toast < 600 else "Spiel-Log"
            return f"Verkäufe ({src}): {so['n']} Items für {fmt(so['gold'])} Gold"
        ga = self.state.gold_audit
        if ga.balance is None:
            return "Verkaufsgold: Inventar im Spiel kurz öffnen – der Tracker liest den Kontostand mit."
        ago = fmt_dur(time.time() - ga.balance_t)
        if ga.rate_h is None:
            return (f"Kontostand {fmt(ga.balance)} (vor {ago}) · Verkaufsgold: Inventar nach ein paar Runs "
                    f"nochmal öffnen")
        skipped = f" · {ga.skipped}× übersprungen (Gold ausgegeben)" if ga.skipped else ""
        return (f"Kontostand {fmt(ga.balance)} (vor {ago}) · Verkaufsgold {fmt(ga.extra)} in "
                f"{fmt_dur(ga.measured_s)} gemessen{skipped}")

    def _refresh_level(self, s, sr):
        st = self.state
        if st.xpm.changed:  # persist learned level requirements
            st.xpm.changed = False
            self.cfg["xp_learned"] = st.xpm.learned
            save_config(self.cfg)
        e = st.level_eta()
        if not e:
            self.lbl_lvl_eta.configure(text="-")
            self.lbl_lvl_runs.configure(text="")
            self.lbl_lvl_sub.configure(text="Wartet auf die Login-Zeile im Log – Spiel einmal neu starten.")
            return
        self.lbl_lvl_title.configure(text=f"bis Level {e['level'] + 1}")
        approx = "" if e["exact"] else "≈ "
        frac = min(e["xp"] / e["need"], 1.0) if e["need"] else 0
        w = max(self.lvl_bar.winfo_width(), 1)
        self.lvl_bar.delete("all")
        filled = int(w * frac)
        steps = 24  # ember -> gold gradient
        a, b = ui.EMBER, ui.ACCENT
        for i in range(steps):
            x0, x1 = filled * i // steps, filled * (i + 1) // steps
            t = i / (steps - 1)
            col = "#%02x%02x%02x" % tuple(int(int(a[k:k + 2], 16) * (1 - t) + int(b[k:k + 2], 16) * t) for k in (1, 3, 5))
            self.lvl_bar.create_rectangle(x0, 0, x1, 12, fill=col, width=0)
        rate = sr["xp_h"] if sr["runs"] >= 3 else s["xp_h"]
        eta_rate = e["left"] / rate * 3600 if rate > 0 else None
        eta = e["eta_stage"] if e["eta_stage"] is not None else eta_rate
        self.lbl_lvl_eta.configure(text=fmt_dur(eta) if eta is not None else "-")
        if e["runs_left"] is not None:
            self.lbl_lvl_runs.configure(
                text=f"{approx}{e['runs_left']:.0f} Runs auf {e['stage']}\nØ {fmt(e['xp_run'])} EXP pro Run")
        else:
            self.lbl_lvl_runs.configure(text="noch kein Run auf dieser Stage gemessen")
        self.lbl_lvl_sub.configure(
            text=f"{approx}{fmt(e['xp'])} von {fmt(e['need'])} EXP ({frac * 100:.1f} %), noch {approx}{fmt(e['left'])}"
                 f"   ·   nach EXP pro Stunde: {fmt_dur(eta_rate) if eta_rate else '-'}")

    def tick(self):
        try:
            self._keep_visible()
            self._sync_game_window()
            self._refresh()
        finally:
            self.root.after(500, self.tick)

    def _refresh(self):
        st = self.state
        s = st.stats()
        sr = st.stats(ROLLING_WINDOW_S)
        dps = st.live_dps()
        with st.lock:
            cur = st.current
            runs = list(st.runs)

        self.lbl_hero.configure(text=st.char_name)
        self.lbl_hero_sub.configure(text=f"{st.hero}, Level {st.level}" + (f", {st.stage_name}" if st.stage_name else ""))
        ok = lambda cond, warn=False: "ok" if cond else ("warn" if warn else "bad")
        self.dots["log"].set(ok(self.tailer.status == "log ok"), self.tailer.status)
        cap = self.capture.status
        hidden = self.cfg.get("game_hidden")
        self.dots["game"].set("ok" if cap == "Spiel ok" else ("bad" if "nicht gefunden" in cap else "warn"),
                              cap + (" (ausgeblendet)" if hidden else "") +
                              ("\nMinimiert kann der Tracker nichts lesen – mit F10 ausblenden statt minimieren."
                               if "minimiert" in cap else ""))
        ocr_on = self.ocr.enabled.is_set()
        self.dots["dps"].set("ok" if ocr_on and "fps" in self.ocr.status else ("warn" if ocr_on else None),
                             self.ocr.status)
        ps = self.panels.status or "noch nicht gelesen"
        self.dots["panels"].set("ok" if "gelesen" in ps else ("warn" if ps != "noch nicht gelesen" else None), ps)
        self.dots["keys"].set("bad" if self.hotkeys.failed else "ok",
                              ("Belegt (von anderem Programm): " + ", ".join(self.hotkeys.failed)) if self.hotkeys.failed
                              else "F8 Item prüfen · F9 Charakter einlesen · F10 Deskrawl aus-/einblenden")
        if self.busy:
            self.lbl_status.configure(text="lese Item…" if self.job_label == "item" else "lese Attribute…")
        if cur:
            self.lbl_run.configure(
                text=f"▶  Run läuft {fmt_dur(cur.duration)} – {cur.difficulty}, {cur.waves} Waves, "
                     f"Schaden {fmt(cur.damage)}      Bonus: MF {cur.mf:g}, GF {cur.gf:g}, XP {cur.xpm:g}")
        else:
            self.lbl_run.configure(text="Wartet auf den nächsten Run")

        def tile(key, main, sub):
            self.tiles[key][0].configure(text=main)
            self.tiles[key][1].configure(text=sub)

        tile("xp_h", fmt(s["xp_h"]), f"letzte 15 min {fmt(sr['xp_h'])}\nØ {fmt(s['avg_xp_run'])} pro Run")
        ga = st.gold_audit
        so = st.sold_stats()
        if so["n"]:
            sold_h = so["gold"] / s["span"] * 3600
            tile("gold_h", fmt(s["gold_h"] + sold_h), f"Runs {fmt(s['gold_h'])}\nVerkäufe {fmt(sold_h)}")
        elif ga.rate_h is not None:
            tile("gold_h", fmt(s["gold_h"] + ga.rate_h), f"Runs {fmt(s['gold_h'])} + Verkäufe ≈ {fmt(ga.rate_h)}")
        else:
            tile("gold_h", fmt(s["gold_h"]), f"nur Run-Gold\nØ {fmt(s['avg_gold_run'])} pro Run")
        tile("runs_h", f"{s['runs_h']:.1f}", f"letzte 15 min {sr['runs_h']:.1f}\nØ {fmt_dur(s['avg_run'])} pro Run")
        tile("items_h", f"{s['items_h']:.1f}", f"letzte 15 min {sr['items_h']:.1f}\n{s['items']} insgesamt")
        if not self.ocr.enabled.is_set():
            tile("run_dps", "-", "DPS-Messung ist aus")
        elif s["avg_dps"]:
            peak = max((r.peak_dps for r in runs if r.start), default=0)
            tile("run_dps", fmt(s["avg_dps"]), f"letzte 15 min {fmt(sr['avg_dps'])}\nPeak {fmt(peak)}")
        else:
            tile("run_dps", "-", f"läuft · bisher {fmt(cur.damage)} Schaden" if cur and cur.damage else "noch kein Run gemessen")
        dst = st.death_stats()
        tile("deaths", f"{dst['per_h']:.1f}", f"{dst['n']} {'Tod' if dst['n'] == 1 else 'Tode'}\n{dst['rate'] * 100:.0f} % der Runs")

        self._refresh_level(s, sr)
        self._refresh_deaths()
        self._refresh_drops(s["span"])
        self._aggregate_stages()

        b = st.backlog
        self.lbl_totals.configure(
            text=f"Session {fmt_dur(s['span'])}: {s['runs']} Runs, {fmt(s['xp'])} EXP, {fmt(s['gold'])} Gold, "
                 f"{s['items']} Items.   {self._gold_audit_text()}\n"
                 f"Vor dem Tool-Start im Log (ohne Zeitangabe): {b['runs']} Runs, {fmt(b['xp'])} EXP, {fmt(b['gold'])} Gold")

        sig = (len(runs), sum(bool(r.stage_name) for r in runs), sum(bool(r.stage_guess) for r in runs))
        if getattr(self, "_runs_sig", None) != sig:
            self._runs_sig = sig
            self.tree.delete(*self.tree.get_children())
            for r in reversed(runs[-50:]):
                self.tree.insert("", "end", values=(
                    datetime.fromtimestamp(r.end).strftime("%H:%M:%S"),
                    r.stage_name or (f"≈ {r.stage_guess}" if r.stage_guess else "-"), r.difficulty,
                    fmt_dur(r.duration), fmt(r.xp), fmt(r.gold), r.items,
                    fmt(r.avg_dps) if r.damage else "-"))


# --------------------------------------------------------------------------- offline OCR test

def test_video(path: str, crop: str | None):
    import cv2
    hits = []
    ocr = DamageOCR(on_hit=lambda t, v, c: hits.append((t, v, c)))
    cap = cv2.VideoCapture(path)
    fps = cap.get(cv2.CAP_PROP_FPS) or 60
    step = max(1, int(round(fps * 0.15)))  # same ~6-7 fps as live mode
    x = y = w = h = None
    if crop:
        x, y, w, h = map(int, crop.split(","))
    i = 0
    t_ocr = 0.0
    while True:
        ok, f = cap.read()
        if not ok:
            break
        if i % step == 0:
            if crop:
                f = f[y:y + h, x:x + w]
            t1 = time.perf_counter()
            dets = ocr.feed(f, i / fps)
            t_ocr = max(t_ocr, time.perf_counter() - t1)
            print(f"t={i / fps:5.2f}s  " + "  ".join(f"{d[0]}{'*' if d[4] else ''}" for d in dets))
        i += 1
    ocr.flush(1e9, force=True)
    total = sum(v for _, v, _ in hits)
    print("\nUnique hits:")
    for t, v, c in hits:
        print(f"  t={t:5.2f}s  {int(v):>10,}{'  CRIT' if c else ''}")
    dur = i / fps
    print(f"\nslowest OCR frame {t_ocr * 1000:.0f} ms")
    print(f"{len(hits)} hits, total {total:,.0f} dmg over {dur:.1f}s -> {total / dur:,.0f} DPS")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--video", help="run OCR on a recorded clip instead of the live screen")
    ap.add_argument("--crop", help="x,y,w,h crop for --video")
    ap.add_argument("--selftest", help="read a screenshot with both OCR engines, write selftest.txt and exit")
    a = ap.parse_args()
    if a.selftest:
        import cv2
        img = cv2.imread(a.selftest)
        res = item_ocr.parse_tooltip(img)
        attrs = item_ocr.parse_attributes(img)
        with open(os.path.join(APP_DIR, "selftest.txt"), "w", encoding="utf-8") as f:
            f.write(f"tooltip: mode={res.mode} name={res.name} deltas={res.deltas}\n")
            f.write(f"attributes (RapidOCR): {len(attrs)} values\n")
        return
    if a.video:
        test_video(a.video, a.crop)
        return
    root = tk.Tk()
    root.minsize(720, 720)
    # no console when started via pythonw / the .exe: write crashes to a file people can send
    root.report_callback_exception = lambda *exc: log_error(*exc)
    App(root)
    root.mainloop()


ERROR_LOG = os.path.join(APP_DIR, "tracker_errors.log")


def log_error(exc_type, exc, tb):
    import traceback
    try:
        with open(ERROR_LOG, "a", encoding="utf-8") as f:
            f.write(f"--- {datetime.now():%Y-%m-%d %H:%M:%S}\n")
            f.write("".join(traceback.format_exception(exc_type, exc, tb)))
    except Exception:
        pass


if __name__ == "__main__":
    sys.excepthook = log_error
    threading.excepthook = lambda a: log_error(a.exc_type, a.exc_value, a.exc_traceback)
    main()

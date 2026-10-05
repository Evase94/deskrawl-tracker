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
import bis
import talents
import skills
import updater
from version import VERSION
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


# item modes of the earlier German version -> current names
OLD_MODES = {"Schaden": "Damage", "Überleben": "Survival", "Ausgewogen": "Balanced", "Farmen": "Farming"}


def load_config() -> dict:
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    for d in [cfg] + list((cfg.get("profiles") or {}).values()):
        if d.get("item_mode") in OLD_MODES:
            d["item_mode"] = OLD_MODES[d["item_mode"]]
    return cfg


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


# internal hero ids in Game.log -> class names in the game
HERO_CLASSES = {"Barbarian": "Warrior", "Mage": "Sorcerer", "Hunter": "Hunter", "Monk": "Monk"}


def hero_class(hero_id: str) -> str:
    h = hero_id.replace("Hero", "")
    return HERO_CLASSES.get(h, h)


@dataclass
class Run:
    run_id: str
    casts: dict = field(default_factory=dict)  # ability -> casts seen on the skill bar
    char: str = ""           # character that played the run
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
    death: str = ""          # "", "suspected" or "confirmed"
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
        self.characters: dict = {}  # name -> (class, level) of every login seen in the log
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
                        mode=m["mode"] or "", mf=num(m["mf"]), gf=num(m["gf"]), xpm=num(m["xpm"]),
                        char=self.char_name)
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
                    r = self.by_id.get(m["id"]) or Run(run_id=m["id"], char=self.char_name)
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
                    self.char_name, self.hero = m["name"], hero_class(m["hero"])
                    self.characters[m["name"]] = (self.hero, int(m["lvl"]))
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

    def log_needed(self, r) -> bool:
        """Does the in-game log panel have to be opened after run r? Only it names the stage and the
        killer: needed after a death, while no stage is known, or when the run's settings
        (difficulty, waves) differ from the last stage the log named - then the stage changed."""
        with self.lock:
            return bool(r.death) or not self.stage_name or self.stage_key != (r.difficulty, r.waves)

    def add_casts(self, events):
        """Ability casts from the skill bar: count them on the running run."""
        with self.lock:
            r = self.current
            if r is None:
                return
            for _t, name in events:
                r.casts[name] = r.casts.get(name, 0) + 1

    def mark_death_seen(self, t: float, text: str, extra: dict | None = None, max_age: float = 15):
        with self.lock:
            r = self.current or (self.runs[-1] if self.runs else None)
            if r is None or (r.end and t - r.end > max_age):
                return
            if extra:
                r.death_info.update(extra)
            r.death_info.setdefault("screen", text)
            r.death_info.setdefault("seen_at", t)
            if r.end and r.death != "confirmed":  # death screen showed up after the commit
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
            r.death = "confirmed"
        elif suspicious:
            r.death = "suspected"
        if r.death:
            info["dps_before"] = r.avg_dps
            info["peak_dps"] = r.peak_dps or None
            info["level"] = r.level

    def death_stats(self) -> dict:
        with self.lock:
            deaths = [r for r in self.runs if r.death]
            span = self.stats()["span"]  # same time base as EXP/h etc.
            return {"n": len(deaths), "confirmed": sum(r.death == "confirmed" for r in deaths),
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
            open_runs = [o for o in reversed(self.runs) if o.end and not o.stage_name and o.death != "confirmed"
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
        return f"Gem Tier {info['tier'] or '?'}"
    if info["kind"] == "rune":
        return {"set": "Set Rune", "ability": "Ability Rune", "attribute": "Attribute Rune"}.get(info.get("rune_type"), "Rune")
    return {"key": "Treasure Key", "shard": "Soul Shard", "boss_material": "Boss Material", "skull": "Skull",
            "ore": "Ore", "plant": "Plant"}.get(info["kind"], e.get("rarity", "?"))


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
        self.status = "DPS OCR off"
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
            self.status = f"OCR not available: {e}"
            return
        last = time.time()
        while True:
            if not self.enabled.is_set():
                self.status = "DPS OCR off"
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
                self.status = f"DPS OCR on · {self.fps:.1f} fps"
            except Exception as e:
                self.status = f"OCR error: {e}"
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
    """After a run: read the in-game log panel (stage name, killer). If it is not on screen it is
    opened with C - but only when needed (see GameState.log_needed): after a death, while no stage
    is known, or when the stage seems to have changed. Sales and drops come from the pop-ups.

    Deskrawl only reacts to real keys while it has focus (tested: faked focus messages are ignored),
    so in the background mode "always" briefly takes focus. While the game is hidden by the tracker
    the panels are left open; they only close when a death restarts the stage, so focus is taken
    only after a death.
    """
    MODES = {"always": "with focus switch", "fg": "foreground only", "off": "off"}

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
                self.status = f"Panel error: {e}"

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
            self.status = "Log panel: " + self.capture.status
            return
        with self.state.lock:
            last = self.state.runs[-1] if self.state.runs else None
        keys = []
        if mode != "off" and not log and (last is None or self.state.log_needed(last)):
            keys.append(self._keys()["log"])
        elif not log:
            self.status = f"Log panel: not needed, stage known ({datetime.now().strftime('%H:%M:%S')})"
        how, prev = None, None
        if keys:
            self.ocr.suspend(5.0)
            how, prev = self._press(keys, True, mode)
            if how is None:
                self.status = "Log panel: Deskrawl not in the foreground – skipped"
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
                label = {"fg": "foreground", "focus": "focus switch"}[how]
                self.status = f"Log panel opened and read {datetime.now().strftime('%H:%M:%S')} ({label})"
            elif hdr is not None:
                self.status = f"Log panel read {datetime.now().strftime('%H:%M:%S')} (was open)"
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
        self.title("Choose DPS area")
        self.configure(bg=BG)
        self.attributes("-topmost", True)
        H, W = frame.shape[:2]
        self.k = min(1100 / W, 650 / H, 1.0)
        img = Image.fromarray(frame[:, :, ::-1]).resize((int(W * self.k), int(H * self.k)))
        self.photo = ImageTk.PhotoImage(img)
        tk.Label(self, text="Drag a rectangle over the area where damage numbers appear  ·  ESC = cancel",
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
        self.skills = skills.SkillWatcher(self.state, self.capture)
        self.skills.start()
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
        root.attributes("-topmost", self.cfg.get("topmost", False))
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
        self.toggle_skills(bool(self.cfg.get("skills_on")))
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
        for key, label in (("log", "Log"), ("game", "Game"), ("dps", "DPS"), ("panels", "Panels"), ("keys", "Hotkeys")):
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
        self.tab_bis = tk.Frame(self.nb, bg=BG)
        self.tab_tal = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_farm, text="Overview")
        self.nb.add(self.tab_char, text="Character Stats")
        self.nb.add(self.tab_stages, text="Stages")
        self.nb.add(self.tab_drops, text="Drops")
        self.nb.add(self.tab_death, text="Deaths")
        self.nb.add(self.tab_items, text="Item Comparer")
        self.nb.add(self.tab_bis, text="BiS Gear")
        self.nb.add(self.tab_tal, text="Talents")
        self.nb.add(self.tab_gems, text="Gems")
        self.nb.add(self.tab_w, text="Weights")
        self._build_farm(self.tab_farm)
        self._build_items(self.tab_items)
        self._build_char(self.tab_char)
        self._build_weights(self.tab_w)
        self._build_deaths(self.tab_death)
        self._build_drops(self.tab_drops)
        self._build_stages(self.tab_stages)
        self._build_gems(self.tab_gems)
        self._build_bis(self.tab_bis)
        self._build_talents(self.tab_tal)

        side = self.nb.bottom
        tk.Label(side, text="Controls", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=(0, 4))

        def ctl(text, cmd, tip):
            b = ui.button(side, text, cmd, small=True)
            b.configure(anchor="w", bg=PANEL)
            b.bind("<Leave>", lambda e: b.configure(bg=PANEL))
            b.pack(fill="x", padx=6, pady=1)
            ui.Tooltip(b, tip)
            return b

        self.btn_hide = ctl("", self.toggle_hide, "Keep Deskrawl running invisibly instead of minimizing it – "
                                                  "only then can the tracker keep reading.")
        self.btn_ocr = ctl("", self.toggle_ocr, "Read damage numbers in the game (for DPS per run).")
        ctl("Choose DPS area", self.pick_region, "Area of the game picture where damage numbers appear.")
        self.btn_panels = ctl("", self.cycle_panels, "Open the log panel (C) when the tracker needs the stage name or death details "
                                                     "– after a death or a stage change. When it is open it is read "
                                                     "without a key press. "
                                                     "“with focus switch” briefly brings Deskrawl to the front for that.")
        self.btn_skills = ctl("", self.toggle_skills, "Count which abilities you use per run from the skill bar "
                                                       "(about 15 pictures a second – costs some CPU). Results: Stages "
                                                       "page and the ability shares on the Talents page.")
        self.btn_top = ctl("", self.toggle_topmost, "Keep the tracker window on top of other windows.")
        ctl("Change log file", self.open_setup, "Choose the path to Deskrawl's Game.log and check that "
                                                 "Windows' English text recognition is installed.")
        ctl("Check for updates", lambda: self.check_updates(manual=True),
            f"Version {VERSION}. Looks for a newer release on GitHub and installs it; your settings and "
            f"histories stay. The tracker also checks once at every start.")
        ctl("Reset session", self.reset, "Clear the rates and run list of the current session. "
                                                "Stage statistics and histories are kept.")
        self.root.after(5000, lambda: self.check_updates(manual=False))
        if setup_dialog.needs_setup(self.cfg):
            self.root.after(400, lambda: self.open_setup(first_run=True))
        self._update_panels_btn()
        self._update_hide_btn()
        self._update_top_btn()
        self.btn_ocr.configure(text="DPS meter: off")

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
        self.lbl_lvl_title = tk.Label(top, text="to the next level", bg=PANEL, fg=FG, font=ui.F_BODY)
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
        spec = [("xp_h", "EXP per hour"), ("gold_h", "Gold per hour"), ("runs_h", "Runs per hour"),
                ("items_h", "Items per hour"), ("run_dps", "Avg DPS per run"), ("deaths", "Deaths per hour")]
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
        self._section(p, "Recent runs", "“≈” before the stage: taken from the previous run because the log panel "
                                         "could not be read for this run.")
        cols = [("t", "End", 64, "e"), ("stage", "Stage", 150, "w"), ("diff", "Diff", 66, "w"),
                ("dur", "Time", 46, "e"), ("xp", "EXP", 62, "e"), ("gold", "Gold", 54, "e"), ("it", "Items", 42, "e"),
                ("dps", "Avg DPS", 62, "e"), ("peak", "Peak DPS", 66, "e")]
        f, self.tree = self._tree(p, cols, 8)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 12))

    def _build_items(self, page):
        hint = (f"In the game, hover an item until the comparison tooltip is open, then press "
                f"{self.hk.get('item', 'F8')}. For exact results read your character first with "
                f"{self.hk.get('attributes', 'F9')}.\n\nMode: what matters to you right now – damage, "
                f"survival, both, or farming (gold/items/EXP).\nRoll % after a stat: how well it rolled "
                f"(0 % worst, 100 % best possible value).")
        hdr = ui.page_header(page, "Item Comparer", hint)
        self.var_mode = tk.StringVar(value=self.cfg.get("item_mode", "Balanced"))
        cb = ttk.Combobox(hdr, textvariable=self.var_mode, values=list(item_eval.MODES), width=11, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", self._mode_changed)
        tk.Label(hdr, text="Mode", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(14, 6))
        self.var_upg = tk.StringVar(value=self.cfg.get("item_upgrade", "+0"))
        cbu = ttk.Combobox(hdr, textvariable=self.var_upg, values=["+0", "+3", "+4", "+5", "+7", "+10"], width=4,
                           state="readonly")
        cbu.pack(side="right")
        cbu.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("item_upgrade", self.var_upg.get()), self._mode_changed()))
        lab = tk.Label(hdr, text="Upgrade", bg=BG, fg=MUTED, font=ui.F_SMALL)
        lab.pack(side="right", padx=(0, 6))
        ui.Tooltip(lab, "Project both items to this upgrade level (the replaced item is assumed to be +0).")

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
        tk.Label(self.card_new, text=f"Press {self.hk.get('item', 'F8')} in the game while hovering an item",
                 bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(padx=12, pady=20)

        # 2. stat differences
        self._section(p, "Differences")
        cols = [("stat", "Stat", 170, "w"), ("d", "Change", 80, "e"), ("roll", "Roll", 55, "e"),
                ("eff", "Effect", 290, "w")]
        f, self.item_tree = self._tree(p, cols, 5)
        f.pack(fill="x", padx=14, pady=(0, 4))

        # 3. effects
        self.fx_head = self._section(p, "Effects")
        self.lbl_fx = ui.autowrap(tk.Label(p, bg=BG, fg=C_MEH, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_fx.pack(fill="x", padx=16)

        # 4. damage / survival / income
        self._section(p, "Rating")
        met = tk.Frame(p, bg=BG)
        met.pack(fill="x", padx=10)
        self.lbl_m = {}
        for i, (key, title) in enumerate((("dps", "Damage"), ("surv", "Survival"), ("farm", "Income"))):
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
        self._section(p, "Verdict")
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
        hist_head = self._section(p, "Recently checked", "Click a row to show the item again above. Click a column "
                                                         "header to sort. “Clear list” empties this list; "
                                                         "items_history.csv keeps every check.")
        ui.button(hist_head, "Clear list", self.clear_item_history, small=True).pack(side="right")
        cols = [("name", "Item", 210, "w"), ("dps", "Damage", 92, "e"), ("surv", "Survival", 100, "e"),
                ("v", "Verdict", 120, "w")]
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
            ui.Tooltip(lab, f"Name not recognized for sure (read: \"{it.raw_name}\"). The values are not affected.")
        tk.Label(nm, text=it.type_line, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x")
        body = tk.Frame(card, bg=PANEL)
        body.pack(fill="x", padx=12)

        def line(st, col, show_roll):
            r = tk.Frame(body, bg=PANEL)
            r.pack(fill="x")
            unit = "%" if st.pct else ""
            txt = f"+{st.value:g}{unit} {st.name}"  # the change itself is in the "Differences" table
            lab = tk.Label(r, text=txt + ("  ?" if not st.ok else ""), bg=PANEL, fg=col if st.ok else ui.WARN,
                           font=ui.F_SMALL, anchor="w")
            lab.pack(side="left")
            if not st.ok:
                ui.Tooltip(lab, f"Value does not fit item level and rarity – probably misread "
                                f"(OCR: \"{st.raw}\"). It is used anyway.")
            q = rolls.get(st.name) if show_roll else None
            if q:
                qc = ui.GOOD if q[1] >= 75 else (MUTED if q[1] >= 35 else ui.BAD)
                tk.Label(r, text=f"{q[1]:.0f} %", bg=PANEL, fg=qc, font=ui.F_SMALL).pack(side="right")

        # base values laid out like the game: Armor / Damage big, then "Speed ... DPS" on one row
        row = None
        for st in it.base:
            big = st.name in ("Armor", "Weapon Damage")
            label = {"Armor": "Armor", "Weapon Damage": "Damage", "Weapon Speed": "Speed",
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
        for label, rows, col, roll in (("Primary", it.primary, "#a9b4ff", True), ("Secondary", it.secondary, "#a9b4ff", True),
                                       ("Sockets", it.sockets, ui.GOOD, False)):
            if not rows and not (label == "Sockets" and it.empty_sockets):
                continue
            tk.Label(body, text=label, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", pady=(4, 0))
            for st in rows:
                line(st, col, roll)
            if label == "Sockets" and it.empty_sockets:
                fill = (gems or {}).get("fill")
                for _ in range(it.empty_sockets):
                    if fill:
                        t = f"empty → +{fill[2]:g}{'%' if fill[3] else ''} {fill[1]}"
                    else:
                        t = "empty socket"
                    lab = tk.Label(body, text=t, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w")
                    lab.pack(fill="x")
                    if fill:
                        ui.Tooltip(lab, f"Suggested: {fill[0]} – best gem of the chosen tier (Gems tab) "
                                        f"for this mode. Already included in the rating.")
        for fx in it.effects:
            ui.autowrap(tk.Label(body, text=fx, bg=PANEL, fg=ui.RARITY["Legendary"], font=ui.F_SMALL, anchor="w",
                                 justify="left")).pack(fill="x", pady=(4, 0))
        req = it.req_level or (min(70, it.item_level // 10) if it.item_level else None)
        lvl = self.state.level or (self.char_stats.get("Level") or (0,))[0]
        foot = tk.Frame(card, bg=PANEL)
        foot.pack(fill="x", padx=12, pady=(6, 10))
        if req:
            tk.Label(foot, text=f"requires level {req}", bg=PANEL, fg=ui.BAD if lvl and req > lvl else MUTED,
                     font=ui.F_SMALL).pack(side="right")
        if it.item_level:
            tk.Label(foot, text=f"Item level {it.item_level}", bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        else:
            tk.Label(foot, text="Item level not read", bg=PANEL, fg=ui.WARN, font=ui.F_SMALL).pack(side="left")

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
        hdr = ui.page_header(p, "Character Stats", f"In the game open the character window on the “Attributes” tab and press "
                                             f"{key}. Then scroll down and press {key} again – both parts are merged. "
                                             f"Read again after every gear change. Values belong to the character "
                                             f"that is logged in.")
        self._btn(hdr, "Clear", self.clear_char, side="right")
        b = ui.button(hdr, f"Read ({key})", self.scan_attributes, accent=True)
        b.pack(side="right", padx=3)
        self.lbl_char = tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_char.pack(fill="x", padx=16)
        cols = [("stat", "Stat", 240, "w"), ("v", "Value", 110, "e")]
        f, self.char_tree = self._tree(p, cols, 16)
        f.pack(fill="both", expand=True, padx=14, pady=(6, 12))

    RARITY_TAG = {"Common": "meh", "Uncommon": "blue", "Rare": "rare", "Legendary": "leg",
                  **{f"Gem Tier {i}": "gem" for i in range(1, 7)},
                  "Set Rune": "leg", "Ability Rune": "rare", "Attribute Rune": "blue", "Treasure Key": "rare"}

    def _build_gems(self, p):
        g = item_quality.GEMS
        sockets = ", ".join(f"{k} {v}" for k, v in g.get("sockets", {}).items() if v)
        sc = g.get("socket_cost", {})
        sc_txt = (" + ".join(f"{v} {k}" for k, v in sc.items() if k not in ("gold", "per") and isinstance(v, (int, float)))
                  + (f" + {sc['gold'].replace('item level', 'item level').replace(' x ', ' × ')} Gold" if isinstance(sc.get("gold"), str) else "")
                  ) if isinstance(sc, dict) else str(sc)
        top = ui.page_header(p, "Gems", (
            f"What a gem of the chosen tier brings your character in each slot type (in the mode chosen on the "
            f"Item Comparer page). Gems without effect are hidden. ★ = best gem for the slot type.\n\n"
            f"Sockets per slot: {sockets}. Sockets from item level 300, Ancient from 300 with all sockets.\n"
            f"Adding a socket: {sc_txt} per socket.\nCombining: {g.get('combine', '?')}"))
        self._btn(top, "Recalculate", self._fill_gems, side="right")
        self.var_gem_tier = tk.StringVar(value=str(self.cfg.get("gem_tier", 3)))
        cb = ttk.Combobox(top, textvariable=self.var_gem_tier, values=[str(i) for i in range(1, 7)], width=3,
                          state="readonly")
        cb.pack(side="right", padx=(0, 10))
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("gem_tier", int(self.var_gem_tier.get())),
                                                    self._fill_gems(), self._fill_bis()))
        tk.Label(top, text="Tier", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(0, 6))
        self.lbl_gems = ui.autowrap(tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_gems.pack(fill="x", padx=16, pady=(0, 6))
        cols = [("slot", "Socket in", 120, "w"), ("gem", "Gem", 90, "w"), ("bonus", "Bonus", 190, "w"),
                ("eff", "Effect", 200, "w")]
        f, self.gem_tree = self._tree(p, cols, 12)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self._fill_gems()

    def _unused_gem_text(self, p):
        g = item_quality.GEMS
        sockets = ", ".join(f"{k} {v}" for k, v in g.get("sockets", {}).items() if v)
        sc = g.get("socket_cost", {})
        sc_txt = (" + ".join(f"{v} {k}" for k, v in sc.items() if k not in ("gold", "per") and isinstance(v, (int, float)))
                  + (f" + {sc['gold'].replace('item level', 'item level').replace(' x ', ' × ')} Gold" if isinstance(sc.get("gold"), str) else "")
                  ) if isinstance(sc, dict) else str(sc)
        tk.Label(p, bg=BG, fg=MUTED, font=("Segoe UI", 8), anchor="w", justify="left", wraplength=640, text=(
            f"Value = effect of a gem of the chosen tier on your character in the chosen item mode "
            f"(Item Comparer page). ★ = best gem for this slot type.\nSockets per slot: {sockets}. Sockets from item level 300; "
            f"Ancient from 300 with all sockets. Adding a socket: {sc_txt} per socket. "
            f"Combining: {g.get('combine', '?')}")).pack(fill="x", padx=6, pady=(0, 6))
        self._fill_gems()

    def _fill_gems(self):
        tier = int(self.var_gem_tier.get())
        mode = self.cfg.get("item_mode", "Balanced")
        ctx = self._eval_context()
        self.gem_tree.delete(*self.gem_tree.get_children())
        if not ctx.char:
            self.lbl_gems.configure(text="Read your character with F9, then the gems are rated.")
        else:
            self.lbl_gems.configure(text=f"Rated for {ctx.main}, {ctx.elem} damage, mode {mode}")
        label = {"weapon": "Weapon", "armor": "Helm/Chest/Pants", "accessory": "Ring/Necklace"}
        rows = []
        for gem, group, stat, val, pct, name in item_quality.gem_options(tier):
            bonus = f"{stat} +{val:g}{'%' if pct else ''}"
            score, eff = -1e9, "-"
            if ctx.char:
                ev = item_eval.evaluate_deltas({stat: (float(val), pct)}, ctx, mode)
                score = ev.score
                parts = [f"{ev.dps_pct:+.1f} % damage" if abs(ev.dps_pct) >= 0.05 else "",
                         f"{ev.surv_pct:+.1f} % survival" if abs(ev.surv_pct) >= 0.05 else "",
                         f"{ev.farm_pct:+.1f} % income" if abs(ev.farm_pct) >= 0.05 else ""]
                eff = " · ".join(x for x in parts if x) or "no effect"
            rows.append((group, score, gem, bonus, eff))
        order = {"weapon": 0, "armor": 1, "accessory": 2}
        rows.sort(key=lambda r: (order[r[0]], -r[1]))
        seen = set()
        for group, score, gem, bonus, eff in rows:
            if ctx.char and eff == "no effect":
                continue  # no use for this character
            first = group not in seen and score > 0
            seen.add(group)
            self.gem_tree.insert("", "end", tags=("good",) if first else ("meh",) if score <= 0 else (),
                                 values=(label[group], ("★ " if first else "") + gem, bonus, eff))

    # -- best in slot ----------------------------------------------------------
    BIS_ORDER = ["Weapon", "Helm", "Chest Armor", "Necklace", "Pants", "Ring 1", "Boots", "Ring 2", "Gloves",
                 "Belt", "Shoulder", "Back"]
    ICON_BG = "#2b241b"  # warm dark behind item pictures, like the game's slots

    def _build_bis(self, p):
        hdr = ui.page_header(p, "BiS Gear", (
            "The best theoretical item per slot for the logged-in character in the chosen mode: the Legendary "
            "or Divine whose effect is worth most, with the best attributes the slot can roll for your class "
            "(item level 850, top roll – Ancient) and the best gem of the tier chosen on the Gems page in "
            "every socket.\n\nBig number: what swapping your equipped item for it changes (weighted %, like the "
            "Item Comparer) – read each equipped item once with F8. Score: what it adds on top of your current "
            "stats, only for ranking items against each other.\n“?” = effect cannot be calculated; enter your "
            "own value on the Weights page. Item pictures are loaded from wikily.gg once and kept."))
        self.var_bis_mode = tk.StringVar(value=self.cfg.get("bis_mode") or self.cfg.get("item_mode", "Balanced"))
        cb = ttk.Combobox(hdr, textvariable=self.var_bis_mode, values=list(item_eval.MODES), width=11, state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("bis_mode", self.var_bis_mode.get()), self._fill_bis()))
        tk.Label(hdr, text="Mode", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(14, 6))
        sf = ui.ScrollFrame(p)
        sf.pack(fill="both", expand=True)
        self.bis_sf = sf
        body = sf.inner
        self.lbl_bis = ui.autowrap(tk.Label(body, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_bis.pack(fill="x", padx=16, pady=(0, 6))
        self.bis_grid = tk.Frame(body, bg=BG)
        self.bis_grid.pack(fill="x", padx=10)
        for c in (0, 1):
            self.bis_grid.columnconfigure(c, weight=1, uniform="bis")
        self.bis_detail = ui.card(body, fill="x", padx=14, pady=(8, 14))
        self._bis_rows = {}
        self._bis_sel = None
        self._bis_photos = []
        self._icon_cache = {}
        self._icon_loading = set()

    # item pictures: loaded from the wiki in the background once, kept in the user folder
    def _item_photo(self, url, size):
        if not url:
            return None
        import hashlib
        key = (url, size)
        if key in self._icon_cache:
            return self._icon_cache[key]
        path = paths.user("cache", "icons", hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
        if os.path.exists(path):
            try:
                from PIL import Image, ImageTk
                img = Image.open(path).convert("RGBA")
                img.thumbnail((size, size), Image.LANCZOS)
                ph = ImageTk.PhotoImage(img)
                self._icon_cache[key] = ph
                return ph
            except Exception:
                return None
        if url not in self._icon_loading:
            self._icon_loading.add(url)

            def load():
                import base64
                import urllib.request
                try:
                    if "media.wikily.gg" in url:  # the wiki's image server scales the picture down
                        b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
                        src = f"https://img.wikily.gg/unsafe/w:128/{b64}"
                    else:
                        src = url
                    req = urllib.request.Request(src, headers={"User-Agent": "Mozilla/5.0 (DeskrawlTracker)"})
                    data = urllib.request.urlopen(req, timeout=20).read()
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "wb") as f:
                        f.write(data)
                    self.events.put(("icon_ready", url))
                except Exception:
                    pass
            threading.Thread(target=load, daemon=True).start()
        return None

    def _icon_box(self, parent, photo, rarity, size, fallback=None):
        """Item picture on the game's slot background with a rarity coloured frame."""
        rcol = ui.RARITY.get(rarity or "", LINE)
        box = tk.Frame(parent, bg=rcol, width=size + 4, height=size + 4)
        box.pack_propagate(False)
        lab = tk.Label(box, bg=self.ICON_BG, image=photo or "", text="" if photo else (fallback or "…"),
                       fg=MUTED, font=ui.F_SMALL)
        lab.pack(expand=True, fill="both", padx=2, pady=2)
        return box

    def _bis_stage_level(self, stage):
        info = stages.stage_info(stage, getattr(self, "enemy_data", {}))
        return info.get("level_min") if info else None

    def _fill_bis(self):
        if not hasattr(self, "bis_grid"):
            return
        for w in self.bis_grid.winfo_children():
            w.destroy()
        self._bis_photos = []
        ctx = self._eval_context()
        mode = self.var_bis_mode.get() if self.var_bis_mode.get() in item_eval.MODES else "Balanced"
        self._bis_rows = {}
        if not ctx.char:
            self.lbl_bis.configure(text="Read your character with F9 first – the best items depend on your stats.")
            self._bis_detail_show(None)
            return
        eq = self.cfg.get("equipped", {})
        self.lbl_bis.configure(text=f"{self.state.char_name} · {ctx.hero or 'class unknown'} · {mode} mode · "
                                    f"gems tier {ctx.gem_tier} · {len(eq)} of 11 equipped items known"
                                    + ("" if ctx.weapon else " · weapon damage unknown (F8 on your weapon)"))
        for slot in bis.SLOTS:
            cands = bis.candidates(slot, ctx, mode)
            if slot == "Ring":
                for n in (0, 1):
                    self._bis_rows[f"Ring {n + 1}"] = (cands[n] if len(cands) > n else None, cands, "Ring")
            else:
                self._bis_rows[slot] = (cands[0] if cands else None, cands, slot)
        for idx, label in enumerate(self.BIS_ORDER):
            c, cands, slot = self._bis_rows.get(label, (None, [], label))
            card = self._bis_card(label, slot, c, eq.get(slot) if label != "Ring 2" else None, ctx, mode)
            card.grid(row=idx // 2, column=idx % 2, sticky="nsew", padx=4, pady=4)
        if self._bis_sel not in self._bis_rows:
            self._bis_sel = "Weapon"
        self._bis_detail_show(self._bis_sel)

    def _bis_card(self, label, slot, c, eq, ctx, mode):
        sel = label == self._bis_sel
        outer = tk.Frame(self.bis_grid, bg=ui.ACCENT if sel else PANEL)
        card = tk.Frame(outer, bg=PANEL, cursor="hand2")
        card.pack(fill="both", expand=True, padx=1, pady=1)
        if c is None:
            tk.Label(card, text=label, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=12, pady=(10, 0))
            tk.Label(card, text="nothing for this class", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(
                fill="x", padx=12, pady=(0, 10))
            return outer
        it = c["item"]
        rcol = ui.RARITY.get(it["rarity"], FG)
        row = tk.Frame(card, bg=PANEL)
        row.pack(fill="x", padx=10, pady=(10, 4))
        ph = self._item_photo(it.get("icon_url"), 56)
        if ph:
            self._bis_photos.append(ph)
        self._icon_box(row, ph, it["rarity"], 56).pack(side="left")
        right = tk.Frame(row, bg=PANEL)
        right.pack(side="right", anchor="n")
        if slot == "Back" and not c["stats"] and not it.get("effect"):
            big, col, small = "–", MUTED, "cosmetic"
        elif eq:
            sw = bis.swap(c, eq, ctx, mode).score
            big, col, small = f"{sw:+.0f} %", (C_GOOD if sw > 0.5 else (C_BAD if sw < -0.5 else MUTED)), "vs yours"
        else:
            big, col, small = "F8", MUTED, "read yours"
        tk.Label(right, text=big, bg=PANEL, fg=col, font=ui.F_NUM_M, anchor="e").pack(anchor="e")
        tk.Label(right, text=small, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="e").pack(anchor="e")
        mid = tk.Frame(row, bg=PANEL)
        mid.pack(side="left", fill="both", expand=True, padx=(10, 6))
        tk.Label(mid, text=label + ("" if c["effect_known"] else "  ·  effect ?"), bg=PANEL, fg=MUTED,
                 font=ui.F_SMALL, anchor="w").pack(fill="x")
        ui.autowrap(tk.Label(mid, text=it["name"], bg=PANEL, fg=rcol, font=("Bahnschrift SemiBold", 11),
                             anchor="w", justify="left")).pack(fill="x")
        chips = [f"+{v:g}{'%' if p_ else ''} {s_.replace('Damage vs ', 'vs ').replace(' Damage', ' Dmg')}"
                 for s_, v, p_ in c["stats"][:2]]
        if chips:
            tk.Label(mid, text="  ·  ".join(chips), bg=PANEL, fg="#a9b4ff", font=ui.F_SMALL, anchor="w").pack(fill="x")
        farm = bis.farm_text(it)
        boss = next((f for f in farm if " per clear" in f), None)
        where = boss or (farm[0] if farm else "")
        if where:
            tk.Label(card, text="▸ " + where.split(":")[0].replace("Any enemy of level", "Enemies level")
                     .replace(" and their chests (item level 850 only on Inferno)", ""),
                     bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=12, pady=(0, 8))

        def click(_e, lab=label):
            self._bis_sel = lab
            self._fill_bis()
            self.root.after(50, lambda: self.bis_sf.canvas.yview_moveto(1.0))  # show the details below
        for w in [outer, card] + list(card.winfo_children()):
            w.bind("<Button-1>", click)
            for w2 in w.winfo_children():
                w2.bind("<Button-1>", click)
                for w3 in w2.winfo_children():
                    w3.bind("<Button-1>", click)
        return outer

    def _bis_detail_show(self, label, cand=None):
        d = self.bis_detail
        for w in d.winfo_children():
            w.destroy()
        row = self._bis_rows.get(label) if label else None
        if not row or row[0] is None:
            tk.Label(d, text="Click a slot for details.", bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(padx=12, pady=12)
            return
        c, cands, slot = row
        c = cand or c
        it, ev = c["item"], c["ev"]
        ctx = self._eval_context()
        mode = self.var_bis_mode.get()
        rcol = ui.RARITY.get(it["rarity"], FG)
        head = tk.Frame(d, bg=PANEL)
        head.pack(fill="x", padx=14, pady=(12, 6))
        ph = self._item_photo(it.get("icon_url"), 88)
        if ph:
            self._bis_photos.append(ph)
        self._icon_box(head, ph, it["rarity"], 88).pack(side="left")
        nm = tk.Frame(head, bg=PANEL)
        nm.pack(side="left", fill="x", expand=True, padx=(14, 0))
        tk.Label(nm, text=it["name"], bg=PANEL, fg=rcol, font=("Bahnschrift SemiBold", 15), anchor="w").pack(fill="x")
        tk.Label(nm, text=f"{it['rarity']} {it['slot']}  ·  {', '.join(it['classes'])}  ·  drops from level "
                          f"{it.get('min_drop_level') or '?'}", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x")
        if it.get("effect"):
            ui.autowrap(tk.Label(nm, text=it["effect"], bg=PANEL, fg=ui.RARITY["Legendary"], font=ui.F_SMALL,
                                 anchor="w", justify="left")).pack(fill="x", pady=(4, 0))
            for _, _, _, txt, known in ev.effects:
                tk.Label(nm, text=("rated: " if known else "not rated: ") + txt, bg=PANEL,
                         fg=MUTED if known else C_MEH, font=ui.F_SMALL, anchor="w").pack(fill="x")

        cols = tk.Frame(d, bg=PANEL)
        cols.pack(fill="x", padx=14, pady=(4, 0))
        cols.columnconfigure(0, weight=1, uniform="d")
        cols.columnconfigure(1, weight=1, uniform="d")
        left = tk.Frame(cols, bg=PANEL)
        left.grid(row=0, column=0, sticky="nw")
        right = tk.Frame(cols, bg=PANEL)
        right.grid(row=0, column=1, sticky="nw", padx=(12, 0))

        def head_lab(parent, text):
            tk.Label(parent, text=text, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", pady=(6, 0))

        nums = [f"{v:g} {k.replace('Weapon ', '')}" for k, (v, _) in c["numbers"].items()]
        if nums:
            head_lab(left, "Base" + (" (item level 850, top roll)" if it["rarity"] != "Divine" else ""))
            tk.Label(left, text="  ·  ".join(nums), bg=PANEL, fg=FG, font=("Bahnschrift SemiBold", 12), anchor="w").pack(fill="x")
        if c["stats"]:
            head_lab(left, "Fixed attributes" if it["rarity"] == "Divine" else "Best attributes to look for")
            for s_, v, p_ in c["stats"]:
                tk.Label(left, text=f"+{v:g}{'%' if p_ else ''} {s_}", bg=PANEL, fg="#a9b4ff", font=ui.F_SMALL,
                         anchor="w").pack(fill="x")
        if c["sockets"] and c["gem"]:
            g = c["gem"]
            head_lab(left, f"Sockets ({c['sockets']})")
            tk.Label(left, text=f"{c['sockets']}× {g[0]}: +{g[2]:g}{'%' if g[3] else ''} {g[1]} each", bg=PANEL,
                     fg=ui.GOOD, font=ui.F_SMALL, anchor="w").pack(fill="x")

        eq = self.cfg.get("equipped", {}).get(slot) if label != "Ring 2" else None
        if eq:
            sw = bis.swap(c, eq, ctx, mode)
            head_lab(right, f"Swapping your {eq['name']} (iLvl {eq.get('item_level') or '?'})")
            m = tk.Frame(right, bg=PANEL)
            m.pack(fill="x", pady=(2, 0))
            for txt, val in (("Damage", sw.dps_pct), ("Survival", sw.surv_pct), ("Income", sw.farm_pct)):
                f = tk.Frame(m, bg=ui.RAISED)
                f.pack(side="left", padx=(0, 6))
                tk.Label(f, text=txt, bg=ui.RAISED, fg=MUTED, font=ui.F_SMALL).pack(anchor="w", padx=8, pady=(4, 0))
                tk.Label(f, text=f"{val:+.0f} %", bg=ui.RAISED, fg=C_GOOD if val > 0.5 else (C_BAD if val < -0.5 else FG),
                         font=ui.F_NUM_M).pack(anchor="w", padx=8, pady=(0, 4))
        else:
            head_lab(right, "Your item")
            tk.Label(right, text=f"Not known yet – hover your equipped {slot} and press F8.", bg=PANEL, fg=MUTED,
                     font=ui.F_SMALL, anchor="w").pack(fill="x")
        head_lab(right, "Where to get it")
        for line in bis.farm_text(it, self.stage_stats.rows(self.cfg.get("profile") or self.state.char_name),
                                  self._bis_stage_level) or ["unknown"]:
            ui.autowrap(tk.Label(right, text="▸ " + line, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w",
                                 justify="left")).pack(fill="x")

        others = [x for x in cands if x is not c][:6]
        if others:
            tk.Label(d, text="Alternatives (click to view)", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))
            alt = tk.Frame(d, bg=PANEL)
            alt.pack(fill="x", padx=14, pady=(0, 12))
            for x in others:
                f = tk.Frame(alt, bg=PANEL, cursor="hand2")
                f.pack(side="left", padx=(0, 10))
                aph = self._item_photo(x["item"].get("icon_url"), 40)
                if aph:
                    self._bis_photos.append(aph)
                box = self._icon_box(f, aph, x["item"]["rarity"], 40)
                box.pack()
                tk.Label(f, text=f"{x['score']:+.0f}" + ("" if x["effect_known"] else "?"), bg=PANEL, fg=MUTED,
                         font=ui.F_SMALL).pack()
                ui.Tooltip(box, f"{x['item']['name']}\n{x['item'].get('effect', '')}")
                for w in (f, box) + tuple(box.winfo_children()):
                    w.bind("<Button-1>", lambda _e, xx=x, lab=label: self._bis_detail_show(lab, xx))
        else:
            tk.Frame(d, bg=PANEL, height=12).pack()

    # -- talents ---------------------------------------------------------------
    # Layout and look follow the afkmeta.com talent planner: a tree with a gold rail and round row
    # markers on the left, round talent icons with a "rank/max" badge, and a "Your build" panel.
    ABILITY_SLOTS = [("Basic Attack", "Basic Attack"), ("Strong Attack", "Strong Attack"), ("Special 1", "Special"),
                     ("Special 2", "Special")]
    TC = {"bg": "#100e0d", "row": "#171413", "row_line": "#2c2725", "ring": "#3b3431", "gold": "#d4a73c",
          "rail": "#b8902f", "badge": "#3a1714", "badge_line": "#5a2a24", "badge_fg": "#f3e4d0", "panel": "#141211",
          "panel_line": "#3d3833", "title": "#d9b25c", "green": "#7bc47f", "btn": "#4a1d1a", "btn_line": "#7a3a30"}

    def _build_talents(self, p):
        T = self.TC
        hdr = ui.page_header(p, "Talents", (
            "Talent planner for the logged-in character (layout of afkmeta.com). Left click adds a point, right "
            "click or “− Remove” mode removes one. Rows open when the rows above hold enough points; one "
            "capstone per capstone row.\n\nSet the build you have in the game and click “Save as my build” – "
            "your character sheet already contains it, so other builds are compared with it. “Best build” "
            "spends your points where they add most in the chosen mode. Hover a talent for its effect and what "
            "the next point is worth."))
        sf = ui.ScrollFrame(p)
        sf.pack(fill="both", expand=True)
        body = sf.inner
        wrap = tk.Frame(body, bg=BG)
        wrap.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        wrap.columnconfigure(0, weight=1)
        # tree panel
        tp = tk.Frame(wrap, bg=T["panel_line"])
        tp.grid(row=0, column=0, sticky="nsew")
        tin = tk.Frame(tp, bg=T["bg"])
        tin.pack(fill="both", expand=True, padx=1, pady=1)
        tools = tk.Frame(tin, bg=T["bg"])
        tools.pack(fill="x", padx=10, pady=(8, 4))
        self.tal_add_mode = True
        self.btn_tal_add = tk.Label(tools, text="+ Add", bg=T["bg"], fg=T["gold"], font=("Segoe UI Semibold", 9),
                                    padx=8, pady=2, cursor="hand2", highlightthickness=1, highlightbackground=T["gold"])
        self.btn_tal_add.pack(side="left")
        self.btn_tal_rem = tk.Label(tools, text="− Remove", bg=T["bg"], fg=FG, font=("Segoe UI Semibold", 9),
                                    padx=8, pady=2, cursor="hand2", highlightthickness=1, highlightbackground=T["ring"])
        self.btn_tal_rem.pack(side="left", padx=(6, 0))
        self.btn_tal_add.bind("<Button-1>", lambda _e: self._tal_set_mode(True))
        self.btn_tal_rem.bind("<Button-1>", lambda _e: self._tal_set_mode(False))
        tk.Label(tools, text="Right-click removes a point.", bg=T["bg"], fg=MUTED, font=ui.F_SMALL).pack(side="left", padx=10)
        self.tal_canvas = tk.Canvas(tin, bg=T["bg"], highlightthickness=0, height=640)
        self.tal_canvas.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.tal_canvas.bind("<Configure>", lambda _e: self._tal_draw())
        tk.Label(tin, text="The number on each row is the points you must spend in the tree to open it.",
                 bg=T["bg"], fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=12, pady=(0, 8))
        # build panel
        bp = tk.Frame(wrap, bg=T["panel_line"], width=250)
        bp.grid(row=0, column=1, sticky="n", padx=(10, 0))
        b = tk.Frame(bp, bg=T["panel"])
        b.pack(fill="both", expand=True, padx=1, pady=1)
        tk.Label(b, text="Your build", bg=T["panel"], fg=T["title"], font=("Georgia", 13), anchor="w").pack(
            fill="x", padx=12, pady=(10, 6))
        grid = tk.Frame(b, bg=T["panel"])
        grid.pack(fill="x", padx=12)
        tk.Label(grid, text="Class", bg=T["panel"], fg=MUTED, font=ui.F_SMALL).grid(row=0, column=0, sticky="w")
        tk.Label(grid, text="Hero level", bg=T["panel"], fg=MUTED, font=ui.F_SMALL).grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.lbl_tal_class = tk.Label(grid, text="-", bg=ui.RAISED, fg=FG, font=ui.F_BODY, anchor="w", padx=6, width=11)
        self.lbl_tal_class.grid(row=1, column=0, sticky="w")
        self.var_tal_level = tk.StringVar()
        e = tk.Entry(grid, textvariable=self.var_tal_level, width=6, bg=ui.RAISED, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_BODY)
        e.grid(row=1, column=1, sticky="w", padx=(10, 0), ipady=2)
        e.bind("<Return>", lambda _e: self._tal_fill())
        e.bind("<FocusOut>", lambda _e: self._tal_fill())
        facts = tk.Frame(b, bg=T["panel"])
        facts.pack(fill="x", padx=12, pady=(10, 0))
        self.tal_facts = {}
        for k, label in (("pts", "Combat Talent points"), ("need", "Level it needs"), ("mode", "Mode")):
            r = tk.Frame(facts, bg=T["panel"])
            r.pack(fill="x", pady=1)
            tk.Label(r, text=label, bg=T["panel"], fg=MUTED, font=ui.F_SMALL).pack(side="left")
            if k == "mode":
                self.var_tal_mode = tk.StringVar(value=self.cfg.get("talent_mode", "Damage"))
                cb = ttk.Combobox(r, textvariable=self.var_tal_mode, values=list(item_eval.MODES), width=9,
                                  state="readonly")
                cb.pack(side="right")
                cb.bind("<<ComboboxSelected>>", lambda _e: (self._set_cfg("talent_mode", self.var_tal_mode.get()),
                                                             self._tal_fill()))
            else:
                v = tk.Label(r, text="-", bg=T["panel"], fg=FG, font=("Bahnschrift SemiBold", 11))
                v.pack(side="right")
                self.tal_facts[k] = v
        tk.Frame(b, bg=T["panel_line"], height=1).pack(fill="x", padx=12, pady=(8, 6))
        tk.Label(b, text="Compared with my build", bg=T["panel"], fg=T["title"], font=("Georgia", 10), anchor="w").pack(
            fill="x", padx=12)
        cmp_ = tk.Frame(b, bg=T["panel"])
        cmp_.pack(fill="x", padx=12, pady=(2, 0))
        self.tal_m = {}
        for i2, (key, title) in enumerate((("dps", "Damage"), ("surv", "Survival"), ("farm", "Income"))):
            f = tk.Frame(cmp_, bg=ui.RAISED)
            f.grid(row=0, column=i2, sticky="nsew", padx=(0 if i2 == 0 else 4, 0))
            cmp_.columnconfigure(i2, weight=1, uniform="tc")
            tk.Label(f, text=title, bg=ui.RAISED, fg=MUTED, font=ui.F_SMALL).pack(anchor="w", padx=6, pady=(3, 0))
            v = tk.Label(f, text="-", bg=ui.RAISED, fg=FG, font=("Bahnschrift SemiBold", 12))
            v.pack(anchor="w", padx=6, pady=(0, 3))
            self.tal_m[key] = v
        tk.Label(b, text="Effects", bg=T["panel"], fg=T["title"], font=("Georgia", 10), anchor="w").pack(
            fill="x", padx=12, pady=(10, 0))
        self.lbl_tal_fx = tk.Label(b, text="", bg=T["panel"], fg=T["green"], font=ui.F_SMALL, anchor="w",
                                   justify="left", wraplength=220)
        self.lbl_tal_fx.pack(fill="x", padx=12)
        self.lbl_tal_note = tk.Label(b, text="", bg=T["panel"], fg=C_MEH, font=ui.F_SMALL, anchor="w",
                                     justify="left", wraplength=220)
        self.lbl_tal_note.pack(fill="x", padx=12, pady=(6, 0))
        btns = tk.Frame(b, bg=T["panel"])
        btns.pack(fill="x", padx=12, pady=(10, 4))

        def btn(parent, text, cmd, strong=False):
            l = tk.Label(parent, text=text, bg=T["btn"] if strong else ui.RAISED, fg=FG, font=("Georgia", 9),
                         padx=8, pady=5, cursor="hand2", highlightthickness=1,
                         highlightbackground=T["btn_line"] if strong else T["ring"])
            l.bind("<Button-1>", lambda _e: cmd())
            return l
        btn(btns, "Best build", self._tal_best, True).grid(row=0, column=0, sticky="ew", padx=(0, 4), pady=2)
        btn(btns, "Start over", self._tal_clear).grid(row=0, column=1, sticky="ew", pady=2)
        btn(btns, "Save as my build", self._tal_save_mine).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=2)
        btn(btns, "Load my build", self._tal_load_mine).grid(row=1, column=1, sticky="ew", pady=2)
        btn(btns, "Share build", self._tal_share).grid(row=2, column=0, sticky="ew", padx=(0, 4), pady=2)
        btn(btns, "Load build…", self._tal_load_dialog).grid(row=2, column=1, sticky="ew", pady=2)
        self.lbl_tal_share = tk.Label(b, text="", bg=T["panel"], fg=T["green"], font=ui.F_SMALL, anchor="w",
                                      justify="left", wraplength=220)
        self.lbl_tal_share.pack(fill="x", padx=12)
        # saved builds to compare
        tk.Label(b, text="Saved builds", bg=T["panel"], fg=T["title"], font=("Georgia", 10), anchor="w").pack(
            fill="x", padx=12, pady=(10, 2))
        sv = tk.Frame(b, bg=T["panel"])
        sv.pack(fill="x", padx=12)
        self.var_tal_name = tk.StringVar()
        en = tk.Entry(sv, textvariable=self.var_tal_name, bg=ui.RAISED, fg=FG, insertbackground=FG, relief="flat",
                      font=ui.F_SMALL, width=18)
        en.pack(side="left", fill="x", expand=True, ipady=3)
        en.bind("<Return>", lambda _e: self._tal_save_named())
        btn(sv, "Save", self._tal_save_named).pack(side="left", padx=(4, 0))
        ui.Tooltip(en, "Name for the planned build, e.g. “Lightning crit”. Saved builds are compared with your "
                       "build in the current mode; click a name to load it.")
        self.tal_saved = tk.Frame(b, bg=T["panel"])
        self.tal_saved.pack(fill="x", padx=12, pady=(4, 0))
        btns.columnconfigure(0, weight=1)
        btns.columnconfigure(1, weight=1)
        # abilities in use
        tk.Label(b, text="Abilities and share of damage", bg=T["panel"], fg=T["title"], font=("Georgia", 10),
                 anchor="w").pack(fill="x", padx=12, pady=(10, 2))
        self.tal_ab = []
        for label, _slot in self.ABILITY_SLOTS:
            r = tk.Frame(b, bg=T["panel"])
            r.pack(fill="x", padx=12, pady=1)
            v_name, v_share = tk.StringVar(), tk.StringVar()
            c = ttk.Combobox(r, textvariable=v_name, width=16, state="readonly")
            c.pack(side="left")
            en = tk.Entry(r, textvariable=v_share, width=4, bg=ui.RAISED, fg=FG, insertbackground=FG, relief="flat",
                          justify="right")
            en.pack(side="left", padx=(4, 0), ipady=2)
            tk.Label(r, text="%", bg=T["panel"], fg=MUTED, font=ui.F_SMALL).pack(side="left")
            c.bind("<<ComboboxSelected>>", lambda _e: self._tal_abilities_changed())
            en.bind("<FocusOut>", lambda _e: self._tal_abilities_changed())
            en.bind("<Return>", lambda _e: self._tal_abilities_changed())
            ui.Tooltip(c, label)
            self.tal_ab.append((c, v_name, v_share))
        self.lbl_tal_measured = tk.Label(b, text="", bg=T["panel"], fg=MUTED, font=ui.F_SMALL, anchor="w",
                                         justify="left", wraplength=220)
        self.lbl_tal_measured.pack(fill="x", padx=12, pady=(6, 0))
        self.btn_tal_measured = btn(b, "Use measured shares", self._tal_use_measured)
        self.btn_tal_measured.pack(fill="x", padx=12, pady=(4, 0))
        tk.Frame(b, bg=T["panel"], height=10).pack()
        self.tal_build = {}
        self._tal_icons = {}
        self._tal_tip = None
        self._tal_load()

    def _tal_set_mode(self, add):
        T = self.TC
        self.tal_add_mode = add
        self.btn_tal_add.configure(fg=T["gold"] if add else FG, highlightbackground=T["gold"] if add else T["ring"])
        self.btn_tal_rem.configure(fg=T["gold"] if not add else FG, highlightbackground=T["gold"] if not add else T["ring"])

    def _tal_hero(self):
        return self.state.hero if self.state.hero in talents.HEROES else None

    def _tal_points(self):
        try:
            return int(self.var_tal_level.get())
        except (ValueError, AttributeError):
            return int(self.state.level or (self.char_stats.get("Level") or (0,))[0] or 0)

    def _tal_load(self):
        """Fill the page for the logged-in character (also after a character change)."""
        if not hasattr(self, "tal_canvas"):
            return
        hero = self._tal_hero()
        if hero:
            for k in ("talents_mine", "talents_plan"):
                if self.cfg.get(k):
                    self.cfg[k] = talents.normalize(self.cfg[k], hero)
        self.tal_build = dict(self.cfg.get("talents_plan") or self.cfg.get("talents_mine") or {})
        self.var_tal_level.set(str(int(self.state.level or (self.char_stats.get("Level") or (0,))[0] or 0)))
        setup = self.cfg.get("ability_setup") or {}
        defaults = {"Sorcerer": {"Basic Attack": ("Electrocute", 60), "Strong Attack": ("Lightning Storm", 40)}}.get(hero, {})
        for (label, slot), (c, v_name, v_share) in zip(self.ABILITY_SLOTS, self.tal_ab):
            names = [a["name"] for a in talents.abilities_of(hero) if a.get("slot") == slot] if hero else []
            c.configure(values=["–"] + names)
            name, share = setup.get(label) or defaults.get(label) or ("–", 0)
            v_name.set(name if name in names else "–")
            v_share.set(f"{share:g}")
        self._tal_fill()

    def _tal_shares(self):
        out = {}
        for (_label, _slot), (_c, v_name, v_share) in zip(self.ABILITY_SLOTS, self.tal_ab):
            try:
                sh = float(v_share.get().replace(",", ".") or 0)
            except ValueError:
                sh = 0
            if v_name.get() not in ("", "–") and sh > 0:
                out[v_name.get()] = out.get(v_name.get(), 0) + sh
        return out

    def _tal_abilities_changed(self):
        setup = {}
        for (label, _slot), (_c, v_name, v_share) in zip(self.ABILITY_SLOTS, self.tal_ab):
            try:
                setup[label] = [v_name.get(), float(v_share.get().replace(",", ".") or 0)]
            except ValueError:
                setup[label] = [v_name.get(), 0]
        self._set_cfg("ability_setup", setup)
        self._tal_fill()

    def _cast_shares(self, casts: dict) -> dict:
        """Damage share per ability from cast counts: casts x damage % x targets."""
        info = {a["name"]: a for a in skills.ABILITIES}
        w = {k: v * skills.damage_weight(info[k]) for k, v in casts.items() if k in info}
        tot = sum(w.values()) or 1
        return {k: v / tot * 100 for k, v in sorted(w.items(), key=lambda kv: -kv[1])}

    def _measured_casts(self):
        """All casts counted on this character's stages, plus the running session."""
        total, runs = {}, 0
        for x in self.stage_stats.rows(self.cfg.get("profile") or self.state.char_name):
            for k, v in x.get("casts", {}).items():
                total[k] = total.get(k, 0) + v
            runs += x.get("cast_runs", 0)
        return total, runs

    def _tal_use_measured(self):
        casts, runs = self._measured_casts()
        if not casts:
            return
        shares = self._cast_shares(casts)
        slot_of = {a["name"]: a.get("slot") for a in skills.ABILITIES}
        setup, specials = {}, ["Special 1", "Special 2"]
        for name, sh in shares.items():
            slot = slot_of.get(name)
            label = slot if slot in ("Basic Attack", "Strong Attack") else (specials.pop(0) if specials else None)
            if label and label not in setup:
                setup[label] = [name, round(sh)]
        self._set_cfg("ability_setup", setup)
        self._tal_load()

    # share / load / saved builds
    def _tal_share(self):
        hero = self._tal_hero()
        if not hero:
            return
        link = talents.share_link(self.tal_build, hero)
        self.root.clipboard_clear()
        self.root.clipboard_append(link)
        self.lbl_tal_share.configure(text="Link copied – it opens this build on afkmeta.com and can be loaded "
                                          "here with “Load build…”.")

    def _tal_load_dialog(self):
        hero = self._tal_hero()
        if not hero:
            return
        d = tk.Toplevel(self.root, bg=BG)
        d.title("Load build")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Label(d, text="Paste a build link (afkmeta.com talent planner) or code:", bg=BG, fg=FG,
                 font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        var = tk.StringVar()
        try:
            clip = self.root.clipboard_get()
            if "afkmeta.com" in clip and "talents" in clip:
                var.set(clip.strip())
        except Exception:
            pass
        e = tk.Entry(d, textvariable=var, width=60, bg=ui.RAISED, fg=FG, insertbackground=FG, relief="flat",
                     font=ui.F_SMALL)
        e.pack(fill="x", padx=14, ipady=4)
        msg = tk.Label(d, text="", bg=BG, fg=C_BAD, font=ui.F_SMALL, anchor="w")
        msg.pack(fill="x", padx=14, pady=(4, 0))

        def ok():
            r = talents.decode(var.get(), hero)
            if r is None:
                msg.configure(text="Not a talent build link or code.")
                return
            build, link_hero = r
            if link_hero and link_hero != hero:
                msg.configure(text=f"That is a {link_hero} build – you are playing {hero}.")
                return
            self.tal_build = build
            self._tal_store()
            self._tal_fill()
            used = sum(build.values())
            note = "" if talents.valid(build, hero) else " (rows are not open for every point)"
            self.lbl_tal_share.configure(text=f"Build loaded: {used} points{note}.")
            d.destroy()

        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=14, pady=12)
        ui.button(bar, "Load", ok, accent=True).pack(side="right")
        ui.button(bar, "Cancel", d.destroy).pack(side="right", padx=6)
        e.bind("<Return>", lambda _e: ok())
        d.update_idletasks()
        d.geometry(f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 120}")
        e.focus_set()

    def _tal_save_named(self):
        name = self.var_tal_name.get().strip()
        if not name or not self.tal_build:
            self.lbl_tal_share.configure(text="Plan a build and give it a name first.")
            return
        builds = self.cfg.setdefault("talent_builds", {})
        builds[name] = dict(self.tal_build)
        save_config(self.cfg)
        self.var_tal_name.set("")
        self._tal_fill()

    def _tal_load_named(self, name):
        b = (self.cfg.get("talent_builds") or {}).get(name)
        if b is not None:
            self.tal_build = dict(b)
            self._tal_store()
            self._tal_fill()

    def _tal_delete_named(self, name):
        (self.cfg.get("talent_builds") or {}).pop(name, None)
        save_config(self.cfg)
        self._tal_fill()

    def _tal_fill_saved(self, hero, ctx, mode, shares, mine):
        T = self.TC
        for w in self.tal_saved.winfo_children():
            w.destroy()
        builds = self.cfg.get("talent_builds") or {}
        if not builds:
            tk.Label(self.tal_saved, text="No saved builds yet.", bg=T["panel"], fg=MUTED, font=ui.F_SMALL,
                     anchor="w").pack(fill="x")
            return
        head = tk.Frame(self.tal_saved, bg=T["panel"])
        head.pack(fill="x")
        for txt, w in (("Build", 0), ("Dmg", 6), ("Surv", 6)):
            tk.Label(head, text=txt, bg=T["panel"], fg=MUTED, font=ui.F_SMALL, width=w or None,
                     anchor="w" if not w else "e").pack(side="left", fill="x", expand=not w)
        rows = []
        for name, b in builds.items():
            b = talents.normalize(b, hero)
            vals = talents.evaluate(b, mine, hero, ctx, mode, shares) if ctx.char else (0, 0, 0, 0)
            rows.append((vals[3], name, b, vals))
        best = max(r[0] for r in rows)
        for score, name, b, (dps, surv, farm, _) in sorted(rows, key=lambda r: -r[0]):
            r = tk.Frame(self.tal_saved, bg=T["panel"])
            r.pack(fill="x", pady=1)
            cur = b == self.tal_build
            nm = tk.Label(r, text=("● " if cur else "") + name, bg=T["panel"],
                          fg=T["gold"] if score == best and ctx.char else FG, font=ui.F_SMALL, anchor="w", cursor="hand2")
            nm.pack(side="left", fill="x", expand=True)
            nm.bind("<Button-1>", lambda _e, n=name: self._tal_load_named(n))
            x = tk.Label(r, text="✕", bg=T["panel"], fg=MUTED, font=ui.F_SMALL, cursor="hand2")
            x.pack(side="right", padx=(4, 0))
            x.bind("<Button-1>", lambda _e, n=name: self._tal_delete_named(n))
            for val in (surv, dps):
                tk.Label(r, text=f"{val:+.0f}%", bg=T["panel"], width=6, anchor="e", font=ui.F_SMALL,
                         fg=C_GOOD if val > 0.5 else (C_BAD if val < -0.5 else MUTED)).pack(side="right")
            ui.Tooltip(nm, f"{name}: {sum(b.values())} points · {dps:+.1f} % damage · {surv:+.1f} % survival · "
                           f"{farm:+.1f} % income compared with your build ({mode} mode). Click to load.")

    def _tal_store(self):
        self.cfg["talents_plan"] = dict(self.tal_build)
        save_config(self.cfg)

    def _tal_clear(self):
        self.tal_build = {}
        self._tal_store()
        self._tal_fill()

    def _tal_load_mine(self):
        self.tal_build = dict(self.cfg.get("talents_mine") or {})
        self._tal_store()
        self._tal_fill()

    def _tal_save_mine(self):
        self.cfg["talents_mine"] = dict(self.tal_build)
        self._tal_store()
        self._tal_fill()

    def _tal_best(self):
        hero = self._tal_hero()
        if not hero or not self.char_stats:
            return
        self.lbl_tal_note.configure(text="Searching the best build…")
        self.root.update_idletasks()
        self.tal_build = talents.best_build(self._tal_points(), hero, self._eval_context(), self.var_tal_mode.get(),
                                            self._tal_shares(), self.cfg.get("talents_mine") or {})
        self._tal_store()
        self._tal_fill()

    def _tal_click(self, t, delta):
        hero = self._tal_hero()
        if delta > 0 and not self.tal_add_mode:
            delta = -1
        used = sum(self.tal_build.values())
        if delta > 0 and used < self._tal_points() and talents.can_add(self.tal_build, t, hero):
            self.tal_build[talents.key(t)] = self.tal_build.get(talents.key(t), 0) + 1
        elif delta < 0 and talents.can_remove(self.tal_build, t, hero):
            self.tal_build[talents.key(t)] -= 1
            if not self.tal_build[talents.key(t)]:
                del self.tal_build[talents.key(t)]
        else:
            return
        self._tal_store()
        self._tal_fill()

    def _tal_icon(self, url, size, active, locked):
        """Round talent picture (cached per state)."""
        key = (url, size, active, locked)
        if key in self._tal_icons:
            return self._tal_icons[key]
        base = self._item_photo(url, 64)  # loads/caches the file
        if base is None:
            return None
        import hashlib
        from PIL import Image, ImageDraw, ImageEnhance, ImageTk
        path = paths.user("cache", "icons", hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")
        try:
            img = Image.open(path).convert("RGBA").resize((size, size), Image.LANCZOS)
        except Exception:
            return None
        if locked:
            img = ImageEnhance.Brightness(ImageEnhance.Color(img).enhance(0.25)).enhance(0.45)
        mask = Image.new("L", (size * 4, size * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
        img.putalpha(mask.resize((size, size), Image.LANCZOS))
        ph = ImageTk.PhotoImage(img)
        self._tal_icons[key] = ph
        return ph

    def _tal_fill(self):
        if not hasattr(self, "tal_canvas"):
            return
        T = self.TC
        hero = self._tal_hero()
        self.lbl_tal_class.configure(text=hero or "unknown")
        ctx = self._eval_context()
        mode = self.var_tal_mode.get() if self.var_tal_mode.get() in item_eval.MODES else "Damage"
        shares = self._tal_shares()
        mine = self.cfg.get("talents_mine") or {}
        pts, used = self._tal_points(), sum(self.tal_build.values())
        self.tal_facts["pts"].configure(text=f"{used} / {pts}", fg=T["gold"] if used else FG)
        self.tal_facts["need"].configure(text=str(used) if used else "–")
        notes = []
        if not hero:
            notes.append("Class unknown – start the game once so the log names your hero.")
        if not self.char_stats:
            notes.append("Read your character with F9 – talent values depend on your stats.")
        if hero and not mine:
            notes.append("Set your current build and click “Save as my build”.")
        if hero and not shares:
            notes.append("Choose your abilities below, or ability talents count as nothing.")
        self.lbl_tal_note.configure(text="\n".join(notes))
        casts, runs = self._measured_casts()
        if casts:
            self.lbl_tal_measured.configure(text=f"Measured on the skill bar ({runs} runs): " + " · ".join(
                f"{k} {v:.0f} %" for k, v in self._cast_shares(casts).items()))
        else:
            self.lbl_tal_measured.configure(text="Switch on “Skill tracking” (Controls) to measure your ability "
                                                 "use instead of guessing.")
        self._tal_vals = {}
        if hero and ctx.char:
            dps, surv, farm, score = talents.evaluate(self.tal_build, mine, hero, ctx, mode, shares)
            for key, val in (("dps", dps), ("surv", surv), ("farm", farm)):
                self.tal_m[key].configure(text=f"{val:+.1f} %", fg=C_GOOD if val > 0.05 else (C_BAD if val < -0.05 else FG))
            for t in talents.tree(hero):
                rank = self.tal_build.get(talents.key(t), 0)
                if talents.rated(t, hero) and rank < t["ranks"] and talents.can_add(self.tal_build, t, hero):
                    b2 = dict(self.tal_build)
                    b2[talents.key(t)] = rank + 1
                    self._tal_vals[talents.key(t)] = talents.evaluate(b2, mine, hero, ctx, mode, shares)[3] - score
        else:
            for v in self.tal_m.values():
                v.configure(text="-", fg=FG)
        # effects of the planned build, like the planner's list
        lines = []
        if hero:
            stats, ab = talents.effects(self.tal_build, hero)
            for k, v in sorted(stats.items()):
                pct = k not in ("Intelligence", "Strength", "Dexterity", "Armor", "Max Health", "Magic Resist",
                                "Max Mana", "Mana Regeneration", "Life Regeneration", "Thorns")
                lines.append(f"+{v:g}{'%' if pct else ''} {k}")
            names = {"ability": "damage", "crit_chance": "crit chance", "crit_damage": "crit damage"}
            for kind, tg, v in ab:
                if kind in names:
                    lines.append(f"+{v:g}% {tg.replace('tag:', '')} {names[kind]}")
                elif kind == "mana_to_int":
                    lines.append(f"{v:g}% of Max Mana as Intelligence")
                elif kind == "mr_pct":
                    lines.append(f"+{v:g}% Magic Resist")
            for t in talents.tree(hero):
                if self.tal_build.get(talents.key(t)) and not talents.rated(t, hero):
                    lines.append(f"{t['name']} {self.tal_build[talents.key(t)]}/{t['ranks']} (not rated)")
        self.lbl_tal_fx.configure(text="\n".join("• " + l for l in lines) if lines else "Pick talents to see their effects.")
        if hero:
            self._tal_fill_saved(hero, ctx, mode, shares, mine)
        self._tal_draw()

    def _tal_draw(self):
        cv = self.tal_canvas
        cv.delete("all")
        hero = self._tal_hero()
        if not hero:
            return
        T = self.TC
        W = max(cv.winfo_width(), 300)
        rows = talents.rows(hero)
        size = max(30, min(44, int((W - 70) / 7)))
        row_h = size + 30
        cv.configure(height=row_h * len(rows) + 8)
        rail_x = 18
        x0, x1 = 40, W - 6
        # rail
        cv.create_line(rail_x, row_h / 2, rail_x, row_h * (len(rows) - 0.5), fill=T["rail"], width=5)
        tree_ = talents.tree(hero)
        for r_i, thr in enumerate(rows):
            y0 = r_i * row_h + 3
            open_ = talents.spent_below(self.tal_build, hero, thr) >= thr
            cv.create_rectangle(x0, y0, x1, y0 + row_h - 6, fill=T["row"], outline=T["row_line"])
            cy = y0 + (row_h - 6) / 2
            cv.create_oval(rail_x - 12, cy - 12, rail_x + 12, cy + 12, fill="#1d1916",
                           outline=T["gold"] if open_ else T["ring"], width=2)
            cv.create_text(rail_x, cy, text=str(thr), fill=T["gold"] if open_ else MUTED, font=("Segoe UI Semibold", 8))
            for t in [x for x in tree_ if x["points"] == thr]:
                rank = self.tal_build.get(talents.key(t), 0)
                cx = x0 + 10 + size / 2 + t.get("x", 0.5) * (x1 - x0 - 20 - size)
                iy = y0 + 4 + size / 2
                tag = "t_" + t["id"] if t.get("id") else "t_" + str(abs(hash(t["name"])))
                ph = self._tal_icon(t.get("icon_url"), size, rank > 0, not open_)
                if ph:
                    cv.create_image(cx, iy, image=ph, tags=(tag,))
                else:
                    cv.create_oval(cx - size / 2, iy - size / 2, cx + size / 2, iy + size / 2, fill="#221d1a",
                                   outline="", tags=(tag,))
                ring = T["gold"] if rank else (T["ring"] if open_ else "#2a2523")
                cv.create_oval(cx - size / 2 - 1, iy - size / 2 - 1, cx + size / 2 + 1, iy + size / 2 + 1,
                               outline=ring, width=2, tags=(tag,))
                txt = f"{rank}/{t['ranks']}"
                bw = 8 + 6 * len(txt)
                by = iy + size / 2 + 2
                cv.create_rectangle(cx - bw / 2, by, cx + bw / 2, by + 14, fill=T["badge"], outline=T["badge_line"], tags=(tag,))
                cv.create_text(cx, by + 7, text=txt, fill=T["badge_fg"] if open_ else MUTED,
                               font=("Segoe UI Semibold", 7), tags=(tag,))
                cv.tag_bind(tag, "<Button-1>", lambda _e, tt=t: self._tal_click(tt, +1))
                cv.tag_bind(tag, "<Button-3>", lambda _e, tt=t: self._tal_click(tt, -1))
                cv.tag_bind(tag, "<Shift-Button-1>", lambda _e, tt=t: self._tal_click(tt, -1))
                cv.tag_bind(tag, "<Enter>", lambda e, tt=t: self._tal_show_tip(e, tt))
                cv.tag_bind(tag, "<Leave>", lambda _e: self._tal_hide_tip())
        cv.configure(cursor="hand2")

    def _tal_show_tip(self, e, t):
        self._tal_hide_tip()
        hero = self._tal_hero()
        rank = self.tal_build.get(talents.key(t), 0)
        mine = (self.cfg.get("talents_mine") or {}).get(talents.key(t), 0)
        lines = [f"{t['name']}   {rank}/{t['ranks']}",
                 f"{t['points']} points spent" + (f" · {t['capstone']}" if t.get("capstone") else "")]
        if t.get("rank1") and t["ranks"] > 1:
            lines.append(f"Rank 1: {t['rank1']}")
        lines.append(f"Rank {t['ranks']}: {t['rank_max']}" if t["ranks"] > 1 else t["rank_max"])
        if not talents.rated(t, hero):
            lines.append("Not rated – the effect cannot be calculated.")
        elif talents.key(t) in self._tal_vals:
            lines.append(f"Next point: {self._tal_vals[talents.key(t)]:+.2f} % ({self.var_tal_mode.get()})")
        if self.cfg.get("talents_mine"):
            lines.append(f"Your build: {mine}/{t['ranks']}")
        tip = tk.Toplevel(self.root)
        tip.wm_overrideredirect(True)
        tip.attributes("-topmost", True)
        tk.Label(tip, text="\n".join(lines), bg=ui.RAISED, fg=FG, font=ui.F_SMALL, justify="left", wraplength=300,
                 padx=10, pady=7).pack(padx=1, pady=1)
        tip.configure(bg=self.TC["gold"])
        tip.geometry(f"+{e.x_root + 14}+{e.y_root + 12}")
        self._tal_tip = tip

    def _tal_hide_tip(self):
        if self._tal_tip is not None:
            try:
                self._tal_tip.destroy()
            except Exception:
                pass
            self._tal_tip = None

    def _build_stages(self, p):
        ui.page_header(p, "Stages", "All runs per stage and difficulty of this character, across restarts. Gold "
                                    "includes sales. ★ = best stage for EXP or gold (from 3 runs).\n\nClick a row: "
                                    "enemies and their damage types, item level of drops and a forecast for the "
                                    "next difficulty.")
        cols = [("stage", "Stage", 180, "w"), ("diff", "Diff", 64, "w"), ("runs", "Runs", 50, "e"),
                ("t", "Time", 56, "e"), ("xp", "EXP/h", 75, "e"), ("gold", "Gold/h", 72, "e"),
                ("it", "Items/h", 70, "e"), ("dead", "Deaths", 66, "e"), ("dps", "DPS", 66, "e")]
        f, self.stage_tree = self._tree(p, cols, 9)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.stage_tree.tag_configure("best", foreground=C_GOOD)
        self.stage_tree.bind("<<TreeviewSelect>>", self._stage_select)
        det = ui.card(p, fill="x", padx=14, pady=(0, 12))
        self.lbl_stage_detail = ui.autowrap(tk.Label(det, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left",
                                                     text="Click a stage in the list for details."), 24)
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
            stage = r.stage_name or r.stage_guess or f"Unknown ({r.waves} waves)"
            cycle = r.end - prev.end if prev and prev.end and 0 < r.end - prev.end < r.duration * 1.5 + 60 else r.duration
            t0 = prev.end if prev and prev.end else r.start
            sold_gold = sum(g for t, _, g, _ in sold if t0 < t <= r.end + 10)
            self.stage_stats.add_run(r.char or self.state.char_name, stage, r.difficulty, cycle, r.xp, r.gold,
                                     sold_gold, r.items,
                                     r.death == "confirmed" or r.death == "suspected",
                                     r.damage, r.duration if r.damage else 0, r.casts or None)
            r.aggregated = True
            changed = True
            prev = r
        if changed:
            self.stage_stats.save()
        rows = self.stage_stats.rows(self.cfg.get("profile") or self.state.char_name)
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
        parts = [f"{x['stage']} · {x['difficulty']} · {x['runs']} runs · avg {fmt(x['xp_run'])} EXP/run"]
        if x.get("cast_runs"):
            n = x["cast_runs"]
            per = sorted(((v / n, k) for k, v in x["casts"].items()), reverse=True)
            shares = self._cast_shares(x["casts"])
            parts.append(f"Abilities per run ({n} watched runs): " + " · ".join(f"{k} {v:.0f}" for v, k in per))
            parts.append("Estimated damage share: " + " · ".join(f"{k} {v:.0f} %" for k, v in shares.items()))
        if info:
            prof = stages.damage_profile(info)
            lv = f"Level {info.get('level_min')}–{info.get('level_max')}" if info.get("level_min") else ""
            dmg = ", ".join(f"{k} {v * 100:.0f} %" for k, v in sorted(prof.items(), key=lambda kv: -kv[1])) or "unknown"
            dot = ", ".join(info.get("dot") or []) or "none known"
            parts.append(f"Enemies: {lv} · damage types: {dmg} · damage over time: {dot}"
                         + (f" · Boss: {info['boss']}" if info.get("boss") else ""))
            if info.get("enemies"):
                parts.append("Enemy types: " + ", ".join(info["enemies"][:12]))
            lo, hi = item_quality.drop_item_level(info.get("level_min") or 70, x["difficulty"])
            loot = [f"Drops: item level {lo}–{hi} (wearable from level {min(70, lo // 10)})"]
            if info.get("boss"):
                blvl = 70 if x["difficulty"] != "Normal" else (info.get("level_max") or 70)
                shards = item_quality.soul_shards(blvl, x["difficulty"])
                per_h = shards * 3600 / x["avg_s"] if x.get("avg_s") else 0
                loot.append(f"Boss {info['boss']}: {shards} Soul Shards per boss kill (≈ {per_h:.0f}/h)")
            parts.append(" · ".join(loot))
        else:
            parts.append("Enemy data: this stage is not in data/enemies.json.")
        level = info.get("level_max") if info else None
        fc = stages.forecast(x, level)
        if fc:
            parts.append(
                f"Forecast {fc['difficulty']} (same gear): enemies {fc['hp_factor']:.1f}× HP, "
                f"{fc['dmg_factor']:.2f}× damage → run ≈ {fmt_dur(fc['run_s'])}, EXP/h ≈ {fmt(fc['xp_h'])} "
                f"(now {fmt(x['xp_h'])}), Gold/h ≈ {fmt(fc['gold_h'])} (now {fmt(x['gold_h'])}).")
            need = fc["dmg_factor"]
            parts.append(
                f"To be as safe there you would need ≈ {need:.2f}× your current effective HP "
                f"(Toughness {fmt(self._toughness() or 0)} → ≈ {fmt((self._toughness() or 0) * need)})"
                + (f" · now {x['death_rate'] * 100:.0f} % deaths" if x["runs"] else "") + "."
                + ("" if fc["stage_level_known"] else " Stage level unknown: the rise to level 70 is not included."))
        self.lbl_stage_detail.configure(text="\n".join(parts))

    def _toughness(self):
        c = {k: v[0] for k, v in self.char_stats.items()}
        if not c:
            return None
        ctx = self._eval_context()
        return item_eval.defense(c, c, ctx)["toughness"]

    def _build_drops(self, p):
        hdr = ui.page_header(p, "Drops", "Source: the pop-ups at the bottom left of the game (“Sold …”, “Obtained …”), "
                                         "otherwise the log panel. Rarity from the colour of the name: white Common, "
                                         "blue Uncommon, yellow Rare, orange Legendary. Gems by tier "
                                         "(Raw Sphere 1 to Radiant Octagon 6).")
        self.lbl_drops = tk.Label(hdr, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="e")
        self.lbl_drops.pack(side="right")
        cols = [("r", "Rarity", 120, "w"), ("n", "Count", 70, "e"), ("h", "per h", 70, "e"),
                ("pct", "Share", 70, "e"), ("sold", "sold", 70, "e"), ("gold", "Sales gold", 100, "e")]
        f, self.drop_tree = self._tree(p, cols, 6)
        f.pack(fill="x", padx=14, pady=(0, 4))
        self._section(p, "Recent drops")
        cols = [("t", "Time", 62, "e"), ("item", "Item", 230, "w"), ("r", "Rarity", 90, "w"),
                ("a", "Action", 90, "w"), ("g", "Gold", 60, "e")]
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
        self.lbl_drops.configure(text=f"{total} drops this session" if total else "No drops yet")
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
        top = ui.page_header(p, "Deaths", "The game writes deaths only to the log panel. “confirmed”: death line "
                                        "(“Killed by …”) or death text read. “suspected”: the run brought less than 60 % "
                                        "of the usual EXP of this stage and ended early (from 3 comparison runs). "
                                        "Wave ≈ estimated from the EXP share. Saved in deaths_log.csv.")
        self.lbl_deaths = tk.Label(top, text="", bg=BG, fg=FG, font=("Bahnschrift SemiBold", 13), anchor="e")
        self.lbl_deaths.pack(side="right")
        self.lbl_deaths_sub = tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left")
        self.lbl_deaths_sub.pack(fill="x", padx=16)
        cols = [("t", "Time", 62, "e"), ("st", "Status", 74, "w"), ("stage", "Stage", 130, "w"),
                ("killer", "Killed by", 150, "w"),
                ("wave", "Wave ≈", 60, "e"), ("dur", "Duration", 56, "e"), ("xp", "EXP share", 76, "e"),
                ("dps", "Avg DPS", 66, "e")]
        f, self.death_tree = self._tree(p, cols, 10)
        f.pack(fill="both", expand=True, padx=14, pady=(6, 6))
        self.death_tree.bind("<<TreeviewSelect>>", self._death_select)
        det = ui.card(p, fill="x", padx=14, pady=(0, 12))
        self.lbl_death_detail = ui.autowrap(tk.Label(det, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left",
                                                     text="Click a death in the list for details."), 24)
        self.lbl_death_detail.pack(fill="x", padx=12, pady=10)
        self._death_rows = None

    def _refresh_deaths(self):
        d = self.state.death_stats()
        if d["n"] == 0:
            self.lbl_deaths.configure(text="No deaths this session", fg=C_GOOD)
        else:
            self.lbl_deaths.configure(text=f"{d['n']} {'death' if d['n'] == 1 else 'deaths'}, {d['per_h']:.1f} per hour",
                                      fg=C_BAD)
        self.lbl_deaths_sub.configure(
            text=f"{d['confirmed']} confirmed · {d['n'] - d['confirmed']} suspected · "
                 f"{d['rate'] * 100:.1f} % of runs")
        self.nb.tab(self.tab_death, text=f"Deaths ({d['n']})" if d["n"] else "Deaths")
        sig = (d["n"], sum(bool(r.death_info.get("killer")) for r in d["list"]))
        if sig == self._death_rows:
            return
        self._death_rows = sig
        self._death_list = d["list"]
        self.death_tree.delete(*self.death_tree.get_children())
        for idx, r in reversed(list(enumerate(d["list"]))):
            i = r.death_info
            xr = i.get("xp_ratio")
            self.death_tree.insert("", "end", iid=str(idx), tags=("bad" if r.death == "confirmed" else "meh",), values=(
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
                 f"Stage: {r.difficulty}, {r.waves} waves · died ≈ wave {i.get('est_wave', '?')}",
                 f"Run time {fmt_dur(r.duration)} ({(i.get('dur_ratio') or 0) * 100:.0f} % of usual) · "
                 f"EXP {fmt(r.xp)} ({(i.get('xp_ratio') or 0) * 100:.0f} % of usual, based on {i.get('ref_runs', 0)} runs)",
                 f"Gold {fmt(r.gold)} · Items {r.items} · MF {r.mf:g} GF {r.gf:g}"]
        if r.damage:
            parts.append(f"Avg DPS in the run {fmt(r.avg_dps)} · peak {fmt(r.peak_dps)}")
        if i.get("killer"):
            parts.append(f"Killed by {i['killer']} (Lv. {i.get('killer_level', '?')})"
                         + (f" · {i['source']}" if i.get("source") else "")
                         + f" · {i.get('damage', '?')} {i.get('element', '')} Damage")
        elif i.get("screen"):
            parts.append(f"Screen: “{i['screen']}”")
        self.lbl_death_detail.configure(text="\n".join(parts))

    def _build_weights(self, p):
        top = ui.page_header(p, "Weights", "Basis for the item comparison and the gems. Damage and survival "
                                             "follow the game's formulas; here you set what cannot be "
                                             "calculated. Saved per character.")
        self._btn(top, "Restore defaults", self.reset_weights, side="right")
        row = tk.Frame(p, bg=BG)
        row.pack(fill="x", padx=16, pady=(0, 4))
        tk.Label(row, text="Damage element", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left", padx=(0, 6))
        self.var_elem = tk.StringVar(value=self.cfg.get("element", "Auto"))
        cb = ttk.Combobox(row, textvariable=self.var_elem, values=["Auto"] + item_eval.ELEMENTS, width=10,
                          state="readonly")
        cb.pack(side="left")
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("element", self.var_elem.get()), self._fill_eval_tab()))
        self.lbl_ctx = ui.autowrap(tk.Label(p, bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"))
        self.lbl_ctx.pack(fill="x", padx=16, pady=(0, 2))
        self._section(p, "Legendary effects", "Double-click “Own value”: e.g. “15” = +15 % damage, "
                                              "“0/10” = +10 % survival. Empty = automatic rating.")
        cols = [("name", "Legendary", 180, "w"), ("slot", "Slot", 70, "w"), ("auto", "Rating", 250, "w"),
                ("own", "Own value", 90, "e")]
        f, self.leg_tree = self._tree(p, cols, 8)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.leg_tree.bind("<Double-1>", self._edit_legendary)
        self._section(p, "Other stats", "Stats outside the formulas. Double-click the value to change it: "
                                           "how many percent damage, survival or income one point (or 1 %) brings.")
        cols = [("stat", "Stat", 200, "w"), ("tgt", "affects", 100, "w"), ("per", "% per unit", 100, "e")]
        f, self.w_tree = self._tree(p, cols, 5)
        f.pack(fill="both", expand=False, padx=14, pady=(0, 12))
        self.w_tree.bind("<Double-1>", self._edit_other)

    TARGET_LABEL = {"dps": "Damage", "surv": "Survival", "farm": "Income"}

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
                                 gem_tier=int(self.cfg.get("gem_tier", 3)),
                                 ability_shares=self._ability_shares())

    def _ability_shares(self) -> dict:
        """Ability -> share of damage, as set on the Talents page (or taken from the skill bar there)."""
        out = {}
        for name, share in (self.cfg.get("ability_setup") or {}).values():
            if name and name not in ("–", "-") and share:
                out[name] = out.get(name, 0) + float(share)
        return out

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
        self._fill_bis()
        ctx = self._eval_context()
        deaths = self._death_history()
        dw = ", ".join(f"{e} {w * 100:.0f} %" for e, w in sorted(ctx.damage_weights.items(), key=lambda kv: -kv[1])
                       if w >= 0.1) if ctx.damage_weights else "all equal (fewer than 3 deaths, no enemy data)"
        self.lbl_ctx.configure(text=(
            f"Main attribute: {ctx.main} ({ctx.hero or 'class unknown'}) · Element: {ctx.elem} · "
            f"Enemy level: {ctx.enemy_level or str(ctx.level or '?') + ' (hero level)'} · "
            f"Damage types ({'from ' + str(len(deaths)) + ' deaths' if len(deaths) >= 3 else 'enemies of stage ' + (self.state.stage_name or '?')}): "
            f"{dw} · DoT share {ctx.dot_share * 100:.0f} %\n"
            ""))
        self.leg_tree.delete(*self.leg_tree.get_children())
        ov = self.cfg.get("legendary_values", {})
        for L in item_eval.LEGENDARIES:
            _, txt, known, _ = item_eval._legendary_deltas(L, L["effect"], 1, ctx, {})
            txt = txt.replace(" – set your own value in the Weights tab", " – set your own value →")
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
        self.lbl_char.configure(text=f"{n} values · as of {self.char_stats_time}" if n else "Not read yet")

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
                if ev[0] == "update":
                    self._update_result(*ev[1:])
                elif ev[0] == "icon_ready":
                    if not getattr(self, "_icon_refill", False):  # several pictures arrive at once
                        self._icon_refill = True

                        def refill():
                            self._icon_refill = False
                            self._fill_bis()
                            self._tal_draw()
                        self.root.after(300, refill)
                elif ev[0] == "hotkey":
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
                    self.lbl_verdict.configure(text=f"Error: {ev[2]}", fg=C_BAD) if ev[1] == "item" else \
                        self.lbl_char.configure(text=f"Error: {ev[2]}")
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def _merge_attributes(self, stats: dict):
        if not stats:
            self.lbl_char.configure(text="No attributes found – is the character window open?")
            self.nb.select(self.tab_char)
            return
        self.char_stats.update(stats)
        self.char_stats_time = datetime.now().strftime("%d.%m. %H:%M")
        self._save_char()
        self._fill_char()
        self._fill_gems()
        self._fill_eval_tab()
        self.lbl_char.configure(text=f"+{len(stats)} read · {len(self.char_stats)} values · as of {self.char_stats_time}")
        self.nb.select(self.tab_char)

    def _remember_equipped(self, res):
        """Store the equipped item of a slot whenever a tooltip shows it (comparison or equipped single)."""
        its = [getattr(res, "old_item", None)]
        if res.mode == "single" and getattr(res, "new_item", None) is not None and res.new_item.equipped:
            its.append(res.new_item)
        eq = self.cfg.setdefault("equipped", {})
        for it in its:
            if it is None or not it.type_line:
                continue
            slot = item_quality.item_kind(it.type_line)[1]
            if not slot:
                continue
            stats = {}
            for st in it.base + it.primary + it.secondary + it.sockets:
                v, p_ = stats.get(st.name, (0.0, st.pct))
                stats[st.name] = [v + st.value, p_ or st.pct]
            eq[slot] = {"name": it.name, "rarity": item_quality.item_kind(it.type_line)[0], "stats": stats,
                        "effects": list(it.effects), "item_level": it.item_level,
                        "time": datetime.now().strftime("%d.%m. %H:%M")}
        save_config(self.cfg)

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
            self._remember_equipped(res)
            self._fill_bis()
        self.nb.select(self.tab_items)
        self.item_tree.delete(*self.item_tree.get_children())
        if res.mode == "none":
            self._fill_card(self.card_new, "No item tooltip found", None, {},
                            "Hover the item until its tooltip is open, then press the hotkey.")
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
        color = {"EQUIP": C_GOOD, "EQUIP LATER": C_GOOD, "DO NOT EQUIP": C_BAD}.get(ev.verdict, C_MEH)
        if res.mode == "single":
            verdict, color = "NO COMPARISON", C_MEH
        elif not self.char_stats:
            verdict, color = "READ CHARACTER (F9)", C_MEH
        else:
            verdict = ev.verdict
        sure = "certain" if ev.confident else "uncertain"
        nice = {"EQUIP": "Equip", "EQUIP LATER": "Equip later", "DO NOT EQUIP": "Do not equip",
                "SIDEGRADE": "Equal", "NO COMPARISON": "No comparison",
                "READ CHARACTER (F9)": "Read character (F9)"}.get(verdict, verdict)
        self.lbl_verdict.configure(text=nice, fg=color)
        self.lbl_sure.configure(text=sure, fg=MUTED if ev.confident else C_MEH)
        if verdict in ("EQUIP", "EQUIP LATER", "DO NOT EQUIP", "SIDEGRADE"):
            what = {"Damage": "mostly damage", "Survival": "mostly survival",
                    "Balanced": "damage and survival equally", "Farming": "mostly gold, items and EXP"}[mode]
            reason = f"In {mode} mode ({what} counts) your character changes by {ev.score:+.1f} % overall."
            if verdict == "SIDEGRADE":
                reason += " That is below ±1 % – no noticeable difference, keep whichever you prefer."
            elif verdict == "EQUIP LATER":
                reason += f" You can wear it from level {res.req_level}."
        elif verdict == "NO COMPARISON":
            reason = "No comparison tooltip: either the slot is empty or the game shows only this item."
        else:
            reason = "Without a character sheet the effect cannot be calculated."
        self.lbl_reason.configure(text=reason)

        q_new = item_quality.roll_report(new_stats, rarity, res.item_level) if rarity else {"stats": {}, "avg": None}
        q_old = item_quality.roll_report(old_stats, o_rar, res.old_item_level) if o_rar and res.old_item_level else None
        self._fill_card(self.card_new, "New", res.new_item, q_new["stats"], gems=ev.gems.get("new"))
        if res.mode == "compare":
            self.card_old.grid()
            self._fill_card(self.card_old, "Equipped", res.old_item, q_old["stats"] if q_old else {},
                            gems=ev.gems.get("old"))
        else:
            self.card_old.grid_remove()

        def metric(key, val, sub):
            lab, sl = self.lbl_m[key]
            lab.configure(text=f"{val:+.1f} %" if val is not None else "-",
                          fg=FG if val is None or abs(val) < 0.05 else (C_GOOD if val > 0 else C_BAD))
            sl.configure(text=sub)

        has_char = bool(self.char_stats)
        metric("dps", ev.dps_pct if has_char else None, f"{ev.main}, {ev.element}" if has_char else "F9 needed")
        metric("surv", ev.surv_pct if has_char else None,
               f"Toughness {fmt(ev.tough_old)} → {fmt(ev.tough_new)}" if has_char else "F9 needed")
        metric("farm", ev.farm_pct if has_char else None,
               (", ".join(f"{k} {v:+.1f} %" for k, v in ev.farm.items() if abs(v) >= 0.05) or "no change")
               if has_char else "F9 needed")

        warn = list(dict.fromkeys(ev.reasons)) + ["Misread? " + w for w in getattr(res, "warnings", [])]
        self.lbl_item.configure(text="\n".join("⚠ " + w for w in warn))
        q_new = item_quality.roll_report(new_stats, rarity, res.item_level) if rarity else {"stats": {}, "avg": None}
        q_old = item_quality.roll_report(old_stats, o_rar, res.old_item_level) if o_rar and res.old_item_level else None
        qparts = []
        if q_new["avg"] is not None:
            qparts.append(f"Roll quality new: avg {q_new['avg']:.0f} %" + (" (Ancient)" if ancient else "")
                          + (f" · replaced item: avg {q_old['avg']:.0f} % (iLvl {res.old_item_level})" if q_old and q_old["avg"] is not None else ""))
            if q_new["upgraded_or_gem"]:
                qparts.append("above maximum (already upgraded or a gem?): " + ", ".join(q_new["upgraded_or_gem"]))
        if rarity and res.item_level:
            tgt = max(level, 5)
            c = item_quality.upgrade_cost(tgt, res.item_level, rarity, slot, ancient)
            ores = ", ".join(f"{fmt(v)} {k}" for k, v in c["ores"].items())
            qparts.append(f"Upgrade to +{tgt}: ≈ {fmt(c['gold'])} gold, {ores}, avg {c['attempts']:.1f} attempts"
                          + (f", {c['boss'][1]}× {c['boss'][0]}" if c["boss"] else "")
                          + (f" · sockets: {item_quality.sockets(slot)}" if slot and item_quality.sockets(slot) else ""))
        self.lbl_quality.configure(text="\n".join(qparts))
        fx = []
        for sign, item, effect, txt, known in ev.effects:
            head = "New" if sign > 0 else "Lost"
            fx.append(f"{head}: {item} – {effect}\n      Rating: {txt}")
        if not fx:
            fx = ["No effects that change." if res.mode == "compare" else "No effects."]
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
        return " + ".join(f"{v:g}×{n}" for n, v in (("Damage", w["dps"]), ("Survival", w["surv"]), ("Income", w["farm"])))

    def clear_item_history(self):
        self.item_history.clear()
        self._hist_icons = []
        self.hist_tree.delete(*self.hist_tree.get_children())

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
    # -- updates ---------------------------------------------------------------
    def check_updates(self, manual=False):
        if not manual and self.cfg.get("update_check") is False:
            return
        if manual:
            self.lbl_status.configure(text="checking for updates…")
        threading.Thread(target=lambda: self.events.put(("update", updater.latest(), manual)), daemon=True).start()

    def _update_result(self, info, manual):
        if info is None:
            if manual:
                self.lbl_status.configure(text="update check failed – no connection to GitHub")
            return
        if not updater.is_newer(info):
            if manual:
                self.lbl_status.configure(text=f"Deskrawl Tracker {VERSION} is the latest version")
            return
        if not manual and self.cfg.get("skip_version") == info["version"]:
            return
        self.lbl_status.configure(text="")
        self._update_dialog(info)

    def _update_dialog(self, info):
        d = tk.Toplevel(self.root, bg=BG)
        d.title("Update available")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Label(d, text=f"Version {info['version']} is available", bg=BG, fg=FG, font=ui.F_HEAD, anchor="w").pack(
            fill="x", padx=18, pady=(16, 0))
        tk.Label(d, text=f"You have {VERSION}. Settings, character data and histories are kept.", bg=BG, fg=MUTED,
                 font=ui.F_SMALL, anchor="w").pack(fill="x", padx=18)
        notes = tk.Text(d, height=10, width=64, bg=PANEL, fg=FG, relief="flat", wrap="word", font=ui.F_SMALL,
                        padx=10, pady=8)
        notes.insert("1.0", info.get("notes") or "No release notes.")
        notes.configure(state="disabled")
        notes.pack(fill="both", padx=18, pady=10)
        lbl = tk.Label(d, text="", bg=BG, fg=C_MEH, font=ui.F_SMALL, anchor="w")
        lbl.pack(fill="x", padx=18)
        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=18, pady=(6, 16))

        def progress(done, total):
            txt = f"downloading… {done / 1e6:.0f} MB" + (f" of {total / 1e6:.0f} MB" if total else "")
            self.root.after(0, lambda: lbl.configure(text=txt))

        def run():
            try:
                r = updater.install(info, progress)
            except Exception as e:
                r = f"Update failed: {e}"
            self.root.after(0, lambda: done(r))

        def done(r):
            if r == "restart":
                lbl.configure(text="Installing – the tracker restarts in a few seconds…")
                self.root.after(800, self.close)
            else:
                lbl.configure(text=r, fg=C_BAD)
                b_now.configure(text="Update now")

        def now():
            if b_now.cget("text") != "Update now":
                return
            b_now.configure(text="Updating…")
            threading.Thread(target=run, daemon=True).start()

        def skip():
            self._set_cfg("skip_version", info["version"])
            d.destroy()

        b_now = ui.button(bar, "Update now", now, accent=True)
        b_now.pack(side="right")
        ui.button(bar, "Later", d.destroy).pack(side="right", padx=6)
        ui.button(bar, "Skip this version", skip).pack(side="left")
        d.update_idletasks()
        d.geometry(f"+{self.root.winfo_rootx() + 40}+{self.root.winfo_rooty() + 80}")
        d.focus_force()

    def toggle_skills(self, on=None):
        on = (not self.skills.enabled.is_set()) if on is None else on
        if on:
            self.skills.enabled.set()
        else:
            self.skills.enabled.clear()
        self.cfg["skills_on"] = on
        save_config(self.cfg)
        self.btn_skills.configure(text="Skill tracking: on" if on else "Skill tracking: off", fg=C_RUN if on else FG)

    def toggle_ocr(self):
        if self.ocr.enabled.is_set():
            self.ocr.enabled.clear()
        else:
            self.ocr.enabled.set()
        on = self.ocr.enabled.is_set()
        self.btn_ocr.configure(text="DPS meter: on" if on else "DPS meter: off", fg=C_RUN if on else FG)
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
        self.btn_panels.configure(text=f"Log panel: {self.PANEL_MODES[mode]}", fg=FG)

    def toggle_hide(self):
        self.set_game_hidden(not self.cfg.get("game_hidden", False))
        self.boss_grace_until = time.time() + 2.5

    def set_game_hidden(self, hidden: bool):
        hwnd = self.capture.find()
        if not hwnd:
            self.lbl_status.configure(text="Deskrawl not found")
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
        self.btn_hide.configure(text=f"{'Show' if hidden else 'Hide'} Deskrawl ({key})",
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
        self.cfg["topmost"] = not self.cfg.get("topmost", False)
        self.root.attributes("-topmost", self.cfg["topmost"])
        self._update_top_btn()

    def _update_top_btn(self):
        self.btn_top.configure(text="Always on top: " + ("on" if self.cfg.get("topmost", False) else "off"))

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
        if not self.cfg.get("topmost", False):
            return
        try:
            u = ctypes.windll.user32
            hwnd = self.hwnd
            if u.IsIconic(hwnd):
                u.ShowWindow(hwnd, 4)  # SW_SHOWNOACTIVATE
            # an open drop-down list (Combobox) is its own window; pushing ours on top would close it
            if self.root.grab_current() is not None:
                return
            if u.GetWindowLongW(hwnd, -20) & 0x8:  # WS_EX_TOPMOST still set: nothing to do
                return
            # HWND_TOPMOST, SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER
            u.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0200)
        except Exception:
            pass

    def _gold_audit_text(self):
        so = self.state.sold_stats()
        if so["n"]:
            src = "sale pop-ups" if time.time() - self.state.last_toast < 600 else "game log"
            return f"Sales ({src}): {so['n']} items for {fmt(so['gold'])} gold"
        ga = self.state.gold_audit
        if ga.balance is None:
            return "Sales gold: open the inventory in the game once – the tracker reads the balance."
        ago = fmt_dur(time.time() - ga.balance_t)
        if ga.rate_h is None:
            return (f"Balance {fmt(ga.balance)} ({ago} ago) · sales gold: open the inventory again "
                    f"after a few runs")
        skipped = f" · {ga.skipped}× skipped (gold spent)" if ga.skipped else ""
        return (f"Balance {fmt(ga.balance)} ({ago} ago) · sales gold {fmt(ga.extra)} measured in "
                f"{fmt_dur(ga.measured_s)}{skipped}")

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
            self.lbl_lvl_sub.configure(text="Waiting for the login line in the log – restart the game once.")
            return
        self.lbl_lvl_title.configure(text=f"to level {e['level'] + 1}")
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
                text=f"{approx}{e['runs_left']:.0f} runs on {e['stage']}\navg {fmt(e['xp_run'])} EXP per run")
        else:
            self.lbl_lvl_runs.configure(text="no run measured on this stage yet")
        self.lbl_lvl_sub.configure(
            text=f"{approx}{fmt(e['xp'])} of {fmt(e['need'])} EXP ({frac * 100:.1f} %), {approx}{fmt(e['left'])} to go"
                 f"   ·   by EXP per hour: {fmt_dur(eta_rate) if eta_rate else '-'}")

    def tick(self):
        try:
            self._check_profile()
            self._keep_visible()
            self._sync_game_window()
            self._refresh()
        finally:
            self.root.after(500, self.tick)

    # -- characters ----------------------------------------------------------
    # Settings that belong to one character live at the top level of the config while that
    # character is played and in cfg["profiles"][name] otherwise.
    PROFILE_KEYS = ("char_stats", "char_stats_time", "weapon", "other_stats", "legendary_values", "element",
                    "item_mode", "weights", "equipped", "bis_mode", "talents_mine", "talents_plan",
                    "talent_mode", "ability_setup", "talent_builds")

    def _check_profile(self):
        if self.tailer.first:
            return  # the log is still being read; the last login decides
        name = self.state.char_name
        if not name or name == "-":
            return
        cur = self.cfg.get("profile")
        if cur is None:
            cur = self._adopt_old_settings()
            if cur is None:
                return
        if name != cur:
            self._switch_profile(cur, name)

    def _adopt_old_settings(self):
        """Config from before profiles: give its character sheet to the character it fits."""
        chars = self.state.characters
        if not chars:
            return None
        stats = self.cfg.get("char_stats") or {}
        owner = self.state.char_name
        if stats:
            lvl = (stats.get("Level") or [0])[0]
            main = max(item_eval.MAIN_STATS, key=lambda k: (stats.get(k) or [0])[0])
            fits = [n for n, (cls, l) in chars.items() if item_eval.CLASS_MAIN.get(cls) == main]
            if fits:
                owner = min(fits, key=lambda n: abs(chars[n][1] - lvl))
        self.cfg["profile"] = owner
        self.stage_stats.adopt(owner)
        self.stage_stats.save()
        save_config(self.cfg)
        return owner

    def _switch_profile(self, old, new):
        profiles = self.cfg.setdefault("profiles", {})
        profiles[old] = {k: self.cfg[k] for k in self.PROFILE_KEYS if k in self.cfg}
        for k in self.PROFILE_KEYS:
            self.cfg.pop(k, None)
        self.cfg.update(profiles.pop(new, {}))
        self.cfg["profile"] = new
        save_config(self.cfg)
        self.char_stats = {k: tuple(v) for k, v in self.cfg.get("char_stats", {}).items()}
        self.char_stats_time = self.cfg.get("char_stats_time", "")
        self.var_mode.set(self.cfg.get("item_mode", "Balanced"))
        self.var_bis_mode.set(self.cfg.get("bis_mode") or self.cfg.get("item_mode", "Balanced"))
        self.var_elem.set(self.cfg.get("element", "Auto"))
        self._last_item = None
        self.item_history.clear()
        self.hist_tree.delete(*self.hist_tree.get_children())
        self.item_tree.delete(*self.item_tree.get_children())
        for card in (self.card_new, self.card_old):
            for w in card.winfo_children():
                w.destroy()
        self.state.reset()  # rates of one character must not mix with another's
        self._stage_sig = None
        self._fill_char()
        self._fill_gems()
        self._fill_eval_tab()
        self._tal_load()
        cls = self.state.characters.get(new, ("?", 0))[0]
        self.lbl_char.configure(text=(f"Character changed: {new} ({cls}). " +
                                      ("Values loaded." if self.char_stats else "No values yet – press F9.")))

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
        self.dots["game"].set("ok" if cap == "game ok" else ("bad" if "not found" in cap else "warn"),
                              cap + (" (hidden)" if hidden else "") +
                              ("\nThe tracker cannot read a minimized game – hide it with F10 instead of minimizing."
                               if "minimized" in cap else ""))
        ocr_on = self.ocr.enabled.is_set()
        self.dots["dps"].set("ok" if ocr_on and "fps" in self.ocr.status else ("warn" if ocr_on else None),
                             self.ocr.status)
        ps = self.panels.status or "not read yet"
        self.dots["panels"].set("ok" if " read" in ps or "not needed" in ps else ("warn" if ps != "not read yet" else None), ps)
        self.dots["keys"].set("bad" if self.hotkeys.failed else "ok",
                              ("In use by another program: " + ", ".join(self.hotkeys.failed)) if self.hotkeys.failed
                              else "F8 check item · F9 read character · F10 hide/show Deskrawl")
        if self.busy:
            self.lbl_status.configure(text="reading item…" if self.job_label == "item" else "reading attributes…")
        if cur:
            self.lbl_run.configure(
                text=f"▶  Run in progress {fmt_dur(cur.duration)} – {cur.difficulty}, {cur.waves} waves, "
                     f"damage {fmt(cur.damage)}      Bonus: MF {cur.mf:g}, GF {cur.gf:g}, XP {cur.xpm:g}")
        else:
            self.lbl_run.configure(text="Waiting for the next run")

        def tile(key, main, sub):
            self.tiles[key][0].configure(text=main)
            self.tiles[key][1].configure(text=sub)

        tile("xp_h", fmt(s["xp_h"]), f"last 15 min {fmt(sr['xp_h'])}\navg {fmt(s['avg_xp_run'])} per run")
        ga = st.gold_audit
        so = st.sold_stats()
        if so["n"]:
            sold_h = so["gold"] / s["span"] * 3600
            tile("gold_h", fmt(s["gold_h"] + sold_h), f"runs {fmt(s['gold_h'])}\nsales {fmt(sold_h)}")
        elif ga.rate_h is not None:
            tile("gold_h", fmt(s["gold_h"] + ga.rate_h), f"runs {fmt(s['gold_h'])} + sales ≈ {fmt(ga.rate_h)}")
        else:
            tile("gold_h", fmt(s["gold_h"]), f"run gold only\navg {fmt(s['avg_gold_run'])} per run")
        tile("runs_h", f"{s['runs_h']:.1f}", f"last 15 min {sr['runs_h']:.1f}\navg {fmt_dur(s['avg_run'])} per run")
        tile("items_h", f"{s['items_h']:.1f}", f"last 15 min {sr['items_h']:.1f}\n{s['items']} in total")
        if not self.ocr.enabled.is_set():
            tile("run_dps", "-", "DPS meter is off")
        elif s["avg_dps"]:
            peak = max((r.peak_dps for r in runs if r.start), default=0)
            tile("run_dps", fmt(s["avg_dps"]), f"last 15 min {fmt(sr['avg_dps'])}\npeak {fmt(peak)}")
        else:
            tile("run_dps", "-", f"running · {fmt(cur.damage)} damage so far" if cur and cur.damage else "no run measured yet")
        dst = st.death_stats()
        tile("deaths", f"{dst['per_h']:.1f}", f"{dst['n']} {'death' if dst['n'] == 1 else 'deaths'}\n{dst['rate'] * 100:.0f} % of runs")

        self._refresh_level(s, sr)
        self._refresh_deaths()
        self._refresh_drops(s["span"])
        self._aggregate_stages()

        b = st.backlog
        self.lbl_totals.configure(
            text=f"Session {fmt_dur(s['span'])}: {s['runs']} runs, {fmt(s['xp'])} EXP, {fmt(s['gold'])} gold, "
                 f"{s['items']} items.   {self._gold_audit_text()}\n"
                 f"In the log before the tracker started (no timestamps): {b['runs']} runs, {fmt(b['xp'])} EXP, {fmt(b['gold'])} gold")

        sig = (len(runs), sum(bool(r.stage_name) for r in runs), sum(bool(r.stage_guess) for r in runs))
        if getattr(self, "_runs_sig", None) != sig:
            self._runs_sig = sig
            self.tree.delete(*self.tree.get_children())
            for r in reversed(runs[-50:]):
                self.tree.insert("", "end", values=(
                    datetime.fromtimestamp(r.end).strftime("%H:%M:%S"),
                    r.stage_name or (f"≈ {r.stage_guess}" if r.stage_guess else "-"), r.difficulty,
                    fmt_dur(r.duration), fmt(r.xp), fmt(r.gold), r.items,
                    fmt(r.avg_dps) if r.damage else "-", fmt(r.peak_dps) if r.peak_dps else "-"))


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

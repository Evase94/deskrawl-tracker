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
from tkinter import messagebox, ttk

import paths
import errlog
import storage
import setup_dialog
import bis
import talents
import skills
import updater
import changelog
import minions
import build_profile
import combat_sim
import paragon
import loot_watch
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
    cfg = storage.read_json(CONFIG_PATH, {})
    if not isinstance(cfg, dict):
        return {}
    for d in [cfg] + list((cfg.get("profiles") or {}).values()):
        if d.get("item_mode") in OLD_MODES:
            d["item_mode"] = OLD_MODES[d["item_mode"]]
    return cfg


def save_config(cfg: dict) -> None:
    storage.write_json(CONFIG_PATH, cfg, indent=2)


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
    game_seconds: int | None = None  # run time from the stage end screen (or the log panel)
    end_screen: dict = field(default_factory=dict)  # what the stage end screen showed for this run
    death_info: dict = field(default_factory=dict)
    ground_legendaries: int = 0  # orange item names seen on the ground during this run (LootWatcher)

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
        self.ui_busy_until = 0.0   # the tracker window is being moved/resized: background readers pause
        self.level = 0
        self.map = "-"
        self.window_rect = None  # (x, y, w, h) of the game window from the log
        self.backlog = {"runs": 0, "xp": 0, "gold": 0, "items": 0}
        self.xpm = XpModel(load_config().get("xp_learned"))
        self.paragon = paragon.Paragon(load_config().get("paragon"))  # account-wide: sum of level-70 run XP
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
        self.legendary_q: queue.Queue = queue.Queue()  # (source, text) -> sound + status line in the UI
        self.last_rarity: dict = {}  # stage -> {"Legendary": total, ...} from the last end screen
        self.ground_pending: list = []  # (t, name, run) orange names waiting for GROUND_WAIT_S

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
                    self._paragon_commit(m["id"], xp, lvl)
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
                    ps = getattr(self, "pending_stage", None)
                    if ps and now - ps[0] < 30 and not r.stage_name:
                        r.stage_name = ps[1]
                        if ps[2].get("rarities"):
                            self._end_legendaries(r, ps[1], ps[2]["rarities"])
                        self.stage_key = (r.difficulty, r.waves)
                        self.pending_stage = None
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

    def ui_busy(self) -> bool:
        return time.time() < self.ui_busy_until

    def apply_stage_end(self, info: dict, t: float):
        """Stage end screen: name the run that just ended (or remember it until the commit arrives)."""
        name = f"{info['stage']}: {info['n']}"
        with self.lock:
            r = next((o for o in reversed(self.runs) if o.end and t - o.end < 120), None)
            self.stage_name = name
            if r is None:
                self.pending_stage = (t, name, info)
                return
            if not r.stage_name:
                r.stage_name = name
            if info.get("seconds"):
                r.game_seconds = info["seconds"]
            self.stage_key = (r.difficulty, r.waves)
            first_clears = "clears" in info and "clears" not in r.end_screen
            if info.get("rarities") and "legendaries" not in r.end_screen:
                self._end_legendaries(r, name, info["rarities"])
            r.end_screen.update({k: v for k, v in info.items() if v is not None})
            if first_clears:
                # CLEARS counts cleared runs of this stage: unchanged since the last run = not cleared (died)
                last = getattr(self, "last_clears", {})
                prev = last.get(name)
                last[name] = info["clears"]
                self.last_clears = last
                if prev is not None and info["clears"] == prev and r.death != "confirmed":
                    r.death_info.setdefault("screen", "stage not cleared (end screen)")
                    r.death = "confirmed"
                    r.death_info["level"] = r.level
                    append_death_csv(r, self.char_name)

    def wants_rarities(self, t: float) -> bool:
        """Is the run that just ended still without its Legendary count from the end screen?"""
        with self.lock:
            r = next((o for o in reversed(self.runs) if o.end and t - o.end < 120), None)
            return r is None or "legendaries" not in r.end_screen

    def _end_legendaries(self, r, name: str, rar: dict):
        """Legendary + Divine drops of run r from the end screen: "(+N)" when read, otherwise the change of
        the total since the last run of this stage. More than the ground labels showed: sound for the rest."""
        last = self.last_rarity.setdefault(name, {})
        n = 0
        for k in ("Legendary", "Divine"):
            if k not in rar:
                continue
            total, plus = rar[k]
            if plus is None:
                prev = last.get(k)
                plus = total - prev if prev is not None and 0 < total - prev <= 5 else 0
            last[k] = total
            n += plus
        r.end_screen["legendaries"] = n
        for _ in range(max(n - r.ground_legendaries, 0)):
            self.legendary_q.put(("end screen", ""))

    def in_run(self, t: float) -> bool:
        """A run is going on (or ended a few seconds ago: the boss loot lands right at the end)."""
        with self.lock:
            return self.current is not None or bool(self.runs and self.runs[-1].end and t - self.runs[-1].end < 8)

    GROUND_WAIT_S = 4.0  # an item the carriage unloads shows its name too, followed by an "Obtained" pop-up

    def ground_legendary(self, text: str, t: float):
        """LootWatcher saw a new orange item name on the ground. It counts once no pop-up with that name
        followed within GROUND_WAIT_S (or came just before): then it was a drop, not the carriage unloading."""
        with self.lock:
            if self._popup_named(text, t - 6):
                return
            r = self.current or next((o for o in reversed(self.runs) if o.end and t - o.end < 20), None)
            self.ground_pending.append((t, text, r))

    def _popup_named(self, text: str, since: float) -> bool:
        import difflib
        key = re.sub(r"[^a-z]", "", text.lower())
        names = [i for tt, i, *_ in self.drops + self.sold if tt >= since]
        return any(difflib.SequenceMatcher(None, key, re.sub(r"[^a-z]", "", n.lower())).ratio() > 0.75
                   for n in names)

    def flush_ground(self, now: float):
        with self.lock:
            keep = []
            for t, text, r in self.ground_pending:
                if self._popup_named(text, t - 6):
                    continue
                if now - t < self.GROUND_WAIT_S:
                    keep.append((t, text, r))
                    continue
                if r is not None:
                    r.ground_legendaries += 1
                self.legendary_q.put(("ground", text))
            self.ground_pending = keep

    def log_needed(self, r) -> bool:
        """Does the in-game log panel have to be opened after run r? Only it names the stage and the
        killer: needed after a death, while no stage is known, or when the run's settings
        (difficulty, waves) differ from the last stage the log named - then the stage changed."""
        with self.lock:
            return not self.stage_name or self.stage_key != (r.difficulty, r.waves)

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
    def _paragon_commit(self, run_id: str, xp: int, lvl: int):
        """Past level 70 all run XP goes to Paragon (self.level = level before this run)."""
        if lvl < 70:
            return
        if self.level >= 70:
            self.paragon.add(run_id, xp)
        elif self.level:
            self.paragon.reached_70()  # the run that reached 70: Paragon XP counts from here

    def scan_paragon(self, path: str):
        """Count the level-70 runs of an older log (Game-prev.log) once; no other effect."""
        level = 0
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    m = RE_COMMIT.search(line)
                    if m:
                        with self.lock:
                            saved, self.level = self.level, level
                            self._paragon_commit(m["id"], int(m["xp"]), int(m["level"]))
                            self.level = saved
                        level = int(m["level"])
                        continue
                    m = RE_LOGIN.search(line)
                    if m:
                        level = int(m["lvl"])
        except FileNotFoundError:
            pass

    def observe_paragon(self, t: float, hud: dict):
        with self.lock:
            self.paragon.observe_hud(hud["level"])

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
            if self.level >= 70 or xm.level >= 70:
                pg = self.paragon
                if not pg.known():
                    return {"paragon": True, "unknown": True, "level": pg.level}
                level, xp = pg.level, pg.xp
                need = paragon.xp_to_next(level)
                out = {"paragon": True, "level": level, "xp": xp, "need": need, "left": max(need - xp, 0),
                       "exact": pg.exact(), "complete": pg.complete, "total": pg.total}
            else:
                if xm.xp is None or not xm.level:
                    return None
                need = xm.required(xm.level)
                out = {"level": xm.level, "xp": xm.xp, "need": need, "left": max(need - xm.xp, 0),
                       "exact": xm.exact}
            left = out["left"]
            known = [r for r in self.runs if r.difficulty != "?"]  # runs seen from their start
            ref = self.current or (known[-1] if known else None)
            out.update({"stage": None, "runs_left": None, "eta_stage": None})
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
        errlog.report("deaths_csv", f"cannot write {DEATHS_CSV}")


def drop_category(e: dict) -> str:
    """Bucket for the drop statistics: equipment by rarity, gems by tier, other loot by kind."""
    info = item_ocr.classify_drop(e.get("item", ""))
    if info["kind"] == "gem":
        return f"Gem Tier {info['tier'] or '?'}"
    if info["kind"] == "rune":
        return {"set": "Set Rune", "ability": "Ability Rune", "attribute": "Attribute Rune"}.get(info.get("rune_type"), "Rune")
    return {"key": "Treasure Key", "shard": "Soul Shard", "boss_material": "Boss Material", "skull": "Skull",
            "ore": "Ore", "plant": "Plant"}.get(info["kind"], e.get("rarity", "?"))


LEGENDARY = ("Legendary", "Divine")


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
        errlog.report("runs_csv", f"cannot write {RUNS_CSV}")


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
        # Paragon XP is the sum of all level-70 runs: count the previous session's log too
        self.state.scan_paragon(os.path.join(os.path.dirname(self.path), "Game-prev.log"))
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
                errlog.report("log_tailer", "reading Game.log failed")
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
            errlog.log.error("OCR not available", exc_info=True)
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
            if t < self.suspend_until or self.state.ui_busy():
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
                errlog.report("dps_ocr", "DPS OCR failed")
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
            if self.state.ui_busy():
                continue
            try:
                frame = self.capture.grab()
                if frame is None:
                    continue
                lines = item_ocr.ocr_windows(frame)
                gold = item_ocr.find_gold(lines)
                if gold is not None:
                    self.state.observe_gold(time.time(), gold)
                end = item_ocr.find_stage_end(lines)
                if end:
                    if self.state.wants_rarities(time.time()):
                        end["rarities"] = item_ocr.read_end_rarities(frame, lines)
                    self.state.apply_stage_end(end, time.time())
                death = item_ocr.find_death_text(lines, (frame.shape[1], frame.shape[0]))
                if death:
                    self.state.mark_death_seen(time.time(), death)
                if self.state.level >= 70:  # Paragon level + XP bar at the bottom left
                    hud = item_ocr.read_paragon(frame)
                    if hud:
                        self.state.observe_paragon(time.time(), hud)
                hdr = item_ocr.find_log_header(lines)
                if hdr is not None:
                    self.state.ingest_log(item_ocr.read_log_panel(frame, hdr), time.time())
            except Exception:
                errlog.report("gold_watcher", "gold / stage end / death reader failed")


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
            try:
                if self.watch_stage_end():
                    continue  # the end screen named the stage: no log panel needed
                with self.lock:
                    self.read_once()
            except Exception as e:
                self.status = f"Panel error: {e}"
                errlog.report("panel_reader", "log panel / stage end reader failed")

    def watch_stage_end(self, seconds=6.0) -> bool:
        """Right after a run: the stage end screen shows for a few seconds and names the stage."""
        until = time.time() + seconds
        while time.time() < until:
            frame = self.capture.grab()
            if frame is not None:
                lines = item_ocr.ocr_windows(frame)
                end = item_ocr.find_stage_end(lines)
                if end:
                    if self.state.wants_rarities(time.time()):
                        end["rarities"] = item_ocr.read_end_rarities(frame, lines)
                    self.state.apply_stage_end(end, time.time())
                    self.status = f"Stage from the end screen: {end['stage']}: {end['n']} " \
                                  f"({datetime.now().strftime('%H:%M:%S')}) – log panel not needed"
                    return True
            time.sleep(0.4)
        return False

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
            if self.state.ui_busy():
                continue
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
                errlog.report("toast_watcher", "drop / sale toast reader failed")


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
        self.loot = loot_watch.LootWatcher(self.state, self.capture,
                                           lambda text: self.state.ground_legendary(text, time.time()))
        self.loot.enabled = bool(self.cfg.get("legendary_sound", True) or self.cfg.get("legendary_space", True))
        self.loot.start()
        self._leg_session = 0
        self._last_beep = 0.0
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
        root.bind("<Configure>", self._root_configure, add="+")
        try:  # 1 ms timer resolution: Tk's after() callbacks run on time instead of every ~16 ms
            ctypes.windll.winmm.timeBeginPeriod(1)
        except Exception:
            pass
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
        self.tab_db = tk.Frame(self.nb, bg=BG)
        self.tab_mn = tk.Frame(self.nb, bg=BG)
        self.tab_sk = tk.Frame(self.nb, bg=BG)
        self.nb.add(self.tab_farm, text="Overview")
        self.nb.add(self.tab_char, text="Character Stats")
        self.nb.add(self.tab_stages, text="Stages")
        self.nb.add(self.tab_drops, text="Drops")
        self.nb.add(self.tab_death, text="Deaths")
        self.nb.add(self.tab_items, text="Item Comparer")
        self.nb.add(self.tab_db, text="Item Database")
        self.nb.add(self.tab_mn, text="Minions")
        self.nb.add(self.tab_sk, text="Skill Tracking")
        # BiS Gear, Talents and Weights are hidden for now: still built (their settings keep feeding the
        # item rating), just not in the menu. Add them to the menu again to show them.
        self.nb.add(self.tab_gems, text="Gems")
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
        self._build_itemdb(self.tab_db)
        self._build_minions(self.tab_mn)
        self._build_skills(self.tab_sk)

        side = self.nb.bottom
        head = tk.Label(side, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w", cursor="hand2")
        head.pack(fill="x", padx=14, pady=(0, 4))
        body = tk.Frame(side, bg=PANEL)

        def show_controls(open_):
            head.configure(text=("▾  Controls" if open_ else "▸  Controls"))
            if open_:
                body.pack(fill="x")
            else:
                body.pack_forget()

        def toggle_controls(_e=None):
            open_ = not self.cfg.get("controls_open", False)
            self._set_cfg("controls_open", open_)
            show_controls(open_)
        head.bind("<Button-1>", toggle_controls)
        head.bind("<Enter>", lambda e: head.configure(fg=FG))
        head.bind("<Leave>", lambda e: head.configure(fg=MUTED))
        ui.Tooltip(head, "Show or hide the controls.")

        def ctl(text, cmd, tip):
            b = ui.button(body, text, cmd, small=True)
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
                                                       "page.")
        self.btn_top = ctl("", self.toggle_topmost, "Keep the tracker window on top of other windows.")
        ctl("Change log file", self.open_setup, "Choose the path to Deskrawl's Game.log and check that "
                                                 "Windows' English text recognition is installed.")
        ctl("Check for updates", lambda: self.check_updates(manual=True),
            f"Version {VERSION}. Looks for a newer release on GitHub and installs it; your settings and "
            f"histories stay. The tracker also checks once at every start.")
        ctl("What's new", self.show_changelog, "All versions and their changes, newest first.")
        ctl("Reset session", self.reset, "Clear the rates and run list of the current session. "
                                                "Stage statistics and histories are kept.")
        show_controls(self.cfg.get("controls_open", False))
        last = self.cfg.get("last_version")
        if last != VERSION:  # first start after an update: show what changed
            self._set_cfg("last_version", VERSION)
            if last:
                self.root.after(1500, self.show_changelog)
        self.root.after(5000, lambda: self.check_updates(manual=False))
        failed = updater.pending_failure()
        if failed:
            errlog.log.error(failed)
            self.root.after(500, lambda: self.lbl_status.configure(
                text="Last update failed - see tracker_errors.log. Use \"Check for updates\" to try again."))
        if setup_dialog.needs_setup(self.cfg):
            self.root.after(400, lambda: self.open_setup(first_run=True))
        self._update_panels_btn()
        self._update_hide_btn()
        self._update_top_btn()
        self.btn_ocr.configure(text="DPS meter: off")

    def _root_configure(self, e):
        if e.widget is self.root:  # the window itself moved or changed size
            self.state.ui_busy_until = time.time() + 0.4

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
                    errlog.report("icon_download", f"cannot download item icon {src}", exc_info=False)
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
        self.tal_canvas.bind("<Configure>", self._tal_resized)
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
        b_cur = btn(btns, "Load current build", self._tal_load_current, True)
        b_cur.grid(row=0, column=0, columnspan=2, sticky="ew", pady=2)
        ui.Tooltip(b_cur, "Reads your talents from the game (talent window open) into the planner and keeps them "
                          "as “my build” – the build everything is compared with.")
        btn(btns, "Save build", self._tal_save_dialog).grid(row=1, column=0, sticky="ew", padx=(0, 4), pady=2)
        btn(btns, "Reset", self._tal_clear).grid(row=1, column=1, sticky="ew", pady=2)
        btn(btns, "Share build", self._tal_share).grid(row=2, column=0, sticky="ew", padx=(0, 4), pady=2)
        btn(btns, "Load build…", self._tal_load_dialog).grid(row=2, column=1, sticky="ew", pady=2)
        mine_link = tk.Label(b, text="Use planner as my build", bg=T["panel"], fg=MUTED, font=ui.F_SMALL,
                             cursor="hand2", anchor="w")
        mine_link.pack(fill="x", padx=12)
        mine_link.bind("<Button-1>", lambda _e: self._tal_save_mine())
        ui.Tooltip(mine_link, "Makes the build in the planner your reference build, e.g. after correcting the read "
                              "build by hand.")
        self.lbl_tal_share = tk.Label(b, text="", bg=T["panel"], fg=T["green"], font=ui.F_SMALL, anchor="w",
                                      justify="left", wraplength=220)
        self.lbl_tal_share.pack(fill="x", padx=12)
        # saved builds to compare
        tk.Label(b, text="Saved builds", bg=T["panel"], fg=T["title"], font=("Georgia", 10), anchor="w").pack(
            fill="x", padx=12, pady=(10, 2))
        self.var_tal_name = tk.StringVar()
        self.tal_saved = tk.Frame(b, bg=T["panel"])
        self.tal_saved.pack(fill="x", padx=12, pady=(4, 0))
        self.btn_tal_builds = btn(b, "All saved builds…", self._tal_builds_window)
        self.btn_tal_builds.pack(fill="x", padx=12, pady=(4, 0))
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

    def _tal_resized(self, e):
        """Redraw the tree once the width settled (not for every pixel while the window is dragged)."""
        if e.width == getattr(self, "_tal_drawn_w", None):
            return
        if getattr(self, "_tal_job", None):
            self.root.after_cancel(self._tal_job)

        def go():
            self._tal_job = None
            self._tal_drawn_w = self.tal_canvas.winfo_width()
            self._tal_draw()
        self._tal_job = self.root.after(120, go)

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
                                          "here with “Load build…”.", fg=self.TC["green"])

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
            self.lbl_tal_share.configure(text=f"Build loaded: {used} points{note}.", fg=self.TC["green"])
            d.destroy()

        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=14, pady=12)
        ui.button(bar, "Load", ok, accent=True).pack(side="right")
        ui.button(bar, "Cancel", d.destroy).pack(side="right", padx=6)
        e.bind("<Return>", lambda _e: ok())
        d.update_idletasks()
        d.geometry(f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 120}")
        e.focus_set()

    def _tal_save_dialog(self):
        """Ask for a name and save the planned build to the list of saved builds."""
        if not self.tal_build:
            self.lbl_tal_share.configure(text="Plan a build first.")
            return
        d = tk.Toplevel(self.root, bg=BG)
        d.title("Save build")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Label(d, text="Name of the build:", bg=BG, fg=FG, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=(12, 4))
        n = len(self.cfg.get("talent_builds") or {}) + 1
        self.var_tal_name.set(f"Build {n}")
        e = tk.Entry(d, textvariable=self.var_tal_name, width=34, bg=ui.RAISED, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_SMALL)
        e.pack(fill="x", padx=14, ipady=4)
        e.select_range(0, "end")

        def ok():
            self._tal_save_named()
            d.destroy()

        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=14, pady=12)
        ui.button(bar, "Save", ok, accent=True).pack(side="right")
        ui.button(bar, "Cancel", d.destroy).pack(side="right", padx=6)
        e.bind("<Return>", lambda _e: ok())
        d.update_idletasks()
        d.geometry(f"+{self.root.winfo_rootx() + 60}+{self.root.winfo_rooty() + 120}")
        e.focus_set()

    def _tal_load_current(self):
        """Read the talent build from the game picture (talent window open)."""
        hero = self._tal_hero()
        if not hero:
            return
        self.lbl_tal_share.configure(text="Reading the talent window…", fg=MUTED)

        def work():
            try:
                frame = self.capture.grab()
                if frame is None:
                    raise RuntimeError(self.capture.status)
                import talent_ocr
                r = talent_ocr.read_build(frame, hero)
            except Exception as e:
                errlog.log.error("Load current build failed", exc_info=True)
                r = str(e)
            self.root.after(0, lambda: self._tal_current_done(r))
        threading.Thread(target=work, daemon=True).start()

    def _tal_current_done(self, r):
        """Merge what the talent window showed (a second read after scrolling completes the first)."""
        if isinstance(r, str) or r is None:
            self.lbl_tal_share.configure(text=r or "Talent window not found – open it in the game and try again.",
                                         fg=C_BAD)
            return
        build, unsure, seen = r
        hero = self._tal_hero()
        prev = getattr(self, "_tal_partial", None)
        if prev and time.time() - prev[3] < 300 and prev[4] == hero:
            pb, pu, ps = prev[0], prev[1], prev[2]
            keep = {k: v for k, v in pb.items() if k not in seen}
            build = {**keep, **build}
            names_now = {t["name"] for t in talents.tree(hero) if talents.key(t) in seen}
            unsure = [n for n in pu if n not in names_now] + list(unsure)
            seen = set(ps) | set(seen)
        self._tal_partial = (dict(build), list(unsure), set(seen), time.time(), hero)
        tree = talents.tree(hero)
        missing_rows = sorted({t["points"] for t in tree if talents.key(t) not in seen})
        # all points must add up to the hero level: one unreadable number follows from the others
        level = self._tal_points()
        if not missing_rows and len(unsure) == 1 and level:
            t = next((x for x in tree if x["name"] == unsure[0]), None)
            if t is not None:
                rest = sum(v for k, v in build.items() if k != talents.key(t))
                if 1 <= level - rest <= t["ranks"]:
                    build[talents.key(t)] = level - rest
                    unsure = []
        self.tal_build = dict(build)
        if not missing_rows:
            self.cfg["talents_mine"] = dict(build)
        self._tal_store()
        self._tal_fill()
        used = sum(build.values())
        if missing_rows:
            msg = (f"Read {used} points so far. Rows {', '.join(map(str, missing_rows))} were not visible – "
                   f"scroll the talent window and click “Load current build” again.")
            col = C_MEH
        else:
            msg = f"Current build read: {used} points, saved as my build."
            col = self.TC["green"]
        if unsure:
            msg += f" Please check: {', '.join(unsure)}."
            col = C_MEH
        self.lbl_tal_share.configure(text=msg, fg=col)

    def _tal_builds_window(self):
        """Window with every saved build: values against my build, load, delete."""
        hero = self._tal_hero()
        if not hero:
            return
        old = getattr(self, "_tal_win", None)
        if old is not None and old.winfo_exists():
            old.destroy()
        w = tk.Toplevel(self.root, bg=BG)
        self._tal_win = w
        w.title("Saved builds")
        w.transient(self.root)
        mode = self.var_tal_mode.get() if self.var_tal_mode.get() in item_eval.MODES else "Damage"
        tk.Label(w, text=f"Saved builds · {hero} · compared with my build in {mode} mode", bg=BG, fg=FG,
                 font=("Bahnschrift SemiBold", 12), anchor="w").pack(fill="x", padx=14, pady=(12, 6))
        body = tk.Frame(w, bg=PANEL)
        body.pack(fill="both", expand=True, padx=14, pady=(0, 6))

        def fill():
            for c in body.winfo_children():
                c.destroy()
            builds = self.cfg.get("talent_builds") or {}
            if not builds:
                tk.Label(body, text="No saved builds yet – plan a build and click “Save build”.", bg=PANEL,
                         fg=MUTED, font=ui.F_SMALL).grid(row=0, column=0, padx=12, pady=12)
                return
            ctx = self._eval_context()
            mine = self.cfg.get("talents_mine") or {}
            shares = self._tal_shares()
            heads = ("Build", "Points", "Damage", "Survival", "Income", "", "")
            for c, h in enumerate(heads):
                tk.Label(body, text=h, bg=PANEL, fg=MUTED, font=ui.F_SMALL,
                         anchor="w" if c == 0 else "e").grid(row=0, column=c, sticky="ew", padx=8, pady=(8, 2))
            rows = []
            for name, b in builds.items():
                b = talents.normalize(b, hero)
                v = talents.evaluate(b, mine, hero, ctx, mode, shares) if ctx.char else (0, 0, 0, 0)
                rows.append((v[3], name, b, v))
            best = max(r[0] for r in rows)
            for i, (score, name, b, (dps, surv, farm, _)) in enumerate(sorted(rows, key=lambda r: -r[0]), start=1):
                cur = b == self.tal_build
                tk.Label(body, text=("● " if cur else "") + name, bg=PANEL,
                         fg=ui.ACCENT if score == best and ctx.char else FG, font=ui.F_SMALL, anchor="w").grid(
                    row=i, column=0, sticky="ew", padx=8, pady=2)
                tk.Label(body, text=str(sum(b.values())), bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="e").grid(
                    row=i, column=1, sticky="ew", padx=8)
                for c, val in ((2, dps), (3, surv), (4, farm)):
                    tk.Label(body, text=f"{val:+.1f} %", bg=PANEL, font=ui.F_SMALL, anchor="e",
                             fg=C_GOOD if val > 0.05 else (C_BAD if val < -0.05 else MUTED)).grid(
                        row=i, column=c, sticky="ew", padx=8)
                ui.button(body, "Load", lambda n=name: (self._tal_load_named(n), fill()), small=True).grid(
                    row=i, column=5, padx=(8, 2), pady=2)
                ui.button(body, "Delete", lambda n=name: (self._tal_delete_named(n), fill()), small=True).grid(
                    row=i, column=6, padx=(2, 8), pady=2)
            body.columnconfigure(0, weight=1)

        fill()
        bar = tk.Frame(w, bg=BG)
        bar.pack(fill="x", padx=14, pady=(4, 12))
        ui.button(bar, "Close", w.destroy).pack(side="right")
        w.update_idletasks()
        w.geometry(f"+{self.root.winfo_rootx() + 80}+{self.root.winfo_rooty() + 100}")

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
        self.btn_tal_builds.configure(text=f"All saved builds ({len(rows)})…")
        for score, name, b, (dps, surv, farm, _) in sorted(rows, key=lambda r: -r[0])[:3]:
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
            notes.append("No reference build yet: click “Load current build” (talent window open in the game) "
                         "or plan it and click “Use planner as my build”.")
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
        hdr = ui.page_header(p, "Stages", "All stages: every run per stage and difficulty of this character, across "
                                          "restarts, grouped by region in map order (incl. the Dream realms). Stages without runs "
                                          "are grey with “no data”. Gold includes sales. ★ = best stage for EXP or "
                                          "gold (from 3 runs)."
                                          "\n\nBoss farming: the Silver bosses (stage 4 of a region, drop the skulls) "
                                          "and Gold bosses (last stage, more legendaries) you have farmed, ranked by "
                                          "kills per hour or by efficiency (kill speed and EXP together)."
                                          "\n\nClick a row: enemies and their damage types, item level of drops and "
                                          "a forecast for the next difficulty.")
        self.boss_info = stages.boss_stages(self.enemy_data)
        self._stage_views = {}
        seg = tk.Frame(hdr, bg=BG)
        seg.pack(side="right")
        b_runs = ui.button(hdr, "Runs…", lambda: self.show_runs(), small=True)
        b_runs.pack(side="right", padx=(0, 12))
        ui.Tooltip(b_runs, "Look at single runs and delete them, or clear the data of a stage.")
        body = tk.Frame(p, bg=BG)
        body.pack(fill="both", expand=True)
        body.grid_columnconfigure(0, weight=1)
        body.grid_rowconfigure(0, weight=1)

        # all stages
        v_all = tk.Frame(body, bg=BG)
        self._build_stage_filters(v_all)
        cols = [("stage", "Stage", 190, "w"), ("boss", "Boss", 66, "w"), ("diff", "Diff", 56, "w"),
                ("runs", "Runs", 50, "e"), ("t", "Time", 56, "e"), ("xp", "EXP/h", 75, "e"),
                ("gold", "Gold/h", 72, "e"), ("it", "Items/h", 66, "e"), ("dead", "Deaths", 60, "e"),
                ("dps", "DPS", 66, "e")]
        f, self.stage_tree = self._tree(v_all, cols, 9)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 6))
        self.stage_tree.tag_configure("best", foreground=C_GOOD)
        self.stage_tree.tag_configure("unknown", foreground=MUTED)
        self.stage_tree.tag_configure("nodata", foreground="#6f6b64", font=("Segoe UI", 9, "italic"))
        self.stage_tree.tag_configure("region", foreground=ui.ACCENT, background=ui.RAISED, font=ui.F_LABEL)
        self.stage_tree.configure(show="tree headings")
        self.stage_tree.column("#0", width=22, minwidth=22, stretch=False)
        self.stage_tree.bind("<<TreeviewSelect>>", self._stage_select)
        self.lbl_stage_count = tk.Label(v_all, text="", bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_stage_count.pack(fill="x", padx=14, pady=(0, 6))

        # boss farming
        v_boss = tk.Frame(body, bg=BG)
        self._build_boss_view(v_boss)

        for name, frame in (("All stages", v_all), ("Boss farming", v_boss)):
            frame.grid(row=0, column=0, sticky="nsew")
            frame.grid_remove()
            b = ui.button(seg, name, lambda n=name: self._stage_view(n), small=True)
            b.pack(side="left", padx=(6, 0))
            self._stage_views[name] = (frame, b)
        det = ui.card(p, fill="x", padx=14, pady=(0, 12))
        self.lbl_stage_detail = ui.autowrap(tk.Label(det, bg=PANEL, fg=FG, font=ui.F_SMALL, anchor="w", justify="left",
                                                     text="Click a stage in the list for details."), 24)
        self.lbl_stage_detail.pack(fill="x", padx=12, pady=10)
        self.stage_btns = tk.Frame(det, bg=PANEL)  # shown for a stage with data
        self._stage_btn_row = None
        ui.button(self.stage_btns, "Runs of this stage", lambda: self.show_runs(*self._stage_btn_row),
                  small=True).pack(side="left", padx=(0, 8))
        ui.button(self.stage_btns, "Clear this stage…", lambda: self._stage_clear(*self._stage_btn_row),
                  small=True).pack(side="left")
        self._stage_rows = []
        self._stage_shown = []
        self._boss_rows = []
        self._stage_sig = None
        self._stage_view(self.cfg.get("stage_view", "All stages"))

    def _stage_view(self, name):
        if name not in self._stage_views:
            name = "All stages"
        for n, (frame, b) in self._stage_views.items():
            on = n == name
            (frame.grid if on else frame.grid_remove)()
            b.configure(bg=ui.ACCENT if on else ui.RAISED, fg=BG if on else FG)
            # ui.button restores its own colours on mouse-leave: keep the active one highlighted
            b.bind("<Leave>", lambda e, w=b, o=on: w.configure(bg=ui.ACCENT if o else ui.RAISED))
        if self.cfg.get("stage_view") != name:
            self._set_cfg("stage_view", name)

    BOSS_RANKS = ("Time (kills/h)", "Efficiency (time + EXP)")
    # sort options of "All stages": label -> (key, descending)
    STAGE_SORTS = {
        "EXP/h": (lambda x: x["xp_h"], True),
        "Gold/h": (lambda x: x["gold_h"], True),
        "Items/h": (lambda x: x["items_h"], True),
        "Run time (fastest)": (lambda x: x.get("run_s") or x["avg_s"] or 1e9, False),
        "Runs": (lambda x: x["runs"], True),
        "Deaths (fewest)": (lambda x: x["death_rate"], False),
        "DPS": (lambda x: x["dps"] or 0, True),
    }

    def _build_stage_filters(self, p):
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 6))
        c = self.cfg
        self.var_st_search = tk.StringVar(value="")
        self.var_st_type = tk.StringVar(value=c.get("stage_type", "All"))
        self.var_st_diff = tk.StringVar(value=c.get("stage_diff", "All"))
        sorts = list(self.STAGE_SORTS) + ["Map order"]
        self.var_st_sort = tk.StringVar(value=c.get("stage_sort") if c.get("stage_sort") in sorts else "Map order")
        self.var_st_empty = tk.BooleanVar(value=c.get("stage_show_empty", True))

        tk.Label(bar, text="Search", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        e = tk.Entry(bar, textvariable=self.var_st_search, width=18, bg=PANEL, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_SMALL)
        e.pack(side="left", padx=(6, 12), ipady=3)
        ui.Tooltip(e, "Region, stage or boss, e.g. “Tazan”, “Kings Woods: 4”, “Raptor”.")
        self.var_st_search.trace_add("write", lambda *_: self._fill_stages())

        def combo(label, var, values, key, width):
            tk.Label(bar, text=label, bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
            cb = ttk.Combobox(bar, textvariable=var, values=values, width=width, state="readonly")
            cb.pack(side="left", padx=(6, 12))
            cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg(key, var.get()), self._fill_stages()))

        combo("Type", self.var_st_type, ["All", "Normal stages", "Silver bosses", "Gold bosses", "Unknown"],
              "stage_type", 13)
        combo("Difficulty", self.var_st_diff, ["All", "Normal", "Nightmare", "Inferno"], "stage_diff", 10)
        combo("Sort by", self.var_st_sort, sorts, "stage_sort", 17)
        bar2 = tk.Frame(p, bg=BG)
        bar2.pack(fill="x", padx=14, pady=(0, 6))
        cb = tk.Checkbutton(bar2, text="Show stages without runs (map order)", variable=self.var_st_empty, bg=BG, fg=FG,
                            selectcolor=PANEL, activebackground=BG, activeforeground=FG, font=ui.F_SMALL,
                            highlightthickness=0, bd=0,
                            command=lambda: (self._set_cfg("stage_show_empty", self.var_st_empty.get()),
                                             self._fill_stages()))
        cb.pack(side="left")
        ui.Tooltip(cb, "In map order: also list the stages you have not played yet (on the chosen difficulty), "
                       "marked grey with “no data”.")

    def _stage_order(self) -> dict:
        """Map position of every known stage: lower-case name -> (region number, stage number)."""
        if not hasattr(self, "_stage_pos"):
            self._stage_pos = {str(s["stage"]).lower(): (s.get("region_no") or 0, s.get("stage_no") or 0)
                               for s in self.enemy_data.get("stages", [])}
        return self._stage_pos

    def _fill_stages(self):
        if not hasattr(self, "var_st_sort"):
            return
        q = self.var_st_search.get().strip().lower()
        typ = self.var_st_type.get()
        diff = self.var_st_diff.get()
        pos = self._stage_order()
        grouped = self.var_st_sort.get() == "Map order"
        show_empty = self.var_st_empty.get()
        diff_order = ["Normal", "Nightmare", "Inferno"]

        def diff_key(x):
            base = stages.base_difficulty(x["difficulty"])
            return (diff_order.index(base) if base in diff_order else 9, x["difficulty"])

        def type_ok(kind, known):
            return (typ == "All" or typ == "Normal stages" and known and not kind
                    or typ == "Silver bosses" and kind == "Silver" or typ == "Gold bosses" and kind == "Gold"
                    or typ == "Unknown" and not known)

        rows = [x for x in self._stage_rows
                if diff == "All" or stages.base_difficulty(x["difficulty"]) == diff]
        t = self.stage_tree
        t._sort = None  # the "Sort by" choice decides; a click on a header still sorts by that column
        for col, title in t._titles.items():
            ttk.Treeview.heading(t, col, text=title)
        t.delete(*t.get_children())
        shown = []        # (parent iid, row) in display order
        groups = []       # (iid, region label, kind label, [rows])
        if grouped:
            if not hasattr(self, "_catalog"):
                self._catalog = stages.stage_catalog(self.enemy_data)
            used = set()
            for g in self._catalog:
                children = []
                for st in g["stages"]:
                    name = st["stage"].lower()
                    if not type_ok(st["kind"] if st["kind"] != "Treasure" else "Treasure", True):
                        continue
                    if q and q not in f"{st['stage']} {st['boss']} {g['region']}".lower():
                        continue
                    data = [x for x in rows if x["stage"].lower() == name
                            or g["dream"] and x["stage"].lower().startswith(name)]
                    used.update(id(x) for x in data)
                    if data:
                        children += sorted(data, key=diff_key)
                    elif show_empty:
                        children.append(self._empty_stage(st["stage"], diff))
                if children:
                    groups.append((g["region"], (g["region"], f"Lv {g['levels']}"), children))
            rest = [x for x in rows if id(x) not in used and type_ok("", x["stage"].lower() in pos)
                    and (not q or q in x["stage"].lower())]
            if rest:
                groups.append(("unrecognised", ("Stage name not read", ""),
                               sorted(rest, key=lambda x: (x["stage"], diff_key(x)))))
        else:
            flat = []
            for x in rows:
                name = x["stage"].lower()
                b = self.boss_info.get(name)
                if not type_ok(b["kind"] if b else "", name in pos):
                    continue
                if q and q not in f"{x['stage']} {b['boss'] if b else ''} {b['region'] if b else ''}".lower():
                    continue
                flat.append(x)
            key, desc = self.STAGE_SORTS.get(self.var_st_sort.get(), self.STAGE_SORTS["EXP/h"])
            flat.sort(key=key, reverse=desc)
            groups.append((None, None, flat))

        data_rows = [x for _, _, ch in groups for x in ch if not x.get("nodata")]
        best_xp = max((x["xp_h"] for x in data_rows if x["runs"] >= 3), default=None)
        best_gold = max((x["gold_h"] for x in data_rows if x["runs"] >= 3), default=None)
        self._stage_shown = []
        n_empty = 0
        for gi, (gid, label, children) in enumerate(groups):
            parent = ""
            if gid is not None:
                parent = f"g{gi}"
                runs = sum(x["runs"] for x in children)
                t.insert("", "end", iid=parent, open=True, tags=("region",),
                         values=(label[0], label[1], "", runs or "", "", "", "", "", "", ""))
            for x in children:
                iid = str(len(self._stage_shown))
                self._stage_shown.append(x)
                b = self.boss_info.get(x["stage"].lower())
                kind = b["kind"] if b else ("Treasure" if x["stage"].lower().startswith("dreamy") else "")
                if x.get("nodata"):
                    n_empty += 1
                    t.insert(parent, "end", iid=iid, tags=("nodata",), values=(
                        x["stage"], kind, stages.short_difficulty(x["difficulty"]), "0", "no data",
                        "", "", "", "", ""))
                    continue
                star_x = " ★" if best_xp and x["xp_h"] == best_xp else ""
                star_g = " ★" if best_gold and x["gold_h"] == best_gold else ""
                tags = ("best",) if star_x or star_g else ("unknown",) if x["stage"].lower() not in pos else ()
                t.insert(parent, "end", iid=iid, tags=tags, values=(
                    x["stage"], kind, stages.short_difficulty(x["difficulty"]), x["runs"],
                    fmt_dur(x.get("run_s") or x["avg_s"]), fmt(x["xp_h"]) + star_x,
                    fmt(x["gold_h"]) + star_g, f"{x['items_h']:.1f}", f"{x['death_rate'] * 100:.0f} %",
                    fmt(x["dps"]) if x["dps"] else "-"))
        n_data = len(self._stage_shown) - n_empty
        text = f"{n_data} rows with runs"
        if grouped and show_empty:
            text += f", {n_empty} stages without runs (grey, “no data”)"
        text += ". ★ = best EXP/h or gold/h in this list (from 3 runs)."
        if not grouped:
            text += " Choose “Map order” to see every stage of every region."
        self.lbl_stage_count.configure(text=text)

    def _empty_stage(self, stage, diff):
        """Row for a stage without runs (on the chosen difficulty)."""
        return {"stage": stage, "difficulty": diff if diff != "All" else "–", "runs": 0, "nodata": True,
                "xp_run": 0, "kills_h": 0, "leg_h": None, "avg_s": None, "run_s": None, "xp_h": 0, "gold_h": 0,
                "items_h": 0, "death_rate": 0, "dps": None, "casts": {}, "cast_runs": 0}

    def _build_boss_view(self, p):
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 6))
        c = self.cfg
        self.var_boss_search = tk.StringVar(value="")
        self.var_boss_kind = tk.StringVar(value=c.get("boss_kind", "All bosses"))
        self.var_boss_diff = tk.StringVar(value=c.get("boss_diff", "All"))
        self.var_boss_rank = tk.StringVar(value=c.get("boss_rank", self.BOSS_RANKS[1]))
        self.var_boss_w = tk.IntVar(value=int(c.get("boss_weight", 50)))

        tk.Label(bar, text="Search", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        e = tk.Entry(bar, textvariable=self.var_boss_search, width=18, bg=PANEL, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_SMALL)
        e.pack(side="left", padx=(6, 12), ipady=3)
        ui.Tooltip(e, "Region, boss or stage, e.g. “Cinder”, “Frost Dragon”, “Kings Woods South”.")
        self.var_boss_search.trace_add("write", lambda *_: self._fill_bosses())

        def combo(label, var, values, key, width):
            tk.Label(bar, text=label, bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
            cb = ttk.Combobox(bar, textvariable=var, values=values, width=width, state="readonly")
            cb.pack(side="left", padx=(6, 12))
            cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg(key, var.get()), self._fill_bosses()))

        combo("Boss", self.var_boss_kind, ["All bosses", "Silver", "Gold"], "boss_kind", 10)
        combo("Difficulty", self.var_boss_diff, ["All", "Normal", "Nightmare", "Inferno"], "boss_diff", 10)
        combo("Rank by", self.var_boss_rank, list(self.BOSS_RANKS), "boss_rank", 21)

        wrow = tk.Frame(p, bg=BG)
        wrow.pack(fill="x", padx=14, pady=(0, 6))
        tk.Label(wrow, text="Efficiency weighting:  kill speed", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        sc = tk.Scale(wrow, from_=0, to=100, orient="horizontal", variable=self.var_boss_w, showvalue=False,
                      length=160, width=10, sliderlength=18, bg=ui.ACCENT, fg=FG, troughcolor=ui.RAISED,
                      highlightthickness=0, bd=0, sliderrelief="flat", activebackground=ui.ACCENT, resolution=5)
        self.boss_scale = sc
        sc.pack(side="left", padx=8)
        tk.Label(wrow, text="EXP", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        self.lbl_boss_w = tk.Label(wrow, text="", bg=BG, fg=FG, font=ui.F_SMALL)
        self.lbl_boss_w.pack(side="left", padx=(10, 0))
        ui.info(wrow, "Score 0–100: kills/h and EXP/h are each compared with the best boss in the list "
                      "(best = 100) and mixed with this weighting. 50 = both count the same; 100 = EXP only."
                ).pack(side="left", padx=(6, 0))
        self._boss_w_job = None

        def weight_moved(*_):
            if self._boss_w_job:
                self.root.after_cancel(self._boss_w_job)

            def apply():
                self._boss_w_job = None
                self._set_cfg("boss_weight", self.var_boss_w.get())
                self._fill_bosses()
            self._boss_w_job = self.root.after(150, apply)
        self.var_boss_w.trace_add("write", weight_moved)

        cols = [("rank", "#", 34, "e"), ("boss", "Boss", 160, "w"), ("kind", "Type", 56, "w"),
                ("stage", "Stage", 176, "w"), ("diff", "Diff", 66, "w"), ("runs", "Runs", 52, "e"),
                ("t", "Run time", 74, "e"), ("kh", "Kills/h", 60, "e"), ("xp", "EXP/h", 72, "e"),
                ("leg", "Leg./h", 56, "e"), ("score", "Score", 54, "e")]
        f, self.boss_tree = self._tree(p, cols, 9)
        f.pack(fill="both", expand=True, padx=14, pady=(0, 4))
        self.boss_tree.tag_configure("Silver", foreground="#c9d1dc")
        self.boss_tree.tag_configure("Gold", foreground="#f2c14e")
        self.boss_tree.tag_configure("few", foreground=MUTED)
        self.boss_tree.bind("<<TreeviewSelect>>", self._boss_select)
        self.lbl_boss_note = ui.autowrap(tk.Label(p, text="", bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w",
                                                  justify="left"), 24)
        self.lbl_boss_note.pack(fill="x", padx=14, pady=(0, 6))

    def _fill_bosses(self):
        if not hasattr(self, "boss_tree"):
            return
        w = self.var_boss_w.get() / 100
        efficiency = self.var_boss_rank.get() == self.BOSS_RANKS[1]
        self.lbl_boss_w.configure(text=f"{100 - self.var_boss_w.get()} % kill speed · {self.var_boss_w.get()} % EXP",
                                  fg=FG if efficiency else MUTED)
        self.boss_scale.configure(state="normal" if efficiency else "disabled",
                                  bg=ui.ACCENT if efficiency else ui.LINE)
        q = self.var_boss_search.get().strip().lower()
        kind = self.var_boss_kind.get()
        diff = self.var_boss_diff.get()
        rows = []
        for x in self._stage_rows:
            b = self.boss_info.get(x["stage"].lower())
            base = stages.base_difficulty(x["difficulty"])
            if not b or not x["kills_h"] or base not in stages.DIFFICULTY:
                continue  # "?" = difficulty unknown (runs recorded by an older version)
            if kind != "All bosses" and b["kind"] != kind:
                continue
            if diff != "All" and base != diff:
                continue
            if q and q not in f"{b['boss']} {b['region']} {x['stage']}".lower():
                continue
            rows.append((x, b))
        best_k = max((x["kills_h"] for x, _ in rows), default=0) or 1
        best_x = max((x["xp_h"] for x, _ in rows), default=0) or 1
        scored = []
        for x, b in rows:
            score = 100 * ((1 - w) * x["kills_h"] / best_k + w * x["xp_h"] / best_x)
            scored.append((score if efficiency else x["kills_h"], score, x, b))
        scored.sort(key=lambda s: -s[0])
        self._boss_rows = [s[2] for s in scored]
        t = self.boss_tree
        t._sort = None  # the ranking decides the order; a click on a header still sorts by that column
        for c, title in t._titles.items():
            ttk.Treeview.heading(t, c, text=title)
        t.delete(*t.get_children())
        for i, (_, score, x, b) in enumerate(scored):
            tags = (b["kind"],) if x["runs"] >= 3 else ("few",)
            t.insert("", "end", iid=str(i), tags=tags, values=(
                i + 1, b["boss"], b["kind"], x["stage"],
                stages.short_difficulty(x["difficulty"]), x["runs"],
                fmt_dur(x.get("run_s") or x["avg_s"]), f"{x['kills_h']:.1f}", fmt(x["xp_h"]),
                f"{x['leg_h']:.1f}" if x.get("leg_h") is not None else "–", f"{score:.0f}"))
        farmed = len({x["stage"].lower() for x in self._stage_rows if x["stage"].lower() in self.boss_info})
        if not scored:
            note = ("No boss runs match the filter." if farmed else
                    "No boss runs yet. Farm a Silver boss (stage 4 of a region) or a Gold boss (last stage) "
                    "and it shows up here.")
        else:
            note = (f"Kills/h counts the full cycle incl. the time between runs. Grey rows: fewer than 3 runs. "
                    f"Leg./h = legendary and divine drops from the stage end screen (also items sold right "
                    f"away); “–” = no runs recorded yet. "
                    f"Gold bosses need one skull of the difficulty per run (not included).")
        self.lbl_boss_note.configure(text=note)

    def _boss_select(self, _=None):
        sel = self.boss_tree.selection()
        if sel:
            self._stage_detail(self._boss_rows[int(sel[0])])

    def _aggregate_stages(self):
        """Count runs into the persistent stage statistics once their stage is known (or 2.5 min passed)."""
        st = self.state
        now = time.time()
        changed = False
        with st.lock:
            runs = list(st.runs)
            sold = list(st.sold)
            drops = list(st.drops)
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
            # end screen "Legendary N (+x)": every drop, also items sold right away; without it the
            # pop-ups (only what was picked up) or the orange names seen on the ground, whichever is more
            legendaries = r.end_screen.get("legendaries")
            if legendaries is None:
                legendaries = max(sum(1 for t, _, cat, _ in drops if t0 < t <= r.end + 10 and cat in LEGENDARY)
                                  + sum(1 for t, _, _, rar in sold if t0 < t <= r.end + 10 and rar in LEGENDARY),
                                  r.ground_legendaries)
            self.stage_stats.add_run(r.char or self.state.char_name, stage, r.difficulty, cycle, r.xp, r.gold,
                                     sold_gold, r.items,
                                     r.death == "confirmed" or r.death == "suspected",
                                     r.damage, r.duration if r.damage else 0, r.casts or None,
                                     run_s=r.game_seconds, legendaries=legendaries, run_id=r.run_id, t=r.end)
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
        self._fill_bosses()
        self._fill_stages()
        self._fill_skills()
        self._fill_minions()

    def _stage_select(self, _=None):
        sel = self.stage_tree.selection()
        if sel and not sel[0].startswith("g"):
            self._stage_detail(self._stage_shown[int(sel[0])])

    def _stage_detail(self, x):
        info = stages.stage_info(x["stage"], self.enemy_data)
        dream = next((d for d in stages.DREAM_REALMS if x["stage"].lower().startswith(d["region"].lower())), None)
        if x.get("nodata"):
            parts = [f"{x['stage']} · no runs recorded yet"
                     + ("" if x["difficulty"] == "–" else f" on {x['difficulty']}")]
        else:
            parts = [f"{x['stage']} · {x['difficulty']} · {x['runs']} runs · avg {fmt(x['xp_run'])} EXP/run"]
        b = self.boss_info.get(x["stage"].lower())
        if dream:
            info = None
            parts.append(f"Treasure map (needs a Treasure Key) · boss: {dream['boss']}, level {dream['level']} on "
                         f"Normal · opened by clearing {dream['after']}: 7. Its drake drops treasure chests and "
                         f"gems.")
        elif b and x.get("nodata"):
            parts.append(f"{b['kind']} boss: {b['boss']}"
                         + (" · drops the skulls for the region's Gold boss" if b["kind"] == "Silver"
                            else " · needs one skull of the difficulty per run"))
        elif b:
            parts.append(f"{b['kind']} boss: {b['boss']} · {x['kills_h']:.1f} kills/h"
                         + (f" · {x['leg_h']:.1f} legendaries/h" if x.get("leg_h") is not None else "")
                         + (" · drops the skulls for the region's Gold boss" if b["kind"] == "Silver"
                            else " · needs one skull of this difficulty per run"))
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
            base = stages.base_difficulty(x["difficulty"])
            lo, hi = item_quality.drop_item_level(info.get("level_min") or 70, base)
            loot = [f"Drops: item level {lo}–{hi} (wearable from level {min(70, lo // 10)})"]
            if info.get("boss"):
                blvl = 70 if base != "Normal" else (info.get("level_max") or 70)
                shards = item_quality.soul_shards(blvl, base)
                per_h = shards * 3600 / x["avg_s"] if x.get("avg_s") else 0
                loot.append(f"Boss {info['boss']}: {shards} Soul Shards per boss kill (≈ {per_h:.0f}/h)")
            parts.append(" · ".join(loot))
        elif not dream:
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
        if x.get("nodata"):
            self.stage_btns.pack_forget()
        else:
            self._stage_btn_row = (x["stage"], x["difficulty"])
            self.stage_btns.pack(fill="x", padx=12, pady=(0, 10))

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
        leg = ui.card(p, fill="x", padx=14, pady=(0, 8))
        row = tk.Frame(leg, bg=PANEL)
        row.pack(fill="x", padx=12, pady=(8, 2))
        self.var_leg_sound = tk.BooleanVar(value=self.cfg.get("legendary_sound", True))
        tk.Checkbutton(row, text="Sound when a Legendary drops", variable=self.var_leg_sound,
                       command=self._toggle_leg_sound, bg=PANEL, fg=ui.RARITY["Legendary"], selectcolor=BG,
                       activebackground=PANEL, activeforeground=FG, font=ui.F_LABEL, highlightthickness=0,
                       bd=0).pack(side="left")
        ui.button(row, "Play sound", self._play_legendary, small=True).pack(side="right")
        row2 = tk.Frame(leg, bg=PANEL)
        row2.pack(fill="x", padx=12, pady=(0, 2))
        self.var_leg_space = tk.BooleanVar(value=self.cfg.get("legendary_space", True))
        tk.Checkbutton(row2, text="Press Space in the game when a Legendary drops (puts it into the inventory)",
                       variable=self.var_leg_space,
                       command=lambda: (self._set_cfg("legendary_space", bool(self.var_leg_space.get())),
                                        self._loot_enabled()),
                       bg=PANEL, fg=FG, selectcolor=BG, activebackground=PANEL, activeforeground=FG,
                       font=ui.F_SMALL, highlightthickness=0, bd=0).pack(side="left")
        self.lbl_leg = tk.Label(leg, text="No Legendary this session yet", bg=PANEL, fg=FG, font=ui.F_SMALL,
                                anchor="w")
        self.lbl_leg.pack(fill="x", padx=12)
        ui.autowrap(tk.Label(leg, text="Plays when an orange item name appears on the ground. If one is missed "
                                       "there (it dropped off screen, or straight into the carriage), the stage "
                                       "end screen catches it: its “Legendary (+1)” is also what Legendaries/h "
                                       "in Stages counts – every drop, also items sold right away.",
                             bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left"), 40).pack(
            fill="x", padx=12, pady=(0, 8))
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
        ctx = item_eval.Context(char=char, hero=self.state.hero, level=self.state.level or int(char.get("Level", 0)),
                                element=self.cfg.get("element", "Auto"), enemy_level=enemy, damage_weights=weights,
                                dot_share=dot, other=other, overrides=self.cfg.get("legendary_values", {}),
                                weapon=tuple(self.cfg["weapon"]) if self.cfg.get("weapon") else None,
                                gem_tier=int(self.cfg.get("gem_tier", 3)),
                                ability_shares=self._ability_shares())
        prof = self._profile_cached()
        if prof.get("ok"):  # what skill tracking measured replaces the estimates
            ctx.slot_shares = dict(prof["slot_share"])
            ctx.out_dot = prof["dot_share"]
            cond = prof["condition"]
            ctx.conditions = {"Damage vs Burned": cond.get("Burning", 0.0), "Damage vs Slowed": cond.get("Slowed", 0.0),
                              "Damage vs Immobilized": cond.get("Immobilized", 0.0),
                              "Damage vs Bleeding": cond.get("Bleeding", 0.0),
                              "Damage vs Poisoned": cond.get("Poisoned", 0.0),
                              "Damage vs Vulnerable": cond.get("Vulnerable", 0.0)}
            ctx.attack_share = sum(x["share"] for x in prof["abilities"].values()
                                   if x["slot"] in ("Basic Attack", "Strong Attack")) or 1.0
            ctx.wd_per_s = prof["wd_per_s"]
        bar = [s[0] for s in (self.skills.bar.slots if self.skills.bar else [])]
        ctx.used_abilities = tuple(bar or prof.get("abilities", {}).keys())
        sim = self._combat_sim(bar, char, prof)
        if sim is not None:
            ctx.sim, ctx.kills_per_s = sim
        return ctx

    def _combat_sim(self, bar, char, prof):
        """(CombatSim of the skill bar, kills per second calibrated on the measured casts) or None."""
        slots = bar or [n for n, _ in sorted(prof.get("abilities", {}).items(),
                                             key=lambda kv: ["Basic Attack", "Strong Attack", "Special"].index(
                                                 kv[1]["slot"]) if kv[1]["slot"] in ("Basic Attack", "Strong Attack",
                                                                                      "Special") else 3)][:4]
        if not slots:
            return None
        key = (tuple(slots), self.state.hero, tuple(round(float(char.get(k, 0) or 0), 2) for k in combat_sim.STATS),
               getattr(self, "_prof_key", None), str(self.cfg.get("rating_overrides")))
        if getattr(self, "_sim_key", None) != key:
            sim = combat_sim.CombatSim(slots, self.state.hero)
            if not sim.ok():
                self._sim_key, self._sim_val = key, None
            else:
                measured = {k: v["cps"] for k, v in prof.get("abilities", {}).items()
                            if v.get("cps")} if prof.get("ok") else {}
                kr = sim.calibrate(char, measured) if measured else 1.0
                own = (self.cfg.get("rating_overrides") or {}).get("kills_per_s")
                if own:
                    kr = float(own)
                self._sim_key, self._sim_val = key, (sim, kr)
        return self._sim_val

    def _profile_cached(self) -> dict:
        """Build profile, worked out again only when the stage statistics or the character changed."""
        key = (self._stage_char(), id(self.stage_stats), getattr(self, "_stage_sig", None),
               (self.char_stats.get("Attack Speed") or (None,))[0],
               tuple(s[0] for s in (self.skills.bar.slots if getattr(self, "skills", None) and self.skills.bar else [])))
        if getattr(self, "_prof_key", None) != key:
            self._prof_key, self._prof_val = key, self._profile()
        return self._prof_val

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
        except FileNotFoundError:
            pass
        except Exception:
            errlog.report("deaths_read", f"cannot read {DEATHS_CSV}")
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
            if txt.startswith("cannot be calculated"):
                txt += " – set your own value →"
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
        self._fill_minions()  # minion values depend on the character sheet
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
                errlog.log.error(f"{label} failed", exc_info=True)
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
                            self._fill_itemdb()
                            if self._db_sel:
                                self._itemdb_select()
                            self._fill_minions()
                            self._fill_skills()
                            self._sk_slot_sig = None
                        self.root.after(300, refill)
                elif ev[0] == "status":
                    self.lbl_status.configure(text=ev[1])
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
        try:
            while True:
                self._legendary_dropped(*self.state.legendary_q.get_nowait())
        except queue.Empty:
            pass
        self.root.after(100, self.poll_events)

    def _legendary_dropped(self, source: str, text: str):
        self._leg_session += 1
        what = f"Legendary dropped: {text}" if text else "Legendary dropped (end screen)"
        self.lbl_status.configure(text=f"{what} – {datetime.now().strftime('%H:%M:%S')}")
        self.lbl_leg.configure(text=f"{self._leg_session} this session · last: {text or 'from the end screen'} "
                                    f"({datetime.now().strftime('%H:%M')})")
        if self.cfg.get("legendary_sound", True) and time.time() - self._last_beep > 1.5:
            self._last_beep = time.time()
            self._play_legendary()
        if self.cfg.get("legendary_space", True) and time.time() - getattr(self, "_last_space", 0) > 2.0:
            self._last_space = time.time()
            threading.Thread(target=self._press_space, daemon=True).start()

    def _press_space(self):
        """Tap Space in the game once (puts the item into the inventory). Like the log panel: right away while
        Deskrawl has focus, otherwise with a short focus switch if the panel mode allows it."""
        try:
            time.sleep(0.3)
            hwnd = self.capture.find()
            if not hwnd:
                return
            switch = self.cfg.get("auto_panels", "always") == "always" and not self.cfg.get("game_hidden")
            prev = game_input.press_keys(hwnd, ["SPACE"], allow_focus_switch=switch)
            if prev is None:
                self.events.put(("status", "Legendary: Space not pressed – Deskrawl not in the foreground"))
            game_input.restore_focus(prev)
        except Exception:
            errlog.report("space", "Space for a Legendary could not be pressed")

    def _play_legendary(self):
        try:
            import winsound
            winsound.PlaySound(paths.res("data", "legendary.wav"), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception:
            errlog.report("sound", "legendary sound could not be played")

    def _toggle_leg_sound(self):
        self._set_cfg("legendary_sound", bool(self.var_leg_sound.get()))
        self._loot_enabled()

    def _loot_enabled(self):
        self.loot.enabled = bool(self.cfg.get("legendary_sound", True) or self.cfg.get("legendary_space", True))

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
            errlog.report("items_csv", "cannot write the item history")

    # -- actions -------------------------------------------------------------
    # -- item database -------------------------------------------------------
    DB_SLOTS = ["All slots", "Weapon", "Helm", "Chest Armor", "Shoulder", "Gloves", "Belt", "Pants", "Boots",
                "Necklace", "Ring", "Back"]
    DB_RARITIES = ["Legendary + Divine", "Legendary", "Divine", "Black Mist possible"]
    DB_CLASSES = ["All classes", "Warrior", "Sorcerer", "Hunter", "Monk"]

    def _load_item_db(self):
        try:
            with open(paths.res("data", "item_db.json"), encoding="utf-8") as f:
                return json.load(f)["items"]
        except Exception:
            errlog.log.error("cannot read data/item_db.json", exc_info=True)
            return []

    def _build_itemdb(self, p):
        self.item_db = self._load_item_db()
        hdr = ui.page_header(p, "Item Database", (
            "Every Legendary and Divine item of the game by slot: its unique effect, the numbers it can roll, "
            "the attributes it can get for each class and where it drops.\n\nBlack Mist is not an item of its "
            "own: any Legendary item (not Back) dropped by a level-70 enemy or chest on Nightmare or Inferno has a "
            "0.2 % chance to be Black Mist – item level 850, top base value, no sockets, and 5 attributes that "
            "Lady “Shadow” reveals (each a pick between 2, at the top roll). Ancient (10 %) takes the top roll of "
            "every number.\n\nData from wikily.gg; pictures are loaded once and kept."))
        self.lbl_db_count = tk.Label(hdr, text="", bg=BG, fg=MUTED, font=ui.F_SMALL)
        self.lbl_db_count.pack(side="right")
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 6))
        c = self.cfg
        self.var_db_search = tk.StringVar(value="")
        self.var_db_slot = tk.StringVar(value=c.get("db_slot", "All slots"))
        self.var_db_rarity = tk.StringVar(value=c.get("db_rarity", self.DB_RARITIES[0]))
        self.var_db_class = tk.StringVar(value=c.get("db_class", "All classes"))
        tk.Label(bar, text="Search", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        e = tk.Entry(bar, textvariable=self.var_db_search, width=16, bg=PANEL, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_SMALL)
        e.pack(side="left", padx=(6, 12), ipady=3)
        ui.Tooltip(e, "Name, effect, attribute or boss, e.g. “Crown”, “Critical”, “Frost Dragon”.")
        self.var_db_search.trace_add("write", lambda *_: self._fill_itemdb())

        def combo(label, var, values, key, width):
            tk.Label(bar, text=label, bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
            cb = ttk.Combobox(bar, textvariable=var, values=values, width=width, state="readonly")
            cb.pack(side="left", padx=(6, 12))
            cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg(key, var.get()), self._fill_itemdb()))

        combo("Slot", self.var_db_slot, self.DB_SLOTS, "db_slot", 11)
        combo("Rarity", self.var_db_rarity, self.DB_RARITIES, "db_rarity", 17)
        combo("Class", self.var_db_class, self.DB_CLASSES, "db_class", 10)

        cols = [("name", "Item", 200, "w"), ("slot", "Slot", 80, "w"), ("rar", "Rarity", 70, "w"),
                ("cls", "Classes", 150, "w"), ("lvl", "Min lvl", 56, "e"), ("src", "Source", 220, "w")]
        f, self.db_tree = self._tree(p, cols, 7, icons=True)
        f.pack(fill="x", padx=14, pady=(0, 6))
        for r in ("Legendary", "Divine"):
            self.db_tree.tag_configure(r, foreground=ui.RARITY[r])
        self.db_tree.bind("<<TreeviewSelect>>", self._itemdb_select)
        sf = ui.ScrollFrame(p)
        sf.pack(fill="both", expand=True, padx=0, pady=(0, 8))
        self.db_detail = ui.card(sf.inner, fill="x", padx=14, pady=(0, 6))
        tk.Label(self.db_detail, text="Click an item in the list for its numbers and where it drops.", bg=PANEL,
                 fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=12)
        self._db_rows = []
        self._db_sel = None
        self._fill_itemdb()

    @staticmethod
    def _db_source(it):
        """Short "where" for the table."""
        w = [t for t, _ in it["where"]]
        if it["rarity"] == "Divine":
            first = next((t for t in w if t not in ("Elites and bosses", "Treasure chests") and
                          not t.startswith("Mystery")), None)
            return (f"{first}; " if first else "") + "Inferno lvl 70 elites/bosses, chests"
        boss = next((t for t in w if t not in ("Legendary drops",) and not t.startswith("Mystery")), None)
        parts = []
        if boss:
            pct = re.search(r"([\d.]+%)", dict(it["where"]).get(boss, ""))
            parts.append(f"{boss} ({pct.group(1)})" if pct else boss)
        if "Legendary drops" in w:
            parts.append("all enemies")
        if any(t.startswith("Mystery") for t in w):
            parts.append("Mystery vendor")
        return ", ".join(parts) or "-"

    def _fill_itemdb(self):
        if not hasattr(self, "db_tree"):
            return
        q = self.var_db_search.get().strip().lower()
        slot, rar, cls = self.var_db_slot.get(), self.var_db_rarity.get(), self.var_db_class.get()
        order = {s: i for i, s in enumerate(self.DB_SLOTS)}
        rows = []
        for it in self.item_db:
            if slot != "All slots" and it["slot"] != slot:
                continue
            if rar == "Legendary" and it["rarity"] != "Legendary" or rar == "Divine" and it["rarity"] != "Divine":
                continue
            if rar == "Black Mist possible" and not it["info"].get("Can be Black Mist", "").startswith("Yes"):
                continue
            if cls != "All classes" and cls not in it["classes"]:
                continue
            if q:
                hay = " ".join([it["name"], it["effect"], it["ability_levels"], it["slot"],
                                " ".join(t + " " + s for t, s in it["where"]),
                                " ".join(a for a, *_ in it["attributes"]), " ".join(a for a, _ in it["fixed"])])
                if q not in hay.lower():
                    continue
            rows.append(it)
        rows.sort(key=lambda it: (order.get(it["slot"], 99), it["rarity"] != "Divine", it["name"]))
        self._db_rows = rows
        t = self.db_tree
        t.delete(*t.get_children())
        for i, it in enumerate(rows):
            classes = "all" if len(it["classes"]) == 4 else ", ".join(it["classes"])
            lvl = it["info"].get("Minimum drop level") or "-"
            t.insert("", "end", iid=str(i), image=self._item_photo(it.get("icon_url"), 30) or "",
                     tags=(it["rarity"],), values=(it["name"], it["slot"], it["rarity"], classes,
                                                   f"lvl {lvl}" if lvl != "-" else "-", self._db_source(it)))
        n_leg = sum(1 for it in rows if it["rarity"] == "Legendary")
        self.lbl_db_count.configure(text=f"{len(rows)} items · {n_leg} Legendary · {len(rows) - n_leg} Divine")
        if self._db_sel and any(it["slug"] == self._db_sel for it in rows):
            k = next(i for i, it in enumerate(rows) if it["slug"] == self._db_sel)
            t.selection_set(str(k))
            t.see(str(k))

    def _itemdb_select(self, _=None):
        sel = self.db_tree.selection()
        if sel:
            it = self._db_rows[int(sel[0])]
            self._db_sel = it["slug"]
            self._itemdb_detail(it)

    def _itemdb_detail(self, it):
        d = self.db_detail
        for w in d.winfo_children():
            w.destroy()
        rcol = ui.RARITY.get(it["rarity"], FG)
        top = tk.Frame(d, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(12, 6))
        self._icon_box(top, self._item_photo(it.get("icon_url"), 64), it["rarity"], 64,
                       fallback=it["slot"][:4]).pack(side="left", padx=(0, 12))
        head = tk.Frame(top, bg=PANEL)
        head.pack(side="left", fill="x", expand=True)
        tk.Label(head, text=it["name"], bg=PANEL, fg=rcol, font=ui.F_HEAD, anchor="w").pack(fill="x")
        classes = "all classes" if len(it["classes"]) == 4 else ", ".join(it["classes"])
        tk.Label(head, text=f"{it['rarity']} {it['slot']} · {classes}", bg=PANEL, fg=MUTED, font=ui.F_SMALL,
                 anchor="w").pack(fill="x")
        if it["flavor"]:
            ui.autowrap(tk.Label(head, text=it["flavor"], bg=PANEL, fg=MUTED, font=("Segoe UI", 9, "italic"),
                                 anchor="w", justify="left")).pack(fill="x", pady=(2, 0))

        def section(title):
            tk.Label(d, text=title, bg=PANEL, fg=ui.ACCENT, font=ui.F_LABEL, anchor="w").pack(
                fill="x", padx=14, pady=(10, 2))

        def text(s, fg=FG, pad=(0, 0)):
            ui.autowrap(tk.Label(d, text=s, bg=PANEL, fg=fg, font=ui.F_SMALL, anchor="w", justify="left"),
                        30).pack(fill="x", padx=14, pady=pad)

        def table(cols, rows, widths):
            g = tk.Frame(d, bg=PANEL)
            g.pack(fill="x", padx=14, pady=(2, 2))
            for j, (c, w) in enumerate(zip(cols, widths)):
                g.columnconfigure(j, weight=w)
                tk.Label(g, text=c, bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w" if j == 0 else "e",
                         padx=6).grid(row=0, column=j, sticky="ew")
            for i, r in enumerate(rows, 1):
                bg = "#272a32" if i % 2 else PANEL  # zebra rows like the tables
                for j, v in enumerate(r):
                    tk.Label(g, text=v, bg=bg, fg=FG, font=ui.F_SMALL, anchor="w" if j == 0 else "e", padx=6,
                             wraplength=420 if j == 0 else 0, justify="left").grid(
                        row=i, column=j, sticky="nsew", ipady=1)

        if it["effect"] or it["ability_levels"]:
            section("Unique effect")
            if it["effect"]:
                text(it["effect"])
            if it["ability_levels"]:
                text(it["ability_levels"])
        if it["rarity"] == "Divine":
            section("Fixed numbers (Divine items do not roll)")
            if it["weapon"]:
                w = it["weapon"]
                text(f"Weapon damage {w.get('damage', '-')} · speed {w.get('speed', '-')} · DPS {w.get('dps', '-')}")
            if it["fixed"]:
                table(["Attribute", "Value"], it["fixed"], (3, 1))
            if not it["weapon"] and not it["fixed"]:
                text("No numbers – a Back item carries no base stat and no attributes.", MUTED)
        else:
            if it["base"]:
                section("Base value by item level (roll 0.85–1.15)")
                table(it["base"]["columns"], it["base"]["rows"], (1,) * len(it["base"]["columns"]))
            if it["attributes"]:
                cols = it.get("attribute_columns") or ["Attribute", "", ""]
                section("Attribute ranges")
                table(cols, it["attributes"], (3, 1, 1))
            pool = it["pool"]
            if pool["primary"] or pool["secondary"]:
                section("Attribute pool – 4 primary + 1 secondary")
                mine = self.var_db_class.get()
                if mine == "All classes" and self.state.hero in it["classes"]:
                    mine = self.state.hero
                text("Primary: " + ", ".join(pool["primary"]))
                for cl, extra in pool["classes"].items():
                    text(f"{cl}: also " + ", ".join(extra), FG if cl == mine else MUTED)
                if pool["secondary"]:
                    text("Secondary: " + ", ".join(pool["secondary"]))
            elif pool["note"]:
                section("Attributes")
                text(pool["note"], MUTED)
            bm = it["info"].get("Can be Black Mist", "")
            anc = it["info"].get("Can be Ancient", "")
            if bm.startswith("Yes") or anc.startswith("Yes"):
                section("Ancient and Black Mist")
                if anc.startswith("Yes"):
                    text(f"Ancient: {anc.split(',', 1)[-1].strip()} of drops from level-70 enemies and chests – "
                         f"top roll on every number except weapon speed; twice the upgrade gold and sell price.")
                if bm.startswith("Yes"):
                    text(f"Black Mist: {bm.split(',', 1)[-1].strip()} on Nightmare and Inferno. "
                         + (it["black_mist"] or "Item level 850 at the top base value.")
                         + " No sockets; Lady “Shadow” reveals the attributes, each a pick between 2 at the top "
                           "roll. Tradable until its first reveal.")
        section("Where it drops")
        for title, body in it["where"]:
            tk.Label(d, text=title, bg=PANEL, fg=FG, font=ui.F_LABEL, anchor="w").pack(fill="x", padx=14,
                                                                                      pady=(4, 0))
            if body:
                text(body, MUTED)
        info = it["info"]
        facts = [f"{k}: {info[k]}" for k in ("Required level", "Sockets", "Upgradable", "Tradable") if k in info]
        if facts:
            section("Item")
            text(" · ".join(facts), MUTED, pad=(0, 12))

    # -- single runs: view, delete, clear a stage ---------------------------------
    def _stage_char(self):
        return self.cfg.get("profile") or self.state.char_name

    def _stages_changed(self):
        """Stage statistics were edited: save and redraw the Stages page (and an open runs window)."""
        self.stage_stats.save()
        self._stage_sig = None
        self._aggregate_stages()
        if getattr(self, "_runs_win", None) is not None and self._runs_win.winfo_exists():
            self._runs_fill()

    def _stage_clear(self, stage, diff):
        n = self.stage_stats.data.get(self.stage_stats.key(self._stage_char(), stage, diff), {}).get("runs", 0)
        if not messagebox.askyesno(
                "Clear stage data",
                f"Delete all {n} recorded runs of {stage} on {diff} for {self._stage_char()}?\n\n"
                f"The stage starts again with no data; new runs are counted as usual. This cannot be undone.",
                icon="warning", parent=self.root):
            return
        self.stage_stats.clear(self._stage_char(), stage, diff)
        self.lbl_stage_detail.configure(text=f"{stage} · {diff}: data cleared.")
        self.stage_btns.pack_forget()
        self._stages_changed()

    def show_runs(self, stage=None, diff=None):
        """Window with every recorded run (one by one), to look at them and delete single runs."""
        if getattr(self, "_runs_win", None) is not None and self._runs_win.winfo_exists():
            w = self._runs_win
            w.lift()
        else:
            w = tk.Toplevel(self.root, bg=BG)
            self._runs_win = w
            w.title("Runs – Deskrawl Tracker")
            w.transient(self.root)
            w.geometry("980x600")
            w.minsize(640, 360)
            hdr = tk.Frame(w, bg=BG)
            hdr.pack(fill="x", padx=16, pady=(12, 6))
            tk.Label(hdr, text="Runs", bg=BG, fg=FG, font=ui.F_HEAD).pack(side="left")
            ui.info(hdr, "Every run counted in the stage statistics of this character, newest first. Select one "
                         "or more runs (Ctrl/Shift + click) and delete them: they are taken out of the stage "
                         "statistics. “Clear stage” removes all data of one stage and difficulty.\n\nRuns "
                         "recorded before version 1.0.10 are only in the totals – they cannot be listed one by "
                         "one and go only with “Clear stage”.").pack(side="left", padx=(6, 0))
            bar = tk.Frame(w, bg=BG)
            bar.pack(fill="x", padx=16, pady=(0, 6))
            tk.Label(bar, text="Stage", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
            self.var_runs_stage = tk.StringVar(value="All stages")
            self.cb_runs_stage = ttk.Combobox(bar, textvariable=self.var_runs_stage, width=38, state="readonly")
            self.cb_runs_stage.pack(side="left", padx=(6, 12))
            self.cb_runs_stage.bind("<<ComboboxSelected>>", lambda _: self._runs_fill())
            self.lbl_runs_info = tk.Label(bar, text="", bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
            self.lbl_runs_info.pack(side="left", fill="x", expand=True)
            cols = [("t", "Finished", 120, "w"), ("stage", "Stage", 170, "w"), ("diff", "Diff", 54, "w"),
                    ("run", "Run time", 62, "e"), ("cyc", "Cycle", 56, "e"), ("xp", "EXP", 70, "e"),
                    ("gold", "Gold", 60, "e"), ("it", "Items", 46, "e"), ("leg", "Leg.", 40, "e"),
                    ("dead", "Died", 40, "center"), ("dps", "DPS", 64, "e")]
            f, self.runs_tree = self._tree(w, cols, 14)
            self.runs_tree.configure(selectmode="extended")
            self.runs_tree.tag_configure("died", foreground=C_BAD)
            f.pack(fill="both", expand=True, padx=16, pady=(0, 8))
            self.runs_tree.bind("<<TreeviewSelect>>", lambda _e: self._runs_buttons())
            self.runs_tree.bind("<Delete>", lambda _e: self._runs_delete())
            btns = tk.Frame(w, bg=BG)
            btns.pack(fill="x", padx=16, pady=(0, 14))
            ui.button(btns, "Close", w.destroy).pack(side="right")
            self.btn_runs_clear = ui.button(btns, "Clear stage…", self._runs_clear)
            self.btn_runs_clear.pack(side="right", padx=(0, 8))
            self.btn_runs_del = ui.button(btns, "Delete selected runs", self._runs_delete)
            self.btn_runs_del.pack(side="right", padx=(0, 8))
            self.lbl_runs_sel = tk.Label(btns, text="", bg=BG, fg=MUTED, font=ui.F_SMALL)
            self.lbl_runs_sel.pack(side="left")
        if stage:
            self.var_runs_stage.set(f"{stage} · {diff}")
        self._runs_fill()

    def _runs_filter(self):
        v = self.var_runs_stage.get()
        if v == "All stages" or " · " not in v:
            return None
        stage, diff = v.rsplit(" · ", 1)
        return stage, diff

    def _runs_fill(self):
        char = self._stage_char()
        keys = sorted({(st, df) for st, df, _ in self.stage_stats.runs(char)}
                      | {tuple(k.split("|", 2)[1:]) for k in self.stage_stats.data
                         if k.count("|") == 2 and k.split("|", 1)[0] == char})
        self.cb_runs_stage.configure(values=["All stages"] + [f"{s} · {d}" for s, d in keys])
        flt = self._runs_filter()
        if flt and flt not in keys:
            self.var_runs_stage.set("All stages")
            flt = None
        t = self.runs_tree
        t.delete(*t.get_children())
        self._runs_rows = {}
        rows = [r for r in self.stage_stats.runs(char) if not flt or (r[0], r[1]) == flt]
        for i, (stage, diff, e) in enumerate(rows):
            iid = str(i)
            self._runs_rows[iid] = (stage, diff, e)
            when = datetime.fromtimestamp(e["t"]).strftime("%d.%m. %H:%M:%S") if e.get("t") else "-"
            dps = e["damage"] / e["dmg_seconds"] if e.get("dmg_seconds") else None
            t.insert("", "end", iid=iid, tags=("died",) if e.get("died") else (), values=(
                when, stage, stages.short_difficulty(diff), fmt_dur(e["run_s"]) if e.get("run_s") else "-",
                fmt_dur(e["cycle_s"]), fmt(e["xp"]), fmt(e["gold"] + e.get("sold_gold", 0)), e["items"],
                "-" if e.get("legendaries") is None else e["legendaries"], "✕" if e.get("died") else "",
                fmt(dps) if dps else "-"))
        older = sum(self.stage_stats.untracked(char, s, d) for s, d in ([flt] if flt else keys))
        self.lbl_runs_info.configure(text=f"{len(rows)} runs" + (
            f" · {older} older runs only in the totals" if older else ""))
        self._runs_buttons()

    def _runs_buttons(self):
        n = len(self.runs_tree.selection())
        self.lbl_runs_sel.configure(text=f"{n} selected – Del deletes them" if n else
                                    "Select runs to delete them (Ctrl/Shift + click for several).")
        self.btn_runs_clear.configure(fg=FG if self._runs_filter() else MUTED)

    def _runs_delete(self):
        sel = self.runs_tree.selection()
        if not sel:
            return
        if not messagebox.askyesno("Delete runs", f"Delete {len(sel)} run(s) from the stage statistics?\n\n"
                                                  f"This cannot be undone.", icon="warning", parent=self._runs_win):
            return
        by_stage = {}
        for iid in sel:
            stage, diff, e = self._runs_rows[iid]
            by_stage.setdefault((stage, diff), set()).add(e["id"])
        for (stage, diff), ids in by_stage.items():
            self.stage_stats.remove_runs(self._stage_char(), stage, diff, ids)
        self._stages_changed()

    def _runs_clear(self):
        flt = self._runs_filter()
        if not flt:
            messagebox.showinfo("Clear stage", "Choose a stage in the “Stage” list first.", parent=self._runs_win)
            return
        self._stage_clear(*flt)

    # -- minions -----------------------------------------------------------------
    MN_SORTS = {"Score (mode)": "score", "Damage": "dps", "Survival": "surv", "Farming": "farm"}

    def _build_minions(self, p):
        hdr = ui.page_header(p, "Minions", (
            "What each minion's passives and buff abilities are worth for the logged-in character, worked out "
            "with your character sheet (F9) and the same damage / survival model as the Item Comparer.\n\n"
            "Damage % and Survival %: change against having no minion. Conditional bonuses (“+30% Damage to "
            "Slowed enemies”) count for an estimated share of the time. Abilities that deal damage are listed "
            "but not rated – how the game scales a minion's damage is not documented. Mana bonuses are not "
            "rated either.\n\nOnly one minion is active at a time; minions are unlocked with their Rein."))
        self.var_mn_mode = tk.StringVar(value=self.cfg.get("minion_mode") or self.cfg.get("item_mode", "Balanced"))
        cb = ttk.Combobox(hdr, textvariable=self.var_mn_mode, values=list(item_eval.MODES), width=11,
                          state="readonly")
        cb.pack(side="right")
        cb.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg("minion_mode", self.var_mn_mode.get()),
                                                   self._fill_minions()))
        tk.Label(hdr, text="Mode", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="right", padx=(14, 6))
        bar = tk.Frame(p, bg=BG)
        bar.pack(fill="x", padx=14, pady=(0, 6))
        self.var_mn_search = tk.StringVar(value="")
        self.var_mn_rarity = tk.StringVar(value=self.cfg.get("minion_rarity", "All"))
        self.var_mn_sort = tk.StringVar(value=self.cfg.get("minion_sort", "Score (mode)"))
        tk.Label(bar, text="Search", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        e = tk.Entry(bar, textvariable=self.var_mn_search, width=16, bg=PANEL, fg=FG, insertbackground=FG,
                     relief="flat", font=ui.F_SMALL)
        e.pack(side="left", padx=(6, 12), ipady=3)
        ui.Tooltip(e, "Name, passive or where it drops, e.g. “Lightning”, “Health”, “Frost Dragon”.")
        self.var_mn_search.trace_add("write", lambda *_: self._fill_minions())

        def combo(label, var, values, key, width):
            tk.Label(bar, text=label, bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
            c = ttk.Combobox(bar, textvariable=var, values=values, width=width, state="readonly")
            c.pack(side="left", padx=(6, 12))
            c.bind("<<ComboboxSelected>>", lambda _: (self._set_cfg(key, var.get()), self._fill_minions()))

        combo("Rarity", self.var_mn_rarity, ["All", "Legendary", "Rare", "Uncommon", "Common"], "minion_rarity", 10)
        combo("Sort by", self.var_mn_sort, list(self.MN_SORTS), "minion_sort", 12)
        self.lbl_mn_note = tk.Label(bar, text="", bg=BG, fg=C_MEH, font=ui.F_SMALL, anchor="w")
        self.lbl_mn_note.pack(side="left", fill="x", expand=True)

        cols = [("rank", "#", 30, "e"), ("name", "Minion", 160, "w"), ("rar", "Rarity", 86, "w"),
                ("dps", "Damage", 74, "e"), ("surv", "Survival", 74, "e"), ("farm", "Farming", 74, "e"),
                ("score", "Score", 54, "e"), ("pas", "Passives", 210, "w"), ("src", "Rein drops on", 170, "w")]
        f, self.mn_tree = self._tree(p, cols, 8, icons=True)
        f.pack(fill="x", padx=14, pady=(0, 6))
        for r in ("Legendary", "Rare", "Uncommon", "Common"):
            self.mn_tree.tag_configure(r, foreground=ui.RARITY.get(r, FG))
        self.mn_tree.tag_configure("zero", foreground=MUTED)
        self.mn_tree.bind("<<TreeviewSelect>>", self._minion_select)
        sf = ui.ScrollFrame(p)
        sf.pack(fill="both", expand=True, pady=(0, 8))
        self.mn_detail = ui.card(sf.inner, fill="x", padx=14, pady=(0, 6))
        tk.Label(self.mn_detail, text="Click a minion for its passives, what they are worth and how to get it.",
                 bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x", padx=14, pady=12)
        self._mn_rows, self._mn_rates, self._mn_sel = [], {}, None
        self._fill_minions()

    @staticmethod
    def _minion_where(m) -> list:
        """Stages where the Rein's enemy is met ("Veldak Highlands: 4")."""
        return [s for s in m["sources"] if re.match(r".+: \d+$", s)]

    def _minion_source(self, m):
        if m["obtained"] == "Enemy drop" and m["rein_chance"]:
            where = self._minion_where(m)
            place = where[0] + (f" (+{len(where) - 1})" if len(where) > 1 else "") if where else ""
            return f"{place} · {m['rein_chance']}" if place else m["rein_chance"]
        return m["obtained"] or "-"

    def _fill_minions(self):
        if not hasattr(self, "mn_tree"):
            return
        ctx = self._eval_context()
        mode = self.var_mn_mode.get()
        prof = self._profile()
        self._mn_prof = prof
        self._mn_rates = {m["slug"]: minions.rate(m, ctx, mode, prof) for m in minions.MINIONS}
        known = any(r["known"] for r in self._mn_rates.values())
        if not known:
            self.lbl_mn_note.configure(text="Read your character with F9 first – values need your stats.", fg=C_MEH)
        elif prof["ok"]:
            self.lbl_mn_note.configure(text=f"Measured: {prof['runs']} runs of skill tracking", fg=C_GOOD)
        else:
            self.lbl_mn_note.configure(text="Estimates – turn on skill tracking for values from your build", fg=C_MEH)
        q = self.var_mn_search.get().strip().lower()
        rar = self.var_mn_rarity.get()
        key = self.MN_SORTS.get(self.var_mn_sort.get(), "score")
        rows = []
        for m in minions.MINIONS:
            if rar != "All" and m["rarity"] != rar:
                continue
            if q and q not in " ".join([m["name"], m["species"], m["obtained"], " ".join(m["sources"]),
                                        " ".join(p["text"] for p in m["passives"] + m["abilities"])]).lower():
                continue
            rows.append(m)
        rows.sort(key=lambda m: (-self._mn_rates[m["slug"]][key], m["name"]))
        self._mn_rows = rows
        t = self.mn_tree
        t._sort = None
        for c, title in t._titles.items():
            ttk.Treeview.heading(t, c, text=title)
        t.delete(*t.get_children())
        pc = lambda v: f"{v:+.1f} %" if abs(v) >= 0.05 else "–"
        for i, m in enumerate(rows):
            r = self._mn_rates[m["slug"]]
            passives = " · ".join(p["text"].rstrip(".") for p in m["passives"])
            if m["abilities"]:
                passives += "  + " + m["abilities"][0]["name"]
            tags = (m["rarity"],) if abs(r[key]) >= 0.05 else ("zero",)
            t.insert("", "end", iid=str(i), tags=tags, image=self._item_photo(m.get("icon_url"), 30) or "",
                     values=(i + 1, m["name"], m["rarity"], pc(r["dps"]), pc(r["surv"]), pc(r["farm"]),
                f"{r['score']:.1f}" if abs(r["score"]) >= 0.05 else "–", passives, self._minion_source(m)))
        if self._mn_sel:
            k = next((i for i, m in enumerate(rows) if m["slug"] == self._mn_sel), None)
            if k is not None:
                t.selection_set(str(k))

    def _minion_select(self, _=None):
        sel = self.mn_tree.selection()
        if not sel:
            return
        m = self._mn_rows[int(sel[0])]
        self._mn_sel = m["slug"]
        r = self._mn_rates[m["slug"]]
        d = self.mn_detail
        for w in d.winfo_children():
            w.destroy()
        top = tk.Frame(d, bg=PANEL)
        top.pack(fill="x", padx=14, pady=(12, 4))
        self._icon_box(top, self._item_photo(m.get("icon_url"), 72), m["rarity"], 72,
                       fallback=m["name"][:4]).pack(side="left", padx=(0, 12))
        head = tk.Frame(top, bg=PANEL)
        head.pack(side="left", fill="x", expand=True)
        tk.Label(head, text=m["name"], bg=PANEL, fg=ui.RARITY.get(m["rarity"], FG), font=ui.F_HEAD,
                 anchor="w").pack(fill="x")
        tk.Label(head, text=f"{m['rarity']} {m['species']} minion · carriage capacity {m['capacity']}", bg=PANEL,
                 fg=MUTED, font=ui.F_SMALL, anchor="w").pack(fill="x")
        tk.Label(head, text=("values from your measured build" if r.get("measured") else
                             "values from estimates – skill tracking makes them exact"), bg=PANEL,
                 fg=C_GOOD if r.get("measured") else C_MEH, font=ui.F_SMALL, anchor="w").pack(fill="x")
        nums = tk.Frame(d, bg=PANEL)
        nums.pack(fill="x", padx=14, pady=(2, 6))
        for label, v in (("Damage", r["dps"]), ("Survival", r["surv"]), ("Farming", r["farm"])):
            box = tk.Frame(nums, bg=PANEL)
            box.pack(side="left", padx=(0, 28))
            tk.Label(box, text=f"{v:+.1f} %", bg=PANEL, fg=C_GOOD if v > 0.05 else (C_BAD if v < -0.05 else MUTED),
                     font=ui.F_NUM_M).pack(anchor="w")
            tk.Label(box, text=label, bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack(anchor="w")

        def section(title):
            tk.Label(d, text=title, bg=PANEL, fg=ui.ACCENT, font=ui.F_LABEL, anchor="w").pack(
                fill="x", padx=14, pady=(8, 2))

        def text(s, fg=FG, font=ui.F_SMALL):
            ui.autowrap(tk.Label(d, text=s, bg=PANEL, fg=fg, font=font, anchor="w", justify="left"), 30).pack(
                fill="x", padx=14)

        parts = {k: v for k, v in (r.get("farm_parts") or {}).items() if abs(v) >= 0.05}
        if parts:
            text("Farming: " + " · ".join(f"{v:+.1f} % {k}" for k, v in parts.items()), MUTED)
        section("Passives and ability")
        for src, line, note, rated in r["lines"]:
            text(f"{src} – {line}")
            if note:
                text(f"      {note}", C_GOOD if rated else MUTED)
        if not r["known"]:
            text("Read your character with F9 to see what it is worth for you.", C_MEH)
        section("How to get it")
        text(m["summary"], MUTED)
        where = self._minion_where(m)
        if where:
            text("Rein drops on: " + ", ".join(where) + (f" – {m['rein_chance']} per cleared encounter, all "
                                                         f"difficulties" if m["rein_chance"] else ""), MUTED)
        if m.get("rein_tradable"):
            text(f"Rein tradable: {m['rein_tradable']}", MUTED)
        tk.Frame(d, bg=PANEL, height=10).pack()

    # -- skill tracking page -------------------------------------------------------
    def _profile(self, stage=None, raw=False) -> dict:
        """What the build does, measured by skill tracking (see build_profile)."""
        char = self._stage_char()
        casts, sec, runs = build_profile.measure(self.stage_stats, char, stage)
        slot_of = {a["name"]: a.get("slot") for a in skills.ABILITIES}
        bar = self.skills.bar.slots if self.skills.bar else []
        basic = next((s[0] for s in bar if slot_of.get(s[0]) == "Basic Attack"), None)  # on the skill bar now
        if not basic:
            basic = ((self.cfg.get("ability_setup") or {}).get("Basic Attack") or [None])[0]
        if not basic or basic in ("–", "-"):  # the Basic Attack seen most on the skill bar so far
            basic = None
            allc, _ = self._measured_casts()
            basics = [a for a in allc if slot_of.get(a) == "Basic Attack"]
            basic = max(basics, key=allc.get) if basics else None
        hero = self.state.hero
        names = {talents.key(t): t["name"] for t in talents.tree(hero)} if hero in talents.HEROES else {}
        mine = [names.get(k, k) for k, v in (self.cfg.get("talents_mine") or {}).items() if v]
        aspd = (self.char_stats.get("Attack Speed") or (None,))[0] if self.cfg.get("estimate_basic") else None
        prof = build_profile.profile(casts, sec, runs, mine, aspd, basic, hero)
        return prof if raw else build_profile.apply_overrides(prof, self.cfg.get("rating_overrides") or {})

    def _build_skills(self, p):
        hdr = ui.page_header(p, "Skill Tracking", (
            "Counts which abilities you cast from the skill bar at the bottom of the game (about 15 pictures a "
            "second, costs some CPU). Basic Attacks fire all the time and do not flash, so their number is "
            "estimated from your attack speed.\n\nFrom the counted runs the tracker works out your damage "
            "shares, damage per second (in % weapon damage) and how much of the time enemies are Burning, "
            "Chilled, Vulnerable … – the Minions page and the item rating use these instead of fixed guesses."))
        self.btn_sk_toggle = ui.button(hdr, "", lambda: (self.toggle_skills(), self._skills_header()), accent=True)
        self.btn_sk_toggle.pack(side="right")
        b_read = ui.button(hdr, "Read skill bar", self._sk_read_bar)
        b_read.pack(side="right", padx=(0, 8))
        ui.Tooltip(b_read, "Look at the skill bar in the game now and take the abilities that are on it. The "
                           "tracker also does this at the start of every run, so swapped skills are picked up.")
        sf = ui.ScrollFrame(p)
        sf.pack(fill="both", expand=True)
        body = sf.inner
        live = ui.card(body, fill="x", padx=14, pady=(0, 8))
        self.lbl_sk_status = tk.Label(live, text="", bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_sk_status.pack(fill="x", padx=14, pady=(10, 4))
        self.sk_slots = tk.Frame(live, bg=PANEL)
        self.sk_slots.pack(fill="x", padx=14, pady=(0, 12))
        self._sk_slot_sig = None

        bar = tk.Frame(body, bg=BG)
        bar.pack(fill="x", padx=14, pady=(4, 6))
        tk.Label(bar, text="Your build", bg=BG, fg=FG, font=("Bahnschrift", 12)).pack(side="left")
        tk.Label(bar, text="   Stage", bg=BG, fg=MUTED, font=ui.F_SMALL).pack(side="left")
        self.var_sk_stage = tk.StringVar(value="All stages")
        self.cb_sk_stage = ttk.Combobox(bar, textvariable=self.var_sk_stage, width=30, state="readonly")
        self.cb_sk_stage.pack(side="left", padx=(6, 12))
        self.cb_sk_stage.bind("<<ComboboxSelected>>", lambda _: self._fill_skills())
        b = ui.button(bar, "Use for ratings", self._sk_use_shares, small=True)
        b.pack(side="right")
        b_ed = ui.button(bar, "Edit rating values…", self._sk_edit_values, small=True)
        b_ed.pack(side="right", padx=(0, 8))
        ui.Tooltip(b_ed, "Set damage shares, damage over time, status uptimes or kills per second by hand – they "
                         "replace the measurement in all ratings.")
        ui.Tooltip(b, "Take the measured damage shares as the ability setup the item rating uses (legendary "
                      "effects of single abilities). The Minions page always uses the measurement.")
        cols = [("ab", "Ability", 150, "w"), ("slot", "Slot", 90, "w"), ("el", "Element", 70, "w"),
                ("run", "Casts / run", 76, "e"), ("min", "Casts / min", 76, "e"), ("wd", "% WD / s", 70, "e"),
                ("share", "Damage share", 90, "e")]
        f, self.sk_tree = self._tree(body, cols, 6, icons=True)
        f.pack(fill="x", padx=14, pady=(0, 4))
        self.sk_tree.tag_configure("est", foreground=MUTED)
        self.lbl_sk_profile = ui.autowrap(tk.Label(body, text="", bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w",
                                                   justify="left"), 24)
        self.lbl_sk_profile.pack(fill="x", padx=14, pady=(0, 10))

        tk.Label(body, text="Counted casts", bg=BG, fg=FG, font=("Bahnschrift", 12), anchor="w").pack(fill="x", padx=14)
        cols = [("ab", "Ability", 150, "w"), ("slot", "Slot", 90, "w"), ("runs", "In runs", 70, "e"),
                ("tot", "Total", 60, "e"), ("avg", "Ø / run", 60, "e"), ("min", "Min", 46, "e"), ("max", "Max", 46, "e"),
                ("pm", "/ min", 56, "e")]
        f, self.sk_counted = self._tree(body, cols, 6, icons=True)
        f.pack(fill="x", padx=14, pady=(0, 4))
        self.lbl_sk_counted = tk.Label(body, text="", bg=BG, fg=MUTED, font=ui.F_SMALL, anchor="w")
        self.lbl_sk_counted.pack(fill="x", padx=14, pady=(0, 10))

        tk.Label(body, text="Runs", bg=BG, fg=FG, font=("Bahnschrift", 12), anchor="w").pack(fill="x", padx=14)
        cols = [("t", "Finished", 110, "w"), ("stage", "Stage", 160, "w"), ("diff", "Diff", 50, "w"),
                ("dur", "Run time", 62, "e"), ("casts", "Casts", 420, "w")]
        f, self.sk_runs = self._tree(body, cols, 10)
        f.pack(fill="x", padx=14, pady=(0, 14))
        self._skills_header()
        self._fill_skills()

    def _sk_read_bar(self):
        self.lbl_sk_status.configure(text="reading the skill bar…")

        def work():
            msg = self.skills.read_bar()
            self.root.after(0, lambda: self._sk_read_done(msg))
        threading.Thread(target=work, daemon=True).start()

    def _sk_read_done(self, msg):
        self.lbl_sk_status.configure(text=msg)
        self._sk_slot_sig = None
        self._skills_live(force=True)
        bar = self.skills.bar
        if bar is None or not bar.slots or not getattr(bar, "crops", None):
            return
        # show what was read: the game's own icons, each with the ability it was taken for
        from PIL import Image, ImageTk
        d = tk.Toplevel(self.root, bg=BG)
        d.title("Skill bar")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Label(d, text="Your skill bar", bg=BG, fg=FG, font=ui.F_HEAD).pack(anchor="w", padx=18, pady=(14, 2))
        ui.autowrap(tk.Label(d, text="These are the icons in the game and the abilities the tracker took them for. "
                                     "Correct a wrong name and press “Use these skills” – the tracker keeps the "
                                     "game's icon and recognises the ability by it from now on.",
                             bg=BG, fg=MUTED, font=ui.F_SMALL, justify="left", anchor="w"), 20).pack(fill="x", padx=18)
        row = tk.Frame(d, bg=BG)
        row.pack(padx=18, pady=10)
        hero_ab = [a["name"] for a in skills.ABILITIES if a.get("hero") == self.state.hero] or             [a["name"] for a in skills.ABILITIES]
        vars_, photos = [], []
        for i, (name, *_rest) in enumerate(bar.slots):
            col = tk.Frame(row, bg=PANEL)
            col.pack(side="left", padx=6)
            crop = bar.crops[i] if i < len(bar.crops) else None
            if crop is not None and crop.size:
                img = Image.fromarray(crop[:, :, ::-1]).resize((64, 64), Image.LANCZOS)
                ph = ImageTk.PhotoImage(img)
                photos.append(ph)
                tk.Label(col, image=ph, bg=PANEL).pack(padx=10, pady=(10, 4))
            v = tk.StringVar(value=name)
            vars_.append(v)
            ttk.Combobox(col, textvariable=v, values=hero_ab, width=16, state="readonly").pack(padx=8, pady=(0, 10))
        d._photos = photos  # keep the pictures alive
        bar_btn = tk.Frame(d, bg=BG)
        bar_btn.pack(fill="x", padx=18, pady=(0, 14))

        def use():
            names = [v.get() for v in vars_]
            self.skills.confirm(names)
            self.lbl_sk_status.configure(text="Skill bar: " + ", ".join(names) + " – icons remembered.")
            self._sk_slot_sig = None
            self._skills_live(force=True)
            d.destroy()
        ui.button(bar_btn, "Cancel", d.destroy).pack(side="right")
        ui.button(bar_btn, "Use these skills", use, accent=True).pack(side="right", padx=(0, 8))

    def _skills_header(self):
        on = self.skills.enabled.is_set()
        self.btn_sk_toggle.configure(text="Skill tracking: on" if on else "Skill tracking: off – turn on")

    def _skills_live(self, force=False):
        """Live part (status, skill bar, casts of the running run); called by the refresh loop."""
        if self.nb.current is not self.tab_sk and not force:
            return
        on = self.skills.enabled.is_set()
        if not force:
            self.lbl_sk_status.configure(text=(self.skills.status if on else "Skill tracking is off.")
                                         + ("" if on else "  Turn it on above – then play a few runs."))
        with self.state.lock:
            cur = dict(self.state.current.casts) if self.state.current else {}
        bar = self.skills.bar
        slots = [s[0] for s in bar.slots] if bar else []
        sig = (tuple(slots), tuple(sorted(cur.items())))
        if sig == self._sk_slot_sig:
            return
        self._sk_slot_sig = sig
        for w in self.sk_slots.winfo_children():
            w.destroy()
        info = {a["name"]: a for a in skills.ABILITIES}
        for name in slots or list(cur):
            box = tk.Frame(self.sk_slots, bg=PANEL)
            box.pack(side="left", padx=(0, 18))
            a = info.get(name, {})
            self._icon_box(box, self._item_photo(a.get("icon_url"), 44), "Rare", 44, fallback=name[:3]).pack()
            tk.Label(box, text=name, bg=PANEL, fg=FG, font=ui.F_SMALL).pack()
            tk.Label(box, text=f"{cur.get(name, 0)} casts this run", bg=PANEL, fg=MUTED, font=ui.F_SMALL).pack()
        if not slots and not cur:
            tk.Label(self.sk_slots, text="No skill bar found yet." if on else "", bg=PANEL, fg=MUTED,
                     font=ui.F_SMALL).pack(anchor="w")

    def _fill_skills(self):
        if not hasattr(self, "sk_tree"):
            return
        char = self._stage_char()
        stages_with = sorted({k.split("|", 2)[1] for k, d in self.stage_stats.data.items()
                              if k.count("|") == 2 and k.split("|", 1)[0] == char and d.get("cast_runs")})
        self.cb_sk_stage.configure(values=["All stages"] + stages_with)
        stage = self.var_sk_stage.get()
        stage = None if stage == "All stages" or stage not in stages_with else stage
        prof = self._profile(stage)
        self._sk_counted_fill(stage)
        info = {a["name"]: a for a in skills.ABILITIES}
        t = self.sk_tree
        t.delete(*t.get_children())
        runs = max(prof["runs"], 1)
        for i, (name, x) in enumerate(sorted(prof["abilities"].items(), key=lambda kv: -kv[1]["share"])):
            est = name == prof.get("basic_estimated")
            t.insert("", "end", iid=str(i), image=self._item_photo(info.get(name, {}).get("icon_url"), 30) or "",
                     tags=("est",) if est else (), values=(
                         name + (" (estimated)" if est else ""), x["slot"], x.get("element", ""),
                         f"{x['casts'] / runs:.1f}", f"{x['cps'] * 60:.1f}", f"{x['wd_per_s']:.0f}",
                         f"{x['share'] * 100:.0f} %"))
        if not prof["abilities"]:
            self.lbl_sk_profile.configure(text="No counted runs yet. Turn skill tracking on and play a few runs.")
        else:
            st = [(k, v) for k, v in prof["status"].items() if v["uptime"] >= 0.005]
            parts = (["Your own values are set (Edit rating values) – they replace the measurement."]
                     if prof.get("overridden") else [])
            parts += [f"{prof['runs']} runs · {fmt_dur(prof['seconds'])} fight time · {prof['wd_per_s']:.0f}% weapon "
                     f"damage per second" + ("" if prof["ok"] else " · too little data yet – ratings still use "
                                                                    "estimates"),
                     "Damage by slot: " + " · ".join(f"{k} {v * 100:.0f}%" for k, v in
                                                     sorted(prof["slot_share"].items(), key=lambda kv: -kv[1])),
                     "Damage by element: " + " · ".join(f"{k} {v * 100:.0f}%" for k, v in
                                                        sorted(prof["element_share"].items(), key=lambda kv: -kv[1])),
                     f"Damage over time: {prof['dot_share'] * 100:.0f}%",
                     "Enemies carry: " + (" · ".join(f"{k} {v['uptime'] * 100:.0f}% ({', '.join(v['sources'])})"
                                                     for k, v in st) or "no status from your abilities")]
            if prof.get("basic_estimated"):
                parts.append(f"{prof['basic_estimated']}: Basic Attacks do not flash on the skill bar – estimated "
                             f"from your attack speed ({(self.char_stats.get('Attack Speed') or ('?',))[0]}/s).")
            setup = self.cfg.get("ability_setup") or {}
            ctx = self._eval_context()
            if ctx.sim is not None and stage is None:
                stats = {k: (self.char_stats.get(k) or (0,))[0] for k in combat_sim.STATS}
                cps = ctx.sim.run(stats, ctx.kills_per_s)
                meas = {k: v["cps"] for k, v in prof["abilities"].items()}
                parts.append(f"Combat simulation (game's auto-combat rules, {ctx.kills_per_s:.2f} kills/s fitted to "
                             f"your casts): " + " · ".join(
                                 f"{k} {v * 60:.1f}/min" + (f" (counted {meas[k] * 60:.1f})" if k in meas else "")
                                 for k, v in cps.items())
                             + " – the Item Comparer and Minions use it for Mana on Kill, Mana Regeneration, Max "
                               "Mana, Mana Cost Reduction, Cooldown Reduction and Attack Speed.")
            used = [f"{k}: {v[0]} {float(v[1]):g}%" for k, v in setup.items()
                    if v and v[0] and v[0] not in ("–", "-") and len(v) > 1 and v[1]]
            if used:
                parts.append("Ability setup used by the item rating: " + " · ".join(used))
            self.lbl_sk_profile.configure(text="\n".join(parts))
        r = self.sk_runs
        r.delete(*r.get_children())
        rows = []
        for k, d in self.stage_stats.data.items():
            if k.count("|") == 2 and k.split("|", 1)[0] == char:
                _, stg, diff = k.split("|", 2)
                if stage and stg != stage:
                    continue
                rows += [(stg, diff, e) for e in d.get("log", []) if e.get("casts")]
        rows.sort(key=lambda x: -x[2].get("t", 0))
        for i, (stg, diff, e) in enumerate(rows[:300]):
            when = datetime.fromtimestamp(e["t"]).strftime("%d.%m. %H:%M") if e.get("t") else "-"
            casts = " · ".join(f"{a} {n}" for a, n in sorted(e["casts"].items(), key=lambda kv: -kv[1]))
            r.insert("", "end", iid=str(i), values=(when, stg, stages.short_difficulty(diff),
                                                    fmt_dur(e.get("run_s") or e.get("cycle_s")), casts))
        if not rows:
            r.insert("", "end", iid="none", values=("", "", "", "", "No runs with counted casts yet – runs are kept "
                                                                  "one by one since version 1.0.10."))

    def _sk_counted_fill(self, stage):
        """Table of the casts skill tracking counted, run by run."""
        t = self.sk_counted
        t.delete(*t.get_children())
        cs = build_profile.counted_stats(self.stage_stats, self._stage_char(), stage)
        info = {a["name"]: a for a in skills.ABILITIES}
        rows = sorted(((k, v) for k, v in cs.items() if not k.startswith("_")), key=lambda kv: -kv[1]["avg"])
        for i, (name, x) in enumerate(rows):
            t.insert("", "end", iid=str(i), image=self._item_photo(info.get(name, {}).get("icon_url"), 30) or "",
                     values=(name, info.get(name, {}).get("slot", ""), f"{x['runs']} / {cs['_runs']}", x["total"],
                             f"{x['avg']:.1f}", x["min"], x["max"], f"{x['per_min']:.1f}"))
        self.lbl_sk_counted.configure(
            text=(f"{cs['_runs']} runs watched · {fmt_dur(cs['_seconds'])} run time. Only runs with skill tracking on, "
                  f"counted one by one (since 1.0.10)." if cs["_runs"] else
                  "No watched runs yet – turn skill tracking on and play a few runs."))

    def _sk_edit_values(self):
        """Dialog: set the values the ratings use by hand (damage shares, damage over time, status uptimes,
        kills per second); empty fields keep the measurement."""
        ov = dict(self.cfg.get("rating_overrides") or {})
        raw = self._profile(raw=True)
        d = tk.Toplevel(self.root, bg=BG)
        d.title("Rating values")
        d.transient(self.root)
        d.resizable(False, False)
        tk.Label(d, text="Values for the ratings", bg=BG, fg=FG, font=ui.F_HEAD).pack(anchor="w", padx=18, pady=(14, 2))
        ui.autowrap(tk.Label(d, text="The Item Comparer, Minions and BiS use these. Grey numbers are what skill tracking "
                                     "measured; type a value to use your own instead, leave a field empty to keep the "
                                     "measurement.", bg=BG, fg=MUTED, font=ui.F_SMALL, justify="left", anchor="w"),
                    20).pack(fill="x", padx=18)
        grid = tk.Frame(d, bg=BG)
        grid.pack(fill="x", padx=18, pady=10)
        entries = {}
        row = [0]

        def line(label, key, measured, unit="%"):
            tk.Label(grid, text=label, bg=BG, fg=FG, font=ui.F_SMALL, anchor="w").grid(row=row[0], column=0, sticky="w",
                                                                                     pady=2)
            v = tk.StringVar(value="" if key[1] is None else str(key[1]))
            tk.Entry(grid, textvariable=v, width=8, bg=PANEL, fg=FG, insertbackground=FG, relief="flat",
                     font=ui.F_SMALL, justify="right").grid(row=row[0], column=1, padx=8, ipady=2)
            tk.Label(grid, text=f"{unit}   measured {measured}", bg=BG, fg=MUTED, font=ui.F_SMALL,
                     anchor="w").grid(row=row[0], column=2, sticky="w")
            entries[key[0]] = v
            row[0] += 1

        def head(text):
            tk.Label(grid, text=text, bg=BG, fg=ui.ACCENT, font=ui.F_LABEL, anchor="w").grid(
                row=row[0], column=0, columnspan=3, sticky="w", pady=(8, 2))
            row[0] += 1

        head("Damage share of each ability")
        names = list(dict.fromkeys(list(raw["abilities"]) + [s[0] for s in (self.skills.bar.slots if self.skills.bar else [])]
                                   + list((ov.get("shares") or {}).keys())))
        for n in names:
            m = raw["abilities"].get(n, {}).get("share")
            line(n, (("share", n), (ov.get("shares") or {}).get(n)), f"{m * 100:.0f}%" if m is not None else "–")
        head("Damage over time and how often enemies are …")
        line("Damage over time", (("dot", None), ov.get("dot_share")), f"{raw['dot_share'] * 100:.0f}%")
        for c in ("Burning", "Slowed", "Vulnerable", "Poisoned", "Bleeding", "Stunned", "Immobilized"):
            line(c, (("cond", c), (ov.get("conditions") or {}).get(c)),
                 f"{raw['condition'].get(c, 0) * 100:.0f}%")
        head("Combat simulation")
        sim = self._combat_sim([s[0] for s in (self.skills.bar.slots if self.skills.bar else [])],
                               {k: v[0] for k, v in self.char_stats.items()}, raw)
        line("Kills per second", (("kills", None), ov.get("kills_per_s")), f"{sim[1]:.2f}" if sim else "–", unit="")
        est = tk.BooleanVar(value=bool(self.cfg.get("estimate_basic")))
        tk.Checkbutton(d, text="Estimate Basic Attacks from attack speed instead of counting them", variable=est,
                       bg=BG, fg=FG, selectcolor=PANEL, activebackground=BG, activeforeground=FG, font=ui.F_SMALL,
                       highlightthickness=0, bd=0).pack(anchor="w", padx=18)
        btn = tk.Frame(d, bg=BG)
        btn.pack(fill="x", padx=18, pady=(10, 14))

        def num(v):
            try:
                return float(v.get().replace(",", ".")) if v.get().strip() else None
            except ValueError:
                return None

        def save():
            new = {"shares": {}, "conditions": {}}
            for (kind, key), v in entries.items():
                x = num(v)
                if x is None:
                    continue
                if kind == "share":
                    new["shares"][key] = x
                elif kind == "cond":
                    new["conditions"][key] = x
                elif kind == "dot":
                    new["dot_share"] = x
                elif kind == "kills":
                    new["kills_per_s"] = x
            new = {k: v for k, v in new.items() if v not in ({}, None)}
            self._set_cfg("rating_overrides", new)
            self._set_cfg("estimate_basic", bool(est.get()))
            self._ratings_changed()
            d.destroy()

        def reset():
            self._set_cfg("rating_overrides", {})
            self._ratings_changed()
            d.destroy()
        ui.button(btn, "Cancel", d.destroy).pack(side="right")
        ui.button(btn, "Save", save, accent=True).pack(side="right", padx=(0, 8))
        ui.button(btn, "Reset to measured", reset).pack(side="left")

    def _ratings_changed(self):
        self._prof_key = None
        self._sim_key = None
        self._fill_skills()
        self._fill_minions()
        self._fill_eval_tab()

    def _sk_use_shares(self):
        prof = self._profile()
        if not prof["abilities"]:
            return
        setup, specials = {}, ["Special 1", "Special 2"]
        for name, x in sorted(prof["abilities"].items(), key=lambda kv: -kv[1]["share"]):
            label = x["slot"] if x["slot"] in ("Basic Attack", "Strong Attack") else (specials.pop(0) if specials else None)
            if label and label not in setup:
                setup[label] = [name, round(x["share"] * 100)]
        self._set_cfg("ability_setup", setup)
        self._fill_eval_tab()
        self._fill_minions()
        self._fill_skills()

    # -- release notes ---------------------------------------------------------
    def show_changelog(self):
        if getattr(self, "_changelog_win", None) is not None and self._changelog_win.winfo_exists():
            self._changelog_win.lift()
            return
        d = tk.Toplevel(self.root, bg=BG)
        self._changelog_win = d
        d.title("What's new – Deskrawl Tracker")
        d.transient(self.root)
        d.geometry("660x640")
        d.minsize(420, 300)
        hdr = tk.Frame(d, bg=BG)
        hdr.pack(fill="x", padx=18, pady=(14, 4))
        tk.Label(hdr, text="What's new", bg=BG, fg=FG, font=ui.F_HEAD).pack(side="left")
        src = tk.Label(hdr, text="", bg=BG, fg=MUTED, font=ui.F_SMALL)
        src.pack(side="right")
        box = tk.Frame(d, bg=PANEL)
        box.pack(fill="both", expand=True, padx=18, pady=(4, 8))
        txt = tk.Text(box, bg=PANEL, fg=FG, relief="flat", wrap="word", font=ui.F_BODY, padx=14, pady=10,
                      highlightthickness=0, cursor="arrow", spacing1=1, spacing3=1)
        sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        txt.pack(side="left", fill="both", expand=True)
        txt.tag_configure("ver", font=("Bahnschrift SemiBold", 14), foreground=ui.ACCENT, spacing1=14)
        txt.tag_configure("date", font=ui.F_SMALL, foreground=MUTED)
        txt.tag_configure("cur", font=ui.F_LABEL, foreground=C_GOOD)
        txt.tag_configure("new", font=ui.F_LABEL, foreground=ui.ACCENT)
        txt.tag_configure("sec", font=ui.F_LABEL, foreground=FG, spacing1=8)
        txt.tag_configure("item", lmargin1=8, lmargin2=26)
        txt.tag_configure("sub", lmargin1=28, lmargin2=44)
        txt.tag_configure("para", foreground=MUTED, spacing1=6)
        bar = tk.Frame(d, bg=BG)
        bar.pack(fill="x", padx=18, pady=(0, 14))
        ui.button(bar, "Close", d.destroy).pack(side="right")
        ui.button(bar, "Open on GitHub", lambda: __import__("webbrowser").open(
            f"https://github.com/{updater.REPO}/releases")).pack(side="right", padx=(0, 8))

        def clean(s):
            return re.sub(r"[*`]", "", s).strip()

        def render(releases, where):
            if not d.winfo_exists():
                return
            txt.configure(state="normal")
            txt.delete("1.0", "end")
            if not releases:
                txt.insert("end", "No release notes found.", "para")
            for i, r in enumerate(releases):
                v = updater.parse(r["tag"])
                txt.insert("end", ("\n" if i else "") + r["tag"], "ver")
                if r["date"]:
                    txt.insert("end", f"   {r['date']}", "date")
                if v == updater.parse(VERSION):
                    txt.insert("end", "   installed", "cur")
                elif v > updater.parse(VERSION):
                    txt.insert("end", "   not installed yet – Check for updates", "new")
                txt.insert("end", "\n")
                for line in r["notes"].splitlines():
                    if not line.strip():
                        continue
                    if line.startswith("#"):
                        txt.insert("end", clean(line.lstrip("#")) + "\n", "sec")
                    elif re.match(r"\s{2,}[-*] ", line):
                        txt.insert("end", "◦  " + clean(line.strip()[2:]) + "\n", "sub")
                    elif re.match(r"[-*] ", line):
                        txt.insert("end", "•  " + clean(line[2:]) + "\n", "item")
                    else:
                        txt.insert("end", clean(line) + "\n", "para")
            txt.configure(state="disabled")
            src.configure(text=where)

        render(changelog.local(), "notes shipped with this version · loading from GitHub…")

        def fetch():
            rel = changelog.online()
            self.root.after(0, lambda: render(rel, "from GitHub") if rel
                            else src.configure(text="GitHub not reachable – notes shipped with this version"))
        threading.Thread(target=fetch, daemon=True).start()

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
                errlog.log.error("update failed", exc_info=True)
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
        if hasattr(self, "btn_sk_toggle"):
            self._skills_header()

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
        if st.paragon.changed:  # Paragon is shared by all characters: top level of the config
            st.paragon.changed = False
            self.cfg["paragon"] = st.paragon.to_dict()
            save_config(self.cfg)
        e = st.level_eta()
        if e and e.get("unknown"):
            self.lbl_lvl_eta.configure(text="-")
            self.lbl_lvl_title.configure(text="to the next Paragon level")
            self.lbl_lvl_runs.configure(text="")
            self.lvl_bar.delete("all")
            self.lbl_lvl_sub.configure(
                text="Paragon: no level-70 run in the log yet. After the first run (or once “Lv. 70 (N)” at "
                     "the bottom left of the game has been read) the Paragon progress shows here.")
            return
        if not e:
            self.lbl_lvl_eta.configure(text="-")
            self.lbl_lvl_runs.configure(text="")
            self.lbl_lvl_sub.configure(text="Waiting for the login line in the log – restart the game once.")
            return
        if e.get("paragon"):
            self.lbl_lvl_title.configure(text=f"to Paragon {e['level'] + 1}")
        else:
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
        head = f"Paragon {e['level']}: " if e.get("paragon") else ""
        tail = ""
        if e.get("paragon"):
            tail = (f"   ·   total {fmt(e['total'])} Paragon EXP from the log" if e["complete"] else
                    "   ·   estimate: the log starts after level 70, counted from the start of the Paragon "
                    "level shown in the game")
        self.lbl_lvl_sub.configure(
            text=f"{head}{approx}{fmt(e['xp'])} of {fmt(e['need'])} EXP ({frac * 100:.1f} %), "
                 f"{approx}{fmt(e['left'])} to go   ·   by EXP per hour: {fmt_dur(eta_rate) if eta_rate else '-'}"
                 + tail)

    def tick(self):
        if self.state.ui_busy():  # window is being moved or resized: keep the UI thread free
            self.root.after(150, self.tick)
            return
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

        self._skills_live()
        self.lbl_hero.configure(text=st.char_name)
        pg = f" · Paragon {st.paragon.level}" if st.level >= 70 and st.paragon.level else ""
        self.lbl_hero_sub.configure(text=f"{st.hero}, Level {st.level}{pg}"
                                    + (f", {st.stage_name}" if st.stage_name else ""))
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
        self.dots["panels"].set("ok" if " read" in ps or "not needed" in ps or "end screen" in ps else ("warn" if ps != "not read yet" else None), ps)
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
                    fmt_dur(r.game_seconds or r.duration), fmt(r.xp), fmt(r.gold), r.items,
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
    root.report_callback_exception = lambda *exc: errlog.uncaught(*exc)
    App(root)
    root.mainloop()


if __name__ == "__main__":
    errlog.setup()
    errlog.log.info(f"Deskrawl Tracker {VERSION} started")
    main()

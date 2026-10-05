"""Per-stage farming statistics (persistent) and a forecast for the next difficulty.

Difficulty rules (wikily.gg/deskrawl/difficulty):
  Normal     health x1,   damage x1,    XP x1
  Nightmare  health x3.5, damage x1.35, XP x1.5, all enemies level 70, +0.5 % gold & item find per map level
  Inferno    health x6,   damage x1.8,  XP x2,   all enemies level 70, +1 % gold & item find per map level
  Enemy scaling per level (51-70): health x1.06, damage x1.03 per level.
"""
import paths
import json
import os

STATS_PATH = paths.user("stage_stats.json")
ENEMIES_PATH = paths.res("data", "enemies.json")

DIFFICULTY = {
    "Normal": {"hp": 1.0, "dmg": 1.0, "xp": 1.0, "find_per_lvl": 0.0},
    "Nightmare": {"hp": 3.5, "dmg": 1.35, "xp": 1.5, "find_per_lvl": 0.5},
    "Inferno": {"hp": 6.0, "dmg": 1.8, "xp": 2.0, "find_per_lvl": 1.0},
}
NEXT = {"Normal": "Nightmare", "Nightmare": "Inferno"}
FIGHT_SHARE = 0.7  # share of a run spent killing (rest: walking, waiting for waves) - estimate


def level_factor(level_from: int, level_to: int, per_level: float) -> float:
    return per_level ** max(level_to - level_from, 0)


class StageStats:
    FIELDS = ("runs", "seconds", "xp", "gold", "sold_gold", "items", "deaths", "damage", "dmg_seconds")

    def __init__(self):
        self.data = {}
        try:
            with open(STATS_PATH, encoding="utf-8") as f:
                self.data = json.load(f)
        except Exception:
            pass

    def save(self):
        try:
            with open(STATS_PATH, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=1, ensure_ascii=False)
        except Exception:
            pass

    @staticmethod
    def key(stage: str, difficulty: str) -> str:
        return f"{stage}|{difficulty}"

    def add_run(self, stage: str, difficulty: str, cycle_s: float, xp: int, gold: int, sold_gold: int,
                items: int, died: bool, damage: float, dmg_seconds: float):
        d = self.data.setdefault(self.key(stage, difficulty), {f: 0 for f in self.FIELDS})
        for f, v in (("runs", 1), ("seconds", cycle_s), ("xp", xp), ("gold", gold), ("sold_gold", sold_gold),
                     ("items", items), ("deaths", int(died)), ("damage", damage), ("dmg_seconds", dmg_seconds)):
            d[f] = d.get(f, 0) + v

    def rows(self) -> list:
        out = []
        for k, d in self.data.items():
            stage, diff = k.rsplit("|", 1)
            h = d["seconds"] / 3600 if d["seconds"] else None
            out.append({
                "stage": stage, "difficulty": diff, "runs": d["runs"],
                "avg_s": d["seconds"] / d["runs"] if d["runs"] else None,
                "xp_h": d["xp"] / h if h else 0, "gold_h": (d["gold"] + d["sold_gold"]) / h if h else 0,
                "items_h": d["items"] / h if h else 0, "death_rate": d["deaths"] / d["runs"] if d["runs"] else 0,
                "xp_run": d["xp"] / d["runs"] if d["runs"] else 0,
                "dps": d["damage"] / d["dmg_seconds"] if d.get("dmg_seconds") else None,
            })
        return out


# ----------------------------------------------------------------------------- enemy data (item 6)

def load_enemies() -> dict:
    try:
        with open(ENEMIES_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def stage_info(stage_label: str, enemies: dict) -> dict | None:
    """Find the stage in data/enemies.json by its log name ("The Cinder Crown: 6")."""
    if not enemies or not stage_label:
        return None
    name, _, num = stage_label.partition(":")
    name, num = name.strip().lower(), num.strip()
    best = None
    for st in enemies.get("stages", []):
        region = (st.get("region") or "").lower()
        sname = str(st.get("stage") or "").lower()
        if name and (name == region or name == sname or name in region or name in sname):
            if num and str(st.get("stage_no")) == num:
                return st
            best = best or st
    return best


def damage_profile(stage: dict | None) -> dict:
    """Normalized share per element from a stage entry ({"Fire": 0.6, ...})."""
    if not stage or not stage.get("damage_types"):
        return {}
    dt = {k.capitalize(): float(v) for k, v in stage["damage_types"].items() if v}
    tot = sum(dt.values())
    return {k: v / tot for k, v in dt.items()} if tot else {}


# ----------------------------------------------------------------------------- forecast (item 3)

def forecast(row: dict, stage_level: int | None) -> dict | None:
    """What the same stage would give on the next difficulty, at today's gear."""
    cur = row["difficulty"]
    nxt = NEXT.get(cur)
    if not nxt or not row.get("avg_s") or not row["runs"]:
        return None
    a, b = DIFFICULTY[cur], DIFFICULTY[nxt]
    lvl_from = stage_level if (cur == "Normal" and stage_level) else 70
    hp = b["hp"] / a["hp"] * level_factor(lvl_from, 70, 1.06)
    dmg = b["dmg"] / a["dmg"] * level_factor(lvl_from, 70, 1.03)
    run_s = row["avg_s"] * (FIGHT_SHARE * hp + (1 - FIGHT_SHARE))
    xp_run = row["xp_run"] * b["xp"] / a["xp"]
    find = (1 + b["find_per_lvl"] * 70 / 100) / (1 + a["find_per_lvl"] * 70 / 100)
    return {"difficulty": nxt, "hp_factor": hp, "dmg_factor": dmg, "run_s": run_s,
            "xp_h": xp_run / run_s * 3600, "gold_h": row["gold_h"] * find * row["avg_s"] / run_s,
            "find_factor": find, "stage_level_known": stage_level is not None or cur != "Normal"}

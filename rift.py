"""Mythic Rift: tiers 1-100 on Inferno, what each tier asks of the character and what it pays.

Data: data/rift.json (wiki): enemy health and damage, gold and experience multipliers per tier, the boss, the
entry cost (3 Skull of Inferno per completed run) and the boss enrage (from tier 75 after 15 minutes).

Your own rift runs are recorded like stages ("Mythic Rift: 37"). From one tier you have played, the others are
forecast: the fight time grows with the enemies' health (your damage stays the same), the rewards with the gold
and experience multipliers, the danger with the enemies' damage. The fixed time of a run (the first encounter
after 1.5 s, the next ones 2.5 s after the last kill) does not grow.
"""
import json
import math
import re

import paths

STAGE = "Mythic Rift"
_DATA = None


def data() -> dict:
    global _DATA
    if _DATA is None:
        try:
            with open(paths.res("data", "rift.json"), encoding="utf-8") as f:
                _DATA = json.load(f)
        except Exception:
            _DATA = {"points": {}, "tiers": 100}
    return _DATA


def factor(kind: str, tier: int) -> float | None:
    """A multiplier at a tier; between the listed tiers health/damage grow geometrically, gold/experience linearly."""
    pts = sorted((int(k), float(v)) for k, v in data().get("points", {}).get(kind, {}).items())
    if not pts:
        return None
    tier = min(max(int(tier), pts[0][0]), pts[-1][0])
    for (t0, v0), (t1, v1) in zip(pts, pts[1:]):
        if t0 <= tier <= t1:
            x = (tier - t0) / (t1 - t0) if t1 > t0 else 0.0
            if kind in ("gold", "xp"):
                return v0 + (v1 - v0) * x
            return math.exp(math.log(v0) + (math.log(v1) - math.log(v0)) * x)
    return pts[-1][1]


def tier_of(stage: str) -> int | None:
    m = re.match(rf"{STAGE}\s*:\s*(\d+)", stage or "")
    return int(m.group(1)) if m else None


def is_rift(stage: str) -> bool:
    return (stage or "").startswith(STAGE)


def forecast(ref: dict, tiers=range(1, 101)) -> list:
    """Forecast of every tier from a played tier. ref: a stage row of a rift tier (stages.StageStats.rows)."""
    d = data()
    t_ref = tier_of(ref["stage"])
    if not t_ref or not ref.get("runs"):
        return []
    over = float(d.get("spawn_overhead_s", 24.0))
    run_s = ref.get("run_s") or ref.get("avg_s") or 0.0
    cycle_s = ref.get("avg_s") or run_s
    between = max(cycle_s - run_s, 0.0)  # time between runs (town, loading)
    fight = max(run_s - over, 1.0)
    h0, x0, g0, dm0 = (factor(k, t_ref) for k in ("health", "xp", "gold", "damage"))
    out = []
    enr = d.get("enrage", {})
    for t in tiers:
        h, x, g, dm = (factor(k, t) for k in ("health", "xp", "gold", "damage"))
        f_s = fight * h / h0
        r_s = over + f_s
        c_s = r_s + between
        xp_run = ref["xp_run"] * x / x0
        gold_run = (ref["gold_h"] * cycle_s / 3600) * g / g0 if cycle_s else 0.0
        out.append({
            "tier": t, "run_s": r_s, "xp_h": xp_run * 3600 / c_s, "gold_h": gold_run * 3600 / c_s,
            "health": h / h0, "damage": dm / dm0, "skulls_h": d.get("entry", {}).get("per_run", 3) * 3600 / c_s,
            "enrage": t >= enr.get("from_tier", 75) and r_s > enr.get("after_s", 900) * 0.8,
            "played": t == t_ref,
        })
    return out


def recommend(rows: list, max_damage: float = 1.5, max_run_s: float = 600.0, key: str = "xp_h") -> dict | None:
    """Best tier of a forecast: most EXP (or gold) per hour with enemies hitting at most max_damage times as hard as
    on the tier you played, a run shorter than max_run_s and no boss enrage."""
    ok = [r for r in rows if r["damage"] <= max_damage and r["run_s"] <= max_run_s and not r["enrage"]]
    return max(ok, key=lambda r: r[key]) if ok else None

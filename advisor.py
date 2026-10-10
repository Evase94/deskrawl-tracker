"""Where to farm: every stage (and Mythic Rift tier) you have played, ranked by what matters to you.

Score = weighted share of the best value among your stages for EXP per hour, gold per hour and legendaries per hour,
minus a penalty for deaths (safety). A stage with few runs counts less sure: its values are pulled towards the
average of all stages until it has MIN_SURE runs, so one lucky run does not win.
"""
import rift
import stages

MIN_SURE = 5     # runs from which a stage's own values count fully
DEATH_SCALE = 4  # a 25 % death rate costs the whole safety weight


def rank(rows: list, w_xp: float, w_gold: float, w_leg: float, w_safe: float, min_runs: int = 1,
         boss_info: dict | None = None) -> list:
    """rows: stages.StageStats.rows(). -> [{row, score, parts, notes}] best first."""
    rows = [r for r in rows if r.get("runs", 0) >= min_runs and not r["stage"].startswith("Unknown")]
    if not rows:
        return []
    keys = {"xp_h": w_xp, "gold_h": w_gold, "leg_h": w_leg}
    avg = {k: sum((r.get(k) or 0.0) for r in rows) / len(rows) for k in keys}
    runs_all = sum(r["runs"] for r in rows) or 1
    avg_death = sum(r.get("death_rate", 0.0) * r["runs"] for r in rows) / runs_all  # your usual death rate
    shrunk = []
    for r in rows:
        f = min(r["runs"] / MIN_SURE, 1.0)
        v = {k: f * (r.get(k) or 0.0) + (1 - f) * avg[k] for k in keys}
        v["death_rate"] = f * r.get("death_rate", 0.0) + (1 - f) * avg_death  # few runs: your usual rate
        shrunk.append((r, v))
    best = {k: max(v[k] for _, v in shrunk) or 1.0 for k in keys}
    total_w = sum(keys.values()) or 1.0
    out = []
    for r, v in shrunk:
        parts = {k: v[k] / best[k] for k in keys}
        score = sum(w * parts[k] for k, w in keys.items()) / total_w * 100
        penalty = min(v["death_rate"] * DEATH_SCALE, 1.0) * w_safe / 10 * 100
        notes = []
        info = (boss_info or {}).get(r["stage"].lower())
        if info and info.get("kind") == "Gold":
            notes.append("Gold boss: 1 skull of the difficulty per run")
        if rift.is_rift(r["stage"]):
            notes.append("Mythic Rift: 3 Skull of Inferno per completed run")
        if r["runs"] < MIN_SURE:
            notes.append(f"only {r['runs']} run{'s' if r['runs'] != 1 else ''} – not sure yet")
        if r.get("leg_h") is None:
            notes.append("legendaries not counted here yet")
        out.append({"row": r, "score": score - penalty, "parts": parts, "penalty": penalty, "notes": notes,
                    "difficulty": stages.short_difficulty(r["difficulty"])})
    out.sort(key=lambda x: -x["score"])
    return out


def why(e: dict) -> str:
    """Short reason: what the stage is best at."""
    p = e["parts"]
    names = {"xp_h": "EXP", "gold_h": "gold", "leg_h": "legendaries"}
    top = sorted(p, key=lambda k: -p[k])
    strong = [names[k] for k in top if p[k] >= 0.9]
    txt = ("top " + " + ".join(strong)) if strong else f"{p[top[0]] * 100:.0f} % of your best {names[top[0]]}"
    if e["penalty"] >= 5 and e["row"].get("death_rate", 0) > 0:
        txt += f" · deaths cost {e['penalty']:.0f} points"
    return txt

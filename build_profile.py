"""What the character's build actually does, measured by the skill tracking.

From the ability casts counted per run (stage statistics) and the run times:
  - casts per second of every ability,
  - damage per second in "weapon damage %" (casts x damage % x hits x targets, level-1 values),
  - share of damage from Basic Attack, Strong Attack and the special abilities,
  - share of damage dealt over time,
  - how much of the time enemies carry each status (Burn, Chill, Vulnerable ...): casts per second x
    the status duration x the share of enemies the ability reaches, combined over all abilities.
Minion and item ratings use this instead of fixed guesses when skill tracking has measured enough.
"""
import json
import re

import paths
import skills

MIN_SECONDS = 120  # less measured fight time than this: too little to rely on


def _statuses() -> dict:
    try:
        with open(paths.res("data", "status_effects.json"), encoding="utf-8") as f:
            return {s["name"]: s for s in json.load(f)["status_effects"]}
    except Exception:
        return {}


STATUSES = _statuses()

# the conditions bonuses name ("Damage to Slowed enemies") -> statuses that cause them
CONDITIONS = {
    "Burning": ["Burn"], "Burned": ["Burn"], "Poisoned": ["Poisoned"], "Bleeding": ["Bleeding"],
    "Vulnerable": ["Vulnerable"], "Slowed": ["Chill", "Dazed", "Frozen"], "Chilled": ["Chill"],
    "Stunned": ["Stunned", "Short Stun", "Frozen"], "Immobilized": ["Frozen", "Stunned"],
    "Frozen": ["Frozen"],
}
AREA = re.compile(r"all enemies|every enemy|nearby enemies|in its path|in front|in a (?:small )?area|in the "
                  r"blast|in a cone|in a line|caught inside|everything caught|around you", re.I)
TALENT_UPTIME = 0.3  # a talent of your build that applies a status: its share of the time (no data on procs)


def coverage(description: str) -> float:
    """Share of the enemies an ability reaches: areas reach most, single targets a part of a pack."""
    if AREA.search(description or ""):
        return 0.8
    m = re.search(r"(\d+) (?:random |nearby )?enem", description or "")
    if m:
        return min(int(m.group(1)) * 0.25, 0.8)
    return 0.35


def combine(uptimes) -> float:
    """Chance that at least one of several independent sources holds."""
    miss = 1.0
    for u in uptimes:
        miss *= 1 - min(max(u, 0.0), 0.95)
    return 1 - miss


RECENT_RUNS = 40  # the build changes over time: runs recorded one by one count from the newest


def measure(stage_stats, char: str, stage: str | None = None) -> tuple:
    """(casts {ability: n}, fight seconds, runs) of the watched runs of a character (one stage or all).
    Uses the newest RECENT_RUNS runs recorded one by one; older totals only while there are too few."""
    logged = []
    for k, d in stage_stats.data.items():
        if k.count("|") == 2:
            who, st, _diff = k.split("|", 2)
            if who == char and (not stage or st == stage):
                logged += [e for e in d.get("log", []) if e.get("casts")]
    logged.sort(key=lambda e: -e.get("t", 0))
    if len(logged) >= 10:
        casts, seconds = {}, 0.0
        for e in logged[:RECENT_RUNS]:
            for a, n in e["casts"].items():
                casts[a] = casts.get(a, 0) + n
            seconds += e.get("run_s") or e.get("cycle_s") or 0
        return casts, seconds, min(len(logged), RECENT_RUNS)
    casts, seconds, runs = {}, 0.0, 0
    for k, d in stage_stats.data.items():
        if k.count("|") != 2:
            continue
        who, st, _diff = k.split("|", 2)
        if who != char or (stage and st != stage):
            continue
        logged = [e for e in d.get("log", []) if e.get("casts")]
        for e in logged:  # runs recorded one by one: their own run time
            for a, n in e["casts"].items():
                casts[a] = casts.get(a, 0) + n
            seconds += e.get("run_s") or e.get("cycle_s") or 0
            runs += 1
        older = d.get("cast_runs", 0) - len(logged)
        if older > 0:  # older runs only in the totals: their casts minus the logged ones, average run time
            for a, n in d.get("casts", {}).items():
                rest = n - sum(e["casts"].get(a, 0) for e in logged)
                if rest > 0:
                    casts[a] = casts.get(a, 0) + rest
            avg = (d["run_seconds"] / d["timed_runs"]) if d.get("timed_runs") else \
                (d["seconds"] / d["runs"] if d.get("runs") else 0)
            seconds += avg * older
            runs += older
    return casts, seconds, runs


def profile(casts: dict, seconds: float, runs: int = 0, talent_names=(), attack_speed: float | None = None,
            basic_name: str | None = None, hero: str = "") -> dict:
    """attack_speed: attacks per second from the character sheet - Basic Attacks fire constantly and do not
    flash on the skill bar, so their count is estimated from it (basic_name: the Basic Attack in use)."""
    info = {a["name"]: a for a in skills.ABILITIES}
    out = {"seconds": seconds, "runs": runs, "ok": seconds >= MIN_SECONDS and bool(casts),
           "abilities": {}, "wd_per_s": 0.0, "slot_share": {}, "dot_share": 0.0, "status": {}, "condition": {},
           "basic_estimated": None}
    if not casts or seconds <= 0:
        return out
    casts = dict(casts)
    if attack_speed:
        # Basic Attacks fire with every attack and hardly flash on the skill bar: counted casts of them are
        # too low, so they are replaced by attacks per second minus the time the other casts take
        counted_basic = [a for a in casts if info.get(a, {}).get("slot") == "Basic Attack"]
        if not basic_name and counted_basic:
            basic_name = max(counted_basic, key=casts.get)
        if not basic_name:  # the hero's first Basic Attack
            basics = [a for a in skills.ABILITIES if a.get("slot") == "Basic Attack" and (not hero or a["hero"] == hero)]
            basic_name = basics[0]["name"] if basics else None
        if basic_name in info:
            for a in counted_basic:
                casts.pop(a)
            other = sum(casts.values()) / seconds
            per_s = max(attack_speed - other, attack_speed * 0.3)  # other casts take attack time
            casts[basic_name] = per_s * seconds
            out["basic_estimated"] = basic_name
    talents = {t.lower() for t in talent_names}
    total = 0.0
    for name, n in casts.items():
        a = info.get(name)
        if not a:
            continue
        cps = n / seconds
        wd = cps * skills.damage_weight(a)
        total += wd
        out["abilities"][name] = {"casts": n, "cps": cps, "wd_per_s": wd, "slot": a.get("slot") or "Special",
                                  "description": a.get("description", "")}
    out["wd_per_s"] = total
    out["element_share"] = {}
    for name, x in out["abilities"].items():  # element of each ability from its tags (Frost = Cold)
        tags = [("Cold" if t == "Frost" else t) for t in info[name].get("tags", [])]
        el = next((t for t in tags if t in ("Fire", "Cold", "Lightning", "Poison", "Arcane", "Physical")), "Physical")
        x["element"] = el
        out["element_share"][el] = out["element_share"].get(el, 0.0) + (x["wd_per_s"] / total if total else 0.0)
    dot = 0.0
    for name, x in out["abilities"].items():
        x["share"] = x["wd_per_s"] / total if total else 0.0
        slot = x["slot"] if x["slot"] in ("Basic Attack", "Strong Attack") else "Special"
        out["slot_share"][slot] = out["slot_share"].get(slot, 0.0) + x["share"]
        if re.search(r"per tick|over \d+ seconds|each tick", x["description"], re.I):
            dot += x["share"]
    for st_name, st in STATUSES.items():
        dur = st.get("duration_s") or 0
        ups, why = [], []
        for src in st.get("applied_by", []):
            x = out["abilities"].get(src["name"])
            if x and "ability" in src["kind"]:
                u = x["cps"] * dur * coverage(x["description"])
                ups.append(u)
                why.append(f"{src['name']} {min(u, 0.95) * 100:.0f}%")
            elif src["name"].lower() in talents and "talent" in src["kind"]:
                ups.append(TALENT_UPTIME)
                why.append(f"{src['name']} (talent) ~{TALENT_UPTIME * 100:.0f}%")
        out["status"][st_name] = {"uptime": combine(ups), "sources": why, "duration": dur}
    # damage over time from statuses that deal damage
    for st_name in ("Burn", "Poisoned", "Bleeding"):
        dot += 0.1 * out["status"].get(st_name, {}).get("uptime", 0.0)
    out["dot_share"] = min(dot, 0.9)
    for cond, sts in CONDITIONS.items():
        out["condition"][cond] = combine(out["status"].get(s, {}).get("uptime", 0.0) for s in sts)
    return out


def counted_stats(stage_stats, char: str, stage: str | None = None) -> dict:
    """Casts counted by skill tracking, run by run: {ability: {"runs", "total", "avg", "min", "max", "per_min"}}
    plus "_runs" and "_seconds". Only runs that were watched (recorded one by one, with casts)."""
    runs = []
    for k, d in stage_stats.data.items():
        if k.count("|") == 2:
            who, st, _diff = k.split("|", 2)
            if who == char and (not stage or st == stage):
                runs += [e for e in d.get("log", []) if e.get("casts")]
    names = sorted({a for e in runs for a in e["casts"]})
    secs = sum(e.get("run_s") or e.get("cycle_s") or 0 for e in runs)
    out = {"_runs": len(runs), "_seconds": secs}
    for a in names:
        per = [e["casts"].get(a, 0) for e in runs]
        tot = sum(per)
        out[a] = {"runs": sum(1 for n in per if n), "total": tot, "avg": tot / len(runs) if runs else 0,
                  "min": min(per) if per else 0, "max": max(per) if per else 0,
                  "per_min": tot / secs * 60 if secs else 0}
    return out


def apply_overrides(prof: dict, ov: dict) -> dict:
    """Values the player set by hand (Skill Tracking page) replace the measured ones."""
    if not ov:
        return prof
    prof = dict(prof)
    shares = {k: float(v) for k, v in (ov.get("shares") or {}).items() if v not in (None, "")}
    if shares:
        tot = sum(shares.values()) or 1.0
        info = {a["name"]: a for a in skills.ABILITIES}
        abil, slot_share, el_share = {}, {}, {}
        for name, v in shares.items():
            a = info.get(name, {})
            x = dict(prof["abilities"].get(name) or {"casts": 0, "cps": 0.0, "wd_per_s": 0.0,
                                                    "slot": a.get("slot") or "Special",
                                                    "description": a.get("description", "")})
            x["share"] = v / tot
            tags = [("Cold" if t == "Frost" else t) for t in a.get("tags", [])]
            x["element"] = next((t for t in tags if t in ("Fire", "Cold", "Lightning", "Poison", "Arcane",
                                                           "Physical")), x.get("element", "Physical"))
            abil[name] = x
            slot = x["slot"] if x["slot"] in ("Basic Attack", "Strong Attack") else "Special"
            slot_share[slot] = slot_share.get(slot, 0.0) + x["share"]
            el_share[x["element"]] = el_share.get(x["element"], 0.0) + x["share"]
        prof.update(abilities=abil, slot_share=slot_share, element_share=el_share, ok=True)
    if ov.get("dot_share") not in (None, ""):
        prof["dot_share"] = float(ov["dot_share"]) / 100
    cond = {k: float(v) / 100 for k, v in (ov.get("conditions") or {}).items() if v not in (None, "")}
    if cond:
        prof["condition"] = {**prof.get("condition", {}), **cond}
        prof["ok"] = True
    prof["overridden"] = True
    return prof

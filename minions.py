"""Minions: what each minion's passives (and buff abilities) are worth for the current character.

Every passive is turned into stat changes and run through the same damage / survival model as the
item comparison (item_eval). Conditional damage ("+30% Damage to Slowed enemies") counts with an
estimated share of the time the condition holds. Abilities that deal damage are listed but not rated:
how the game scales a minion's damage is not documented.
"""
import json
import re

import item_eval
import paths

# share of the time / damage a condition holds - estimates, like item_eval.UPTIME
COND_UPTIME = {"Healthy": 0.5, "Injured": 0.5, "Distant": 0.5, "Elite and Boss": 0.2, "Elite": 0.2,
               "Slowed": 0.3, "Chilled": 0.3, "Bleeding": 0.25, "Poisoned": 0.25, "Burning": 0.25,
               "Vulnerable": 0.25, "Stunned": 0.1, "Immobilized": 0.15}
DOT_SHARE = 0.3          # share of damage dealt over time
ABILITY_SHARE = 0.5      # share of damage from special abilities
REF = "Damage vs Healthy"  # item_eval models this one with uptime REF_UPTIME; others are scaled onto it
REF_UPTIME = item_eval.UPTIME[REF]


def load() -> list:
    try:
        with open(paths.res("data", "minions.json"), encoding="utf-8") as f:
            data = json.load(f)["minions"]
    except Exception:
        return []
    for m in data:  # entries of minions without a source page section
        m["passives"] = [p for p in m["passives"] if not p["text"].startswith(("Use to permanently", "Enemies",
                                                                                  "Mythic Rift"))
                         and p["name"] not in ("Enemies", "Mythic Rift", "Rein", "Towns")]
    return data


MINIONS = load()


def _cond(deltas, share, pct, label):
    """Damage that applies part of the time: put onto the reference conditional stat."""
    deltas[REF] = deltas.get(REF, 0.0) + pct * share / REF_UPTIME
    return f"+{pct:g}% damage, counted for ~{share * 100:.0f}% of the time ({label})"


def passive_deltas(text: str, base: dict, ctx) -> tuple:
    """(deltas {stat: value}, note, rated) for one passive or buff text."""
    t = re.sub(r"\s*\(the game shows [^)]*\)", "", text).strip().rstrip(".")
    d = {}
    g = lambda k: float(base.get(k, 0.0))
    m = re.match(r"\+?(-?[\d.]+)%\s+(Fire|Cold|Lightning|Poison|Arcane|Physical)\s+Damage$", t, re.I)
    if m:
        el = m.group(2).capitalize()
        d[f"{el} Damage"] = float(m.group(1))
        return d, ("" if el == ctx.elem else f"no effect for a {ctx.elem} build"), el == ctx.elem
    m = re.match(r"\+([\d.]+)%\s+(?:Damage to|damage to)\s+(.+?)\s+enemies$", t)
    if m:
        what = m.group(2)
        if what.startswith("enemies more than") or "units away" in t:
            what = "Distant"
        share = COND_UPTIME.get(what, 0.2)
        return d, _cond(d, share, float(m.group(1)), f"{what} enemies"), True
    m = re.match(r"\+([\d.]+)%\s+damage to enemies more than \d+ units away from you$", t, re.I)
    if m:
        return d, _cond(d, COND_UPTIME["Distant"], float(m.group(1)), "distant enemies"), True
    m = re.match(r"\+([\d.]+)%\s+Damage Over Time$", t, re.I)
    if m:
        return d, _cond(d, DOT_SHARE, float(m.group(1)), "damage over time"), True
    m = re.match(r"\+([\d.]+)%\s+Special Ability (?:Bonus )?damage$", t, re.I)
    if m:
        return d, _cond(d, ABILITY_SHARE, float(m.group(1)), "special abilities"), True
    simple = [
        (r"\+([\d.]+)%\s+Critical Hit Damage", "Critical Hit Damage"),
        (r"\+([\d.]+)%\s+Critical Hit Chance", "Critical Hit Chance"),
        (r"\+([\d.]+)%\s+Attack Speed", "Attack Speed Bonus"),
        (r"\+([\d.]+)%\s+Strong Attack Damage", "Strong Attack Damage"),
        (r"\+([\d.]+)%\s+Basic Attack Damage", "Basic Attack Damage"),
        (r"\+([\d.]+)%\s+All Damage", "All Damage"),
        (r"\+([\d.]+)%\s+Cooldown Reduction", "Cooldown Reduction"),
        (r"\+([\d.]+)%\s+Armor", "Bonus Armor"),
        (r"\+([\d.]+)%\s+Max(?:imum)? Health", "Bonus Health"),
        (r"Increase ([\d.]+) Max HP", "Max Health"),
        (r"\+([\d.]+)%\s+Dodge(?: Chance)?", "Dodge Chance"),
        (r"\+([\d.]+)\s+Life On Hit", "Life on Hit"),
        (r"Restores ([\d.]+) Health per second", "Life Regeneration"),
        (r"\+([\d.]+) Health Potion charges?", "Bonus Potion Charges"),
        (r"\+([\d.]+)%\s+Thorns?", "Thorns"),
        (r"\+([\d.]+)%\s+Gold gained from enemies", "Gold Find"),
        (r"\+([\d.]+)%\s+Item Find", "Item Find"),
        (r"\+?(-?[\d.]+)%\s+Move Speed", "Bonus Move Speed"),
    ]
    for pat, stat in simple:
        m = re.match(pat + "$", t, re.I)
        if m:
            d[stat] = float(m.group(1))
            return d, "", True
    m = re.match(r"(?:Reduces (\w+) damage taken by|Take) ([\d.]+)%(?: less (\w+) damage)?", t, re.I)
    if m:
        el = (m.group(1) or m.group(3) or "").capitalize()
        if el in item_eval.ELEMENTS:
            d["Physical Damage Reduction" if el == "Physical" else f"{el} Damage Reduction"] = float(m.group(2))
            return d, "", True
    m = re.match(r"\+([\d.]+)%\s+(Strength|Dexterity|Intelligence)$", t, re.I)
    if m:
        if m.group(2).capitalize() != ctx.main:
            return d, f"no effect ({ctx.main} is your main stat)", False
        d["Main Stat %"] = float(m.group(1))
        return d, "", True
    m = re.match(r"\+([\d.]+)%\s+Magic Resist", t, re.I)
    if m:
        d["Magic Resist"] = g("Magic Resist") * float(m.group(1)) / 100
        return d, f"{m.group(1)}% of your Magic Resist", True
    m = re.match(r"Life Regeneration \+([\d.]+)%", t, re.I)
    if m:
        d["Life Regeneration"] = g("Life Regeneration") * float(m.group(1)) / 100
        return d, f"{m.group(1)}% of your Life Regeneration", True
    m = re.match(r"Life On Hit \+([\d.]+)%", t, re.I)
    if m:
        d["Life on Hit"] = g("Life on Hit") * float(m.group(1)) / 100
        return d, f"{m.group(1)}% of your Life on Hit", True
    return d, "not rated", False


def ability_deltas(text: str, base: dict, ctx) -> tuple:
    """Buffs with an uptime, heals and shields count; damage abilities are not rated."""
    t = re.sub(r"\s*\(the game shows [^)]*\)", "", text).strip()
    cd = re.search(r"Cooldown ([\d.]+)s", t)
    cooldown = float(cd.group(1)) if cd else None
    m = re.match(r"(\+[\d.]+%\s+[A-Za-z ]+?), lasting (\d+) seconds", t)
    if m and cooldown:
        share = min(float(m.group(2)) / cooldown, 1.0)
        d, note, rated = passive_deltas(m.group(1), base, ctx)
        d = {k: v * share for k, v in d.items()}
        return d, f"active {share * 100:.0f}% of the time" + (f" – {note}" if note else ""), rated
    m = re.search(r"(?:Heal player|Shields you for) ([\d.]+)% max(?:imum)? health", t, re.I)
    if m and cooldown:
        hp = float(base.get("Max Health", 0.0))
        per_s = hp * float(m.group(1)) / 100 / cooldown
        return {"Life Regeneration": per_s}, f"≈ {per_s:.0f} health per second", True
    if re.search(r"damage|damaging", t, re.I):
        return {}, "deals damage – not rated (minion damage scaling unknown)", False
    return {}, "not rated", False


def rate(minion: dict, ctx, mode: str) -> dict:
    """{"dps", "surv", "farm", "score", "lines": [(source, text, note, rated)]} for the character."""
    base = dict(ctx.char)
    total = {}
    lines = []
    for p in minion["passives"]:
        d, note, rated = passive_deltas(p["text"], base, ctx)
        for k, v in d.items():
            total[k] = total.get(k, 0.0) + v
        lines.append(("Passive", f"{p['name']}: {p['text']}", note, rated))
    for a in minion["abilities"]:
        d, note, rated = ability_deltas(a["text"], base, ctx)
        for k, v in d.items():
            total[k] = total.get(k, 0.0) + v
        lines.append(("Ability", f"{a['name']}: {a['text']}", note, rated))
    out = {"dps": 0.0, "surv": 0.0, "farm": 0.0, "score": 0.0, "lines": lines, "known": bool(base)}
    if not base or not total:
        return out
    ev = item_eval.evaluate_deltas({k: (v, True) for k, v in total.items()}, ctx, mode)
    farm = sum(ev.farm.values())  # more gold + more items + more EXP (%)
    w = item_eval.MODES.get(mode, item_eval.MODES["Balanced"])
    out.update(dps=ev.dps_pct, surv=ev.surv_pct, farm=farm, farm_parts=dict(ev.farm),
               score=w["dps"] * ev.dps_pct + w["surv"] * ev.surv_pct + w["farm"] * farm)
    return out

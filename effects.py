"""Value of legendary effects that depend on the abilities a hero uses.

Needs the ability shares (which abilities deal how much of the damage; Talents page or skill tracking).
Every ability has a "weight" = weapon damage % of one cast (all hits, all targets, skills.damage_weight).
An effect adds extra weapon damage % per cast (or per hit) of some abilities, or multiplies their damage;
its value for the character is
    damage gain = sum over used abilities a: share_a x (extra_a / weight_a)      (procs)
                + sum over used abilities a: share_a x multiplier_a                (more damage / crit)
Effects that only change status effects, mana or movement are left to the player's own value.
"""
import re

import item_eval
import skills
import talents

ALL_LEVELS_BONUS = 0.30  # "+10 ability levels": an ability at level 10 deals about 30 % more at level 20


def _abilities(hero):
    return {a["name"]: a for a in skills.ABILITIES if a.get("hero") == hero}


def _norm_shares(shares):
    tot = sum(shares.values()) or 1.0
    return {k: v / tot for k, v in shares.items() if v > 0}


def _hits(a):
    """Hits of one cast: targets x bolts."""
    desc = a.get("description", "")
    m = re.search(r"(\d+) [\w ]*?(?:bolts|hits|strikes|projectiles|arrows)[^.]*?each", desc)
    return skills.targets(a) * (int(m.group(1)) if m else 1)


def _matches(target, a):
    """Does "Basic Attack", "Poison", "Lightning Storm" ... describe ability a?"""
    t = target.strip().lower()
    names = {a["name"].lower(), (a.get("slot") or "").lower()}
    if t in names or t.rstrip("s") in {n.rstrip("s") for n in names}:  # "Sacred Orbs" / "Basic Attacks"
        return True
    t = t.rstrip("s")
    if t in ("attack", "any", "hit", "every hit", "direct damage hit"):
        return True
    tags = [x.lower() for x in a.get("tags", [])]
    return t in tags or {"cold": "frost"}.get(t) in tags


def rate(effect: str, hero: str, shares: dict, ctx=None):
    """(damage %, survival %, text) or None if the effect cannot be rated from ability use."""
    if not hero or not shares:
        return None
    abil = _abilities(hero)
    sh = {k: v for k, v in _norm_shares(shares).items() if k in abil}
    if not sh:
        return None
    e = re.sub(r"(\d),(\d{3})", lambda m: m.group(1) + m.group(2), effect)  # 1,000% -> 1000%
    gain, notes = 0.0, []

    def per_cast(target, extra_pct, label):
        nonlocal gain
        g = 0.0
        for name, s in sh.items():
            a = abil[name]
            w = skills.damage_weight(a)
            if w > 0 and _matches(target, a):
                g += s * extra_pct / w
        if g:
            gain += g * 100
            notes.append(label)
        return g

    def per_hit(target, extra_pct, label):
        nonlocal gain
        g = 0.0
        for name, s in sh.items():
            a = abil[name]
            w = skills.damage_weight(a)
            if w > 0 and _matches(target, a):
                g += s * extra_pct * _hits(a) / w
        if g:
            gain += g * 100
            notes.append(label)
        return g

    def multiply(target, pct, label):
        nonlocal gain
        g = sum(s for name, s in sh.items() if skills.damage_weight(abil[name]) > 0 and _matches(target, abil[name]))
        if g:
            gain += g * pct
            notes.append(label)
        return g

    m = re.search(r"Every (\d+) ([\w ]+?) Abilities used, summons? .*?deals (\d+)% Weapon Damage", e, re.I)
    if m:
        per_cast(m.group(2), float(m.group(3)) / int(m.group(1)), f"+{float(m.group(3)) / int(m.group(1)):g}% weapon damage per {m.group(2)}")
    m = re.search(r"Each ([\w ]+?) also deals (\d+)% of Weapon Damage", e, re.I)
    if m:
        per_cast(m.group(1), float(m.group(2)), f"+{m.group(2)}% weapon damage per {m.group(1)}")
    m = re.search(r"(?:Casting an? )?([\w ]+?) has a (\d+)% chance to also cast ([\w ]+?) for free", e, re.I)
    if m:
        a2 = next((a for n, a in _abilities(hero).items() if n.lower() == m.group(3).strip().lower()), None)
        w2 = skills.damage_weight(a2) if a2 else 0
        if w2:
            per_cast(m.group(1), float(m.group(2)) / 100 * w2, f"{m.group(2)}% free {m.group(3)}")
    m = re.search(r"([\w ]+?) ability hits have a (\d+)% chance to .*?dealing (?:a total of )?(\d+)% weapon damage",
                  e, re.I)
    if m:
        per_hit(m.group(1), float(m.group(2)) / 100 * float(m.group(3)), f"{m.group(2)}% chance per {m.group(1)} hit")
    m = re.search(r"Every hit has a (\d+)% chance to gain [^;]+; (\d+) stacks release [\w ]+? for (\d+)% Weapon Damage", e, re.I)
    if m:
        per_hit("hit", float(m.group(1)) / 100 / int(m.group(2)) * float(m.group(3)), "stacking proc")
    m = re.search(r"(?:Attack )?hits have a (\d+)% chance to rain [\w ]+?, each dealing (\d+)% weapon damage", e, re.I)
    if m:  # number of daggers not stated: one assumed
        per_hit("hit", float(m.group(1)) / 100 * float(m.group(2)), "proc (1 dagger assumed)")
    m = re.search(r"hits have a (\d+)% chance to rain multiple .*?Each \w+ hit deals (\d+)% weapon damage", e, re.I)
    if m:  # "multiple": three assumed
        per_hit("hit", float(m.group(1)) / 100 * 3 * float(m.group(2)), "proc (3 daggers assumed)")
    m = re.search(r"Every hit grants a stack of [\w ]+?\. Consumes (\d+) stacks .*?dealing (\d+)% Weapon Damage", e, re.I)
    if m:
        per_hit("hit", float(m.group(2)) / int(m.group(1)), f"{m.group(2)}% weapon damage every {m.group(1)} hits")
    m = re.search(r"Every (\d+)(?:st|nd|rd|th) ([\w ]+?) becomes a", e, re.I)
    if m:  # the stronger version's damage is not stated: twice the damage assumed
        multiply(m.group(2), 100.0 / int(m.group(1)), f"every {m.group(1)}th {m.group(2)} stronger (2x assumed)")
    m = re.search(r"([\w ]+?) deflects? to (\d+) additional enem", e, re.I)
    if m:
        multiply(m.group(1), 100.0 * int(m.group(2)), f"{m.group(2)} more target(s)")
    m = re.search(r"([\w ]+?)(?: and ([\w ]+?))? can pierce through one target", e, re.I)
    if m:  # a second target behind the first: about half the time
        multiply(m.group(1), 50.0, "pierce (half of the hits reach a second target)")
        if m.group(2):
            multiply(m.group(2), 50.0, "pierce")
    m = re.search(r"\+(\d+)% Critical Damage with ([\w ]+)", e, re.I)
    if m and ctx is not None:
        chc = min(ctx.char.get("Critical Hit Chance", 0), item_eval.CAP) / 100
        chd = ctx.char.get("Critical Hit Damage", 0) / 100
        multiply(m.group(2).strip(" ."), chc * float(m.group(1)) / (1 + chc * chd), f"crit damage for {m.group(2).strip(' .')}")
    m = re.search(r"All ability levels \+(\d+)", e, re.I)
    if m:
        multiply("any", ALL_LEVELS_BONUS * 100 * int(m.group(1)) / 10, "about +30 % ability damage per 10 levels")
    m = re.search(r"([\w ]+?) (?:will summon|conjures) one additional", e, re.I)
    if m:
        multiply(m.group(1), 100.0, f"one more {m.group(1)}")
    m = re.search(r"([\w ]+?) hurls (\w+) additional", e, re.I)
    if m:
        n = {"one": 1, "two": 2, "three": 3}.get(m.group(2).lower(), 1)
        multiply(m.group(1), 100.0 * n, f"{n} more projectiles")
    m = re.search(r"standing still, gain \+(\d+)% ([\w ]+?) Damage every second, stacking up to (\d+) times", e, re.I)
    if m:  # half of the maximum assumed: the hero walks between packs
        multiply(m.group(2), float(m.group(1)) * int(m.group(3)) / 2, "half of the stacks assumed")
    if not notes:
        return None
    used = ", ".join(f"{k} {v * 100:.0f} %" for k, v in sh.items())
    return gain, 0.0, f"{' · '.join(notes)} → {gain:+.1f} % damage (abilities: {used})"


# ----------------------------------------------------------------------------- effects on stats
MOVING_SHARE = 0.3       # share of a run spent walking between packs (estimate)
AFTER_KILL_UPTIME = 0.6  # "for N seconds after a kill": kills come often in a run (estimate)


def ability_uptime(name: str, hero: str) -> float | None:
    """Share of the time an ability with a duration is active when used on cooldown, None if unknown."""
    a = next((x for x in skills.ABILITIES if x["name"].lower() == name.strip().lower()
              and (not hero or x["hero"] == hero)), None)
    if not a:
        return None
    dur = re.search(r"(?:for|lasting) (\d+(?:\.\d+)?)\s?s(?:econds)?", a.get("description", ""))
    cd = re.search(r"([\d.]+)\s*s", str(a.get("cooldown") or ""))
    if not dur or not cd:
        return None
    return min(float(dur.group(1)) / float(cd.group(1)), 1.0)


def stat_rate(effect: str, ctx) -> dict | None:
    """Effects that change stats (all the time or part of it): {"deltas", "dps", "surv", "farm", "text"}
    or None. deltas go through the normal damage / survival model; dps/surv/farm are fixed % on top."""
    e = effect.strip()
    used = {n.lower() for n in getattr(ctx, "used_abilities", ())}
    out = {"deltas": {}, "dps": 0.0, "surv": 0.0, "farm": 0.0, "notes": []}
    g = lambda k: float(ctx.char.get(k, 0.0))
    m = re.search(r"Damage Over Time deals (\d+)% increased damage", e, re.I)
    if m:
        out["deltas"]["Damage Over Time"] = float(m.group(1))
        out["notes"].append(f"+{m.group(1)}% to your damage over time")
    m = re.search(r"Increase Potion Charges by (\d+)", e, re.I)
    if m:
        out["surv"] += int(m.group(1)) * item_eval.OTHER_DEFAULTS["Bonus Potion Charges"][1]
        out["notes"].append(f"+{m.group(1)} potion charges")
    m = re.search(r"Restore (\d+)% of Max Health per second while moving", e, re.I)
    if m:
        regen = g("Max Health") * float(m.group(1)) / 100 * MOVING_SHARE
        out["deltas"]["Life Regeneration"] = regen
        out["notes"].append(f"≈ {regen:.0f} health per second (moving ~{MOVING_SHARE * 100:.0f}% of the time)")
    m = re.search(r"\+(\d+)% Movement Speed for (\d+) seconds after a kill", e, re.I)
    if m:
        out["farm"] += float(m.group(1)) * AFTER_KILL_UPTIME * item_eval.OTHER_DEFAULTS["Bonus Move Speed"][1]
        out["notes"].append(f"+{m.group(1)}% move speed ~{AFTER_KILL_UPTIME * 100:.0f}% of the time")
    m = re.search(r"(?:While ([\w ]+?) is active, you also gain|Gain) (.+?)(?: while ([\w ]+?) is active)?\.?$", e, re.I)
    if m and (m.group(1) or m.group(3)):
        ab = (m.group(1) or m.group(3)).strip()
        up = ability_uptime(ab, ctx.hero)
        if ab.lower() not in used and used:
            out["notes"].append(f"only while {ab} is active – {ab} is not on your skill bar")
        elif up is None:
            out["notes"].append(f"only while {ab} is active – its duration is not known")
        else:
            for val, stat in re.findall(r"\+?(\d+)% ([A-Za-z ]+?)(?= and |$|\.)", m.group(2)):
                stat = stat.strip()
                stat = "Attack Speed Bonus" if stat == "Attack Speed" else stat
                out["deltas"][stat] = out["deltas"].get(stat, 0.0) + float(val) * up
            out["notes"].append(f"{ab} active ~{up * 100:.0f}% of the time")
    # damage while an ability is active: "While Rage Shield is active, deal 210% ... every 3 seconds",
    # "Spirit Stream ... pulses every second, dealing 160% weapon damage"
    hit = None
    m = re.search(r"While ([\w ]+?) is active, deal (\d+)% of Weapon Damage .*? every (\d+) seconds", e, re.I)
    if m:
        hit = (m.group(1), float(m.group(2)), float(m.group(3)))
    m = re.search(r"pulses every (?:second|(\d+) seconds?), dealing (\d+)% weapon damage", e, re.I)
    if m:
        ab = next((x["name"] for x in skills.ABILITIES if x["name"].lower() in e.lower()), "")
        hit = (ab, float(m.group(2)), float(m.group(1) or 1))
    wd = getattr(ctx, "wd_per_s", 0.0)
    if hit and hit[0] and wd:
        ab, pct, every = hit
        up = ability_uptime(ab, ctx.hero) if ab.lower() in used or not used else 0.0
        if up:
            gain = pct / every * up / wd * 100
            out["dps"] += gain
            out["notes"].append(f"≈ {pct / every * up:.0f}% weapon damage per second against your {wd:.0f}%")
    if not out["notes"]:
        return None
    out["text"] = " · ".join(out["notes"])
    return out

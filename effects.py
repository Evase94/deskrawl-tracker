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
    t = target.strip().rstrip("s").lower()
    if t in (a["name"].lower(), (a.get("slot") or "").lower()):
        return True
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
    m = re.search(r"([\w ]+?) has a (\d+)% chance to also cast ([\w ]+?) for free", e, re.I)
    if m:
        a2 = next((a for n, a in _abilities(hero).items() if n.lower() == m.group(3).strip().lower()), None)
        w2 = skills.damage_weight(a2) if a2 else 0
        if w2:
            per_cast(m.group(1), float(m.group(2)) / 100 * w2, f"{m.group(2)}% free {m.group(3)}")
    m = re.search(r"([\w ]+?) ability hits have a (\d+)% chance to .*?dealing (\d+)% weapon damage", e, re.I)
    if m:
        per_hit(m.group(1), float(m.group(2)) / 100 * float(m.group(3)), f"{m.group(2)}% chance per {m.group(1)} hit")
    m = re.search(r"Every hit has a (\d+)% chance to gain [^;]+; (\d+) stacks release [\w ]+? for (\d+)% Weapon Damage", e, re.I)
    if m:
        per_hit("hit", float(m.group(1)) / 100 / int(m.group(2)) * float(m.group(3)), "stacking proc")
    m = re.search(r"(?:Attack )?hits have a (\d+)% chance to rain [\w ]+?, each dealing (\d+)% weapon damage", e, re.I)
    if m:  # number of daggers not stated: one assumed
        per_hit("hit", float(m.group(1)) / 100 * float(m.group(2)), "proc (1 dagger assumed)")
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

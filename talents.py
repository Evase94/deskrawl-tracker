"""Talent calculator: what a talent build is worth for a character, and the best build for a mode.

Every talent's rank text is turned into one of these effects per rank:
  stats       - character sheet numbers (Intelligence, Lightning Damage, Armor, ...) -> item_eval model
  ability     - more damage for some abilities (one ability, a slot like "Basic Attack", or a tag like
                "Lightning abilities"); counts with the share of damage those abilities deal
  crit_chance / crit_damage for some abilities - same, through the crit formula
Text that matches none of the rules is "not rated" (status effects, mana, range...).

The character sheet already contains the talents spent right now, so a build is always compared with
the player's current build: stats(build) - stats(current), and ability bonuses as a ratio.
Rows open when the points spent in the rows below reach the row's threshold; capstone rows allow one
capstone.
"""
import json
import re

import item_eval
import paths


def _load(name, key):
    try:
        with open(paths.res("data", name), encoding="utf-8") as f:
            return json.load(f)[key]
    except Exception:
        return {} if key == "heroes" else []


HEROES = _load("talents.json", "heroes")
ABILITIES = __import__("patch_data").abilities(_load("abilities.json", "abilities"))
ELEMENTS = ["Fire", "Cold", "Lightning", "Poison", "Arcane", "Physical"]
TAG_ELEMENT = {"Frost": "Cold"}


def key(t) -> str:
    """Build key of a talent: its id (two Sorcerer talents are both called "Flame Ward")."""
    return t.get("id") or t["name"]


def normalize(build: dict, hero: str) -> dict:
    """Builds saved with talent names (older versions) -> keyed by talent id."""
    ids = {key(t) for t in HEROES.get(hero, [])}
    by_name = {}
    for t in HEROES.get(hero, []):
        by_name.setdefault(t["name"], key(t))
    return {(k if k in ids else by_name.get(k, k)): v for k, v in (build or {}).items() if v}


def abilities_of(hero):
    return [a for a in ABILITIES if a.get("hero") == hero]


def tree(hero):
    return HEROES.get(hero, [])


def rows(hero):
    return sorted({t["points"] for t in tree(hero)})


# ----------------------------------------------------------------------------- text -> effects

_N = r"([\d.]+)"


def parse(text: str, hero: str) -> list:
    """Effects of one rank: [(kind, target, value)]; empty = not rated."""
    t = text or ""
    names = sorted((a["name"] for a in abilities_of(hero)), key=len, reverse=True)
    out = []

    def ability_target(s):
        s = s.strip()
        for n in names:
            if s.lower() == n.lower():
                return n
        m = re.fullmatch(r"(Basic Attack|Strong Attack|Special Ability|Special)s?", s, re.I)
        if m:
            return {"special ability": "Special"}.get(m.group(1).lower(), m.group(1).title())
        m = re.fullmatch(r"(\w+) [Aa]bilities", s)
        if m:
            return "tag:" + m.group(1).capitalize()
        return None

    used = []

    def take(pattern, fn):
        for m in re.finditer(pattern, t):
            if any(m.start() < e and m.end() > s for s, e in used):
                continue
            r = fn(m)
            if r is not None:  # [] = recognised but not rated
                used.append((m.start(), m.end()))
                out.extend(r if isinstance(r, list) else [r])

    take(rf"([\d.]+)% of your Max Mana is added to Intelligence", lambda m: ("mana_to_int", "", float(m.group(1))))
    take(rf"[Ii]ncreases? (Basic Attack|Strong Attack) damage by {_N}% for (\d+)s",
         lambda m: ("ability", m.group(1).title(), float(m.group(2)) * 0.5))  # assumed active half the time
    take(rf"\+{_N}% (?:Critical Hit Damage to (\w+) Abilities)",
         lambda m: ("crit_damage", "tag:" + m.group(2).capitalize(), float(m.group(1))))
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Critical Hit Damage",
         lambda m: (("crit_damage", ability_target(m.group(2)), float(m.group(1))) if ability_target(m.group(2)) else None))
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Critical Hit Chance",
         lambda m: (("crit_chance", ability_target(m.group(2)), float(m.group(1))) if ability_target(m.group(2)) else None))
    take(rf"([A-Z][\w' ]+?) deals \+{_N}% [Dd]amage to enemies affected", lambda m: [])  # conditional
    take(r"While \[?[A-Z][^\].,]*\]? is active[^.]*", lambda m: [])  # only while one ability runs
    take(rf"([A-Z][\w' ]+?) deals \+{_N}% [Dd]amage",
         lambda m: (("ability", ability_target(m.group(1)), float(m.group(2))) if ability_target(m.group(1)) else None))
    take(rf"[Rr]educes the cooldown of (\w+) abilities by {_N}%",
         lambda m: ("ability", "tag:" + m.group(1).capitalize(), float(m.group(2)) / (1 - float(m.group(2)) / 100) * 0.5))
    take(rf"\+{_N} (Intelligence|Strength|Dexterity)\b", lambda m: ("stat", m.group(2), float(m.group(1))))
    take(rf"\+{_N}% (Fire|Cold|Lightning|Poison|Arcane|Physical) Damage Reduction",
         lambda m: ("stat", f"{m.group(2)} Damage Reduction", float(m.group(1))))
    take(rf"\+{_N}% (Fire|Cold|Lightning|Poison|Arcane|Physical) Damage\b",
         lambda m: ("stat", f"{m.group(2)} Damage", float(m.group(1))))
    take(rf"\+{_N}% ([A-Z][\w' ]+?) [Dd]amage\b",
         lambda m: (("ability", ability_target(m.group(2)), float(m.group(1))) if ability_target(m.group(2)) else None))
    take(rf"\+{_N}% Damage to Elite", lambda m: ("stat", "Damage vs Elite", float(m.group(1))))
    take(rf"(Basic Attack|Strong Attack) damage \+{_N}%", lambda m: ("ability", m.group(1).title(), float(m.group(2))))
    take(rf"\+{_N}% (\w+) Abilities Cooldown reduction",
         lambda m: ("ability", "tag:" + m.group(2).capitalize(), float(m.group(1)) / (1 - float(m.group(1)) / 100) * 0.5))
    take(rf"[Tt]ake {_N}% less damage", lambda m: ("stat", "Damage Reduction", float(m.group(1)) * 0.5))
    take(rf"\+{_N} Max HP", lambda m: ("stat", "Max Health", float(m.group(1))))
    take(rf"\+{_N} Life Regen", lambda m: ("stat", "Life Regeneration", float(m.group(1))))
    take(rf"\+{_N} Magic Resist", lambda m: ("stat", "Magic Resist", float(m.group(1))))
    take(rf"\+{_N} Armor\b", lambda m: ("stat", "Armor", float(m.group(1))))
    take(rf"\+{_N} Thorns?\b", lambda m: ("stat", "Thorns", float(m.group(1))))
    take(rf"Gain {_N} Armor", lambda m: ("stat", "Armor", float(m.group(1))))
    take(rf"(?:total Armor by {_N}%|\+{_N}% Armor|{_N}% increased Armor)",
         lambda m: ("stat", "Bonus Armor", float(next(g for g in m.groups() if g))))
    take(rf"\+{_N}% Magic Resist", lambda m: ("mr_pct", "", float(m.group(1))))
    take(rf"\+{_N}% Max(?:imum)? Health", lambda m: ("stat", "Max Health %", float(m.group(1))))
    take(rf"\+{_N} Max(?:imum)? Health", lambda m: ("stat", "Max Health", float(m.group(1))))
    # conditional bonuses ("against Frozen enemies", "to Burning targets") are not rated
    take(rf"\+{_N}% [\w ]+? (?:against|to) (?!Elite)\w+ (?:enemies|targets)", lambda m: [])
    take(rf"\+{_N}% Critical Hit Chance", lambda m: ("stat", "Critical Hit Chance", float(m.group(1))))
    take(rf"\+{_N}% Critical Hit Damage", lambda m: ("stat", "Critical Hit Damage", float(m.group(1))))
    take(rf"\+{_N}% Attack Speed", lambda m: ("stat", "Attack Speed Bonus", float(m.group(1))))
    take(rf"\+{_N}% Cooldown Reduction", lambda m: ("stat", "Cooldown Reduction", float(m.group(1))))
    take(rf"\+{_N}% Dodge Chance", lambda m: ("stat", "Dodge Chance", float(m.group(1))))
    take(rf"\+{_N} Max Mana", lambda m: ("stat", "Max Mana", float(m.group(1))))
    take(rf"\+{_N} Mana Regen", lambda m: ("stat", "Mana Regeneration", float(m.group(1))))
    take(rf"\+{_N}% Damage\b", lambda m: ("stat", "All Damage", float(m.group(1))))
    return [e for e in out if e[1] is not None]


def per_rank(t, hero):
    """Effects of one rank. Rank texts are linear (rank N = N x rank 1); the max rank text is used
    when rank 1 is rounded in the game."""
    eff_max = parse(t.get("rank_max", ""), hero)
    if eff_max and t["ranks"] > 1:
        return [(k, tg, v / t["ranks"]) for k, tg, v in eff_max]
    return parse(t.get("rank1", ""), hero)


# ----------------------------------------------------------------------------- value of a build

def _ability_info(hero):
    return {a["name"]: a for a in abilities_of(hero)}


def _targets(target, ability):
    """Does an effect target hit this ability?"""
    if target in (ability["name"], ability.get("slot")):
        return True
    if target == "Special" and ability.get("slot") == "Special":
        return True
    if target.startswith("tag:"):
        tag = target[4:]
        return tag in ability.get("tags", []) or TAG_ELEMENT.get(tag) in ability.get("tags", [])
    return False


def effects(build: dict, hero: str):
    """Summed effects of a build {talent name: rank}."""
    stats, ab = {}, []
    for t in tree(hero):
        r = build.get(key(t), 0)
        if not r:
            continue
        for kind, tg, v in per_rank(t, hero):
            if kind == "stat":
                stats[tg] = stats.get(tg, 0.0) + v * r
            else:
                ab.append((kind, tg, v * r))
    return stats, ab


def _ability_factor(ab, shares, ctx, hero):
    """Damage of the used abilities, share-weighted, relative to no ability bonuses."""
    info = _ability_info(hero)
    chc = min(ctx.char.get("Critical Hit Chance", 0.0), item_eval.CAP) / 100
    chd = ctx.char.get("Critical Hit Damage", 0.0) / 100
    total = sum(shares.values()) or 1.0
    f = 0.0
    for name, share in shares.items():
        a = info.get(name)
        if not a:
            f += share / total
            continue
        dmg = sum(v for k, tg, v in ab if k == "ability" and _targets(tg, a)) / 100
        cc = sum(v for k, tg, v in ab if k == "crit_chance" and _targets(tg, a)) / 100
        cd = sum(v for k, tg, v in ab if k == "crit_damage" and _targets(tg, a)) / 100
        crit = (1 + min(chc + cc, item_eval.CAP / 100) * (chd + cd)) / (1 + chc * chd)
        f += share / total * (1 + dmg) * crit
    return f


def evaluate(build: dict, current: dict, hero: str, ctx, mode: str, shares: dict):
    """(damage %, survival %, income %, weighted score) of switching from the current build to build."""
    s_new, ab_new = effects(build, hero)
    s_cur, ab_cur = effects(current, hero)
    # effects that convert other numbers
    for tgt, (st, a) in (("new", (s_new, ab_new)), ("cur", (s_cur, ab_cur))):
        for k, _, v in a:
            if k == "mana_to_int":
                st["Intelligence"] = st.get("Intelligence", 0.0) + ctx.char.get("Max Mana", 0.0) * v / 100
            elif k == "mr_pct":
                st["Magic Resist"] = st.get("Magic Resist", 0.0) + ctx.char.get("Magic Resist", 0.0) * v / 100
    deltas = {}
    for k in set(s_new) | set(s_cur):
        d = s_new.get(k, 0.0) - s_cur.get(k, 0.0)
        if abs(d) > 1e-9:
            deltas[k] = (d, True)
    ev = item_eval.evaluate_deltas(deltas, ctx, mode)
    ratio = _ability_factor(ab_new, shares, ctx, hero) / _ability_factor(ab_cur, shares, ctx, hero)
    dps = ((1 + ev.dps_pct / 100) * ratio - 1) * 100
    w = item_eval.MODES[mode]
    score = w["dps"] * dps + w["surv"] * ev.surv_pct + w["farm"] * ev.farm_pct
    return dps, ev.surv_pct, ev.farm_pct, score


# ----------------------------------------------------------------------------- rules and optimizer

def spent_below(build, hero, threshold):
    return sum(r for t in tree(hero) for r in [build.get(key(t), 0)] if t["points"] < threshold)


def can_add(build, t, hero):
    if build.get(key(t), 0) >= t["ranks"]:
        return False
    if spent_below(build, hero, t["points"]) < t["points"]:
        return False
    if t.get("capstone"):
        same_row = [x for x in tree(hero) if x["points"] == t["points"] and x.get("capstone") and x is not t]
        if any(build.get(key(x), 0) for x in same_row):
            return False
    return True


def can_remove(build, t, hero):
    if build.get(key(t), 0) <= 0:
        return False
    b = dict(build)
    b[key(t)] -= 1
    return valid(b, hero)


def valid(build, hero):
    for t in tree(hero):
        if build.get(key(t), 0) and spent_below(build, hero, t["points"]) < t["points"]:
            return False
    return True


def best_build(points: int, hero: str, ctx, mode: str, shares: dict, current: dict):
    """Greedy: spend one point at a time where it adds most; then try moving single points."""
    build = {}
    base = lambda b: evaluate(b, current, hero, ctx, mode, shares)[3]
    score = base(build)
    for _ in range(points):
        best, best_s = None, None
        for t in tree(hero):
            if not can_add(build, t, hero):
                continue
            b = dict(build)
            b[key(t)] = b.get(key(t), 0) + 1
            s = base(b)
            # a talent without any rated effect still opens rows: tiny preference for cheap, early ones
            s -= t["points"] * 1e-6
            if best_s is None or s > best_s:
                best, best_s = t, s
        if best is None:
            break
        build[key(best)] = build.get(key(best), 0) + 1
        score = best_s
    # local improvement: move one point from one talent to another
    improved = True
    rounds = 0
    while improved and rounds < 40:
        improved, rounds = False, rounds + 1
        for a in tree(hero):
            if not build.get(key(a)):
                continue
            b1 = dict(build)
            b1[key(a)] -= 1
            if not b1[key(a)]:
                del b1[key(a)]
            if not valid(b1, hero):
                continue
            for t in tree(hero):
                if t is a or not can_add(b1, t, hero):
                    continue
                b2 = dict(b1)
                b2[key(t)] = b2.get(key(t), 0) + 1
                s = base(b2)
                if s > score + 1e-6:
                    build, score, improved = b2, s, True
                    break
            if improved:
                break
    return build


def rated(t, hero) -> bool:
    return bool(per_rank(t, hero))


# ----------------------------------------------------------------------------- share / load (afkmeta links)
# afkmeta.com/en/deskrawl/talents/<hero>?c=0x2-1-2x2 : talent index in tree order, "xN" for N points (1 = no x)

def encode(build: dict, hero: str) -> str:
    parts = []
    for i, t in enumerate(tree(hero)):
        r = build.get(key(t), 0)
        if r:
            parts.append(str(i) if r == 1 else f"{i}x{r}")
    return "-".join(parts)


def share_link(build: dict, hero: str) -> str:
    return f"https://afkmeta.com/en/deskrawl/talents/{hero.lower()}?c={encode(build, hero)}"


def decode(text: str, hero: str):
    """Build from an afkmeta link or a bare code; (build, hero of the link or None). None if unreadable."""
    import re as _re
    text = (text or "").strip()
    m = _re.search(r"talents/(\w+)", text)
    link_hero = m.group(1).capitalize() if m else None
    m = _re.search(r"[?&]c=([0-9x\-]*)", text)
    code = m.group(1) if m else text
    if not _re.fullmatch(r"(\d+(x\d+)?)(-\d+(x\d+)?)*|", code):
        return None
    nodes = tree(link_hero or hero)
    build = {}
    for part in filter(None, code.split("-")):
        i, _, n = part.partition("x")
        i, n = int(i), int(n or 1)
        if i < len(nodes):
            build[key(nodes[i])] = min(n, nodes[i]["ranks"])
    return build, link_hero

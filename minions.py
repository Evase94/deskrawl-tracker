"""Minions: what each minion's passives and abilities are worth for the current character.

Passives become stat changes and run through the same damage / survival model as the item comparison
(item_eval). With enough skill tracking (build_profile) the guesses are replaced by measurements:
  - element bonuses count with the share of your damage in that element,
  - Basic / Strong Attack and special ability bonuses with your measured damage shares,
  - "Damage to Burning / Slowed / ... enemies" with how often your abilities (and the minion itself) keep
    enemies in that state,
  - damage abilities ("200% Frost damage, cooldown 6 s") against your own damage per second,
  - applying Vulnerable (enemies take 30% more damage) for the time it adds.
Without measurements the fixed estimates below are used and the result says so.
"""
import json
import re

import build_profile
import item_eval
import paths

# share of the time / damage a condition holds - estimates when nothing was measured
COND_UPTIME = {"Healthy": 0.5, "Injured": 0.5, "Distant": 0.5, "Elite and Boss": 0.2, "Elite": 0.2,
               "Slowed": 0.3, "Chilled": 0.3, "Bleeding": 0.25, "Poisoned": 0.25, "Burning": 0.25,
               "Burned": 0.25, "Vulnerable": 0.25, "Stunned": 0.1, "Immobilized": 0.15}
NOT_STATUS = ("Healthy", "Injured", "Distant", "Elite and Boss", "Elite")  # not caused by abilities
DOT_SHARE = 0.3
ABILITY_SHARE = 0.5
VULNERABLE = 30.0  # Vulnerable: "Take 30% more damage"
REF = "Damage vs Healthy"  # item_eval models this one with uptime REF_UPTIME; others are scaled onto it
REF_UPTIME = item_eval.UPTIME[REF]
STATUS_WORDS = [(r"Burning|sets? it Burning", "Burn"), (r"Chill", "Chill"), (r"Vulnerable", "Vulnerable"),
                (r"Poisoned", "Poisoned"), (r"stun", "Short Stun")]
ELEM_WORDS = {"Frost": "Cold", "Cold": "Cold", "Fire": "Fire", "fireball": "Fire", "molten": "Fire",
              "Lightning": "Lightning", "Poison": "Poison", "Arcane": "Arcane", "Physical": "Physical"}


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


def _clean(text):
    return re.sub(r"\s*\(the game shows [^)]*\)", "", text).strip()


class Rater:
    """Rates passives for one character, build profile and minion (its own status effects)."""

    def __init__(self, base: dict, ctx, prof: dict | None, own_status: dict | None = None):
        self.base, self.ctx = base, ctx
        self.prof = prof if prof and prof.get("ok") else None
        self.own = own_status or {}

    def g(self, k):
        return float(self.base.get(k, 0.0))

    def uptime(self, cond: str) -> tuple:
        """(share of the time, how it was found)."""
        if self.prof and cond not in NOT_STATUS:
            build = self.prof["condition"].get(cond, 0.0)
            sts = build_profile.CONDITIONS.get(cond, [])
            mine = build_profile.combine(self.own.get(s, 0.0) for s in sts)
            u = build_profile.combine([build, mine])
            how = f"your abilities {build * 100:.0f}%" + (f", the minion itself {mine * 100:.0f}%" if mine else "")
            return u, how
        return COND_UPTIME.get(cond, 0.2), "estimate"

    def share(self, slot: str, default: float) -> tuple:
        if self.prof:
            return self.prof["slot_share"].get(slot, 0.0), "measured"
        return default, "estimate"

    def cond(self, d, share, pct, label, how):
        d[REF] = d.get(REF, 0.0) + pct * share / REF_UPTIME
        return f"+{pct:g}% damage for ~{share * 100:.0f}% of your damage ({label}; {how})"

    def element(self, el, pct) -> tuple:
        """(extra dps %, note) of +pct% damage of one element."""
        if self.prof:
            sh = self.prof.get("element_share", {}).get(el, 0.0)
            if sh <= 0:
                return 0.0, f"none of your measured damage is {el}"
            extra = sh * pct / (100 + self.g(f"{el} Damage")) * 100
            return extra, f"{sh * 100:.0f}% of your damage is {el} (measured)"
        return None, ""

    def passive(self, text: str) -> tuple:
        """(deltas, extra dps %, note, rated) for one passive or buff text."""
        t = _clean(text).rstrip(".")
        d, ctx = {}, self.ctx
        m = re.match(r"\+?(-?[\d.]+)%\s+(Fire|Cold|Lightning|Poison|Arcane|Physical)\s+Damage$", t, re.I)
        if m:
            el, pct = m.group(2).capitalize(), float(m.group(1))
            extra, note = self.element(el, pct)
            if extra is not None:
                return d, extra, note, extra > 0
            d[f"{el} Damage"] = pct
            return d, 0.0, ("" if el == ctx.elem else f"no effect for a {ctx.elem} build"), el == ctx.elem
        m = re.match(r"\+([\d.]+)%\s+damage to (?:enemies more than \d+ units away|(.+?)\s+enemies)", t, re.I)
        if m:
            what = m.group(2) or "Distant"
            if "units away" in t:
                what = "Distant"
            u, how = self.uptime(what)
            return d, 0.0, self.cond(d, u, float(m.group(1)), f"{what} enemies", how), u > 0
        m = re.match(r"\+([\d.]+)%\s+Damage Over Time$", t, re.I)
        if m:
            sh = self.prof["dot_share"] if self.prof else DOT_SHARE
            return d, 0.0, self.cond(d, sh, float(m.group(1)), "damage over time",
                                     "measured" if self.prof else "estimate"), sh > 0
        for pat, slot, default in ((r"Special Ability (?:Bonus )?damage", "Special", ABILITY_SHARE),
                                   (r"Strong Attack Damage", "Strong Attack", item_eval.STRONG_SHARE),
                                   (r"Basic Attack Damage", "Basic Attack", item_eval.BASIC_SHARE)):
            m = re.match(r"\+([\d.]+)%\s+" + pat + "$", t, re.I)
            if m:
                sh, how = self.share(slot, default)
                return d, 0.0, self.cond(d, sh, float(m.group(1)), slot, how), sh > 0
        simple = [
            (r"\+([\d.]+)%\s+Critical Hit Damage", "Critical Hit Damage"),
            (r"\+([\d.]+)%\s+Critical Hit Chance", "Critical Hit Chance"),
            (r"\+([\d.]+)%\s+Attack Speed", "Attack Speed Bonus"),
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
                return d, 0.0, "", True
        m = re.match(r"(?:Reduces (\w+) damage taken by|Take) ([\d.]+)%(?: less (\w+) damage)?", t, re.I)
        if m:
            el = (m.group(1) or m.group(3) or "").capitalize()
            if el in item_eval.ELEMENTS:
                d["Physical Damage Reduction" if el == "Physical" else f"{el} Damage Reduction"] = float(m.group(2))
                return d, 0.0, "", True
        m = re.match(r"\+([\d.]+)%\s+(Strength|Dexterity|Intelligence)$", t, re.I)
        if m:
            if m.group(2).capitalize() != ctx.main:
                return d, 0.0, f"no effect ({ctx.main} is your main stat)", False
            d["Main Stat %"] = float(m.group(1))
            return d, 0.0, "", True
        for pat, stat in ((r"\+([\d.]+)%\s+Magic Resist", "Magic Resist"),
                          (r"Life Regeneration \+([\d.]+)%", "Life Regeneration"),
                          (r"Life On Hit \+([\d.]+)%", "Life on Hit")):
            m = re.match(pat, t, re.I)
            if m:
                d[stat] = self.g(stat) * float(m.group(1)) / 100
                return d, 0.0, f"{m.group(1)}% of your {stat} ({self.g(stat):g})", True
        return d, 0.0, "not rated", False

    def ability(self, text: str) -> tuple:
        """(deltas, extra dps %, note, rated): buffs by uptime, heals/shields as regeneration, damage
        against your own damage per second, Vulnerable for the time it adds."""
        t = _clean(text)
        cd = re.search(r"Cooldown ([\d.]+)s", t)
        cooldown = float(cd.group(1)) if cd else None
        m = re.match(r"(\+[\d.]+%\s+[A-Za-z ]+?), lasting (\d+) s", t)
        if m and cooldown:
            share = min(float(m.group(2)) / cooldown, 1.0)
            d, extra, note, rated = self.passive(m.group(1))
            d = {k: v * share for k, v in d.items()}
            return d, extra * share, f"active {share * 100:.0f}% of the time" + (f" – {note}" if note else ""), rated
        m = re.search(r"(?:Heal player|Shields you for) ([\d.]+)% max(?:imum)? health", t, re.I)
        if m and cooldown:
            per_s = self.g("Max Health") * float(m.group(1)) / 100 / cooldown
            return {"Life Regeneration": per_s}, 0.0, f"≈ {per_s:.0f} health per second", True
        notes, extra, rated = [], 0.0, False
        if "Vulnerable" in t and cooldown:
            before = self.prof["condition"].get("Vulnerable", 0.0) if self.prof else 0.0
            after = build_profile.combine([before, self.own.get("Vulnerable", 0.0)])
            gain = ((1 + VULNERABLE / 100 * after) / (1 + VULNERABLE / 100 * before) - 1) * 100
            extra += gain
            notes.append(f"Vulnerable {self.own.get('Vulnerable', 0) * 100:.0f}% of the time: {gain:+.1f}% damage")
            rated = True
        m = re.search(r"(?:dealing|for) ([\d.]+)% (?:weapon )?(?:(\w+) )?damage", t, re.I)
        if m and cooldown:
            el = next((v for k, v in ELEM_WORDS.items() if k.lower() in t.lower()), self.ctx.elem)
            if self.prof and self.prof["wd_per_s"] > 0:
                wd = float(m.group(1)) * build_profile.coverage(t) / 0.35 / cooldown  # per second, per target hit
                elem = (1 + self.g(f"{el} Damage") / 100) / (1 + self.g(f"{self.ctx.elem} Damage") / 100)
                gain = wd / self.prof["wd_per_s"] * 100 * elem
                extra += gain
                notes.append(f"≈ {wd:.0f}% weapon damage per second against your {self.prof['wd_per_s']:.0f}% "
                             f"({el}): {gain:+.1f}% damage – assumes it scales like your own damage")
                rated = True
            else:
                notes.append("damage – needs skill tracking to compare with your damage")
        elif re.search(r"damage|damaging", t, re.I):
            notes.append("damage without a number in its description – not rated")
        return {}, extra, " · ".join(notes) or "not rated", rated


def own_status(minion: dict) -> dict:
    """Statuses the minion's ability puts on enemies, as share of the time."""
    out = {}
    for a in minion.get("abilities", []):
        t = _clean(a["text"])
        cd = re.search(r"Cooldown ([\d.]+)s", t)
        if not cd:
            continue
        for pat, st in STATUS_WORDS:
            if re.search(pat, t, re.I) and st in build_profile.STATUSES:
                dur = build_profile.STATUSES[st].get("duration_s") or 0
                out[st] = min(dur / float(cd.group(1)), 1.0) * build_profile.coverage(t)
    return out


def rate(minion: dict, ctx, mode: str, prof: dict | None = None) -> dict:
    """{"dps", "surv", "farm", "score", "lines": [(source, text, note, rated)], "measured"}."""
    base = dict(ctx.char)
    r = Rater(base, ctx, prof, own_status(minion))
    total, extra, lines = {}, 0.0, []
    for kind, items, fn in (("Passive", minion["passives"], r.passive), ("Ability", minion["abilities"], r.ability)):
        for p in items:
            d, x, note, rated = fn(p["text"])
            for k, v in d.items():
                total[k] = total.get(k, 0.0) + v
            extra += x
            lines.append((kind, f"{p['name']}: {_clean(p['text'])}", note, rated))
    out = {"dps": extra, "surv": 0.0, "farm": 0.0, "score": 0.0, "lines": lines, "known": bool(base),
           "measured": r.prof is not None, "farm_parts": {}}
    if not base:
        return out
    if total:
        ev = item_eval.evaluate_deltas({k: (v, True) for k, v in total.items()}, ctx, mode)
        out.update(dps=ev.dps_pct + extra, surv=ev.surv_pct, farm=sum(ev.farm.values()), farm_parts=dict(ev.farm))
    w = item_eval.MODES.get(mode, item_eval.MODES["Balanced"])
    out["score"] = w["dps"] * out["dps"] + w["surv"] * out["surv"] + w["farm"] * out["farm"]
    return out

"""Item comparison: what an item swap does to damage, survival and farming income.

Formulas (from the game's damage rules):
  hit damage  = (weapon damage + Damage) x ability x (1 + primary attribute/100)
                x (1 + element% + All Damage%) x crit x (1 + sum of "Damage vs" bonuses)
                x (1 + Basic/Strong attack bonus)
  crit        = 1 + min(Crit Chance, 85%) x Crit Damage      (expected value)
  DPS         = hit damage x attacks per second, attacks/s = weapon speed x (1 + Attack Speed Bonus)
  incoming    = x (1 - R / (R + 50 x attacker level))   R = Armor (physical) / Magic Resist (elements)
                x (1 - element Damage Reduction) x (1 - Damage Reduction); dodge (max 85%) on direct hits
  Toughness   = Max Health / ((1 - Dodge) x (1 - 0.5 x (armor cut + magic resist cut)))  [game's own value]

Verified against a character sheet: Toughness 5,678 and Recovery 102 reproduce exactly.
"""
import paths
import difflib
import json
import os
import re
from dataclasses import dataclass, field

ELEMENTS = ["Fire", "Cold", "Lightning", "Poison", "Arcane", "Physical"]
MAIN_STATS = ["Intelligence", "Strength", "Dexterity"]
CLASS_MAIN = {"Mage": "Intelligence", "Sorcerer": "Intelligence", "Barbarian": "Strength",
              "Warrior": "Strength", "Hunter": "Dexterity", "Monk": "Dexterity"}
CAP = 85.0  # crit chance and dodge chance stop at 85 %

# Share of time / hits a conditional bonus applies (estimates; the game does not tell).
UPTIME = {"Damage vs Healthy": 0.5, "Damage vs Injured": 0.5, "Damage vs Distant": 0.5,
          "Damage vs Elite": 0.2}
STRONG_SHARE = 0.3  # share of damage from Strong Attack abilities
BASIC_SHARE = 0.7

FARM_STATS = {"Gold Find": "Gold", "Item Find": "Items", "XP Gained": "EXP"}

# Stats outside the three models: (target, % per unit). Editable in the tracker.
OTHER_DEFAULTS = {
    "Damage": ("dps", 0.2),              # flat damage on top of the weapon (weapon value unknown)
    "Cooldown Reduction": ("dps", 0.3),
    "Life Regeneration": ("surv", 0.05),
    "Life on Hit": ("surv", 0.05),
    "Bonus Potion Charges": ("surv", 2.0),
    "Health Potion Find": ("surv", 0.02),
    "Bonus Move Speed": ("farm", 0.1),
    "Thorns": ("surv", 0.0),
    "Max Mana": ("dps", 0.0),
    "Mana Regeneration": ("dps", 0.0),
    "Mana on Kill": ("dps", 0.0),
}

MODES = {
    "Damage": {"dps": 1.0, "surv": 0.2, "farm": 0.1},
    "Survival": {"dps": 0.2, "surv": 1.0, "farm": 0.05},
    "Balanced": {"dps": 1.0, "surv": 1.0, "farm": 0.3},
    "Farming": {"dps": 0.5, "surv": 0.2, "farm": 1.0},
}
VERDICT_MARGIN = 1.0  # % of the weighted score; inside = sidegrade


# ----------------------------------------------------------------------------- legendaries

def load_legendaries() -> list:
    try:
        with open(paths.res("legendaries.json"), encoding="utf-8") as f:
            return json.load(f)["items"]
    except Exception:
        return []


LEGENDARIES = load_legendaries()
_norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())


def find_legendary(name: str, effects: list) -> dict | None:
    """Match by item name, else by effect text (OCR may garble either)."""
    if name and name != "?":
        best = max(LEGENDARIES, key=lambda L: difflib.SequenceMatcher(None, _norm(L["name"]), _norm(name)).ratio(),
                   default=None)
        if best and difflib.SequenceMatcher(None, _norm(best["name"]), _norm(name)).ratio() >= 0.8:
            return best
    for fx in effects:
        best = max(LEGENDARIES, key=lambda L: difflib.SequenceMatcher(None, _norm(L["effect"]), _norm(fx)).ratio(),
                   default=None)
        if best and difflib.SequenceMatcher(None, _norm(best["effect"]), _norm(fx)).ratio() >= 0.7:
            return best
    return None


def _first_pct(text: str) -> float | None:
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*%", text or "")
    return float(m.group(1).replace(",", ".")) if m else None


# ----------------------------------------------------------------------------- character model

@dataclass
class Context:
    char: dict                     # {stat: value} from the character sheet
    hero: str = ""                 # "Sorcerer", "Warrior", "Hunter", "Monk" (from the log)
    level: int = 0
    element: str = "Auto"
    enemy_level: int | None = None
    damage_weights: dict = field(default_factory=dict)  # {"Fire": 0.6, "Physical": 0.4} from deaths
    dot_share: float = 0.2         # share of incoming damage that is damage over time (not dodgeable)
    other: dict = field(default_factory=dict)            # stat -> (target, % per unit)
    overrides: dict = field(default_factory=dict)        # legendary name -> {"dps": %, "surv": %}
    weapon: tuple | None = None    # (damage, speed) of the equipped weapon, from its last read tooltip
    gem_tier: int = 3              # gems assumed for empty sockets
    ability_shares: dict = field(default_factory=dict)  # ability -> share of damage (Talents page / skill bar)

    @property
    def main(self) -> str:
        if self.hero in CLASS_MAIN:
            return CLASS_MAIN[self.hero]
        present = [(self.char.get(s, 0), s) for s in MAIN_STATS]
        return max(present)[1]

    @property
    def elem(self) -> str:
        if self.element and self.element != "Auto":
            return self.element
        present = [(self.char.get(f"{e} Damage", 0), e) for e in ELEMENTS]
        return max(present)[1] if any(v for v, _ in present) else "Lightning"


def dps_factor(v: dict, ctx: Context) -> float:
    g = lambda k: v.get(k, 0.0)
    main = g(ctx.main) * (1 + g("Main Stat %") / 100)
    f = 1 + main / 100
    f *= 1 + (g(f"{ctx.elem} Damage") + g("All Damage") + g("Element Damage")) / 100
    f *= 1 + min(max(g("Critical Hit Chance"), 0), CAP) / 100 * g("Critical Hit Damage") / 100
    f *= 1 + g("Attack Speed Bonus") / 100
    f *= 1 + (STRONG_SHARE * g("Strong Attack Damage") + BASIC_SHARE * g("Basic Attack Damage")) / 100
    f *= 1 + sum(u * g(k) for k, u in UPTIME.items()) / 100
    return f


def _final(v: dict, base: dict, flat: str, pct: str) -> float:
    """Sheet value of a stat that has a flat part and a % bonus (Max Health/Bonus Health, Armor/Bonus Armor).
    v holds the base values plus item deltas; the flat delta is applied before the % bonus."""
    pre = base.get(flat, 0.0) / (1 + base.get(pct, 0.0) / 100)
    return (pre + v.get(flat, 0.0) - base.get(flat, 0.0)) * (1 + v.get(pct, 0.0) / 100)


def defense(v: dict, base: dict, ctx: Context) -> dict:
    lvl = ctx.enemy_level or ctx.level or int(base.get("Level", 60))
    hp = _final(v, base, "Max Health", "Bonus Health") * (1 + v.get("Max Health %", 0.0) / 100)
    armor = _final(v, base, "Armor", "Bonus Armor")
    mr = v.get("Magic Resist", 0.0)
    cut = lambda r: r / (r + 50 * lvl) if r > 0 else 0.0
    dodge = min(max(v.get("Dodge Chance", 0.0), 0), CAP) / 100
    dr = min(v.get("Damage Reduction", 0.0), 100) / 100
    phys_dr = v.get("Physical Damage Reduction", 0.0) + v.get("Phys DR from Dodge %", 0.0) / 100 * dodge * 100
    mult = {}
    for e in ELEMENTS:
        edr = (min(phys_dr, 100) if e == "Physical" else min(v.get(f"{e} Damage Reduction", 0.0), 100)) / 100
        r_cut = cut(armor) if e == "Physical" else cut(mr)
        hit = (1 - dodge * (1 - ctx.dot_share))  # damage over time cannot be dodged
        mult[e] = hit * (1 - r_cut) * (1 - edr) * (1 - dr)
    w = ctx.damage_weights or {e: 1 / len(ELEMENTS) for e in ELEMENTS}
    incoming = sum(w.get(e, 0) * mult[e] for e in ELEMENTS) / max(sum(w.values()), 1e-9)
    # the game's own Toughness (no damage reductions, hero level)
    hl = ctx.level or lvl
    hcut = lambda r: r / (r + 50 * hl) if r > 0 else 0.0
    tough = hp / ((1 - dodge) * (1 - 0.5 * (hcut(armor) + hcut(mr))))
    return {"hp": hp, "ehp": hp / incoming if incoming > 0 else hp, "toughness": tough}


def weapon_factor(d: dict, base: dict, weapon) -> float:
    """Change of damage per second from weapon damage, flat Damage and weapon speed.
    The character sheet's "Damage" is only the flat part; the weapon adds its own damage on top."""
    if not weapon:
        return 1.0
    w_dmg, w_speed = weapon
    hit0 = w_dmg + base.get("Damage", 0.0)
    f = (hit0 + d.get("Weapon Damage", 0.0) + d.get("Damage", 0.0)) / hit0 if hit0 > 0 else 1.0
    if w_speed:
        f *= max(w_speed + d.get("Weapon Speed", 0.0), 0.01) / w_speed
    return f


WEAPON_STATS = ("Weapon Damage", "Weapon Speed", "Damage")


def best_gem(slot: str | None, ctx: "Context", mode: str):
    """Best gem of ctx.gem_tier for a socket of this slot in this mode -> (gem name, stat, value, is_pct) or None."""
    import item_quality
    group = item_quality.GEM_GROUP.get(slot or "")
    if not group or not ctx.char:
        return None
    best, best_score = None, None
    for gem, grp, stat, val, pct, name in item_quality.gem_options(ctx.gem_tier):
        if grp != group:
            continue
        sc = evaluate_deltas({stat: (val, pct)}, ctx, mode).score
        if best_score is None or sc > best_score:
            best, best_score = (name, stat, val, pct), sc
    return best


def gem_plan(res, ctx: "Context", mode: str) -> dict:
    """Gems of both items: the ones they carry plus the best gem in every empty socket.
    Gems are not part of the game's own comparison numbers, so they are added here."""
    import item_quality
    out = {}
    for side in ("new", "old"):
        it = getattr(res, f"{side}_item", None)
        if it is None:
            continue
        slot = item_quality.item_kind(it.type_line)[1]
        fill = best_gem(slot, ctx, mode) if it.empty_sockets else None
        stats = {}
        for st in it.sockets:
            v, p = stats.get(st.name, (0.0, st.pct))
            stats[st.name] = (v + st.value, p)
        if fill:
            v, p = stats.get(fill[1], (0.0, fill[3]))
            stats[fill[1]] = (v + fill[2] * it.empty_sockets, p)
        out[side] = {"fill": fill, "empty": it.empty_sockets, "stats": stats,
                     "have": [(st.name, st.value, st.pct) for st in it.sockets]}
    return out


# ----------------------------------------------------------------------------- evaluation

@dataclass
class Evaluation:
    dps_pct: float = 0.0
    surv_pct: float = 0.0
    farm: dict = field(default_factory=dict)   # {"Gold": %, "Items": %, "EXP": %}
    score: float = 0.0
    verdict: str = ""
    confident: bool = True
    reasons: list = field(default_factory=list)   # why it is uncertain
    rows: list = field(default_factory=list)      # (stat, delta, is_pct, effect text, weighted %)
    gems: dict = field(default_factory=dict)      # gem_plan(): per side what sits / would sit in the sockets
    effects: list = field(default_factory=list)   # (sign, item, effect, valuation text, known)
    tough_old: float = 0.0
    tough_new: float = 0.0
    main: str = ""
    element: str = ""

    @property
    def farm_pct(self) -> float:
        return sum(self.farm.values()) / len(self.farm) if self.farm else 0.0


def _ability_effect(text, ctx):
    """Effects that depend on the abilities used (procs, ability damage): (dps %, surv %, text) or None."""
    if not ctx.ability_shares:
        return None
    try:
        import effects
        return effects.rate(text, ctx.hero, ctx.ability_shares, ctx)
    except Exception:
        return None


def _legendary_deltas(L: dict, ocr_effect: str, sign: int, ctx: Context, base: dict):
    """Pseudo stat deltas of a legendary effect -> (deltas, valuation text, known?, fixed (dps%, surv%))."""
    ov = ctx.overrides.get(L["name"])
    if ov:
        return {}, f"own value: {ov.get('dps', 0):+g} % damage, {ov.get('surv', 0):+g} % survival", True, \
            (sign * ov.get("dps", 0), sign * ov.get("surv", 0))
    scale = 1.0
    t_num, o_num = _first_pct(L["effect"]), _first_pct(ocr_effect)
    if t_num and o_num and abs(o_num - t_num) / t_num < 2:  # rolled higher/lower (e.g. ancient)
        scale = o_num / t_num
    if "manual" in L:
        auto = _ability_effect(ocr_effect or L["effect"], ctx)
        if auto:
            dps, surv, txt = auto
            return {}, txt, True, (sign * dps, sign * surv)
        return {}, f"cannot be calculated ({L['manual']})", False, (0, 0)
    if "elements" in L and ctx.elem not in L["elements"]:
        return {}, f"does not affect {ctx.elem} damage", True, (0, 0)
    if "special" in L:
        sp, val = L["special"], L["value"] * scale
        key = {"main_stat_pct": "Main Stat %", "phys_dr_from_dodge": "Phys DR from Dodge %",
               "max_health_pct": "Max Health %"}[sp]
        note = f" · {L['note']}" if L.get("note") else ""
        return {key: sign * val}, f"{key.replace(' %', '')} {val:+g} %{note}", "note" not in L, (0, 0)
    up = L.get("uptime", 1.0)
    deltas = {k: sign * v * scale * up for k, v in L["stats"].items()}
    txt = ", ".join(f"{k} {v * scale:+g}" for k, v in L["stats"].items())
    return deltas, txt + (f" (assumed {up * 100:.0f} % active)" if up < 1 else ""), True, (0, 0)


def evaluate(res, ctx: Context, mode: str = "Balanced") -> Evaluation:
    """res: item_ocr.ItemResult of a comparison tooltip."""
    base = dict(ctx.char)
    ev = Evaluation(main=ctx.main, element=ctx.elem)
    if not base:
        ev.confident = False
        ev.reasons.append("Character not read yet (F9)")
    # items list "+3.9% Attack Speed"; the character sheet calls that "Attack Speed Bonus"
    # ("Attack Speed" there is attacks per second)
    src = {("Attack Speed Bonus" if k == "Attack Speed" and p else k): (d, p) for k, (d, p) in res.deltas.items()}
    deltas = {k: d for k, (d, _) in src.items()}
    pct_flags = {k: p for k, (_, p) in src.items()}
    weapon = ctx.weapon
    old_it = getattr(res, "old_item", None)
    if old_it is not None:
        wb = {st.name: st.value for st in old_it.base}
        if "Weapon Damage" in wb:  # comparing weapons: the equipped one is right here
            weapon = (wb["Weapon Damage"], wb.get("Weapon Speed") or (weapon[1] if weapon else None))
    if weapon is None and ("Weapon Damage" in deltas or "Damage" in deltas):
        ev.reasons.append("Weapon damage unknown – compare a weapon once (F8)")

    # legendary effects: add the new one, remove the old one
    fixed_dps = fixed_surv = 0.0
    eff_deltas: dict = {}
    for sign, name, fx_list in ((+1, res.name, res.effects_new_all), (-1, res.old_name, res.effects_old_all)):
        if not fx_list and not name:
            continue
        L = find_legendary(name, fx_list)
        if not L:
            for fx in fx_list:
                auto = _ability_effect(fx, ctx)
                if auto:
                    fixed_dps += sign * auto[0]
                    fixed_surv += sign * auto[1]
                    ev.effects.append((sign, name, fx, auto[2], True))
                    continue
                ev.effects.append((sign, name, fx, "unknown effect – not rated", False))
                ev.confident = False
            continue
        other_side = res.old_name if sign > 0 else res.name
        if other_side and difflib.SequenceMatcher(None, _norm(other_side), _norm(L["name"])).ratio() >= 0.8:
            continue  # same legendary on both sides: effect does not change
        ocr_fx = next(iter(fx_list), L["effect"])
        d, txt, known, (fd, fs) = _legendary_deltas(L, ocr_fx, sign, ctx, base)
        for k, val in d.items():
            eff_deltas[k] = eff_deltas.get(k, 0.0) + val
        fixed_dps += fd
        fixed_surv += fs
        ev.effects.append((sign, L["name"], L["effect"], txt, known))
        if not known:
            ev.confident = False
            ev.reasons.append(f"Effect of {L['name']} not rated")

    if not base:
        return ev

    # gems: what both items carry, best gem of the chosen tier in every empty socket
    gem_d = {}
    if getattr(res, "new_item", None) is not None and mode in MODES:
        ev.gems = gem_plan(res, ctx, mode)
        for side, sign in (("new", 1), ("old", -1)):
            for k, (v, _) in ev.gems.get(side, {}).get("stats", {}).items():
                k = "Attack Speed Bonus" if k == "Attack Speed" else k
                gem_d[k] = gem_d.get(k, 0.0) + sign * v

    new = dict(base)
    for k, d in list(deltas.items()) + list(eff_deltas.items()) + list(gem_d.items()):
        if k in ("Weapon Damage", "Weapon Speed"):
            continue
        new[k] = new.get(k, 0.0) + d

    all_d = dict(deltas)
    for k, v in gem_d.items():
        all_d[k] = all_d.get(k, 0.0) + v
    wf = weapon_factor(all_d, base, weapon)
    f0, f1 = dps_factor(base, ctx), dps_factor(new, ctx) * wf
    d0, d1 = defense(base, base, ctx), defense(new, base, ctx)
    ev.tough_old, ev.tough_new = d0["toughness"], d1["toughness"]
    ev.dps_pct = (f1 / f0 - 1) * 100 + fixed_dps
    if d0["ehp"] <= 0:  # sheet without Max Health (only part of the list read): survival unknown
        d0 = d1 = {"ehp": 1.0, "hp": 0.0, "toughness": 0.0}
        ev.confident = False
        ev.reasons.append("Max Health missing – read the full character sheet (F9 at the top and at the bottom)")
    ev.surv_pct = (d1["ehp"] / d0["ehp"] - 1) * 100 + fixed_surv
    for stat, label in FARM_STATS.items():
        ev.farm[label] = ((1 + new.get(stat, 0) / 100) / (1 + base.get(stat, 0) / 100) - 1) * 100

    # stats outside the models
    other = {**OTHER_DEFAULTS, **ctx.other}
    unused = (set(MAIN_STATS) - {ctx.main}) | {f"{e} Damage" for e in ELEMENTS if e != ctx.elem}
    for k, d in deltas.items():
        if k == "Damage" and weapon:
            continue  # part of the weapon factor
        if k in other and k not in unused:
            target, per = other[k]
            add = d * per
            if target == "dps":
                ev.dps_pct += add
            elif target == "surv":
                ev.surv_pct += add
            else:
                for lab in ev.farm:
                    ev.farm[lab] += add

    # per-stat rows: effect of that single change; gem rows after the item's own stats
    gem_rows = []
    for side, sign in (("new", 1), ("old", -1)):
        g = ev.gems.get(side)
        if not g:
            continue
        for k, (v, p) in g["stats"].items():
            n_fill = g["empty"] if g["fill"] and g["fill"][1] == k else 0
            have = sum(1 for h in g["have"] if h[0] == k)
            what = []
            if have:
                what.append(f"{have}× socketed")
            if n_fill:
                what.append(f"{n_fill}× suggested")
            label = f"Socket: {k}"  # sign tells new (+) from equipped (-)
            gem_rows.append((label, k, sign * v, p, ", ".join(what)))
    for label, k, d, is_pct, note in [(k, k, d, pct_flags.get(k, False), "") for k, d in deltas.items()] + gem_rows:
        one = dict(base)
        if k not in ("Weapon Damage", "Weapon Speed"):
            one[k] = one.get(k, 0.0) + d
        parts = []
        if k in unused:
            parts.append(f"no effect ({ctx.main if k in MAIN_STATS else ctx.elem + ' build'})")
        else:
            p_dps = (dps_factor(one, ctx) * weapon_factor({k: d}, base, weapon) / f0 - 1) * 100
            p_surv = (defense(one, base, ctx)["ehp"] / d0["ehp"] - 1) * 100 if d0["hp"] > 0 else 0.0
            if k == "Damage" and weapon:
                pass
            elif k in other:
                tgt, per = other[k]
                if tgt == "dps":
                    p_dps += d * per
                elif tgt == "surv":
                    p_surv += d * per
            if abs(p_dps) >= 0.05:
                parts.append(f"{p_dps:+.1f} % Damage")
            if abs(p_surv) >= 0.05:
                parts.append(f"{p_surv:+.1f} % Survival")
            if k in FARM_STATS:
                fp = ((1 + one.get(k, 0) / 100) / (1 + base.get(k, 0) / 100) - 1) * 100
                parts.append(f"{fp:+.1f} % {FARM_STATS[k]}")
            if not parts:
                parts.append("no effect" if k in WEAPON_STATS or note else "not rated")
        if note:
            parts.append(note)
        w = MODES[mode]
        weighted = 0.0
        for p in parts:
            m = re.match(r"([+-][\d.]+) % (Damage|Survival|Gold|Items|EXP)", p)
            if m:
                key = {"Damage": "dps", "Survival": "surv"}.get(m.group(2), "farm")
                weighted += float(m.group(1)) * w[key] / (3 if key == "farm" else 1)
        ev.rows.append((label, d, is_pct, " · ".join(parts), weighted))
    ev.rows.sort(key=lambda r: -abs(r[4]))

    w = MODES[mode]
    ev.score = w["dps"] * ev.dps_pct + w["surv"] * ev.surv_pct + w["farm"] * ev.farm_pct
    lvl = ctx.level or int(base.get("Level", 0))
    if res.req_level and lvl and res.req_level > lvl:
        ev.reasons.append(f"requires level {res.req_level}")
    if ev.score > VERDICT_MARGIN:
        ev.verdict = "EQUIP LATER" if res.req_level and lvl and res.req_level > lvl else "EQUIP"
    elif ev.score < -VERDICT_MARGIN:
        ev.verdict = "DO NOT EQUIP"
    else:
        ev.verdict = "SIDEGRADE"
    return ev


def evaluate_deltas(deltas: dict, ctx: Context, mode: str = "Balanced") -> Evaluation:
    """Evaluate plain stat changes (gems, upgrade gains) without an item tooltip."""
    class _R:
        pass
    r = _R()
    r.deltas, r.name, r.old_name, r.req_level = deltas, "", "", None
    r.effects_new_all, r.effects_old_all = [], []
    return evaluate(r, ctx, mode)

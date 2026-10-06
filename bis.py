"""Best-in-slot: the best theoretical item per slot for the logged-in character and a mode.

A Legendary is valued with its effect plus the best attributes its slot can roll for the class
(4 primary + 1 secondary, item level 850, top roll 1.15 = Ancient), a Divine with its fixed numbers
and effect. Both get the best gem of the chosen tier in every socket. Values come from the same
item_eval model as the Item Comparer, so they use the character sheet of the logged-in character.
"""
import json

import item_eval
import item_quality
import paths
import stages

MAX_ILVL = 850   # highest item level that drops (Inferno, level 70 enemies)
TOP_ROLL = 1.15  # Ancient: every number at the top of its range
SLOTS = ["Weapon", "Helm", "Chest Armor", "Pants", "Boots", "Belt", "Ring", "Necklace", "Gloves", "Shoulder", "Back"]
ARMOR_SLOTS = {"Helm", "Chest Armor", "Pants", "Boots", "Gloves", "Shoulder"}


def _load():
    try:
        with open(paths.res("data", "items.json"), encoding="utf-8") as f:
            return json.load(f)["items"]
    except Exception:
        return []


ITEMS = _load()


def _affix(stat):
    return item_quality.AFFIX.get("affixes", {}).get(stat) or {}


def affix_value(stat, ilvl=MAX_ILVL, roll=TOP_ROLL):
    a = _affix(stat)
    if a.get("fixed_value") is not None:  # e.g. Bonus Potion Charges: always +1
        return float(a["fixed_value"]), bool(a.get("percent"))
    m = item_quality.RARITY_M.get("Legendary", 1.6)
    v = m * ilvl * a.get("per_level", 0) * roll
    return (round(v, 1), True) if a.get("percent") else (float(round(v)), False)


def class_can_roll(stat, cls):
    allowed = _affix(stat).get("classes")
    return not allowed or (cls or "").lower() in [c.lower() for c in allowed]


def base_values(slot):
    """Implicit numbers of a top Legendary: armor, or weapon damage at speed 1.0 (all weapons share one DPS range)."""
    m = item_quality.RARITY_M.get("Legendary", 1.6)
    if slot in ARMOR_SLOTS:
        return {"Armor": (round(m * 0.5 * MAX_ILVL * TOP_ROLL), False)}
    if slot == "Weapon":
        return {"Weapon Damage": (round(m * (0.2 * MAX_ILVL + 5) * TOP_ROLL), False), "Weapon Speed": (1.0, False)}
    return {}


def _rate(deltas: dict, ctx, mode, name="", effect=""):
    """Evaluation of adding these numbers (and this effect) to the character."""
    class R:
        pass
    r = R()
    d = dict(deltas)
    if ctx.weapon and "Weapon Damage" in d:  # weapons: compared with the equipped one
        d["Weapon Damage"] = (d["Weapon Damage"][0] - ctx.weapon[0], False)
        d["Weapon Speed"] = (d.get("Weapon Speed", (ctx.weapon[1], False))[0] - ctx.weapon[1], False)
    elif "Weapon Damage" in d:
        d.pop("Weapon Damage")
        d.pop("Weapon Speed", None)
    r.deltas, r.name, r.old_name, r.req_level = d, name, "", None
    r.effects_new_all, r.effects_old_all = ([effect] if effect else []), []
    return item_eval.evaluate(r, ctx, mode)


def best_attributes(slot, ctx, mode):
    """[(stat, value, is_pct, score)] of the 4 primary + 1 secondary attributes worth most."""
    pools = item_quality.AFFIX.get("slots", {}).get(slot, {})
    cls = ctx.hero
    out = []
    for section, n in (("primary", 4), ("secondary", 1)):
        rated = []
        for stat in pools.get(section, []):
            if not class_can_roll(stat, cls):
                continue
            v, pct = affix_value(stat)
            rated.append((item_eval.evaluate_deltas({stat: (v, pct)}, ctx, mode).score, stat, v, pct))
        rated.sort(reverse=True)
        out += [(stat, v, pct, sc) for sc, stat, v, pct in rated[:n]]
    return out


def candidates(slot, ctx, mode):
    """Every Legendary/Divine of the slot the class may wear, best first."""
    cls = ctx.hero
    attrs = best_attributes(slot, ctx, mode) if slot != "Back" else []
    gem = item_eval.best_gem(slot, ctx, mode)
    n_sockets = item_quality.sockets(slot)
    out = []
    for it in ITEMS:
        if it["slot"] != slot or (cls and cls not in it["classes"]):
            continue
        sockets = min(it.get("max_sockets", 0), n_sockets)
        if it["rarity"] == "Divine":
            numbers = {k: (v, False) for k, v in it.get("base", {}).items() if k != "Weapon DPS"}
            stats = [(s, v, p) for s, v, p in it.get("stats", [])]
        else:
            numbers = dict(base_values(slot))
            stats = [(s, v, p) for s, v, p, _ in attrs]
        deltas = dict(numbers)
        for s, v, p in stats:
            k = "Attack Speed Bonus" if s == "Attack Speed" and p else s
            deltas[k] = (deltas.get(k, (0, p))[0] + v, p)
        if gem and sockets:
            deltas[gem[1]] = (deltas.get(gem[1], (0, gem[3]))[0] + gem[2] * sockets, gem[3])
        ev = _rate(deltas, ctx, mode, it["name"], it.get("effect", ""))
        known = all(k for *_, k in ev.effects)
        out.append({"item": it, "ev": ev, "score": ev.score, "numbers": numbers, "stats": stats, "deltas": deltas,
                    "gem": gem, "sockets": sockets, "effect_known": known})
    out.sort(key=lambda c: -c["score"])
    return out


def _equipped_deltas(eq: dict) -> dict:
    deltas = {}
    for k, (v, p) in (eq.get("stats") or {}).items():
        if k == "Weapon DPS":
            continue
        k = "Attack Speed Bonus" if k == "Attack Speed" and p else k
        deltas[k] = (deltas.get(k, (0, p))[0] + v, p)
    return deltas


def swap(c: dict, eq: dict, ctx, mode):
    """Evaluation of replacing the equipped item eq (as read with F8) by candidate c."""
    new, old = c["deltas"], _equipped_deltas(eq)
    d = {}
    for k in set(new) | set(old):
        v = new.get(k, (0, False))[0] - old.get(k, (0, False))[0]
        if abs(v) > 1e-9:
            d[k] = (v, new.get(k, old.get(k))[1])

    class R:
        pass
    r = R()
    r.deltas, r.name, r.old_name, r.req_level = d, c["item"]["name"], eq.get("name", ""), None
    r.effects_new_all = [c["item"]["effect"]] if c["item"].get("effect") else []
    r.effects_old_all = list(eq.get("effects") or [])
    return item_eval.evaluate(r, ctx, mode)


def farm_text(it, stage_rows=None, stage_level=None) -> list:
    """Where the item comes from, as short lines. stage_level(stage) -> enemy level on Normal or None."""
    lines = []
    for d in it.get("enemy_drops", []):
        lines.append(f"{d['enemy']} in {d['stage']} stage {d['stage_no']}: {d['chance']} per clear, "
                     f"all difficulties")
    if it.get("world_drop"):
        lo = it.get("min_drop_level") or 1
        lines.append(f"Any enemy of level {lo}+ and their chests (item level 850 only on Inferno)")
        if stage_rows and stage_level:
            ok = [r for r in stage_rows if stages.base_difficulty(r["difficulty"]) in ("Nightmare", "Inferno")
                  or (stage_level(r["stage"]) or 0) >= lo]
            ok = [r for r in ok if r["runs"] >= 3]
            if ok:
                best = max(ok, key=lambda r: r["items_h"])
                lines.append(f"Your best stage for it: {best['stage']} ({best['difficulty']}), "
                             f"{best['items_h']:.0f} items/h")
    if it.get("divine_drop"):
        lines.append("Divine: level 70 elites and bosses on Inferno and their chests – extremely rare")
    if it.get("mystery_vendor"):
        lines.append("Mystery Vendor (Lady “Shadow”, Soul Shards): random rarity for your hero")
    return lines

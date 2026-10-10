"""Runes: what every rune and rune set is worth for the character, the best runes for the slots, and the
Rune Transmute at the Alchemists.

Data: data/runes.json (tools/scrape_runes.py, afkmeta.com): 13 sets with bonuses at 2, 4 and 6 runes, every rune
with its values at rune level 1 and 6, the hero levels that open the 6 slots.

Values go through the same model as talents (talent_model): rune stats as character sheet stats, set bonus texts
read like talent texts (statuses, procs, ability bonuses ...), "raises the level of <ability>" as more damage of
that ability by its share of your damage.
"""
import json

import paths
import talent_model

LEVEL_GAIN = 3.0  # % more damage of an ability per ability level (Ice Shards: 115 % + 3.5 points per level)

# Rune Transmute (patch 1.0.2): 9 runes of one rarity -> 1 random rune usable by the character's class
TRANSMUTE = {
    "uncommon": [("rare", 0.50), ("uncommon", 0.50)],
    "rare": [("grand", 0.15), ("rare", 0.85)],
    "grand": [("set", 0.50), ("grand", 0.50)],
    "set": [("set", 1.00)],
}
KIND_NAMES = {"uncommon": "Uncommon", "rare": "Rare", "grand": "Legendary (Grand)", "set": "Rune Set"}

_DATA = None


def data() -> dict:
    global _DATA
    if _DATA is None:
        try:
            with open(paths.res("data", "runes.json"), encoding="utf-8") as f:
                _DATA = json.load(f)
        except Exception:
            _DATA = {"sets": [], "runes": [], "slots": []}
    return _DATA


def runes_of(hero: str) -> list:
    return [r for r in data()["runes"] if r["class"] in (hero, "All")]


def sets_of(hero: str) -> list:
    return [s for s in data()["sets"] if s["class"] == hero]


def slots_open(level: int) -> int:
    return sum(1 for lv in data().get("slots", []) if level >= lv)


def value_at(st: dict, level: int) -> float:
    """A rune value at rune level 1..6 (linear between the listed level 1 and 6 values)."""
    if st.get("l1") is None:
        return 0.0
    l6 = st["l6"] if st.get("l6") is not None else st["l1"]
    return st["l1"] + (l6 - st["l1"]) * (min(max(level, 1), 6) - 1) / 5


def _sheet_stats(rune: dict, level: int, char: dict) -> dict:
    """A rune's numbers as character sheet stats."""
    out = {}
    for st in rune.get("stats", []):
        v = value_at(st, level)
        name = st["stat"]
        if not v:
            continue
        if name == "Attack Speed" and st["pct"]:
            name = "Attack Speed Bonus"
        elif name == "Bonus Magic Resist":
            name, v = "Magic Resist", float(char.get("Magic Resist", 0.0)) * v / 100
        elif name == "Mana Regeneration" and st["pct"]:
            v = float(char.get("Mana Regeneration", 0.0)) * v / 100
        elif name == "Bonus Move Speed":
            pass
        out[name] = out.get(name, 0.0) + v
    return out


def rate_rune(F, current: dict, rune: dict, level: int, mode: str) -> dict:
    """{"dps", "surv", "score", "notes"} of one rune at a rune level."""
    stats = _sheet_stats(rune, level, F.char)
    fx, notes = [], []
    ab = rune.get("raises")
    if ab:
        if ab in F.use:
            fx.append({"k": "dmg", "target": ab, "v": LEVEL_GAIN})
            notes.append(f"+1 level {ab} ≈ +{LEVEL_GAIN:g} % of its damage")
        else:
            notes.append(f"raises {ab} – not in your build")
    dps, surv, _farm, score = talent_model.evaluate_extra(F, current, fx, mode, stats)
    return {"dps": dps, "surv": surv, "score": score, "notes": notes}


def rate_bonus(F, current: dict, text: str, mode: str) -> dict:
    """A set bonus text, read like a talent text."""
    fx = [e for e in talent_model.parse(text, F.hero) if e["k"] != "none"]
    if not fx:
        return {"dps": 0.0, "surv": 0.0, "score": 0.0, "rated": False}
    dps, surv, _farm, score = talent_model.evaluate_extra(F, current, fx, mode)
    return {"dps": dps, "surv": surv, "score": score, "rated": True}


def best_loadout(F, current: dict, hero: str, level: int, slots: int, mode: str) -> list:
    """Best runes for the open slots: each set at 0/2/4/6 pieces (its best runes) plus the best single runes in the
    other slots, and two sets 4 + 2 / 2 + 2 (+2). -> [(score, [runes], [(set, pieces)], dps, surv)] best first."""
    rs = runes_of(hero)
    rated = {r["id"]: rate_rune(F, current, r, level, mode) for r in rs}
    singles = sorted([r for r in rs if not r.get("set")], key=lambda r: -rated[r["id"]]["score"])
    by_set = {s["name"]: sorted([r for r in rs if r.get("set") == s["name"]], key=lambda r: -rated[r["id"]]["score"])
              for s in sets_of(hero)}
    bonus = {s["name"]: {int(k): rate_bonus(F, current, t, mode) for k, t in s["bonus"].items()} for s in sets_of(hero)}

    def score_of(parts):
        runes, sets = [], []
        for name, k in parts:
            runes += by_set[name][:k]
            sets.append((name, k))
        rest = slots - len(runes)
        if rest < 0:
            return None
        # same rune twice is not possible: single runes are all different, set runes are named per piece
        runes += singles[:rest]
        fx_bonus = sum(bonus[n][p]["score"] for n, k in sets for p in (2, 4, 6) if k >= p)
        sc = sum(rated[r["id"]]["score"] for r in runes) + fx_bonus
        d = sum(rated[r["id"]]["dps"] for r in runes) + sum(bonus[n][p]["dps"] for n, k in sets for p in (2, 4, 6) if k >= p)
        s_ = sum(rated[r["id"]]["surv"] for r in runes) + sum(bonus[n][p]["surv"] for n, k in sets for p in (2, 4, 6) if k >= p)
        return sc, runes, sets, d, s_

    options = [[]]
    names = list(by_set)
    for n in names:
        for k in (2, 4, 6):
            options.append([(n, k)])
    for a in names:
        for b in names:
            if a != b:
                options.append([(a, 4), (b, 2)])
                if a < b:
                    options.append([(a, 2), (b, 2)])
    out = []
    for o in options:
        r = score_of(o)
        if r is not None:
            out.append(r)
    out.sort(key=lambda x: -x[0])
    seen, uniq = set(), []
    for x in out:
        key = tuple(sorted(r["id"] for r in x[1]))
        if key not in seen:
            seen.add(key)
            uniq.append(x)
    return uniq


def transmute(counts: dict) -> dict:
    """Expected result of transmuting the runes counted per rarity, 9 at a time: {"batches", "left", "expect"}."""
    out = {"batches": {}, "left": {}, "expect": {}}
    for kind, n in counts.items():
        b = int(n) // 9
        out["batches"][kind] = b
        out["left"][kind] = int(n) - 9 * b
        for res, p in TRANSMUTE.get(kind, []):
            out["expect"][res] = out["expect"].get(res, 0.0) + b * p
    return out

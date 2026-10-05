"""Roll quality, upgrade projection and gem values (see data/*.json).

  attribute = M x item level x per_level x roll       M: Common 1.2, Uncommon 1.4, Rare 1.5, Legendary 1.6
  armor     = M x 0.5 x item level x roll             roll 0.85..1.15, Ancient always 1.15
  upgrade   : +5 % steps dealt round-robin over the item's numbers (Armor first, then attributes in tooltip
              order); 1 step per level for Common/Uncommon, 2 for Rare and above
  gold cost : 3 x (level + 1)^2 x item level per attempt, x2 for Ancient
"""
import paths
import json
import os
import re

DATA = paths.res("data")


def _load(name):
    try:
        with open(os.path.join(DATA, name), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


AFFIX = _load("affixes.json")
UPGRADE = _load("upgrades.json")
GEMS = _load("gems.json")
RARITY_M = AFFIX.get("rarity_multiplier", {"Common": 1.2, "Uncommon": 1.4, "Rare": 1.5, "Legendary": 1.6})
ROLL_MIN, ROLL_MAX = AFFIX.get("roll_min", 0.85), AFFIX.get("roll_max", 1.15)
ARMOR_SLOTS = {"Helm", "Chest Armor", "Pants", "Boots", "Gloves", "Shoulder"}
SLOTS = ["Weapon", "Helm", "Chest Armor", "Pants", "Boots", "Belt", "Ring", "Necklace", "Gloves", "Shoulder", "Back"]


def item_kind(type_line: str):
    """'Ancient Legendary Helm' -> ('Legendary', 'Helm', ancient=True)."""
    t = type_line or ""
    ancient = "ancient" in t.lower()
    rarity = next((r for r in ("Common", "Uncommon", "Rare", "Legendary", "Divine") if r.lower() in t.lower()), None)
    slot = next((s for s in SLOTS if s.lower() in t.lower()), None)
    if slot is None and re.search(r"\b(staff|mace|sword|axe|bow|hammer|wand|dagger)\b", t, re.I):
        slot = "Weapon"
    return rarity, slot, ancient


def affix_count(rarity: str) -> int:
    c = AFFIX.get("affix_counts", {}).get(rarity)
    return c["primary"] + c["secondary"] if isinstance(c, dict) else 99


# ----------------------------------------------------------------------------- roll quality (1)

def roll_of(stat: str, value: float, rarity: str, ilvl: int) -> float | None:
    m = RARITY_M.get(rarity)
    if not m or not ilvl:
        return None
    per = 0.5 if stat == "Armor" else (AFFIX.get("affixes", {}).get(stat) or {}).get("per_level")
    if not per:
        return None
    base = m * ilvl * per
    return abs(value) / base if base else None


def quality(roll: float) -> float:
    return max(0.0, min(100.0, (roll - ROLL_MIN) / (ROLL_MAX - ROLL_MIN) * 100))


def roll_report(stats: list, rarity: str, ilvl: int) -> dict:
    """stats: [(name, value)] of one item in tooltip order -> {name: (roll, quality%)} + summary."""
    out = {}
    for name, val in stats:
        r = roll_of(name, val, rarity, ilvl)
        if r is not None:
            out[name] = (r, quality(r))
    qs = [q for _, q in out.values()]
    over = [n for n, (r, _) in out.items() if r > ROLL_MAX + 0.03]
    return {"stats": out, "avg": sum(qs) / len(qs) if qs else None, "upgraded_or_gem": over}


# ----------------------------------------------------------------------------- upgrades (2)

def steps_per_number(n_numbers: int, level: int, rarity: str) -> list:
    per = UPGRADE.get("steps_per_level", {}).get(rarity, 2 if rarity in ("Rare", "Legendary", "Divine") else 1)
    total = level * per
    if n_numbers <= 0:
        return []
    return [total // n_numbers + (1 if i < total % n_numbers else 0) for i in range(n_numbers)]


def upgrade_gain(stats: list, level: int, rarity: str) -> dict:
    """Extra value per stat after upgrading a +0 item to +level ({stat: +value})."""
    if level <= 0 or not stats:
        return {}
    pct = UPGRADE.get("step_pct", 5) / 100
    steps = steps_per_number(len(stats), level, rarity)
    gain = {}
    for (name, val), st in zip(stats, steps):
        gain[name] = gain.get(name, 0.0) + val * pct * st
    return gain


def upgrade_cost(level_to: int, ilvl: int, rarity: str, slot: str | None, ancient: bool) -> dict:
    """Expected cost from +0 to +level_to when every failed attempt keeps the level (failure rules
    are not documented). Gold and ores per attempt, divided by the success chance."""
    succ = UPGRADE.get("success", {})
    ores_base = UPGRADE.get("ores", {}).get(rarity, {})
    gold, ores, attempts = 0.0, {}, 0.0
    for cur in range(level_to):
        p = float(succ.get(str(cur + 1), 1)) or 1
        n = 1 / p
        attempts += n
        gold += n * 3 * (cur + 1) ** 2 * max(ilvl, 1) * (2 if ancient else 1)
        for ore, amt in ores_base.items():
            ores[ore] = ores.get(ore, 0) + n * amt * (cur + 1) ** 2
    boss = None
    bm = UPGRADE.get("boss_material", {})
    if rarity in bm.get("rarities", []) and level_to >= 9 and slot:
        boss = (bm.get("per_slot", {}).get(slot), bm.get("amount", 3) * (level_to - 8))
    return {"gold": gold, "ores": ores, "attempts": attempts, "boss": boss,
            "chance_last": float(succ.get(str(level_to), 1)) if level_to else 1.0}


# ----------------------------------------------------------------------------- gems (7)

GEM_GROUP = {"Weapon": "weapon", "Helm": "armor", "Chest Armor": "armor", "Pants": "armor",
             "Ring": "accessory", "Necklace": "accessory"}


def gem_options(tier: int) -> list:
    """[(gem, group, stat, value, is_pct, gem_name)] for one tier."""
    out = []
    for gem, d in GEMS.get("gems", {}).items():
        t = d.get("tiers", {}).get(str(tier))
        if not t:
            continue
        for group in ("weapon", "armor", "accessory"):
            b = t.get(group)
            if b:
                out.append((gem, group, b["stat"], b["value"], b.get("percent", False), t.get("name", gem)))
    return out


def sockets(slot: str) -> int:
    return GEMS.get("sockets", {}).get(slot, 0)


# ----------------------------------------------------------------------------- drops / soul shards (8)

def soul_shards(boss_level: int, difficulty: str) -> int:
    mult = {"Normal": 1, "Nightmare": 1.5, "Inferno": 2}.get(difficulty, 1)
    return int(max(1, boss_level // 10) * mult)


def drop_item_level(stage_level_min: int, difficulty: str) -> tuple:
    if difficulty == "Normal":
        return max(10, 10 * (stage_level_min - 1)), 10 * stage_level_min + 9
    b = 700 + stage_level_min
    if difficulty == "Inferno":
        b = 700 + 2 * stage_level_min
    return max(700, b - 5), b + 5

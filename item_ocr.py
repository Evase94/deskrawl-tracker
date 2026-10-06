"""Read character attributes and item tooltips from a game frame, and score item changes.

Engines:
  * Windows OCR (winocr)  - fast, keeps word spacing; good for the item tooltips.
  * RapidOCR (onnx)       - slower (~1.5 s) but reliable on the short right-aligned numbers
                            of the attribute panel, where Windows OCR drops most values.
"""
import paths
import difflib
import re
import unicodedata
import threading
from dataclasses import dataclass, field

try:  # must load before winocr/WinRT initializes, or onnxruntime segfaults later
    import onnxruntime  # noqa: F401
except Exception:
    pass

import cv2
import numpy as np

# Stat names as the game shows them in the Attributes panel. Used to canonicalize OCR output.
KNOWN_STATS = [
    "Intelligence", "Strength", "Dexterity", "Damage", "Attack Speed", "Attack Speed Bonus",
    "Critical Hit Chance", "Critical Hit Damage", "Fire Damage", "Cold Damage", "Lightning Damage",
    "Poison Damage", "Arcane Damage", "Physical Damage",
    "Damage vs Healthy", "Damage vs Injured", "Damage vs Distant", "Damage vs Elite", "Strong Attack Damage",
    "Max Health", "Bonus Health", "Thorns", "Armor", "Bonus Armor", "Magic Resist", "Dodge Chance",
    "Physical Damage Reduction", "Fire Damage Reduction", "Cold Damage Reduction",
    "Lightning Damage Reduction", "Poison Damage Reduction", "Arcane Damage Reduction",
    "Life Regeneration", "Life on Hit", "Max Mana", "Mana Regeneration", "Move Speed", "Bonus Move Speed",
    "Mana on Kill", "Cooldown Reduction", "Bonus Potion Charges", "Gold Find", "Item Find",
    "Health Potion Find", "XP Gained",
    # summary box
    "Health", "Attack", "Toughness", "Recovery",
]

# Points per unit (per 1 point, or per 1 % for percentage stats). Tuned as a rough DPS-first
# default for a caster; edit them in the "Gewichtung" tab.
DEFAULT_WEIGHTS = {
    "Intelligence": 1.0, "Strength": 1.0, "Dexterity": 1.0, "Damage": 2.0,
    "Attack Speed Bonus": 4.0, "Critical Hit Chance": 5.0, "Critical Hit Damage": 1.5,
    "Fire Damage": 1.0, "Cold Damage": 1.0, "Lightning Damage": 1.0, "Poison Damage": 1.0,
    "Arcane Damage": 1.0, "Physical Damage": 1.0,
    "Damage vs Healthy": 0.6, "Damage vs Injured": 0.6, "Damage vs Distant": 0.6, "Damage vs Elite": 0.8,
    "Strong Attack Damage": 0.6, "Cooldown Reduction": 3.0,
    "Max Health": 0.05, "Bonus Health": 1.0, "Thorns": 0.01, "Armor": 0.05, "Bonus Armor": 0.5,
    "Magic Resist": 0.1, "Dodge Chance": 1.5,
    "Physical Damage Reduction": 0.5, "Fire Damage Reduction": 0.5, "Cold Damage Reduction": 0.5,
    "Lightning Damage Reduction": 0.5, "Poison Damage Reduction": 0.5, "Arcane Damage Reduction": 0.5,
    "Life Regeneration": 0.2, "Life on Hit": 0.3, "Max Mana": 0.1, "Mana Regeneration": 0.5,
    "Bonus Move Speed": 0.2, "Mana on Kill": 0.5, "Bonus Potion Charges": 2.0,
    "Gold Find": 0.3, "Item Find": 0.5, "Health Potion Find": 0.05, "XP Gained": 0.5,
}

_KEYS = {re.sub(r"[^a-z]", "", s.lower()): s for s in KNOWN_STATS}


def canon(name: str) -> str:
    """Map an OCR'd stat name ("Thoms", "PhysicalDamageReduction", "cooldown Reduction") to its display name."""
    k = re.sub(r"[^a-z]", "", name.lower())
    if k in _KEYS:
        return _KEYS[k]
    m = difflib.get_close_matches(k, _KEYS.keys(), n=1, cutoff=0.82)
    return _KEYS[m[0]] if m else name.strip()


def parse_number(s: str) -> float | None:
    """'3,261' -> 3261 (thousands), '42,9' -> 42.9, '7.9' -> 7.9, '1,2' -> 1.2."""
    s = s.strip().replace("−", "-").replace("–", "-").replace(" ", "")
    if not re.fullmatch(r"[+-]?\d[\d.,]*", s):
        return None
    sign = -1 if s.startswith("-") else 1
    s = s.lstrip("+-")
    if "," in s and "." not in s:
        s = s.replace(",", "") if re.fullmatch(r"\d{1,3}(,\d{3})+", s) else s.replace(",", ".")
    else:
        s = s.replace(",", "")
    try:
        return sign * float(s)
    except ValueError:
        return None


# ----------------------------------------------------------------------------- OCR engines

@dataclass
class Line:
    text: str
    x: float
    y: float
    w: float
    h: float

    @property
    def cy(self):
        return self.y + self.h / 2

    def cx_center(self):
        return self.x + self.w / 2


def ocr_windows(img) -> list[Line]:
    import winocr
    out = []
    for l in winocr.recognize_cv2_sync(img, "en").get("lines", []):
        ws = l.get("words") or []
        if not ws:
            continue
        b0, b1 = ws[0]["bounding_rect"], ws[-1]["bounding_rect"]
        y0 = min(w["bounding_rect"]["y"] for w in ws)
        y1 = max(w["bounding_rect"]["y"] + w["bounding_rect"]["height"] for w in ws)
        out.append(Line(l["text"].strip(), b0["x"], y0, b1["x"] + b1["width"] - b0["x"], y1 - y0))
    return out


_rapid = None
_rapid_lock = threading.Lock()


def ocr_rapid(img) -> list[Line]:
    global _rapid
    with _rapid_lock:
        if _rapid is None:
            from rapidocr_onnxruntime import RapidOCR
            _rapid = RapidOCR()
        res, _ = _rapid(img)
    out = []
    for box, text, conf in res or []:
        xs = [p[0] for p in box]
        ys = [p[1] for p in box]
        out.append(Line(text.strip(), min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)))
    return out


# ----------------------------------------------------------------------------- attributes

RE_VALUE = re.compile(r"^(\d[\d.,]*%?|%\d+)$")  # "%6": OCR sometimes flips "9%" around


def parse_attributes(img) -> dict:
    """Return {stat: (value, is_pct)} plus 'Level' from the Attributes + Character panels."""
    lines = ocr_rapid(img)
    labels = [l for l in lines if re.search(r"[A-Za-z]{3}", l.text) and not RE_VALUE.match(l.text)]
    values = [l for l in lines if RE_VALUE.match(l.text.replace(" ", ""))]
    stats = {}
    for lab in labels:
        name = canon(lab.text)
        if name not in KNOWN_STATS:
            continue
        # value: same row, to the right, nearest
        best = None
        for v in values:
            if v.x <= lab.x + lab.w - 2 or abs(v.cy - lab.cy) > max(lab.h, v.h) * 0.6:
                continue
            if best is None or v.x < best.x:
                best = v
        if best is None:
            continue
        t = best.text.replace(" ", "")
        if t.startswith("%"):  # read upside down: "%6" is "9%"
            t = t[1:][::-1].translate(str.maketrans("69", "96")) + "%"
        num = parse_number(t.rstrip("%"))
        if num is not None:
            stats[name] = (num, t.endswith("%"))
    for l in lines:
        m = re.search(r"Lv\.?\s*(\d+)", l.text)
        if m:
            stats["Level"] = (int(m.group(1)), False)
    return stats


# ----------------------------------------------------------------------------- item tooltips

_NUM = r"[+\-−–]?\d[\d.,]*"
RE_STAT_DIFF = re.compile(rf"^\W*(?P<val>{_NUM})(?P<p1>%?)\s*(?P<name>[A-Za-z][A-Za-z' ]*?)\s*"
                          rf"\((?P<d>{_NUM})(?P<p2>%?)\)\W*$")
RE_STAT = re.compile(rf"^\W*(?P<val>{_NUM})(?P<p1>%?)\s*(?P<name>[A-Za-z][A-Za-z' ]*?)\s*$")
RE_LOST = re.compile(rf"^\W*(?P<name>[A-Za-z][A-Za-z' ]*?)\s*\((?P<d>{_NUM})(?P<p>%?)\)\W*$")
RARITIES = ("Common", "Uncommon", "Magic", "Rare", "Epic", "Legendary", "Set", "Unique", "Mythic")


@dataclass
class ItemResult:
    name: str = "?"
    type_line: str = ""
    item_level: int | None = None
    req_level: int | None = None
    mode: str = "none"  # compare | single | none
    deltas: dict = field(default_factory=dict)  # stat -> (delta, is_pct)
    effects_new: list = field(default_factory=list)
    effects_lost: list = field(default_factory=list)
    old_name: str = ""          # equipped item in a comparison tooltip
    icon: object = None         # BGR crop of the new item's picture (numpy array)
    old_icon: object = None
    old_item_level: int | None = None
    old_type_line: str = ""
    new_stats: list = field(default_factory=list)   # [(stat, value)] of the new item, tooltip order
    old_stats: list = field(default_factory=list)   # [(stat, value)] of the equipped item
    effects_new_all: list = field(default_factory=list)  # every effect line of the new item
    effects_old_all: list = field(default_factory=list)  # every effect line of the equipped item
    raw: list = field(default_factory=list)
    new_item: object = None     # ItemData of the hovered item
    old_item: object = None     # ItemData of the equipped item
    warnings: list = field(default_factory=list)


def _is_effect(text: str) -> bool:
    words = re.findall(r"[A-Za-z]+", text)
    return len(words) >= 4 and bool(re.search(r"[a-z]", text)) and not RE_STAT.match(text) \
        and not RE_STAT_DIFF.match(text) and "Level" not in text and "Stats lost" not in text


def _similar(a: str, b: str) -> bool:
    return difflib.SequenceMatcher(None, a.lower(), b.lower()).ratio() > 0.85


def _level_after(lines: list[Line], key: str, col) -> int | None:
    for l in lines:
        if key in l.text and col(l):
            m = re.search(r"(\d+)\s*$", l.text)
            if m:
                return int(m.group(1))
            nums = [o for o in lines if o.x > l.x + l.w - 2 and abs(o.cy - l.cy) < l.h * 0.7
                    and re.fullmatch(r"\d+", o.text)]
            if nums:
                return int(min(nums, key=lambda o: o.x).text)
    return None


_ITEM_NAMES = None


def _item_names() -> list:
    global _ITEM_NAMES
    if _ITEM_NAMES is None:
        import json
        import os
        try:
            with open(paths.res("data", "item_names.json"),
                      encoding="utf-8") as f:
                _ITEM_NAMES = json.load(f)["items"]
        except Exception:
            _ITEM_NAMES = []
    return _ITEM_NAMES


def fix_item_name(raw: str, type_line: str = "") -> str:
    """Correct an OCR'd item name with the list of all items (data/item_names.json). The decorative title font
    gets misread ("Bottotieless Potion Belt"); rarity and slot from the type line narrow the search."""
    names = _item_names()
    if not raw or not names:
        return raw
    key = lambda n: re.sub(r"[^a-z]", "", n.lower())
    t = (type_line or "").lower()
    pool = [n for n in names if (not t or n["rarity"].lower() in t) and (not t or n["slot"].lower() in t)] or names
    k = key(raw)
    best, score = None, 0.0
    for n in pool:
        r = difflib.SequenceMatcher(None, key(n["name"]), k).ratio()
        if r > score:
            best, score = n["name"], r
    return best if best and score >= 0.6 else raw


def _icon_left_of(img, type_line: Line):
    """The item picture sits left of the "<Rarity> <Slot>" line, about 3.8 text lines tall."""
    size = type_line.h * 3.8
    x1 = type_line.x - type_line.h * 0.6
    x0, y0 = x1 - size, type_line.y - type_line.h * 0.3
    H, W = img.shape[:2]
    x0, y0, x1, y1 = int(max(x0, 0)), int(max(y0, 0)), int(min(x1, W)), int(min(y0 + size, H))
    if x1 - x0 < 10 or y1 - y0 < 10:
        return None
    return img[y0:y1, x0:x1].copy()


# ----------------------------------------------------------------------------- structured tooltip reader
# A tooltip is read in sections, the way the game draws it:
#
#   CROWN OF THE FALLEN KING          name (small caps, often misread -> matched against the item list)
#   [icon]  Legendary Helm            type line: rarity + slot
#           393 Armor                 base values (Armor / weapon Damage, Speed, DPS)
#   Primary                           header
#   + +34 Intelligence                blue affix lines
#   Secondary
#   + +12.5% Gold Find
#   (gem) +10.0% Bonus Health         green: socketed gem
#   + +30% Attack Speed for ...       orange: legendary effect, may wrap over several lines
#   Required Level: 43 / Item Level: 436
#
# Robustness:
#   * the tooltip is found on a first OCR pass, then cut out and read again at 2x size
#   * the colour of each line decides affix / gem / effect, not the wording
#   * stat names may only be names that exist on that slot (data/affixes.json)
#   * every value is checked against the affix formula (M x item level x per_level x roll); a value
#     that cannot be right is read again (RapidOCR, 3x) and else marked as uncertain
#   * the comparison numbers the game prints "(+x)" are checked against new - equipped

@dataclass
class StatLine:
    name: str
    value: float
    pct: bool = False
    diff: float | None = None   # "(+x)" the game prints in a comparison tooltip
    ok: bool = True             # value plausible for this item
    raw: str = ""


@dataclass
class ItemData:
    name: str = "?"
    name_sure: bool = False
    raw_name: str = ""
    type_line: str = ""
    base: list = field(default_factory=list)       # [StatLine] Armor / Weapon Damage / Weapon Speed / Weapon DPS
    primary: list = field(default_factory=list)    # [StatLine]
    secondary: list = field(default_factory=list)
    sockets: list = field(default_factory=list)
    effects: list = field(default_factory=list)    # merged effect texts
    lost: list = field(default_factory=list)       # [StatLine] "Stats lost" section (diff only)
    legendary: str = ""                            # matched legendary item name
    item_level: int | None = None
    req_level: int | None = None
    icon: object = None
    equipped: bool = False
    empty_sockets: int = 0
    warnings: list = field(default_factory=list)

    @property
    def affixes(self):
        return self.primary + self.secondary

    def all_stats(self):
        return self.base + self.primary + self.secondary + self.sockets


RE_TYPE = re.compile(r"^\W*(?:ancient\s+)?(common|uncommon|rare|legendary|divine)\s+"
                     r"(weapon|helm|chest|pants|boots|belt|ring|necklace|gloves|shoulder|back|staff|mace|sword|axe|bow|"
                     r"hammer|wand|dagger|amulet|cloak|cape)", re.I)
RE_PRIMARY = re.compile(r"^\W*pr[il1|]m[ae]ry\W*$", re.I)
RE_SECONDARY = re.compile(r"^\W*s[ec]c[o0]nd[ae]ry\W*$", re.I)
RE_LOST_HDR = re.compile(r"stats?\s*lost", re.I)
RE_EQUIPPED = re.compile(r"^\W*equ[il1]pped\W*$", re.I)
RE_LINE = re.compile(rf"^[^\w+\-−–]*\+?\s*(?P<val>{_NUM})\s*(?P<p>%?)\s*(?P<name>[A-Za-z][A-Za-z' ]*?)\s*"
                     rf"(?:[(\[{{]\s*(?P<d>{_NUM})\s*(?P<p2>%?)\s*[)\]}}]?)?\W*$")
RE_LOST_LINE = re.compile(rf"^\W*(?P<name>[A-Za-z][A-Za-z' ]*?)\s*[(\[{{]\s*(?P<d>{_NUM})\s*(?P<p>%?)\s*[)\]}}]?\W*$")
BASE_NAMES = {"armor": "Armor", "damage": "Weapon Damage", "speed": "Weapon Speed", "dps": "Weapon DPS",
              "attackspeed": "Weapon Speed", "attacksspersecond": "Weapon Speed"}
RE_CLASSES = re.compile(r"\b(warrior|sorcerer|hunter|monk|rogue|necromancer|paladin|druid)\s*[|Il!/]", re.I)

_AFFIX_DATA = None


def _affix_data() -> dict:
    global _AFFIX_DATA
    if _AFFIX_DATA is None:
        import json
        import os
        try:
            with open(paths.res("data", "affixes.json"),
                      encoding="utf-8") as f:
                _AFFIX_DATA = json.load(f)
        except Exception:
            _AFFIX_DATA = {}
    return _AFFIX_DATA


_GEM_VALUES = None


def _gem_values() -> dict:
    """{stat: {possible gem values}} from data/gems.json."""
    global _GEM_VALUES
    if _GEM_VALUES is None:
        import json
        import os
        _GEM_VALUES = {}
        try:
            with open(paths.res("data", "gems.json"),
                      encoding="utf-8") as f:
                d = json.load(f)
            for g in d.get("gems", {}).values():
                for t in g.get("tiers", {}).values():
                    for kind in ("weapon", "armor", "accessory"):
                        e = t.get(kind)
                        if isinstance(e, dict) and "stat" in e:
                            _GEM_VALUES.setdefault(e["stat"], set()).add(float(e["value"]))
        except Exception:
            pass
    return _GEM_VALUES


def _kind(type_line: str):
    t = (type_line or "").lower()
    rarity = next((r for r in ("Uncommon", "Common", "Rare", "Legendary", "Divine") if r.lower() in t), None)
    slots = ["Weapon", "Helm", "Chest Armor", "Pants", "Boots", "Belt", "Ring", "Necklace", "Gloves", "Shoulder", "Back"]
    slot = next((s for s in slots if s.lower() in t), None)
    if slot is None and "chest" in t:
        slot = "Chest Armor"
    if slot is None and re.search(r"\b(staff|mace|sword|axe|bow|hammer|wand|dagger)\b", t):
        slot = "Weapon"
    return rarity, slot


def _stat_names(slot: str | None, section: str) -> list:
    """Names allowed on this slot/section; falls back to every affix name."""
    d = _affix_data()
    every = list(d.get("affixes", {})) or list(KNOWN_STATS)
    sl = d.get("slots", {}).get(slot or "", {})
    names = sl.get(section) or []
    return names or every


def _match(raw: str, names: list, cutoff: float = 0.75):
    k = re.sub(r"[^a-z]", "", raw.lower())
    if not k:
        return None, 0.0
    best, score = None, 0.0
    for n in names:
        r = difflib.SequenceMatcher(None, re.sub(r"[^a-z]", "", n.lower()), k).ratio()
        if r > score:
            best, score = n, r
    return (best, score) if score >= cutoff else (None, score)


def _roll(stat, value, rarity, ilvl):
    d = _affix_data()
    m = d.get("rarity_multiplier", {}).get(rarity or "")
    per = 0.5 if stat == "Armor" else (d.get("affixes", {}).get(stat) or {}).get("per_level")
    if not m or not ilvl or not per:
        return None
    return abs(value) / (m * ilvl * per)


def _plausible(st: StatLine, kind: str, rarity, ilvl) -> bool:
    """Affix: roll 0.80..1.80 (1.15 max roll, upgrades add +5 % steps). Gem: a value a gem can have."""
    if kind == "socket":
        vals = _gem_values().get(st.name)
        return not vals or any(abs(st.value - v) < 0.051 for v in vals)
    if kind == "base":
        if st.name == "Armor":
            r = _roll("Armor", st.value, rarity, ilvl)
            return r is None or 0.8 <= r <= 1.9
        if st.name == "Weapon Speed":
            return 0.3 <= st.value <= 3.0
        return st.value > 0
    r = _roll(st.name, st.value, rarity, ilvl)
    return r is None or 0.80 <= r <= 1.80


def _color(img, l: Line) -> str:
    """Text colour of a line: blue (affix), green (gem), orange (effect/legendary), white (other)."""
    H, W = img.shape[:2]
    x0, x1 = int(max(l.x, 0)), int(min(l.x + l.w, W))
    y0, y1 = int(max(l.y, 0)), int(min(l.y + l.h, H))
    patch = img[y0:y1, x0:x1]
    if patch.size == 0:
        return "?"
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    h, s, v = hsv[:, 0], hsv[:, 1] / 255, hsv[:, 2] / 255
    txt = v > 0.55
    if txt.sum() < 8:
        return "?"
    sat = txt & (s > 0.30)
    counts = {
        "blue": int((sat & (h >= 100) & (h <= 140)).sum()),
        "green": int((sat & (h >= 38) & (h <= 90)).sum()),
        "orange": int((sat & (h >= 4) & (h <= 24)).sum()),
    }
    white = int((txt & (s < 0.22)).sum())
    best = max(counts, key=counts.get)
    if counts[best] > max(6, white * 0.6):
        return best
    return "white"


def ocr_scaled(img, scale: float, engine: str = "win") -> list[Line]:
    big = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC)
    ls = ocr_windows(big) if engine == "win" else ocr_rapid(big)
    return [Line(l.text, l.x / scale, l.y / scale, l.w / scale, l.h / scale) for l in ls]


def _norm_text(t: str) -> str:
    t = unicodedata.normalize("NFKC", t)
    return t.replace("−", "-").replace("–", "-").replace("’", "'").strip()


def _parse_stat(text: str, names: list, base: bool = False):
    """'+34 Intelligence (+5)' -> StatLine or None."""
    text = _norm_text(text)
    text = re.sub(r"^[^\d\s+\-]{1,2}\s+(?=[+\-]?\d)", "", text)  # gem icon / bullet read as a letter ("V +10%")
    m = RE_LINE.match(text)
    if not m:
        return None
    v = parse_number(m["val"])
    if v is None:
        return None
    raw_name = m["name"].strip()
    if base:
        k = re.sub(r"[^a-z]", "", raw_name.lower())
        name = BASE_NAMES.get(k)
        if name is None:
            hit = difflib.get_close_matches(k, BASE_NAMES.keys(), n=1, cutoff=0.7)
            name = BASE_NAMES[hit[0]] if hit else None
        if name is None:
            return None
    else:
        name, _ = _match(raw_name, names)
        if name is None:
            return None
    pct = bool(m["p"] or m["p2"])
    if not base:
        pct = pct or bool((_affix_data().get("affixes", {}).get(name) or {}).get("percent"))
    d = parse_number(m["d"]) if m["d"] else None
    return StatLine(name, abs(v), pct, d, True, text)


def _reread(img, l: Line, names, kind, rarity, ilvl, first: StatLine | None):
    """Second opinion for a line whose value cannot be right: missing decimal point, then RapidOCR / 3x."""
    if first is not None and first.pct and "." not in first.raw and "," not in first.raw:
        alt = StatLine(first.name, first.value / 10, first.pct, first.diff, True, first.raw)
        if _plausible(alt, kind, rarity, ilvl):
            return alt
    H, W = img.shape[:2]
    pad = l.h * 0.4
    x0, y0 = int(max(l.x - pad * 3, 0)), int(max(l.y - pad, 0))
    x1, y1 = int(min(l.x + l.w + pad * 3, W)), int(min(l.y + l.h + pad, H))
    crop = img[y0:y1, x0:x1]
    if crop.size == 0:
        return None
    for engine, scale in (("rapid", 2.0), ("win", 3.0), ("rapid", 3.0)):
        try:
            cands = ocr_scaled(crop, scale, engine)
        except Exception:
            continue
        text = " ".join(c.text for c in sorted(cands, key=lambda c: c.x))
        st = _parse_stat(text, names, base=(kind == "base"))
        if st is not None and (first is None or st.name == first.name) and _plausible(st, kind, rarity, ilvl):
            return st
    return None


def _rows(lines: list[Line]) -> list[Line]:
    """Join OCR pieces that sit on the same text row (Windows OCR splits at icons and big gaps)."""
    out = []
    for l in sorted(lines, key=lambda l: (l.cy, l.x)):
        row = next((o for o in out if abs(o.cy - l.cy) < min(o.h, l.h) * 0.5
                    and l.x - (o.x + o.w) < o.h * 2.5 and l.x > o.x), None)
        if row is not None:
            row.text = row.text + " " + l.text
            x1 = max(row.x + row.w, l.x + l.w)
            y0, y1 = min(row.y, l.y), max(row.y + row.h, l.y + l.h)
            row.w, row.y, row.h = x1 - row.x, y0, y1 - y0
        else:
            out.append(Line(l.text, l.x, l.y, l.w, l.h))
    return sorted(out, key=lambda l: l.y)


def _merge_effects(parts: list[Line]) -> list[str]:
    effects, cur, last = [], "", None
    for l in parts:
        t = _norm_text(l.text).lstrip("•◆·+* ").strip()
        new_para = last is None or l.y - (last.y + last.h) > l.h * 0.9 or re.match(r"^[•◆·*]", l.text.strip())
        if new_para and cur:
            effects.append(cur)
            cur = ""
        cur = (cur + " " + t).strip()
        last = l
    if cur:
        effects.append(cur)
    out = []
    for e in effects:
        m = RE_CLASSES.search(e)
        if m:  # class restriction line "Warrior | Sorcerer | Monk" is not part of the effect
            e = e[:m.start()].strip()
        if len(re.findall(r"[A-Za-z]{2,}", e)) >= 3:
            out.append(e)
    return out


def _match_legendary(effect: str, slot: str | None):
    try:
        import item_eval
        legs = item_eval.LEGENDARIES
    except Exception:
        return None, 0.0
    k = re.sub(r"[^a-z0-9]", "", effect.lower())
    best, score = None, 0.0
    for L in legs:
        if slot and L.get("slot") and L["slot"] != slot:
            continue
        lk = re.sub(r"[^a-z0-9]", "", L.get("effect", "").lower())
        if not lk:
            continue
        r = difflib.SequenceMatcher(None, lk, k).ratio()
        # wrapped effect text can be cut short: compare against the same length of the reference too
        r = max(r, difflib.SequenceMatcher(None, lk[:len(k)], k).ratio() if len(k) > 25 else 0)
        if r > score:
            best, score = L, r
    return (best, score) if score >= 0.6 else (None, score)


def match_item_name(raw: str, rarity: str | None, slot: str | None):
    """Item list lookup restricted to rarity and slot. Returns (name, sure)."""
    names = _item_names()
    if not raw or not names:
        return raw, False
    pool = [n for n in names if (not rarity or n["rarity"] == rarity) and (not slot or n["slot"] == slot)] or names
    best, score = _match(raw, [n["name"] for n in pool], cutoff=0.0)
    if best and score >= 0.72:
        return best, True
    if best and score >= 0.5 and len(pool) < 60:
        return best, False
    return raw.title(), False


def _find_columns(lines: list[Line], W: int, H: int) -> list:
    """One box per tooltip, found from its "<Rarity> <Slot>" type line."""
    types = sorted([l for l in lines if RE_TYPE.match(l.text) and len(l.text) < 40], key=lambda l: l.x)
    cols = []
    for t in types:
        if any(abs(t.x - c["type"].x) < t.h * 4 for c in cols):
            continue
        h = t.h
        left = t.x - 6.2 * h
        right = left + 24 * h
        top = t.y - 5.0 * h
        lvl = [l for l in lines if re.search(r"item\s*level", l.text, re.I) and left < l.x < right and l.y > t.y]
        bottom = (min(lvl, key=lambda l: l.y).y + 2.0 * h) if lvl else t.y + 34 * h
        # the "Stats lost" part of a comparison may sit below the item level
        extra = [l for l in lines if left < l.x < right and bottom < l.y < bottom + 12 * h
                 and (RE_LOST_HDR.search(l.text) or RE_LOST_LINE.match(l.text))]
        if extra:
            bottom = max(l.y + l.h for l in extra) + h
        cols.append({"type": t, "box": [left, top, right, bottom]})
    for i, c in enumerate(cols[:-1]):  # neighbours must not overlap
        nxt = cols[i + 1]["box"]
        if c["box"][2] > nxt[0]:
            mid = (c["box"][2] + nxt[0]) / 2
            c["box"][2] = max(mid, c["type"].x + c["type"].w + 4)
            nxt[0] = min(mid, cols[i + 1]["type"].x - 5.6 * cols[i + 1]["type"].h)
    for c in cols:
        x0, y0, x1, y1 = c["box"]
        c["box"] = [int(max(x0, 0)), int(max(y0, 0)), int(min(x1, W)), int(min(y1, H))]
        c["equipped"] = any(RE_EQUIPPED.match(l.text) and x0 <= l.x + l.w / 2 <= x1 and y0 - 3 * c["type"].h <= l.y <= y0 + 3 * c["type"].h
                            for l in lines)
    return cols


def read_item(img, box) -> ItemData:
    """Read one tooltip (box in frame coordinates)."""
    x0, y0, x1, y1 = box
    crop = img[y0:y1, x0:x1]
    it = ItemData()
    if crop.size == 0:
        return it
    lines = _rows([Line(_norm_text(l.text), l.x, l.y, l.w, l.h) for l in ocr_scaled(crop, 2.0, "win") if l.text.strip()])
    t = next((l for l in lines if RE_TYPE.match(l.text) and len(l.text) < 40), None)
    if t is None:
        return it
    it.type_line = re.sub(r"\s+", " ", t.text).strip()
    rarity, slot = _kind(it.type_line)
    it.icon = _icon_left_of(crop, t)

    # levels first: the plausibility checks need the item level
    for l in lines:
        m = re.search(r"item\s*level\W*(\d+)", l.text, re.I)
        if m and it.item_level is None:
            it.item_level = int(m.group(1))
        m = re.search(r"required\s*level\W*(\d+)", l.text, re.I)
        if m and it.req_level is None:
            it.req_level = int(m.group(1))
    if it.item_level is not None and not 1 <= it.item_level <= 1000:
        it.item_level = None
    if it.item_level is None:
        lv = next((l for l in lines if re.search(r"item\s*level", l.text, re.I)), None)
        if lv is not None:
            for c in ocr_scaled(crop[int(max(lv.y - 3, 0)):int(lv.y + lv.h + 3), :], 3.0, "rapid"):
                m = re.search(r"(\d{1,4})\s*$", c.text)
                if m and 1 <= int(m.group(1)) <= 1000:
                    it.item_level = int(m.group(1))
    if it.req_level is None:
        rq = next((l for l in lines if re.search(r"required\s*level", l.text, re.I)), None)
        if rq is not None:  # the number is red when the level is too high and often not read
            band = crop[int(max(rq.y - 3, 0)):int(rq.y + rq.h + 3), :]
            for c in ocr_scaled(band, 3.0, "rapid"):
                m = re.search(r"(\d{1,2})\s*$", c.text)
                if m and 1 <= int(m.group(1)) <= 70:
                    it.req_level = int(m.group(1))
    if it.item_level is None:
        it.warnings.append("item level not read")

    # name: the text rows right above the picture
    icon_top = t.y - t.h * 0.4
    above = [l for l in lines if l.y + l.h <= icon_top and t.y - l.y < t.h * 6 and not RE_EQUIPPED.match(l.text)
             and len(re.findall(r"[A-Za-z]", l.text)) >= 4]
    # long names may wrap: try each row and each pair of neighbouring rows, keep the best list match
    cands = [[l] for l in above] + [[a, b] for a in above for b in above if 0 < b.y - a.y < a.h * 1.8]
    best = None
    for c in cands:
        raw = " ".join(l.text for l in c)
        name, sure = match_item_name(raw, rarity, slot)
        score = difflib.SequenceMatcher(None, re.sub(r"[^a-z]", "", name.lower()),
                                        re.sub(r"[^a-z]", "", raw.lower())).ratio()
        if best is None or (sure, score) > (best[2], best[3]):
            best = (raw, name, sure, score)
    if best:
        it.raw_name, it.name, it.name_sure = best[0], best[1], best[2]

    # sections
    section, effect_parts, after_lost = "base", [], False
    for l in lines:
        if l.y <= t.y + t.h * 0.5:
            continue
        text = l.text
        if re.search(r"(required|item)\s*level|unequip|mark\s+as|^\W*\d[\d,.]*\W*$", text, re.I):
            continue
        if RE_PRIMARY.match(text):
            section = "primary"
            continue
        if RE_SECONDARY.match(text):
            section = "secondary"
            continue
        if RE_LOST_HDR.search(text):
            after_lost = True
            continue
        if re.search(r"empty\s*s[o0]cket", text, re.I):
            it.empty_sockets += 1
            continue
        if after_lost:
            m = RE_LOST_LINE.match(text)
            if m:
                name, _ = _match(m["name"], _stat_names(None, "primary") + _stat_names(None, "secondary"))
                d = parse_number(m["d"])
                if name and d is not None:
                    it.lost.append(StatLine(name, 0.0, bool(m["p"]), -abs(d), True, text))
            continue
        col = _color(crop, l)
        if col == "orange" and section != "base" or (col == "orange" and not RE_LINE.match(text)):
            effect_parts.append(l)
            continue
        if section == "base":
            st = _parse_stat(text, [], base=True)
            kind = "base"
            if st is None:  # no "Primary" header (common items): a known affix ends the base part
                section = "primary"
        if section != "base":
            kind = "socket" if col == "green" else section
            names = list(_gem_values()) + _stat_names(None, "primary") if kind == "socket" \
                else _stat_names(slot, section)
            st = _parse_stat(text, names)
            if st is None and kind != "socket":  # wrong section guess: any affix name
                st = _parse_stat(text, _stat_names(None, "primary") + _stat_names(None, "secondary"))
            if st is None:
                if col in ("orange", "?") or len(re.findall(r"[A-Za-z]+", text)) >= 4:
                    effect_parts.append(l)
                continue
        if not _plausible(st, kind, rarity, it.item_level):
            alt = _reread(crop, l, names if kind != "base" else [], kind, rarity, it.item_level, st)
            if alt is not None:
                alt.diff = alt.diff if alt.diff is not None else st.diff
                st = alt
            else:
                st.ok = False
                it.warnings.append(f"{st.name} {st.value:g}{'%' if st.pct else ''} probably misread")
        {"base": it.base, "primary": it.primary, "secondary": it.secondary, "socket": it.sockets}[kind].append(st)

    # OCR returns "0,83 Speed" and "191.7 DPS" in either order (same row): always use the game's order
    order = ["Armor", "Weapon Damage", "Weapon Speed", "Weapon DPS"]
    it.base.sort(key=lambda st: order.index(st.name) if st.name in order else 9)
    it.effects = _merge_effects(effect_parts)
    if it.effects and rarity == "Legendary":
        L, score = _match_legendary(it.effects[0], slot)
        if L is not None:
            it.legendary = L["name"]
            it.effects[0] = L["effect"]
            if not it.name_sure:
                it.name, it.name_sure = L["name"], True
    elif rarity and rarity != "Legendary":
        it.effects = []  # only legendaries carry an effect; anything else is misread name/footer text
    return it


def _totals(st_lists) -> dict:
    out = {}
    for st in st_lists:
        v, p = out.get(st.name, (0.0, st.pct))
        out[st.name] = (v + st.value, p or st.pct)
    return out


def parse_tooltip(img, lines: list[Line] | None = None) -> ItemResult:
    lines = lines if lines is not None else ocr_windows(img)
    res = ItemResult(raw=[l.text for l in lines])
    if img is None:
        return res
    H, W = img.shape[:2]
    cols = _find_columns(lines, W, H)
    if not cols:
        return res
    items = [(c, read_item(img, c["box"])) for c in cols]
    for c, it in items:
        it.equipped = c["equipped"]
    items = [(c, it) for c, it in items if it.type_line]
    if not items:
        return res
    with_diff = [(c, it) for c, it in items if any(s.diff is not None for s in it.all_stats()) or it.lost]
    if with_diff:
        new = with_diff[0][1]
        olds = [it for c, it in items if it is not new]
    elif len(items) >= 2:
        eq = [it for c, it in items if c["equipped"]]
        old = eq[0] if eq else items[0][1]
        new = next(it for c, it in items if it is not old)
        olds = [old]
    else:
        new, olds = items[0][1], []
    old = olds[0] if olds else None
    res.new_item, res.old_item = new, old
    res.mode = "compare" if old is not None or with_diff else "single"

    # legacy fields used by the evaluation and the history
    res.name, res.type_line, res.icon = new.name, new.type_line, new.icon
    res.item_level, res.req_level = new.item_level, new.req_level
    res.effects_new_all = list(new.effects)
    armor = [s for s in new.base if s.name == "Armor"]
    res.new_stats = [(s.name, s.value) for s in armor + new.affixes]
    if old is not None:
        res.old_name, res.old_type_line, res.old_icon, res.old_item_level = old.name, old.type_line, old.icon, old.item_level
        res.effects_old_all = list(old.effects)
        oarmor = [s for s in old.base if s.name == "Armor"]
        res.old_stats = [(s.name, s.value) for s in oarmor + old.affixes]
    res.warnings = [f"New: {w}" for w in new.warnings] + ([f"Equipped: {w}" for w in old.warnings] if old else [])

    # stat changes: from the two read items; the game's own "(+x)" numbers cross-check them
    if old is not None:
        a = _totals(new.base + new.affixes)  # gems belong to the slot, not the item
        b = _totals(old.base + old.affixes)
        deltas = {k: (a.get(k, (0, False))[0] - b.get(k, (0, False))[0], a.get(k, b.get(k))[1]) for k in set(a) | set(b)}
        game = {}
        for s in new.all_stats():
            if s.diff is not None:
                game[s.name] = (game.get(s.name, (0, s.pct))[0] + s.diff, s.pct)
        for s in new.lost:
            game[s.name] = (game.get(s.name, (0, s.pct))[0] + s.diff, s.pct)
        for k, (g, p) in game.items():
            d = deltas.get(k, (0, p))[0]
            if abs(g - d) > max(0.15, abs(g) * 0.02):
                # one of the two readings is wrong; the game's number is the one to trust
                res.warnings.append(f"{k}: game says {g:+g}, read {d:+g}")
                deltas[k] = (g, p)
        res.deltas = {k: v for k, v in deltas.items() if abs(v[0]) > 1e-9 and k != "Weapon DPS"}
    elif with_diff:
        for s in new.all_stats() + new.lost:
            if s.diff is not None and s.name != "Weapon DPS":
                v, p = res.deltas.get(s.name, (0.0, s.pct))
                res.deltas[s.name] = (v + s.diff, p)
    else:
        res.deltas = {k: v for k, v in _totals(new.base + new.affixes).items() if k != "Weapon DPS"}
    if old is not None:
        same = new.legendary and old.legendary and new.legendary == old.legendary
        res.effects_new = [] if same else list(new.effects)
        res.effects_lost = [] if same else list(old.effects)
    else:
        res.effects_new = list(new.effects)
    return res


def save_debug(frame, res, folder: str, keep: int = 30):
    """Keep the last tooltip captures with what was read, to look into misreadings later."""
    import json
    import os
    import time
    try:
        os.makedirs(folder, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        cv2.imwrite(os.path.join(folder, f"item_{stamp}.png"), frame)

        def item(it):
            if it is None:
                return None
            f = lambda xs: [[s.name, s.value, s.pct, s.diff, s.ok, s.raw] for s in xs]
            return {"name": it.name, "raw_name": it.raw_name, "type": it.type_line, "ilvl": it.item_level,
                    "req": it.req_level, "base": f(it.base), "primary": f(it.primary), "secondary": f(it.secondary),
                    "sockets": f(it.sockets), "lost": f(it.lost), "effects": it.effects, "warnings": it.warnings}
        with open(os.path.join(folder, f"item_{stamp}.json"), "w", encoding="utf-8") as fh:
            json.dump({"mode": res.mode, "raw": res.raw, "new": item(getattr(res, "new_item", None)),
                       "old": item(getattr(res, "old_item", None)), "deltas": res.deltas,
                       "warnings": getattr(res, "warnings", [])}, fh, ensure_ascii=False, indent=1)
        files = sorted(f for f in os.listdir(folder) if f.startswith("item_"))
        for f in files[:-keep * 2]:
            os.remove(os.path.join(folder, f))
    except Exception:
        pass


# ----------------------------------------------------------------------------- scoring

def score(deltas: dict, weights: dict) -> tuple[float, list]:
    rows = []
    total = 0.0
    for name, (d, pct) in sorted(deltas.items(), key=lambda kv: -abs(kv[1][0] * weights.get(kv[0], 0))):
        w = weights.get(name)
        pts = d * w if w is not None else 0.0
        total += pts
        rows.append((name, d, pct, w, pts))
    return total, rows


# ----------------------------------------------------------------------------- DPS model
# Final Damage = Base x Ability x MainStat x DmgType x Basic/Strong x Conditional x Crit
# Every group multiplies, so a stat's value depends on how much
# of its own group the character already has - that is why the character sheet matters.

ELEMENTS = ["Fire", "Cold", "Lightning", "Poison", "Arcane", "Physical"]
MAIN_STATS = ["Intelligence", "Strength", "Dexterity"]

# How often a conditional bonus applies. Assumptions, not from the game: an enemy is above
# and below the "injured" threshold about half of its life each; distant applies to most hits
# of a ranged class; elites are a minority of kills; strong attacks a minority of hits.
UPTIME = {"Damage vs Healthy": 0.5, "Damage vs Injured": 0.5, "Damage vs Distant": 0.5,
          "Damage vs Elite": 0.2, "Strong Attack Damage": 0.3}

DPS_STATS = set(MAIN_STATS) | {f"{e} Damage" for e in ELEMENTS} | set(UPTIME) | {
    "Critical Hit Chance", "Critical Hit Damage", "Attack Speed Bonus"}


def main_stat_of(stats: dict) -> str:
    present = [(stats[s][0], s) for s in MAIN_STATS if s in stats]
    return max(present)[1] if present else "Intelligence"


def element_of(stats: dict, choice: str = "Auto") -> str:
    if choice and choice != "Auto":
        return choice
    present = [(stats[f"{e} Damage"][0], e) for e in ELEMENTS if f"{e} Damage" in stats]
    return max(present)[1] if present else "Lightning"


def dps_factor(v: dict, main: str, element: str) -> float:
    """Relative DPS from the multiplicative groups. v: {stat: value}."""
    g = lambda k: v.get(k, 0.0)
    f = 1 + g(main) / 100
    f *= 1 + g(f"{element} Damage") / 100
    f *= 1 + min(max(g("Critical Hit Chance"), 0), 100) / 100 * g("Critical Hit Damage") / 100
    f *= 1 + g("Attack Speed Bonus") / 100
    f *= 1 + UPTIME["Strong Attack Damage"] * g("Strong Attack Damage") / 100
    f *= 1 + sum(UPTIME[k] * g(k) for k in UPTIME if k != "Strong Attack Damage") / 100
    return f


@dataclass
class Evaluation:
    total: float            # points (DPS part + weighted rest)
    dps_pct: float | None   # DPS change in %, None without character sheet
    rows: list              # (stat, delta, pct, weight_or_label, points)
    main: str = ""
    element: str = ""


def evaluate(deltas: dict, char: dict, weights: dict, element_choice: str = "Auto",
             pts_per_dps_pct: float = 10.0) -> Evaluation:
    """Score an item change. With a character sheet, DPS stats go through the damage formula;
    everything else (defense, utility, flat Damage) uses the user weights."""
    if not char:
        total, rows = score(deltas, weights)
        return Evaluation(total, None, rows)
    main, element = main_stat_of(char), element_of(char, element_choice)
    base = {k: v[0] for k, v in char.items()}
    f0 = dps_factor(base, main, element)
    new = dict(base)
    for k, (d, _) in deltas.items():
        new[k] = new.get(k, 0.0) + d
    dps_pct = (dps_factor(new, main, element) / f0 - 1) * 100
    rows, rest = [], 0.0
    unused = (set(MAIN_STATS) - {main}) | {f"{e} Damage" for e in ELEMENTS if e != element}
    for name, (d, pct) in deltas.items():
        if name in unused:  # other main stat / other element: no effect on this build
            rows.append((name, d, pct, "no effect", 0.0))
        elif name in DPS_STATS:
            one = dict(base)
            one[name] = one.get(name, 0.0) + d
            p = (dps_factor(one, main, element) / f0 - 1) * 100
            rows.append((name, d, pct, f"{p:+.2f}% DPS", p * pts_per_dps_pct))
        else:
            w = weights.get(name)
            pts = d * w if w is not None else 0.0
            rest += pts
            rows.append((name, d, pct, w, pts))
    rows.sort(key=lambda r: -abs(r[4]))
    return Evaluation(dps_pct * pts_per_dps_pct + rest, dps_pct, rows, main, element)


# ----------------------------------------------------------------------------- gold balance

RE_GOLD = re.compile(r"^\d{1,3}(?:[,.]\d{3})*$|^\d{1,7}$")


def find_gold(lines: list[Line]) -> int | None:
    """Account gold from the inventory footer: '54,599  [skull] 153  373/3000  7/40'.
    The footer is the row holding the "x/y" counters; gold is its leftmost number."""
    slashes = [l for l in lines if re.fullmatch(r"\d+\s*/\s*\d+", l.text)]
    for s in slashes:
        row = [l for l in lines if abs(l.cy - s.cy) < max(l.h, s.h) * 0.8 and l.x < s.x]
        nums = [l for l in row if RE_GOLD.match(l.text.replace(" ", ""))]
        if len(nums) >= 1 and len([o for o in slashes if abs(o.cy - s.cy) < s.h]) >= 2:
            g = min(nums, key=lambda l: l.x)
            return int(re.sub(r"\D", "", g.text))
    return None


# ----------------------------------------------------------------------------- death screen

# The death screen: "YOU DIED!" (red), "AUTO REVIVE AFTER 11 SECONDS" and a "TOWN" button in the middle.
# The stylised letters are often read with gaps ("Y O U  D I E D"), so spaces are removed before matching.
# "Revive Cooldown: -6s" on item tooltips must not count.
DEATH_PATTERNS = [r"y[o0]ud[i1l]ed", r"auto-?rev[i1l]ve", r"rev[i1l]veafter\d*", r"rev[i1l]vein\d+",
                  r"y[o0]uaredead", r"y[o0]uwereslain"]
_RE_DEATH = re.compile("|".join(DEATH_PATTERNS), re.I)


# ----------------------------------------------------------------------------- stage end screen
# Shown after every run (with "Auto Rerun 2s"): "THE CINDER CROWN: 6", CLEARS 36, TOTAL XP GAINED,
# RUN DURATION 01:34, TOTAL TIME SPENT, drops by rarity, and the buttons TOWN / NEXT STAGE / RERUN.
_REGIONS = None
_END_MARKERS = ("clears", "duration", "xpgained", "timespent", "autorerun", "nextstage", "rerun")


def _regions() -> list:
    global _REGIONS
    if _REGIONS is None:
        import json
        try:
            with open(paths.res("data", "enemies.json"), encoding="utf-8") as f:
                _REGIONS = sorted({s.get("region") for s in json.load(f).get("stages", []) if s.get("region")})
        except Exception:
            _REGIONS = []
    return _REGIONS


def _compact(text):
    return re.sub(r"[^a-z0-9]", "", text.lower())


def is_stage_end(lines: list[Line]) -> bool:
    found = {m for l in lines for m in _END_MARKERS if m in _compact(l.text)}
    return len(found) >= 2


def find_stage_end(lines: list[Line]) -> dict | None:
    """{"stage": "The Cinder Crown", "n": 6, "seconds": 94, "clears": 36} from the stage end screen."""
    if not is_stage_end(lines):
        return None
    rows = []
    for l in sorted(lines, key=lambda l: (l.y, l.x)):
        row = next((r for r in rows if abs(r[0].cy - l.cy) < max(r[0].h, l.h) * 0.6), None)
        if row is None:
            rows.append([l])
        else:
            row.append(l)
    stage = None
    for row in rows:
        text = " ".join(x.text for x in sorted(row, key=lambda x: x.x))
        m = re.search(r"([A-Za-z' ]{4,}?)\s*[:;.]\s*(\d{1,2})\s*$", text)
        if not m:
            continue
        raw = m.group(1).strip()
        best = difflib.get_close_matches(_compact(raw), [_compact(r) for r in _regions()], n=1, cutoff=0.6)
        if best:
            name = next(r for r in _regions() if _compact(r) == best[0])
            stage = (name, int(m.group(2)), min(row, key=lambda x: x.y))
            break
    if stage is None:
        return None
    out = {"stage": stage[0], "n": stage[1]}

    def value_below(marker, pattern):
        mk = next((l for l in lines if marker in _compact(l.text)), None)
        if mk is None:
            return None
        cands = [l for l in lines if mk.y < l.y < mk.y + mk.h * 3 and abs(l.cx_center() - mk.cx_center()) < mk.w]
        for l in sorted(cands, key=lambda l: l.y):
            m = re.search(pattern, l.text)
            if m:
                return m
        return None
    m = value_below("duration", r"(\d{1,2}):(\d{2})")
    if m:
        out["seconds"] = int(m.group(1)) * 60 + int(m.group(2))
    m = value_below("clears", r"(\d+)")
    if m:
        out["clears"] = int(m.group(1))
    m = value_below("xpgained", r"\(\s*\+\s*([\d,.]+)\s*\)")
    if m:
        out["xp"] = int(re.sub(r"\D", "", m.group(1)))
    # gold: "37,579 (+715)" - a line of digits only (the ore lines have names and "x132")
    for l in lines:
        g = re.fullmatch(r"\s*[\d,.]+\s*\(\s*\+\s*([\d,.]+)\s*\)\s*", l.text)
        if g:
            v = int(re.sub(r"\D", "", g.group(1)))
            if v != out.get("xp"):
                out["gold"] = v
                break
    return out


def find_death_text(lines: list[Line], size=None) -> str | None:
    """The on-screen line that announces a death, if any. size=(W, H) of the frame: then a lone
    "TOWN" (the death screen's button) in the middle of the picture counts too."""
    for l in lines:
        compact = re.sub(r"[^a-z0-9-]", "", l.text.lower())
        if len(l.text.split()) <= 8 and "cooldown" not in compact and _RE_DEATH.search(compact):
            return l.text
    if size and not is_stage_end(lines):  # the stage end screen has a TOWN button too
        W, H = size
        for l in lines:
            if re.fullmatch(r"\W*T\s*[O0]\s*W\s*N\W*", l.text) and abs(l.x + l.w / 2 - W / 2) < W * 0.2 \
                    and H * 0.3 < l.y < H * 0.85:
                return "TOWN"
    return None


# ----------------------------------------------------------------------------- in-game "Log" panel
# Entries, oldest at the top:
#   The Cinder Crown: 6 cleared in 01:29
#   Sold [Black Cloth Pants] for 176 gold
#   Killed by Lord of the Cinder (Lv. 70) (Burn): 459 Fire Damage   (may wrap onto 2 lines)
# RapidOCR reads these reliably but drops spaces, so patterns match on space-free text.

RE_LOG_HEADER = re.compile(r"^l[o0e][gcq9]$", re.I)
RE_LOG_ENTRY_HINT = re.compile(r"cleared\s*in|^sold\s|killed\s*by", re.I)
RE_CLEAR = re.compile(r"^(?P<stage>.+?):(?P<n>\d+)clearedin(?P<m>\d+)[:.](?P<s>\d{2})$", re.I)
RE_SOLD = re.compile(r"^Sold\[?(?P<item>.+?)\]?for(?P<gold>[\d,.]+)gold?$", re.I)
RE_ITEM = re.compile(r"^(?P<pre>[^\[]*)\[(?P<item>[^\]]+)\]")
RE_KILLED = re.compile(r"^Killedby(?P<killer>.+?)\(Lv\.?(?P<lvl>\d+)\)(?:\((?P<src>[^)]*)\))?:?"
                       r"(?P<dmg>[\d,.]+)(?P<elem>[A-Za-z]+?)Dam\w*$", re.I)


def _respace(name: str) -> str:
    """'LordoftheCinder' -> 'Lord of the Cinder', 'TheCinderCrown' -> 'The Cinder Crown'."""
    name = name.replace(" ", "")
    name = re.sub(r"([a-z])of(the)?(?=[A-Z])", lambda m: m.group(1) + " of" + (" the" if m.group(2) else " "), name)
    name = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def find_log_header(lines: list[Line]) -> Line | None:
    """The panel's "LOG" title, or - when the title is misread - a stand-in placed above the
    entries that Windows OCR recognised, so read_log_panel crops the same area."""
    hdr = next((l for l in lines if RE_LOG_HEADER.match(l.text.strip())), None)
    if hdr is not None:
        return hdr
    hits = [l for l in lines if RE_LOG_ENTRY_HINT.search(l.text)]
    if len(hits) < 2:
        return None
    x0 = min(l.x for l in hits)
    h = min(l.h for l in hits)  # plain entry text is about as tall as the title
    k = h / 18.0
    # entries start ~200 px (reference scale) left of the title, the first one ~50 px below it;
    # aim a bit higher in case the topmost entry was not recognised
    return Line("LOG", x0 + 200 * k, min(l.y for l in hits) - 75 * k, 37 * k, h)


def parse_log_entry(text: str) -> dict:
    text = unicodedata.normalize("NFKC", text)  # RapidOCR sometimes returns full-width "（" / "："
    s = text.replace(" ", "")
    m = RE_CLEAR.match(s)
    if m:
        return {"type": "clear", "stage": _respace(m["stage"]), "n": int(m["n"]),
                "seconds": int(m["m"]) * 60 + int(m["s"]), "text": text}
    m = RE_SOLD.match(s)
    if m:
        return {"type": "sold", "item": _respace(m["item"].strip("[]()")), "gold": int(re.sub(r"\D", "", m["gold"])), "text": text}
    m = RE_ITEM.match(s)
    if m and len(re.sub(r"[^A-Za-z]", "", m["item"])) >= 3:  # any other line naming an item (kept drop)
        return {"type": "item", "item": _respace(m["item"]), "action": _respace(m["pre"]).strip(":- "),
                "text": text}
    m = RE_KILLED.match(s)
    if m:
        return {"type": "killed", "killer": _respace(m["killer"]), "killer_level": int(m["lvl"]),
                "source": m["src"] or "", "damage": int(re.sub(r"\D", "", m["dmg"])),
                "element": m["elem"].capitalize(), "text": text}
    return {"type": "other", "text": text}


def read_log_panel(frame, header: Line) -> list[dict]:
    """OCR the log panel below its header; returns entries oldest first."""
    k = max(header.h / 18.0, 0.5)  # header height ~18 px at the reference UI scale
    H, W = frame.shape[:2]
    x0, x1 = int(max(header.x - 260 * k, 0)), int(min(header.x + header.w + 280 * k, W))
    y0, y1 = int(max(header.y + header.h + 5 * k, 0)), int(min(header.y + 300 * k, H))
    if x1 - x0 < 50 or y1 - y0 < 30:
        return []
    crop = frame[y0:y1, x0:x1]
    lines = sorted(ocr_rapid(crop), key=lambda l: l.y)
    entries, buf, first = [], "", None

    def emit():
        e = parse_log_entry(buf)
        if e.get("item"):
            e["rarity"] = item_rarity(crop, first, buf)
        entries.append(e)

    for l in lines:
        t = l.text.strip()
        if not t:
            continue
        if re.search(r"combat|settings", t, re.I):
            break  # next panel ("COMBAT SETTINGS (C)") starts right below the log
        starts = re.match(r"^(sold|killed)", t.replace(" ", ""), re.I) or "cleared" in t.replace(" ", "").lower() \
            or "[" in t
        if buf and (starts or parse_log_entry(buf)["type"] != "other"):
            emit()
            buf, first = t, l
        else:
            buf = (buf + " " + t).strip()
            first = first or l
    if buf:
        emit()
    return entries


# Item names in the log are drawn in their rarity colour (OpenCV hue 0-180, S/V 0-1).
RARITY_COLORS = [  # (name, hue range, min saturation, min value)
    ("Uncommon", (95, 130), 0.35, 0.45),   # blue
    ("Rare", (22, 35), 0.45, 0.65),        # yellow
    ("Legendary", (5, 22), 0.60, 0.65),    # orange
    ("Green", (36, 90), 0.35, 0.45),
    ("Purple", (131, 165), 0.30, 0.40),
]
RARITY_ORDER = (["Common", "Uncommon", "Rare", "Legendary", "Green", "Purple"] + [f"Gem Tier {i}" for i in range(1, 7)]
                + ["Set Rune", "Ability Rune", "Attribute Rune", "Rune", "Treasure Key", "Boss Material", "Skull",
                   "Soul Shard", "Ore", "Plant", "?"])


def item_rarity(crop, line: Line | None, text: str) -> str:
    """Classify the colour of the [item name] part of a log line; white text = Common."""
    if line is None:
        return "?"
    t = text.replace(" ", "")
    n = max(len(t), 1)
    # name sits between the brackets; OCR sometimes reads "[" as "("
    a = next((i for i, ch in enumerate(t) if ch in "[("), None)
    if a is None:
        if not t.lower().startswith("sold"):
            return "?"
        a = 3
    b = next((i for i in range(len(t) - 1, a, -1) if t[i] in "])"), None)
    if b is None:
        low = t.lower()
        b = low.rfind("for") - 1 if low.rfind("for") > a else n - 1
    x0, x1 = int(line.x + line.w * a / n), int(line.x + line.w * (b + 1) / n)
    patch = crop[int(line.y):int(line.y + line.h), max(x0, 0):max(x1, x0 + 1)]
    if patch.size == 0:
        return "?"
    hsv = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV).reshape(-1, 3).astype(np.int32)
    h, s, v = hsv[:, 0], hsv[:, 1] / 255, hsv[:, 2] / 255
    white = int(((s < 0.25) & (v > 0.68)).sum())  # toast text is a slightly grey white (V~0.8)
    best, best_n = "Common", 0
    for name, (h0, h1), smin, vmin in RARITY_COLORS:
        cnt = int(((h >= h0) & (h <= h1) & (s > smin) & (v > vmin)).sum())
        if cnt > best_n:
            best, best_n = name, cnt
    return best if best_n > max(20, white * 0.5) else ("Common" if white > 20 else "?")


# ----------------------------------------------------------------------------- pop-ups (toasts)
# Bottom-left pop-ups, stacked upwards, names may wrap:
#   "Sold [Plain Iron Band] for / 228 gold"     "Obtained [Polished / Rhombus Emerald]"
# The orange "Obtained" of a legendary is sometimes not read, so a line starting with "[" also
# opens a new pop-up. Pop-ups have a dark background; log panel entries (tan) are skipped.
TOAST_ROI = (0.0, 0.30, 0.22, 0.58)  # x, y, w, h as fractions of the game client area
RE_TOAST_START = re.compile(r"^\W*(sold|obtained|received|found|looted)\b|^\W*[\[(]", re.I)

GEM_TYPES = ("Ruby", "Sapphire", "Topaz", "Emerald", "Amethyst", "Diamond")
GEM_TIERS = ["Raw Sphere", "Chipped Teardrop", "Rough Square", "Polished Rhombus", "Flawless Hexagon",
             "Radiant Octagon"]


def _load_loot_names() -> dict:
    """Rune, key and material names from data/loot_names.json."""
    import json
    import os
    try:
        with open(paths.res("data", "loot_names.json"),
                  encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return {}
    names = {}
    for r in d.get("runes", []):
        names[_loot_key(r["name"])] = {"kind": "rune", "rune_type": r.get("type"), "set": r.get("set")}
    for k in d.get("treasure_keys", []):
        names[_loot_key(k["name"])] = {"kind": "key"}
    for kind, field_ in (("boss_material", "boss_materials"), ("skull", "skulls"), ("shard", "soul_shards"),
                         ("ore", "ores"), ("plant", "plants")):
        for n in d.get(field_, []):
            names[_loot_key(n)] = {"kind": kind}
    return names


def _loot_key(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower().replace("’", "'"))


LOOT_NAMES = None


def classify_drop(item: str) -> dict:
    """{"kind": "gem", "gem": "Emerald", "tier": 4} / {"kind": "rune", "rune_type": "set"} / key / ... / item."""
    global LOOT_NAMES
    if LOOT_NAMES is None:
        LOOT_NAMES = _load_loot_names()
    low = item.lower()
    for g in GEM_TYPES:
        if g.lower() in low:
            tier = next((i + 1 for i, t in enumerate(GEM_TIERS) if t.split()[0].lower() in low), None)
            return {"kind": "gem", "gem": g, "tier": tier}
    key = _loot_key(item)
    if key in LOOT_NAMES:
        return dict(LOOT_NAMES[key])
    best = difflib.get_close_matches(key, LOOT_NAMES.keys(), n=1, cutoff=0.85) if LOOT_NAMES else []
    if best:
        return dict(LOOT_NAMES[best[0]])
    if "rune" in low:
        return {"kind": "rune"}
    if "key" in low:
        return {"kind": "key"}
    if "shard" in low:
        return {"kind": "shard"}
    return {"kind": "item"}


def _dark_background(roi, lines) -> bool:
    x0 = int(max(min(l.x for l in lines) - 8, 0))
    x1 = int(max(l.x + l.w for l in lines) + 8)
    y0 = int(max(min(l.y for l in lines) - 4, 0))
    y1 = int(max(l.y + l.h for l in lines) + 4)
    patch = roi[y0:y1, x0:x1]
    if patch.size == 0:
        return True
    v = cv2.cvtColor(patch, cv2.COLOR_BGR2HSV)[..., 2]
    return float(np.median(v)) / 255 < 0.42


def read_toasts(frame) -> list:
    """All pop-ups currently visible, top to bottom. Each entry has "y" (position in the stack)."""
    H, W = frame.shape[:2]
    fx, fy, fw, fh = TOAST_ROI
    oy = int(fy * H)
    roi = frame[oy:int((fy + fh) * H), int(fx * W):int((fx + fw) * W)]
    lines = sorted(ocr_windows(roi), key=lambda l: l.y)
    blocks = []
    for l in lines:
        t = l.text.strip()
        if not t or re.fullmatch(r"\d", t):  # stray digit (stack count icon)
            continue
        if re.search(r"combat|settings", t, re.I):
            continue
        prev = blocks[-1] if blocks else None
        joins = prev and not RE_TOAST_START.match(t) and l.y - (prev[-1].y + prev[-1].h) < l.h * 0.9 \
            and "]" not in " ".join(x.text for x in prev) + ("" if "gold" not in t.lower() else "")
        if joins or (prev and not RE_TOAST_START.match(t) and "gold" in t.lower()
                     and l.y - (prev[-1].y + prev[-1].h) < l.h * 0.9):
            prev.append(l)
        elif RE_TOAST_START.match(t):
            blocks.append([l])
    out = []
    for b in blocks:
        if not _dark_background(roi, b):
            continue
        text = " ".join(x.text.strip() for x in b)
        text = re.sub(r"\(([A-Za-z][^)\]]{2,})\]", r"[\1]", text)
        if text.startswith("[") or text.startswith("("):
            text = "Obtained " + text
        e = parse_log_entry(text)
        if e["type"] not in ("sold", "item"):
            continue
        first = next((x for x in b if "[" in x.text or "(" in x.text), b[0])
        e["rarity"] = item_rarity(roi, first, first.text)
        e["y"] = oy + b[0].y
        if e["type"] == "item":
            e.update(classify_drop(e["item"]))
        if e.get("kind", "item") == "item":
            e["item"] = fix_item_name(e["item"], e.get("rarity", "") if e.get("rarity") not in ("?", None) else "")
        out.append(e)
    return out


def read_toast(frame) -> dict | None:
    t = read_toasts(frame)
    return t[-1] if t else None


# ----------------------------------------------------------------------------- level / Paragon HUD

_LEVEL_HUD = re.compile(r"(?:l[vu]\W{0,2}\s*)?(\d{1,2})\s*[\(\[{]\s*(\d{1,4})\s*[\)\]}]", re.I)


def find_paragon(lines: list[Line], frame) -> dict | None:
    """{"level": Paragon level} from the HUD label "Lv. 70 (3)" at the bottom left of the game window.
    (The bar next to it is the character's level bar, empty at level 70 - it shows no Paragon XP.)"""
    H, W = frame.shape[:2]
    for ln in lines:
        if ln.y < H * 0.85 or ln.x > W * 0.45:
            continue
        m = _LEVEL_HUD.search(ln.text)
        if m and m.group(1) == "70":
            return {"level": int(m.group(2))}
    return None


def read_paragon(frame) -> dict | None:
    """find_paragon on an enlarged crop of the bottom left corner (the label is small; OCR of the
    whole window often misses it)."""
    H, W = frame.shape[:2]
    y0, x1 = int(H * 0.88), int(W * 0.45)
    crop = cv2.resize(frame[y0:H, 0:x1], None, fx=2, fy=2, interpolation=cv2.INTER_CUBIC)
    lines = [Line(l.text, l.x / 2, l.y / 2 + y0, l.w / 2, l.h / 2) for l in ocr_windows(crop)]
    return find_paragon(lines, frame)

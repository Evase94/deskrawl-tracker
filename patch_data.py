"""Game patch changes on top of the scraped data (data/patch_overrides.json).

The wiki pages behind data/abilities.json and data/status_effects.json lag behind game patches; the Steam patch
notes are entered in data/patch_overrides.json and applied when the data is loaded, so a new scrape of the wiki
does not undo them. Remove an entry once the wiki shows the new value.
"""
import json

import paths

_OV = None


def overrides() -> dict:
    global _OV
    if _OV is None:
        try:
            with open(paths.res("data", "patch_overrides.json"), encoding="utf-8") as f:
                _OV = json.load(f)
        except Exception:
            _OV = {}
    return _OV


def abilities(items: list) -> list:
    """Abilities with the patch changes (renamed ones under their new name)."""
    ov = overrides().get("abilities", {})
    out = []
    for a in items:
        ch = ov.get(a.get("name"))
        out.append({**a, **ch} if ch else a)
    return out


def statuses(items: list) -> list:
    ov = overrides().get("statuses", {})
    out = []
    seen = set()
    for s in items:
        ch = ov.get(s.get("name"))
        seen.add(s.get("name"))
        if ch:
            s = {**s, **{k: v for k, v in ch.items() if k != "applied_by_remove"}}
            if ch.get("applied_by_remove"):
                s["applied_by"] = [x for x in s.get("applied_by", []) if x.get("name") not in ch["applied_by_remove"]]
        out.append(s)
    for name, ch in ov.items():  # statuses new in the patch
        if name not in seen and ch.get("name"):
            out.append({k: v for k, v in ch.items() if k != "applied_by_remove"})
    return out


def items(items: list, db: bool = False) -> list:
    """Legendary items with the patch changes to their effect (db=True: the longer Item Database texts)."""
    ov = overrides().get("items", {})
    out = []
    for it in items:
        ch = ov.get(it.get("name"))
        if ch:
            it = dict(it)
            it["effect"] = ch.get("effect_db" if db and "effect_db" in ch else "effect", it.get("effect"))
        out.append(it)
    return out

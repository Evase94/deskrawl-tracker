"""Build data/status_effects.json: duration and sources of the status effects that minion and item
bonuses refer to ("+30% Damage to Slowed enemies"), from wikily.gg/deskrawl/status-effects.

Run again after a game patch:  python tools/scrape_status.py
"""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_item_db import Lines, UA  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://wikily.gg/deskrawl/status-effects/"
SLUGS = ["burn", "chill", "poisoned", "bleeding", "vulnerable", "frozen", "dazed", "stunned", "short-stun",
         "electrostatic"]


def parse(slug):
    p = Lines()
    p.feed(urllib.request.urlopen(urllib.request.Request(BASE + slug, headers=UA), timeout=30)
           .read().decode("utf-8", "replace"))
    L = p.out
    i0 = L.index("Edit") + 1
    name, text = L[i0 + 1], L[i0 + 2]
    dur_line = L[L.index("Duration and stacks") + 1] if "Duration and stacks" in L else ""
    m = re.search(r"Lasts ([\d.]+) s", dur_line)
    by = []
    if "Applied by" in L:
        k = L.index("Applied by") + 1
        while k + 1 < len(L) and L[k] != "More status effects":
            by.append({"name": L[k], "kind": L[k + 1]})
            k += 2
    return {"slug": slug, "name": name, "text": text, "duration_s": float(m.group(1)) if m else None,
            "duration": dur_line, "applied_by": by}


def main():
    out = []
    for slug in SLUGS:
        d = parse(slug)
        out.append(d)
        print(f"{d['name']:14} {d['duration_s']} s  by: " + ", ".join(f"{b['name']} ({b['kind']})" for b in d["applied_by"]))
    with open(os.path.join(ROOT, "data", "status_effects.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "wikily.gg/deskrawl/status-effects", "status_effects": out}, f, ensure_ascii=False,
                  indent=1)


if __name__ == "__main__":
    main()

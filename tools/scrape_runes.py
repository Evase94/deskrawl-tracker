"""Build data/runes.json from the afkmeta.com rune page (rune sets with their 2/4/6 bonuses, every rune with its
values at rune level 1 and 6, the hero levels that open the rune slots). Run after a game patch:
    python tools/scrape_runes.py
"""
import html
import json
import os
import re
import urllib.request

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "runes.json")
URL = "https://afkmeta.com/en/deskrawl/runes"


def clean(x):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", x))).strip().replace(" .", ".")


def num(s):
    s = s.replace(",", "")
    return float(s) if s else None


def gives(cell):
    """'<span class="dk-up">+100 → +150</span> Max Health<br>Raises the level of <a>Rage Shield</a>'
    -> stats [{"stat", "l1", "l6", "pct"}], raised ability or None."""
    stats, raises = [], None
    for part in re.split(r"<br\s*/?>", cell):
        t = clean(part)
        m = re.match(r"Raises the level of (.+)$", t)
        if m:
            raises = m.group(1).strip()
            continue
        m = re.match(r"([+-]?[\d.,]+)(%?)\s*→\s*([+-]?[\d.,]+)%?\s+(.+)$", t)
        if m:
            stats.append({"stat": m.group(4).strip(), "l1": num(m.group(1).lstrip("+")),
                          "l6": num(m.group(3).lstrip("+")), "pct": bool(m.group(2))})
            continue
        m = re.match(r"([+-]?[\d.,]+)(%?)\s+(.+)$", t)
        if m:
            v = num(m.group(1).lstrip("+"))
            stats.append({"stat": m.group(3).strip(), "l1": v, "l6": v, "pct": bool(m.group(2))})
        elif t:
            stats.append({"stat": t, "l1": None, "l6": None, "pct": False})
    return stats, raises


RE_ROW = re.compile(r'<tr id="rune-([^"]+)"(?: data-r="(\w+)")?\s*><th scope="row">.*?<span class="dk-r">(.*?)</span>.*?'
                    r'</th><td>(.*?)</td><td>(\w+)</td><td class="num">([\d,]*)</td></tr>', re.S)


def main():
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0 (DeskrawlTracker data update)"})
    s = urllib.request.urlopen(req, timeout=30).read().decode("utf-8")
    slots = [int(x) for x in re.findall(r"<li><span>Slot \d+</span><b[^>]*>Lv\. (\d+)</b></li>", s)]
    out = {"source": URL, "slots": slots, "max_level": 6, "sets": [], "runes": []}
    m = re.search(r"Game data as of ([^<]+)<", s)
    out["as_of"] = m.group(1).strip() if m else ""
    for sec in re.finditer(r'<section class="dk-runeclass" id="runes-(\w+)".*?(?=<section class="dk-runeclass"|</main>)', s, re.S):
        cls = sec.group(1).capitalize()
        body = sec.group(0)
        for art in re.finditer(r'<article class="dk-runeset[^"]*" id="set-([^"]+)">(.*?)</article>', body, re.S):
            sid, a = art.group(1), art.group(2)
            name = clean(re.search(r"<h3>(.*?)</h3>", a, re.S).group(1))
            icon = re.search(r'<h3><img[^>]*src="([^"]+)"', a)
            bonus = {}
            for b in re.finditer(r'<span class="dk-pieces">(\d)</span><div>(.*?)</div></li>', a, re.S):
                bonus[b.group(1)] = clean(re.sub(r"</p>\s*<p>", " ", b.group(2)))
            runes = []
            for r in RE_ROW.finditer(a):
                st, raises = gives(r.group(4))
                runes.append(r.group(3))
                out["runes"].append({"id": r.group(1), "name": clean(r.group(3)), "class": cls, "set": name,
                                     "kind": "set", "stats": st, "raises": raises,
                                     "tradable": r.group(5) == "Yes", "sells": num(r.group(6)) or 0,
                                     "icon_url": "https://afkmeta.com/assets/deskrawl/icons/runes/" + r.group(1) + ".webp"})
            out["sets"].append({"id": sid, "name": name, "class": cls, "bonus": bonus, "runes": [clean(x) for x in runes],
                                "icon_url": ("https://afkmeta.com" + icon.group(1)) if icon else None})
        single = re.search(r'<h3 class="dk-group">Single runes</h3>(.*?)</table>', body, re.S)
        if single:
            for r in RE_ROW.finditer(single.group(1)):
                st, raises = gives(r.group(4))
                kind = {"legendary": "grand", "rare": "rare", "uncommon": "uncommon"}.get(r.group(2) or "", r.group(2) or "rare")
                out["runes"].append({"id": r.group(1), "name": clean(r.group(3)), "class": cls if cls != "All" else "All",
                                     "set": None, "kind": kind, "stats": st, "raises": raises,
                                     "tradable": r.group(5) == "Yes", "sells": num(r.group(6)) or 0,
                                     "icon_url": "https://afkmeta.com/assets/deskrawl/icons/runes/" + r.group(1) + ".webp"})
    seen, runes = set(), []
    for r in out["runes"]:  # the "All" tab repeats every rune
        if r["id"] in seen:
            continue
        seen.add(r["id"])
        runes.append(r)
    out["runes"] = runes
    sets, seen = [], set()
    for st in out["sets"]:
        if st["id"] not in seen:
            seen.add(st["id"])
            sets.append(st)
    out["sets"] = sets
    if len(out["runes"]) < 100 or len(out["sets"]) < 10:
        raise SystemExit(f"only {len(out['runes'])} runes / {len(out['sets'])} sets - page layout changed, nothing written")
    json.dump(out, open(OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    kinds = {}
    for r in out["runes"]:
        kinds[r["kind"]] = kinds.get(r["kind"], 0) + 1
    print(len(out["sets"]), "sets,", len(out["runes"]), "runes", kinds, "slots", slots, out["as_of"])


if __name__ == "__main__":
    main()

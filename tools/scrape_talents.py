"""Update data/talents.json from the afkmeta.com talent planner (afkmeta follows the game's patches).

For every class: the tree layout (row, position, icon, ranks) and the talent texts (rank 1 and max rank).
Capstone labels and notes of talents that already exist are kept. Run after a game patch:
    python tools/scrape_talents.py
"""
import html
import json
import os
import re
import urllib.request

P = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data", "talents.json")
ROWS = [0, 5, 10, 15, 20, 30, 40, 50, 60]
RE_NODE = re.compile(r'<div class="dk-rcell" style="left:([\d.]+)%;top:([\d.]+)%"><button type="button" class="([^"]+)" '
                     r'data-kind="(\w+)" data-id="([^"]+)" data-row="(\d+)" aria-label="([^"]+)"><img class="dk-ic[^"]*" '
                     r'src="([^"]+)"[^>]*><span class="dk-pts"><span data-pts>0</span>/(\d+)</span>')
RE_ROW = re.compile(r'<tr id="talent-([^"]+)"><th scope="row">.*?</th><td class="num">(\d+).*?</td><td class="num">(\d+)</td>'
                    r'<td>(.*?)</td><td>(.*?)</td></tr>', re.S)
CAPSTONE_ROWS = {10: "Capstone Talent I", 30: "Capstone Talent II", 60: "Capstone Talent III"}


def clean(x):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", x))).strip()


def tidy(text):
    """afkmeta text -> game text: drop keyword brackets and the appended "Affects: ..." list (returned
    separately), and fix sign glitches ("-32% ... Mana Cost reduction", "a -40% longer", "−-50%")."""
    affects = []
    m = re.search(r"\s*Affects:\s*(.+)$", text)
    if m:
        affects = [a.strip() for a in m.group(1).split(",") if a.strip()]
        text = text[:m.start()].rstrip()
    text = text.replace("[", "").replace("]", "")
    text = re.sub(r"-(\d+(?:\.\d+)?)% ([\w' ]+?) Mana Cost reduction", r"+\1% \2 Mana Cost reduction", text)
    text = re.sub(r"(a|costs) -(\d+(?:\.\d+)?)% (longer|less)", r"\1 \2% \3", text)
    text = text.replace("−-", "−")
    return text, affects


def fetch(hero):
    req = urllib.request.Request(f"https://afkmeta.com/en/deskrawl/talents/{hero.lower()}",
                                 headers={"User-Agent": "Mozilla/5.0 (DeskrawlTracker data update)"})
    return urllib.request.urlopen(req, timeout=30).read().decode("utf-8")


def main():
    data = json.load(open(P, encoding="utf-8"))
    for hero in ("Sorcerer", "Warrior", "Hunter", "Monk"):
        s = fetch(hero)
        nodes = RE_NODE.findall(s)
        table = {m[0]: m for m in RE_ROW.findall(s)}
        old = {t.get("id") or t["name"]: t for t in data["heroes"].get(hero, [])}
        out, changed = [], []
        for left, top, cls, kind, tid, row, name, src, ranks in nodes:
            name = html.unescape(name)
            pts = ROWS[int(row) - 1]
            tr = table.get(tid)
            r1, _ = tidy(clean(tr[3])) if tr else ("", [])
            rmax, affects = tidy(clean(tr[4])) if tr else ("", [])
            o = old.get(tid, {})
            t = {
                "name": name, "id": tid, "points": pts, "ranks": int(ranks),
                "capstone": o.get("capstone") or (CAPSTONE_ROWS.get(pts, "Capstone") if kind == "c" and int(ranks) == 1
                                                  and pts in CAPSTONE_ROWS else ""),
                "rank1": r1 if r1 not in ("—", "") else (rmax if int(ranks) == 1 else None),
                "rank_max": rmax, "note": o.get("note", ""), "x": float(left) / 100, "y": float(top) / 100,
                "icon_url": "https://afkmeta.com" + src if src.startswith("/") else src, "node_class": cls,
                "affects": affects,
            }
            if not o:
                changed.append(f"NEW {name}: {rmax}")
            elif (o.get("rank_max"), o.get("ranks"), o.get("points"), o.get("name")) != (rmax, t["ranks"], pts, name):
                changed.append(f"{name}: {o.get('rank_max')!r} -> {rmax!r}" +
                               (f" (ranks {o.get('ranks')} -> {t['ranks']})" if o.get("ranks") != t["ranks"] else ""))
            out.append(t)
        gone = [t["name"] for k, t in old.items() if k not in {x["id"] for x in out}]
        if len(out) < 30:
            raise SystemExit(f"{hero}: only {len(out)} talents found - page layout changed, nothing written")
        data["heroes"][hero] = out
        print(f"== {hero}: {len(out)} talents")
        for c in changed:
            print("  ", c)
        for g in gone:
            print("   REMOVED", g)
    json.dump(data, open(P, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()

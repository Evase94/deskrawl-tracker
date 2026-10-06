"""Build data/item_db.json: every Legendary and Divine item with its numbers, attribute pool and
where it drops, from the item pages of wikily.gg/deskrawl/equipment.

Run again after a game patch:  python tools/scrape_item_db.py
"""
import json
import os
import re
import sys
import urllib.request
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = "https://wikily.gg/deskrawl/equipment/"
UA = {"User-Agent": "Mozilla/5.0 (DeskrawlTracker item data)"}


class Lines(HTMLParser):
    """Visible text of <main>, one entry per text node (like the page shows it)."""

    def __init__(self):
        super().__init__()
        self.depth, self.skip, self.out = 0, 0, []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.depth += 1
        elif self.depth and tag in ("script", "style", "noscript"):
            self.skip += 1

    def handle_endtag(self, tag):
        if tag == "main":
            self.depth -= 1
        elif self.depth and tag in ("script", "style", "noscript"):
            self.skip -= 1

    def handle_data(self, data):
        s = data.strip()
        if self.depth and not self.skip and s and s.lower() != "advertisement":
            self.out.append(s)


def page_lines(slug):
    req = urllib.request.Request(BASE + slug, headers=UA)
    html = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    p = Lines()
    p.feed(html)
    return p.out


NUM = re.compile(r"^[\d.,]+(-[\d.,]+)?%?$")


def join_where(parts):
    """The "Where" tab as [(title, text)]: link fragments ("Met in", "The Cinder Crown", "stage 7…")
    are glued back into sentences."""
    out = []
    for x in parts:
        if x.startswith("Enemy special drops are rolled") or x == "Equipment":
            continue
        heading = len(x) < 45 and not x.endswith((".", ":", ",")) and x[:1].isupper() \
            and not (out and out[-1][1] and not out[-1][1].endswith((".", ")")))
        if heading and (not out or out[-1][1]):
            out.append([x, ""])
        elif out:
            sep = "" if x[:1] in ".,;:)" else " "
            out[-1][1] = (out[-1][1] + sep + x).strip() if out[-1][1] else x
        else:
            out.append(["", x])
    return [(t, s) for t, s in out if t or s]


def parse(slug, L):
    i0 = L.index("Edit") + 1
    kind, name, summary = L[i0], L[i0 + 1], L[i0 + 2]
    i_item = L.index("Item")
    head = L[i0 + 3:i_item]
    cuts = [head.index(k) for k in ("Unique effect", "Ability levels") if k in head]
    fixed_raw = head[:min(cuts)] if cuts else head
    effect = flavor = ability = ""
    if "Unique effect" in head:
        k = head.index("Unique effect")
        effect = head[k + 1] if k + 1 < len(head) else ""
        flavor = " ".join(x for x in head[k + 2:] if x != "Ability levels" and not x.startswith("All ability"))
    if "Ability levels" in head:
        ability = head[head.index("Ability levels") + 1]
    # fixed numbers of a Divine item: weapon "430 Damage 0.80 Speed 344.0 DPS", then "name, value" pairs
    fixed, weapon = [], {}
    k = 0
    while k + 1 < len(fixed_raw):
        a, b = fixed_raw[k], fixed_raw[k + 1]
        if NUM.match(a) and b in ("Damage", "Speed", "DPS"):
            weapon[b.lower()] = a
            k += 2
        elif not NUM.match(a) and re.match(r"^[+\-]?[\d.,]+%?$", b):
            fixed.append([a, b])
            k += 2
        else:
            k += 1
    info = {}
    for key in ("Minimum drop level", "Required level", "Sockets", "Can be Ancient", "Can be Black Mist",
                "Tradable", "Upgradable"):
        if key in L:
            info[key] = L[L.index(key) + 1]
    i_where = L.index("Where")
    i_more = L.index("More", i_where)
    i_note = next((k for k in range(i_more, len(L)) if L[k].startswith("Drop chances are")), None)
    if i_note is None:  # Back items without drops of their own: the tab ends at the next section
        i_note = next((k for k in range(i_more, len(L)) if L[k].startswith(("Back items have no", "Upgrades"))),
                      len(L))
    where = join_where(L[i_more + 1:i_note])
    after = L[i_note + 1:]
    i_up = after.index("Upgrades") if "Upgrades" in after else len(after)
    st = after[:i_up]
    base = None
    if "Item level" in st:
        k = st.index("Item level")
        hdr = []
        while k < len(st) and not NUM.match(st[k]):
            hdr.append(st[k])
            k += 1
        rows = []
        while k + len(hdr) <= len(st) and NUM.match(st[k]):
            rows.append(st[k:k + len(hdr)])
            k += len(hdr)
        base = {"columns": hdr, "rows": rows}
    attrs, attr_cols = [], []
    if "Attribute" in st:
        attr_cols = st[st.index("Attribute"):st.index("Attribute") + 3]
        k = st.index("Attribute") + 3
        while k + 2 < len(st) and not NUM.match(st[k]) and NUM.match(st[k + 1]):
            attrs.append([st[k], st[k + 1], st[k + 2]])
            k += 3
    black_mist = next((x for x in st if x.startswith("A Black Mist")), "")
    pool = {"note": "", "primary": [], "classes": {}, "secondary": []}
    rest = after[i_up:]
    k = next((i for i, x in enumerate(rest)
              if re.search(r"rolls \d+ primary attributes|rolls no attributes|has no attributes|roll no attributes",
                           x)), None)
    if k is not None:
        pool["note"] = rest[k]
        mode = cls = None
        for x in rest[k + 1:]:
            low = x.lower()
            if low == "primary pool":
                mode = "p"
            elif low == "per class":
                mode = "c"
            elif low == "secondary pool":
                mode = "s"
            elif x in ("Equipment", "Same slot and rarity", "SAME SLOT AND RARITY"):
                break
            elif mode == "p":
                pool["primary"].append(x)
            elif mode == "s":
                pool["secondary"].append(x)
            elif mode == "c":
                if x.startswith("Also rolls"):
                    pool["classes"][cls] = [a.strip() for a in
                                            re.split(r",| and ", x[len("Also rolls"):].rstrip(".")) if a.strip()]
                else:
                    cls = x
    return {"slug": slug, "name": name, "rarity": kind.split()[0], "summary": summary,
            "effect": effect, "ability_levels": ability, "flavor": flavor, "weapon": weapon, "fixed": fixed,
            "info": info, "where": where, "base": base, "attributes": attrs, "attribute_columns": attr_cols, "black_mist": black_mist,
            "pool": pool}


def main():
    with open(os.path.join(ROOT, "data", "items.json"), encoding="utf-8") as f:
        known = json.load(f)["items"]
    out = []
    for it in known:
        try:
            d = parse(it["slug"], page_lines(it["slug"]))
        except Exception as e:  # keep going; report at the end
            print("failed:", it["slug"], e, file=sys.stderr)
            continue
        d.update({"slot": it["slot"], "classes": it["classes"], "icon_url": it.get("icon_url"),
                  "min_drop_level": it.get("min_drop_level")})
        out.append(d)
        print(f"{d['rarity']:9} {d['slot']:11} {d['name']}")
    with open(os.path.join(ROOT, "data", "item_db.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "wikily.gg/deskrawl/equipment", "items": out}, f, ensure_ascii=False, indent=1)
    print(len(out), "items written")


if __name__ == "__main__":
    main()

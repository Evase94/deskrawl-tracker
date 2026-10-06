"""Build data/minions.json: every minion with rarity, species, carriage capacity, passives, ability and
how to get it, from wikily.gg/deskrawl/minions.

Run again after a game patch:  python tools/scrape_minions.py
"""
import json
import os
import re
import sys
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from scrape_item_db import Lines, UA  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE = "https://wikily.gg/deskrawl/minions/"

# the list page shows 8 minions at a time; these are all 67 (collected from its pages)
SLUGS = """armored-horse tidewrath-crab-king ancient-shellwarden ancient-oak-treant blue-mammoth dark-elf-mage
defiled-wyrm bone-reaper oakback-pack-bull skeleton-warrior sandstorm-harpy-queen dread-sand-manticore
flying-fish-king venom-queen ogre raptor redrock-lord serparmat-the-devourer lord-of-the-crimson-vale goblin-giant
frost-dragon ice-hound lord-of-the-cinder emberfang bloodmoon-werewolf shoal-naga brinescale-serpent dark-horse
undead-axemaster keep-succubus dune-eagle orc-fighter fae-dragon ice-rock-elemental frost-whelp tazan-camel
cinderlash hellguard magma-golem white-horse mutating-wolf driftshell-hermit-crab reef-squid boar chestnut-pony
defiled-ooze carrion-scarab sand-kobold-thief sand-kobold-warrior sand-wasp goblin-warrior venomous-spider
barrens-crocodile scorched-imp redrock-golem barrens-satyr grizzly-bear crimson-guardian mountain-tiger
skeleton-archer frost-slime frozen-zombie-warrior black-spider stumplin ember-bat grey-forest-wolf
brown-horse""".split()


def lines(slug):
    p = Lines()
    req = urllib.request.Request(BASE + slug, headers=UA)
    p.feed(urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace"))
    return p.out


def section(L, start, ends):
    if start not in L:
        return []
    i = L.index(start) + 1
    j = next((k for k in range(i, len(L)) if L[k] in ends), len(L))
    return L[i:j]


def parse(slug, L):
    i0 = L.index("Edit") + 1
    kind, name, summary = L[i0], L[i0 + 1], L[i0 + 2]
    info = {}
    for key in ("Species", "Rarity", "Carriage capacity", "Obtained by", "Rein", "Rein sells for", "Rein tradable"):
        if key in L[i0:]:
            info[key] = L[L.index(key, i0) + 1]
    pas = section(L, "Passives", ("Abilities", "How to get it"))
    passives = [{"name": pas[k], "text": pas[k + 1]} for k in range(0, len(pas) - 1, 2)]
    ab = section(L, "Abilities", ("How to get it",))
    abilities = [{"name": ab[k], "text": ab[k + 1]} for k in range(0, len(ab) - 1, 2)]
    src = section(L, "Sources", ("All minions",))
    src = [x for x in src if not x.startswith(("Rein drop chances are rolled", "Levels are for Normal",
                                                "Use to permanently unlock"))]
    m = re.search(r"\(([\d.]+%) per cleared encounter", summary)
    return {"slug": slug, "name": name, "rarity": info.get("Rarity", kind.split()[0]),
            "species": info.get("Species", ""), "capacity": int(info.get("Carriage capacity", "0") or 0),
            "obtained": info.get("Obtained by", ""), "rein_chance": m.group(1) if m else "",
            "summary": summary, "passives": passives, "abilities": abilities, "sources": src,
            "rein_tradable": info.get("Rein tradable", "")}


def main():
    out = []
    for slug in SLUGS:
        try:
            d = parse(slug, lines(slug))
        except Exception as e:
            print("failed:", slug, e, file=sys.stderr)
            continue
        out.append(d)
        print(f"{d['rarity']:9} {d['name']:28} " + " | ".join(p["text"] for p in d["passives"])
              + (" || " + d["abilities"][0]["text"] if d["abilities"] else ""))
    with open(os.path.join(ROOT, "data", "minions.json"), "w", encoding="utf-8") as f:
        json.dump({"source": "wikily.gg/deskrawl/minions", "minions": out}, f, ensure_ascii=False, indent=1)
    print(len(out), "minions written")


if __name__ == "__main__":
    main()

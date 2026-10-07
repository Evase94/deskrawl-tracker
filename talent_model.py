"""What a talent build is worth for a character, from everything the tracker knows, and the best build.

Facts used:
  - character sheet (F9): primary attribute, crit, element, armor, mana ... -> item_eval's damage formula and
    survival model (Toughness, Recovery, damage types of your deaths),
  - skill tracking: casts per second and damage share of every ability you use, how often enemies carry each
    status (Burn, Chill, Vulnerable ...), kills per second,
  - combat simulation (combat_sim): what cooldown, mana cost and mana talents do to your casts,
  - ability descriptions (damage %, targets, cooldowns) and status durations (wiki).

The character sheet and the measurements already contain the talents you have now, so a build is valued
against your current build ("mine"): every number of the build minus the same number of your build.

Every talent rank is read into effects (parse()). Kinds:
  stat         character sheet stat (+50 Intelligence, +40% Lightning Damage)
  dmg          more damage of some abilities ("all" = every ability), optionally only against enemies with a
               status ("+96% Damage to Burning targets") or per stack of one (Electrostatic)
  crit_chance / crit_dmg   the same for critical hits
  cd / mana_cost / mana_cast / mana_hit / free_cast   change casts -> combat simulation
  apply / status_dur       statuses you inflict -> how often enemies carry them -> conditional bonuses
  buff         a bonus for some seconds after a trigger ("Casting a Special ... for 5s") -> its uptime
  proc         extra weapon damage per hit / cast / kill
  heal / dr    healing and damage reduction (part of the time)
  none         not rated, with the reason (range, knockback, size ...)
"""
import math
import re

import item_eval
import skills
import talents

# assumptions where the game does not give numbers (shown on the Talents page)
ELECTROSTATIC_PER_STACK = 5.0  # % more Lightning damage taken per stack of Electrostatic
VULNERABLE_PCT = 30.0          # "Take 30% more damage" (status text)
HEALTHY_SHARE = 0.7            # share of the time above 80% health
ALONE_SHARE = 0.25             # share of the time with no enemy nearby (ranged heroes)
LOW_HEALTH_SHARE = 0.15        # share of the time below 50-60% health (heals that need low health)
PROC_TARGETS = 2.0             # enemies an explosion "to nearby enemies" reaches

STATUS_ALIASES = {"burning": "Burn", "burned": "Burn", "burn": "Burn", "chill": "Chill", "chilled": "Chill",
                  "frozen": "Frozen", "vulnerable": "Vulnerable", "electrostatic": "Electrostatic",
                  "poisoned": "Poisoned", "poison": "Poisoned", "bleeding": "Bleeding", "bleed": "Bleeding",
                  "stunned": "Stunned", "stun": "Stunned", "short stun": "Short Stun", "dazed": "Dazed",
                  "slowed": "Slowed", "immobilized": "Immobilized"}
# conditions that several statuses cause
COND_OF = {"Slowed": ["Chill", "Dazed", "Frozen"], "Immobilized": ["Frozen", "Stunned"],
           "Stunned": ["Stunned", "Short Stun", "Frozen"]}
ITEM_CONDITIONS = {"Damage vs Burned": "Burn", "Damage vs Slowed": "Slowed", "Damage vs Immobilized": "Immobilized",
                   "Damage vs Bleeding": "Bleeding", "Damage vs Poisoned": "Poisoned",
                   "Damage vs Vulnerable": "Vulnerable"}


def _status(word: str) -> str | None:
    """Status named by one or two words ("Short Stun", "Burn to the target" -> Burn)."""
    w = (word or "").strip().lower().rstrip(".").split()
    return (STATUS_ALIASES.get(" ".join(w[:2])) or STATUS_ALIASES.get(w[0])) if w else None


# ----------------------------------------------------------------------------- abilities

def hero_abilities(hero: str) -> dict:
    return {a["name"]: a for a in skills.ABILITIES if a.get("hero") == hero}


def _tags(a) -> set:
    t = {x.lower() for x in a.get("tags", [])}
    if "frost" in t:
        t.add("cold")
    return t


def match(spec: str, a: dict) -> bool:
    """Does a target ("Basic Attack", "Lightning", "Fire Ball", "all", "Trap") describe ability a?"""
    s = (spec or "").strip().lower()
    if s in ("all", "any", ""):
        return True
    if s == a["name"].lower():
        return True
    slot = (a.get("slot") or "Special").lower()
    if s in (slot, slot + "s") or (s in ("special", "special ability", "specials") and slot == "special"):
        return True
    s1 = s[:-1] if s.endswith("s") else s
    tags = _tags(a)
    return s in tags or s1 in tags or {"frost": "cold", "cold": "frost"}.get(s, "") in tags


def coverage(a) -> float:
    import build_profile
    return build_profile.coverage(a.get("description", ""))


def bolts(a) -> float:
    """Hits of one cast on one enemy (bolts, ticks ...)."""
    desc = a.get("description", "")
    m = re.search(r"(\d+) [\w ]*?(?:bolts|hits|strikes|projectiles|arrows|cuts)[^.]*?each", desc)
    n = int(m.group(1)) if m else 1
    return max(n / max(skills.targets(a), 1), 1)


def weight(a) -> float:
    """Weapon damage % of one cast, all hits and targets (skills.damage_weight, plus hit counts it misses such
    as "3 savage cuts")."""
    w = skills.damage_weight(a)
    desc = a.get("description", "")
    if not re.search(r"(\d+) [\w ]*?(?:bolts|hits|strikes|projectiles|arrows)[^.]*?each", desc):
        m = re.search(r"(\d+) [\w ]*?cuts[^.]*?each", desc)
        if m:
            w *= int(m.group(1))
    return w


def _seconds(v) -> float | None:
    m = re.search(r"([\d.]+)\s*s", str(v or ""))
    return float(m.group(1)) if m else None


def duration(a) -> float | None:
    m = re.search(r"(?:for|lasting) (\d+(?:\.\d+)?)\s?s(?:econds)?", a.get("description", ""))
    return float(m.group(1)) if m else None


# ----------------------------------------------------------------------------- text -> effects

_N = r"([\d.]+)"


def parse(text: str, hero: str) -> list:
    """Effects of a talent text at the strength written there. Each effect is a dict with "k" (kind) and
    "v" (the number that grows with ranks); an empty list means nothing in the text was recognised."""
    t = (text or "").replace("[", "").replace("]", "").replace("’", "'")
    abil = hero_abilities(hero)
    names = sorted(abil, key=len, reverse=True)
    out, used = [], []

    def target(s):
        s = (s or "").strip()
        s = re.sub(r"^(?:your|all|the)\s+", "", s, flags=re.I)
        s = re.sub(r"\s+(?:abilit(?:y|ies)|attacks?|hits?|casts?)$", "", s, flags=re.I) if s.lower() not in (
            "basic attack", "strong attack", "basic attacks", "strong attacks") else s
        for n in names:
            if s.lower() == n.lower() or s.lower().rstrip("s") == n.lower().rstrip("s"):
                return n
        low = s.lower().rstrip("s")
        if low in ("basic attack", "strong attack", "special", "special abilitie", "special ability"):
            return {"special abilitie": "Special", "special ability": "Special"}.get(low, s.title().rstrip("s"))
        if any(match(low, a) for a in abil.values()):
            return s
        return None

    def take(pattern, fn, flags=re.I):
        for m in re.finditer(pattern, t, flags):
            if any(m.start() < e and m.end() > s for s, e in used):
                continue
            r = fn(m)
            if r is None:
                continue
            rs = r if isinstance(r, list) else [r]
            if any(e.get(k, "x") is None for e in rs for k in ("target", "src", "status", "ability", "dst")):
                continue  # names nothing of this hero: let a later rule try
            used.append((m.start(), m.end()))
            out.extend(rs)

    f = lambda m, i: float(m.group(i))

    # --- conversions and specials first (their wording contains plain "+N% X" parts)
    take(rf"{_N}% of your Max Mana is added to Intelligence", lambda m: {"k": "mana_to_int", "v": f(m, 1)})
    take(rf"{_N}% of your Max HP is added to your Thorns?", lambda m: {"k": "hp_to_thorns", "v": f(m, 1)})
    take(rf"Gain bonus Critical Damage equal to {_N}% of your Dodge Chance",
         lambda m: {"k": "dodge_to_cd", "v": f(m, 1)})
    take(rf"While ([A-Z][\w' ]+?) is active, gain \+{_N}% Armor and Magic Resist",
         lambda m: [{"k": "while", "ability": target(m.group(1)) or m.group(1), "stat": "Bonus Armor", "v": f(m, 2)},
                    {"k": "while", "ability": target(m.group(1)) or m.group(1), "stat": "Magic Resist %", "v": f(m, 2)}])
    take(rf"Restore {_N}% of your maximum Health every second during ([A-Z][\w' ]+)",
         lambda m: {"k": "heal_during", "ability": target(m.group(2)) or m.group(2), "v": f(m, 1)})
    take(rf"Heal {_N}% when HP drop below (\d+)% Max HP\.? \((\d+) seconds cooldown\)",
         lambda m: {"k": "heal_every", "v": f(m, 1), "every": float(m.group(3)), "share": LOW_HEALTH_SHARE})
    take(r"A lethal blow restores you to full health.*?Cooldown: (\d+)s",
         lambda m: {"k": "heal_every", "v": 100.0, "every": float(m.group(1)), "share": LOW_HEALTH_SHARE})
    take(rf"Restore {_N} Health for each Mana spent", lambda m: {"k": "heal_per_mana", "v": f(m, 1)})
    take(rf"Each Basic Attack hit adds a shield equal to {_N}% of your Max HP for (\d+)s",
         lambda m: {"k": "shield_hit", "target": "Basic Attack", "v": f(m, 1)})
    # mana
    take(rf"([A-Z][\w' ]+?) generates \+{_N} Mana", lambda m: {"k": "mana_cast", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"\+{_N} Mana Gain from ([A-Z][\w' ]+)", lambda m: {"k": "mana_cast", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"Casting (?:an? )?([A-Z][\w' ]+?) (?:has a 100% chance to )?restores? {_N} Mana",
         lambda m: {"k": "mana_cast", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"Generate {_N} Mana upon activating ([A-Z][\w' ]+)",
         lambda m: {"k": "mana_cast", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"Each direct enemy hit restores {_N}% of your Max Mana", lambda m: {"k": "mana_hit", "target": "all", "v": f(m, 1)})
    take(rf"Casting a different (\w+) ability from your previous \w+ ability restores {_N}% of Max Mana",
         lambda m: {"k": "mana_cast_pct", "target": m.group(1), "v": f(m, 2) * 0.5})
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Mana Cost reduction",
         lambda m: {"k": "mana_cost", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"\+{_N} Mana On Kill", lambda m: {"k": "stat", "stat": "Mana on Kill", "v": f(m, 1)})
    take(rf"Melee attackers have a {_N}% chance to grant you (\d+) Mana", lambda m: [])
    take(rf"Each Dodge will generate {_N} Mana", lambda m: {"k": "mana_dodge", "v": f(m, 1)})
    # cooldowns and casts
    take(rf"Reduces the cooldown of (\w+) abilities by {_N}%", lambda m: {"k": "cd", "target": m.group(1), "v": f(m, 2)})
    take(rf"\+{_N}% (\w+) Abilities Cooldown reduction", lambda m: {"k": "cd", "target": m.group(2), "v": f(m, 1)})
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Cooldown reduction", lambda m: {"k": "cd", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"([A-Z][\w' ]+?) deals \+{_N}% Damage but has a {_N}% longer Cooldown",
         lambda m: [{"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)},
                    {"k": "cd", "target": target(m.group(1)), "v": -f(m, 3)}])
    take(rf"([A-Z][\w' ]+?) has a {_N}% chance to also cast ([A-Z][\w' ]+?)(?: for free)?\.",
         lambda m: {"k": "free_cast", "src": target(m.group(1)), "dst": target(m.group(3)), "v": f(m, 2)})
    take(rf"Casting a Warcry reduces the other Special ability's remaining cooldown by (\d+) second",
         lambda m: {"k": "cd_flat", "trigger": "Warcry", "target": "Special", "v": float(m.group(1))})
    take(rf"Casting a Strong Attack reduces all (\w+) ability cooldowns by (\d+)s",
         lambda m: {"k": "cd_flat", "trigger": "Strong Attack", "target": m.group(1), "v": float(m.group(2))})
    take(rf"(Basic Attack) has a {_N}% chance to strike twice", lambda m: {"k": "dmg", "target": "Basic Attack", "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?) attacks {_N}% faster", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?) strikes and spins {_N}% faster", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?)'s (?:ground effect|channel duration) (?:lasts|is increased by) {_N}%",
         lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?)'s ground effect lasts {_N}% longer", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?) lingers {_N}% longer", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?) carves (\d+) additional cuts", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": float(m.group(2)) / 3 * 100})
    take(rf"([A-Z][\w' ]+?) targets (\d+) additional enemies", lambda m: {"k": "dmg", "target": target(m.group(1)), "v": float(m.group(2)) / 3 * 100 * 0.5})
    take(rf"\+{_N}% rotation speed to ([A-Z][\w' ]+)", lambda m: {"k": "dmg", "target": target(m.group(2)), "v": f(m, 1) * 0.5})
    take(rf"each ([A-Z][\w' ]+?) projectile ricochets to another random enemy\. ([A-Z][\w' ]+?) deals {_N}% more damage",
         lambda m: {"k": "dmg", "target": target(m.group(2)), "v": f(m, 3) + 50})
    take(rf"([A-Z][\w' ]+?) deals {_N}% more damage for each enemy its projectile has already pierced",
         lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2) * 0.5})
    take(rf"([A-Z][\w' ]+?) splashes {_N}% weapon damage as \w+ to nearby enemies",
         lambda m: {"k": "proc", "src": target(m.group(1)), "per": "cast", "p": 1.0, "every": 1, "wd": f(m, 2), "targets": PROC_TARGETS - 1})
    take(rf"\+{_N}% Damage (?:on|to) (\w+) targets", lambda m: (
        {"k": "dmg", "target": "all", "v": f(m, 1), "cond": _status(m.group(2))} if _status(m.group(2)) else None))
    take(rf"\+{_N}% Damage to ([A-Z][\w' ]+?)(?: [Aa]bilities)?(?=\.|$)",
         lambda m: {"k": "dmg", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"\+{_N}% Critical Hit Chance to ([A-Z][\w' ]+?)(?: [Aa]bilities)?(?=\.|$)",
         lambda m: {"k": "crit_chance", "target": target(m.group(2)), "v": f(m, 1)})
    take(rf"([A-Z][\w' ]+?) gains \+{_N}% Critical Hit Chance",
         lambda m: {"k": "crit_chance", "target": target(m.group(1)), "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?) costs {_N}% less Mana and deals \+{_N}% Damage",
         lambda m: [{"k": "mana_cost", "target": target(m.group(1)), "v": f(m, 2)},
                    {"k": "dmg", "target": target(m.group(1)), "v": f(m, 3)}])
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Damage, but also \+{_N}% Mana Cost",
         lambda m: [{"k": "dmg", "target": target(m.group(2)), "v": f(m, 1)},
                    {"k": "mana_cost", "target": target(m.group(2)), "v": -f(m, 3)}])
    take(rf"Casting a ([A-Z][\w' ]+?) ability grants \+{_N}% Attack Speed per stack \(up to (\d+) stacks\)",
         lambda m: {"k": "stat", "stat": "Attack Speed Bonus", "v": f(m, 2) * int(m.group(3)) * 0.9})
    take(rf"([A-Z][\w' ]+?) grants \+{_N}% Dodge Chance for (\d+)s",
         lambda m: {"k": "buff", "trigger": target(m.group(1)), "per": "cast", "every": 1, "dur": float(m.group(3)),
                    "stacks": 1, "give": {"k": "stat", "stat": "Dodge Chance"}, "v": f(m, 2)})
    take(rf"After dodging, gain \+{_N}% (\w+) Damage for (\d+)s",
         lambda m: {"k": "stat", "stat": f"{m.group(2)} Damage", "v": f(m, 1) * 0.3})
    take(rf"\+{_N}% Dodge Chance while surrounded", lambda m: {"k": "stat", "stat": "Dodge Chance", "v": f(m, 1) * 0.5})
    take(r"While out of combat[^.]*", lambda m: {"k": "none", "why": "only out of combat"})
    # buffs with an uptime
    take(rf"Casting an? ([A-Z][\w' ]+?) increases ([A-Z][\w' ]+?) damage by {_N}% for (\d+)s",
         lambda m: {"k": "buff", "trigger": target(m.group(1)), "per": "cast", "every": 1, "dur": float(m.group(4)),
                    "stacks": 1, "give": {"k": "dmg", "target": target(m.group(2)) or m.group(2)}, "v": f(m, 3)})
    take(rf"[Ii]ncreases? (Basic Attack|Strong Attack) damage by {_N}% for (\d+)s",
         lambda m: {"k": "buff", "trigger": m.group(1), "per": "cast", "every": 1, "dur": float(m.group(3)),
                    "stacks": 1, "give": {"k": "dmg", "target": m.group(1)}, "v": f(m, 2)})
    take(rf"(Basic Attack) hits grant a charge\. At (\d+) charges, gain \+{_N}% to all damage for (\d+)s",
         lambda m: {"k": "buff", "trigger": m.group(1), "per": "hit", "every": int(m.group(2)), "dur": float(m.group(4)),
                    "stacks": 1, "give": {"k": "dmg", "target": "all"}, "v": f(m, 3)})
    take(rf"Casting an? (\w+) ability grants [\w ]+ for (\d+)s, increasing (\w+) Damage by {_N}% per stack \(Max (\d+) stacks\)",
         lambda m: {"k": "buff", "trigger": m.group(1), "per": "cast", "every": 1, "dur": float(m.group(2)),
                    "stacks": int(m.group(5)), "give": {"k": "dmg", "target": m.group(3)}, "v": f(m, 4)})
    take(rf"Every (\d+) Basic Attack casts store one charge\. Each Strong Attack spends one charge to deal {_N}% more damage",
         lambda m: {"k": "charge", "every": int(m.group(1)), "target": "Strong Attack", "v": f(m, 2)})
    take(rf"Casting a Warcry reduces nearby enemies' damage dealt by {_N}% for (\d+)s",
         lambda m: {"k": "buff", "trigger": "Warcry", "per": "cast", "every": 1, "dur": float(m.group(2)), "stacks": 1,
                    "give": {"k": "stat", "stat": "Damage Reduction"}, "v": f(m, 1)})
    take(rf"Casting ([A-Z][\w' ]+?) reduces damage taken by {_N}% for (\d+)s",
         lambda m: {"k": "buff", "trigger": target(m.group(1)), "per": "cast", "every": 1, "dur": float(m.group(3)),
                    "stacks": 1, "give": {"k": "stat", "stat": "Damage Reduction"}, "v": f(m, 2)})
    take(rf"When an enemy dies while Bleeding, or is slain by a Bleed Ability, gain a stack of \w+ for (\d+)s, "
         rf"increasing your Damage by {_N}% per stack, up to (\d+) stacks",
         lambda m: {"k": "buff", "trigger": "kill:Bleeding", "per": "kill", "every": 1, "dur": float(m.group(1)),
                    "stacks": int(m.group(3)), "give": {"k": "dmg", "target": "all"}, "v": f(m, 2)})
    take(rf"([A-Z][\w' ]+?)'s Attack Speed bonus is {_N}% stronger and lasts ([\d.]+)s longer",
         lambda m: {"k": "ability_buff", "ability": target(m.group(1)), "v": f(m, 2), "extra_s": float(m.group(3))})
    take(rf"([A-Z][\w' ]+?)'s Dodge Chance and Attack Speed bonuses are increased by {_N}%",
         lambda m: {"k": "ability_buff", "ability": target(m.group(1)), "v": f(m, 2), "extra_s": 0.0})
    take(rf"\+{_N}% All Damage while you are Healthy", lambda m: {"k": "stat", "stat": "All Damage", "v": f(m, 1) * HEALTHY_SHARE})
    take(rf"While no enemies are nearby, gain {_N}% Attack Speed and {_N}% ([\w, ]+?) Damage",
         lambda m: [{"k": "stat", "stat": "Attack Speed Bonus", "v": f(m, 1) * ALONE_SHARE}] +
         [{"k": "dmg", "target": e.strip(), "v": f(m, 2) * ALONE_SHARE}
          for e in re.split(r",\s*(?:and\s+)?|\s+and\s+", m.group(3)) if e.strip()])
    take(rf"After casting a Strong Attack, spend all remaining Mana", lambda m: {"k": "none", "why": "spends all mana – depends on your mana flow"})
    # procs
    take(rf"After every (\d+) direct enemy hits, counterattack nearby enemies for {_N}% weapon damage",
         lambda m: {"k": "proc", "src": "all", "per": "hit", "p": 1.0, "every": int(m.group(1)), "wd": f(m, 2), "targets": PROC_TARGETS})
    take(rf"When you kill an enemy affected by (\w+), there is a {_N}% chance to trigger [\w ]+?, dealing {_N}% weapon damage to nearby enemies",
         lambda m: {"k": "proc", "src": "kill", "cond": _status(m.group(1)), "per": "kill", "p": f(m, 2) / 100, "every": 1,
                    "wd": f(m, 3), "targets": PROC_TARGETS})
    take(rf"When you kill a Bleeding enemy, deal {_N}% of its remaining Bleed damage",
         lambda m: {"k": "proc", "src": "kill", "cond": "Bleeding", "per": "kill", "p": 1.0, "every": 1,
                    "wd": f(m, 1) / 100 * 125, "targets": PROC_TARGETS - 1})
    take(rf"A Lightning hit on an enemy with 5 stacks of Electrostatic consumes them, calling down a lightning strike for {_N}% weapon damage",
         lambda m: {"k": "proc", "src": "Lightning", "per": "electro5", "p": 1.0, "every": 1, "wd": f(m, 1), "targets": 1})
    take(rf"([A-Z][\w' ]+?) hits have a {_N}% chance to apply ([A-Z][\w' ]+?)\. A hit from ([A-Z][\w' ]+?) or ([A-Z][\w' ]+?) consumes all stacks and deals {_N}% weapon damage at 1 stack or {_N}%",
         lambda m: {"k": "proc", "src": target(m.group(1)), "per": "cast", "p": f(m, 2) / 100, "every": 1, "wd": f(m, 7) / 2,
                    "targets": 1})
    take(rf"Bleed Ability hits have a {_N}% chance to execute non-Boss enemies below 30% HP",
         lambda m: {"k": "dmg", "target": "Bleed", "v": f(m, 1) * 0.3})
    take(rf"Critical Hit from (\w+) abilities will have {_N}% chance to trigger your Mana On Kill",
         lambda m: {"k": "none", "why": "mana on critical hits – small, not simulated"})
    take(rf"Thorns? can trigger Critical Hit", lambda m: {"k": "none", "why": "Thorns damage is not part of the damage model"})
    take(rf"\+{_N}% Thorns?\b", lambda m: {"k": "stat", "stat": "Thorns %", "v": f(m, 1)})
    take(r"Taking damage while below \d+% Health inflicts", lambda m: {"k": "none", "why": "only while you are low on health"})
    take(rf"([A-Z][\w' ]+?)'s third bolt restores an additional {_N}% of your Max Health",
         lambda m: {"k": "heal_cast", "target": target(m.group(1)), "every": 3, "v": f(m, 2)})
    take(rf"Poisoned ticks have a {_N}% chance to trigger Life on Hit", lambda m: {"k": "none", "why": "Life on Hit from poison ticks – not modelled"})
    # statuses you inflict
    take(rf"Critical Hits(?: with (\w+) abilities)? have a {_N}% chance to apply (?:an additional stack of )?(\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(3)), "src": f"crit:{m.group(1) or 'all'}", "p": f(m, 2) / 100, "every": 1})
    take(rf"Applying Chill to an enemy has a {_N}% chance to also apply (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(2)), "src": "status:Chill", "p": f(m, 1) / 100, "every": 1})
    take(rf"([A-Z][\w' ]+?) direct critical hits inflict (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(2)), "src": f"crit:{target(m.group(1)) or 'all'}", "p": 1.0, "every": 1})
    take(rf"Every (\d+) casts of ([A-Z][\w' ]+?) (?:apply|inflicts?) (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(3)), "src": target(m.group(2)), "p": 1.0, "every": int(m.group(1))})
    take(rf"When an enemy affected by (\w+) dies, it has a {_N}% chance to apply \w+ to enemies within",
         lambda m: {"k": "apply", "status": _status(m.group(1)), "src": "kill", "p": f(m, 2) / 100 * 2, "every": 1})
    take(rf"([A-Z][\w' ]+?) has an additional {_N}% chance to inflict (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(3)), "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1})
    take(rf"([A-Z][\w' ]+?) has \+{_N}% chance to apply (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(3)), "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1})
    take(rf"([A-Z][\w' ]+?)(?: hits)? have a {_N}% chance to (?:apply|inflict) (\w+(?: \w+)?)",
         lambda m: ({"k": "apply", "status": _status(m.group(3)), "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1}
                    if _status(m.group(3)) else None))
    take(rf"([A-Z][\w' ]+?) has a {_N}% chance to (?:apply|inflict) (\w+(?: \w+)?)",
         lambda m: ({"k": "apply", "status": _status(m.group(3)), "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1}
                    if _status(m.group(3)) else None))
    take(rf"([A-Z][\w' ]+?) have {_N}% to stun", lambda m: {"k": "apply", "status": "Stunned", "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1})
    take(rf"The third strike from ([A-Z][\w' ]+?) has a {_N}% chance to inflict (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(3)), "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 3})
    take(rf"([A-Z][\w' ]+?)'s shockwave has a {_N}% chance to stun",
         lambda m: {"k": "apply", "status": "Stunned", "src": target(m.group(1)), "p": f(m, 2) / 100, "every": 1})
    take(rf"([A-Z][\w' ]+?) hits have an? {_N}% chance to apply (\d+) stacks? of (\w+)",
         lambda m: {"k": "apply", "status": _status(m.group(4)), "src": target(m.group(1)), "p": f(m, 2) / 100 * int(m.group(3)), "every": 1})
    take(rf"Your Chill is {_N}% more potent, slowing enemies more and lasting longer",
         lambda m: {"k": "status_dur", "status": "Chill", "v": f(m, 1)})
    take(rf"(\w+) applied by you lasts {_N}% longer", lambda m: {"k": "status_dur", "status": _status(m.group(1)), "v": f(m, 2)})
    take(rf"\+{_N}% (Poisoned|Bleeding|Burn) Damage and \+([\d.]+)s Duration",
         lambda m: [{"k": "stat", "stat": "Damage Over Time", "v": f(m, 1) * 0.5},
                    {"k": "status_dur", "status": _status(m.group(2)), "v": f(m, 3) / 6 * 100}])
    # conditional damage
    take(rf"([A-Z][\w' ]+?) deals \+{_N}% damage to enemies affected by (\w+) and \+{_N}% damage per stack of (\w+)",
         lambda m: [{"k": "dmg", "target": target(m.group(1)), "v": f(m, 2), "cond": _status(m.group(3))},
                    {"k": "dmg", "target": target(m.group(1)), "v": f(m, 4), "per_stack": _status(m.group(5))}])
    take(rf"([A-Z][\w' ]+?) deals \+{_N}% [Dd]amage to enemies affected by (\w+)",
         lambda m: {"k": "dmg", "target": target(m.group(1)), "v": f(m, 2), "cond": _status(m.group(3))})
    take(rf"\+{_N}% Damage to (\w+) (?:targets|enemies)",
         lambda m: ({"k": "stat", "stat": "Damage vs Elite", "v": f(m, 1)} if m.group(2).lower() == "elite" else
                    {"k": "stat", "stat": "Damage vs Distant", "v": f(m, 1)} if m.group(2).lower() == "distant" else
                    {"k": "dmg", "target": "all", "v": f(m, 1), "cond": _status(m.group(2))} if _status(m.group(2)) else None))
    take(rf"\+{_N}% Critical Hit Chance against (\w+) enemies",
         lambda m: ({"k": "crit_chance", "target": "all", "v": f(m, 1), "cond": _status(m.group(2))} if _status(m.group(2))
                    else {"k": "crit_chance", "target": "all", "v": f(m, 1) * 0.5}))
    # ability damage and crits
    take(rf"\+{_N}% (?:Critical Hit Damage|Critical Damage) to ([A-Z][\w' ]+?)(?: [Aa]bilities)?\b(?=\.|$| and)",
         lambda m: {"k": "crit_dmg", "target": target(m.group(2)) or m.group(2), "v": f(m, 1)})
    take(rf"Gain \+{_N}% Critical Hit Damage to (\w+) Abilities",
         lambda m: {"k": "crit_dmg", "target": m.group(2), "v": f(m, 1)})
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Critical Hit Damage",
         lambda m: ({"k": "crit_dmg", "target": target(m.group(2)), "v": f(m, 1)} if target(m.group(2)) else None))
    take(rf"\+{_N}% ([A-Z][\w' ]+?) Critical Hit Chance",
         lambda m: ({"k": "crit_chance", "target": target(m.group(2)), "v": f(m, 1)} if target(m.group(2)) else None))
    take(rf"([A-Z][\w' ]+?) deals \+?{_N}% (?:more )?[Dd]amage",
         lambda m: ({"k": "dmg", "target": target(m.group(1)), "v": f(m, 2)} if target(m.group(1)) else None))
    take(rf"\+{_N}% (Fire|Cold|Lightning|Poison|Arcane|Physical) Damage Reduction",
         lambda m: {"k": "stat", "stat": f"{m.group(2)} Damage Reduction", "v": f(m, 1)})
    take(rf"\+{_N}% (Fire|Cold|Lightning|Poison|Arcane|Physical) Damage\b",
         lambda m: {"k": "stat", "stat": f"{m.group(2)} Damage", "v": f(m, 1)})
    take(rf"\+{_N}% ([A-Z][\w' ]+?) [Dd]amage\b",
         lambda m: ({"k": "dmg", "target": target(m.group(2)), "v": f(m, 1)} if target(m.group(2)) else None))
    take(rf"(Basic Attack|Strong Attack) damage \+{_N}%", lambda m: {"k": "dmg", "target": m.group(1), "v": f(m, 2)})
    # plain stats
    take(rf"[Tt]ake {_N}% less damage", lambda m: {"k": "stat", "stat": "Damage Reduction", "v": f(m, 1) * 0.5})
    take(rf"\+{_N} (Intelligence|Strength|Dexterity)\b", lambda m: {"k": "stat", "stat": m.group(2), "v": f(m, 1)})
    take(rf"\+{_N} Max HP", lambda m: {"k": "stat", "stat": "Max Health", "v": f(m, 1)})
    take(rf"\+{_N} Life Regen", lambda m: {"k": "stat", "stat": "Life Regeneration", "v": f(m, 1)})
    take(rf"\+{_N} Magic Resist", lambda m: {"k": "stat", "stat": "Magic Resist", "v": f(m, 1)})
    take(rf"(?:Gain {_N} Armor|\+{_N} Armor\b)", lambda m: {"k": "stat", "stat": "Armor", "v": float(next(g for g in m.groups() if g))})
    take(rf"(?:total Armor by {_N}%|\+{_N}% Armor|{_N}% increased Armor)",
         lambda m: {"k": "stat", "stat": "Bonus Armor", "v": float(next(g for g in m.groups() if g))})
    take(rf"\+{_N}% Magic Resist", lambda m: {"k": "mr_pct", "v": f(m, 1)})
    take(rf"\+{_N}% Max(?:imum)? Health", lambda m: {"k": "stat", "stat": "Max Health %", "v": f(m, 1)})
    take(rf"\+{_N} Thorns?\b", lambda m: {"k": "stat", "stat": "Thorns", "v": f(m, 1)})
    take(rf"\+{_N}% Critical Damage Reduction", lambda m: {"k": "stat", "stat": "Critical Damage Reduction", "v": f(m, 1)})
    take(rf"\+{_N}% Critical Hit Chance", lambda m: {"k": "stat", "stat": "Critical Hit Chance", "v": f(m, 1)})
    take(rf"\+{_N}% Critical Hit Damage", lambda m: {"k": "stat", "stat": "Critical Hit Damage", "v": f(m, 1)})
    take(rf"\+{_N}% Attack Speed", lambda m: {"k": "stat", "stat": "Attack Speed Bonus", "v": f(m, 1)})
    take(rf"\+{_N}% Cooldown Reduction", lambda m: {"k": "stat", "stat": "Cooldown Reduction", "v": f(m, 1)})
    take(rf"\+{_N}% Dodge Chance", lambda m: {"k": "stat", "stat": "Dodge Chance", "v": f(m, 1)})
    take(rf"\+{_N} Max Mana", lambda m: {"k": "stat", "stat": "Max Mana", "v": f(m, 1)})
    take(rf"\+{_N} Mana Regen", lambda m: {"k": "stat", "stat": "Mana Regeneration", "v": f(m, 1)})
    take(rf"\+{_N}% (?:Move|Movement) Speed", lambda m: {"k": "stat", "stat": "Bonus Move Speed", "v": f(m, 1)})
    take(rf"\+{_N}% Damage\b", lambda m: {"k": "stat", "stat": "All Damage", "v": f(m, 1)})
    # what cannot be rated
    for pat, why in ((r"Range", "range"), (r"knock", "knockback"), (r"size|radius|length and width|splash radius",
                                                                       "area size"),
                     (r"Movement Speed while channeling", "movement")):
        if not out and re.search(pat, t, re.I):
            out.append({"k": "none", "why": f"{why} – not part of the damage model"})
    return [e for e in out if e.get("target", "x") is not None and e.get("status", "x") is not None
            and e.get("src", "x") is not None and e.get("ability", "x") is not None]


_PARSED = {}


def rank_effects(t: dict, hero: str) -> list:
    """Effects of ONE rank: the max rank text divided by the ranks (texts grow linearly)."""
    key = (hero, talents.key(t))
    if key not in _PARSED:
        fx = parse(t.get("rank_max") or t.get("rank1", ""), hero)
        n = max(int(t.get("ranks") or 1), 1)
        out = []
        for e in fx:
            e = dict(e)
            if "v" in e:
                e["v"] = e["v"] / n
            out.append(e)
        _PARSED[key] = out
    return _PARSED[key]


def rated(t: dict, hero: str) -> bool:
    return any(e["k"] != "none" for e in rank_effects(t, hero))


def why_not(t: dict, hero: str) -> str:
    fx = rank_effects(t, hero)
    reasons = [e["why"] for e in fx if e["k"] == "none"]
    return reasons[0] if reasons else "effect not recognised"


def build_effects(build: dict, hero: str) -> list:
    out = []
    for t in talents.tree(hero):
        r = build.get(talents.key(t), 0)
        if not r:
            continue
        for e in rank_effects(t, hero):
            e = dict(e)
            if "v" in e:
                e["v"] = e["v"] * r
            e["talent"] = t["name"]
            out.append(e)
    return out


# ----------------------------------------------------------------------------- the character's facts

class Facts:
    """Everything one evaluation needs that does not depend on the build."""

    def __init__(self, hero: str, ctx, prof: dict, shares: dict, slots: list, sim=None, kills_per_s: float = 0.8):
        self.hero, self.ctx = hero, ctx
        self.char = dict(ctx.char)
        self.abil = hero_abilities(hero)
        self.sim, self.kills = sim, max(kills_per_s or 0.0, 0.05)
        # used abilities: measured (skill tracking) or the shares set by hand
        self.measured = bool(prof.get("ok"))
        self.use = {}
        if self.measured:
            for n, x in prof["abilities"].items():
                if n in self.abil and x.get("share", 0) > 0:
                    self.use[n] = {"share": x["share"], "cps": x.get("cps") or 0.0}
        if not self.use:
            tot = sum(shares.values()) or 1.0
            for n, sh in shares.items():
                if n in self.abil and sh > 0:
                    self.use[n] = {"share": sh / tot, "cps": 0.0}
        self.slots = [n for n in slots if n in self.abil] or list(self.use)
        # casts per second: measured, else from the simulation
        if sim is not None and sim.ok():
            base = sim.run(self._sim_stats({}), self.kills)
            for n, x in self.use.items():
                if not x["cps"]:
                    x["cps"] = base.get(n, 0.0)
        for n, x in self.use.items():
            if not x["cps"]:
                a = self.abil[n]
                cd = _seconds(a.get("cooldown"))
                x["cps"] = (1 / cd) if cd else float(self.char.get("Attack Speed", 1.0) or 1.0)
        # the simulation fires Basic Attacks at full attack speed; skill tracking may count fewer: mana per
        # cast of an ability counts with the measured share of its simulated casts
        self.sim_base = sim.run(self._sim_stats({}), self.kills) if sim is not None and sim.ok() else {}
        self.wd_total = sum(x["cps"] * weight(self.abil[n]) for n, x in self.use.items()) or 1.0
        self.ok = bool(self.use)
        self._cache = {}
        self.elem_share = {}
        for n, x in self.use.items():
            tags = [("Cold" if t == "Frost" else t) for t in self.abil[n].get("tags", [])]
            el = next((t for t in tags if t in item_eval.ELEMENTS), None)
            if el:
                self.elem_share[el] = self.elem_share.get(el, 0.0) + x["share"]
        # statuses that talents give "Damage vs" for, and the "Damage vs" sum of the sheet now (one sum in the
        # damage formula: a talent's bonus adds to it)
        self.talent_conds = sorted({e["cond"] for t in talents.tree(hero) for e in rank_effects(t, hero)
                                    if e["k"] == "dmg" and e.get("cond") and (e.get("target") or "").lower() == "all"})
        ups = {**item_eval.UPTIME, **item_eval.COND_DEFAULTS, **(ctx.conditions or {})}
        self.vs_sum = sum(u * float(self.char.get(k, 0.0)) for k, u in ups.items()) / 100

    def cast_scale(self, name: str) -> float:
        """Measured casts / simulated casts of an ability (1 without measurements)."""
        sim_c = self.sim_base.get(name, 0.0)
        if not self.measured or name not in self.use or sim_c <= 0:
            return 1.0
        return min(self.use[name]["cps"] / sim_c, 1.0)

    def _sim_stats(self, delta: dict) -> dict:
        c = self.char
        st = {k: float(c.get(k, 0.0)) + float(delta.get(k, 0.0)) for k in
              ("Cooldown Reduction", "Mana Cost Reduction", "Max Mana", "Mana Regeneration", "Mana on Kill",
               "Attack Speed Bonus")}
        st["Max Mana"] = st["Max Mana"] or 100.0
        aps0 = float(c.get("Attack Speed", 1.0) or 1.0)
        st["Attack Speed"] = aps0 * (1 + st["Attack Speed Bonus"] / 100) / (1 + float(c.get("Attack Speed Bonus", 0.0)) / 100)
        return st


# ----------------------------------------------------------------------------- one build

class Model:
    """Numbers of one build (absolute, so two builds can be divided)."""

    def __init__(self, facts: Facts, build: dict):
        self.F = F = facts
        self.fx = build_effects(build, F.hero)
        self.notes = []
        self.stats = {}
        for e in self.fx:
            if e["k"] == "stat":
                self.stats[e["stat"]] = self.stats.get(e["stat"], 0.0) + e["v"]
        self._casts()
        self._statuses()

    # --- casts
    def _targets(self, spec):
        return [n for n, a in self.F.abil.items() if match(spec, a)]

    def _casts(self):
        F = self.F
        mods = {"cd": {}, "mana": {}, "gain": {}, "free": []}
        for e in self.fx:
            if e["k"] == "cd":
                for n in self._targets(e["target"]):
                    mods["cd"][n] = mods["cd"].get(n, 1.0) * max(1 - e["v"] / 100, 0.05)
            elif e["k"] == "mana_cost":
                for n in self._targets(e["target"]):
                    mods["mana"][n] = mods["mana"].get(n, 1.0) * max(1 - e["v"] / 100, 0.0)
            elif e["k"] == "mana_cast":
                for n in self._targets(e["target"]):
                    mods["gain"][n] = mods["gain"].get(n, 0.0) + e["v"] * F.cast_scale(n)
            elif e["k"] == "mana_cast_pct":
                for n in self._targets(e["target"]):
                    mods["gain"][n] = mods["gain"].get(n, 0.0) + e["v"] / 100 * (F.char.get("Max Mana", 100) or 100)
            elif e["k"] == "mana_hit":
                mm = F.char.get("Max Mana", 100) or 100
                for n in self._targets(e["target"]):
                    a = F.abil[n]
                    mods["gain"][n] = mods["gain"].get(n, 0.0) + e["v"] / 100 * mm * skills.targets(a) * bolts(a) * 0.6                         * F.cast_scale(n)
            elif e["k"] == "free_cast":
                for n in self._targets(e["src"]):
                    mods["free"].append((n, e["dst"], e["v"] / 100))
        self.mods = mods
        stats = F._sim_stats(self.stats)
        if F.sim is not None and F.sim.ok():
            self.cps_sim = F.sim.run(stats, F.kills, mods)
        else:
            self.cps_sim = None
        # flat cooldown cuts ("Casting a Strong Attack reduces all Trap cooldowns by 2s")
        self.cd_flat = [e for e in self.fx if e["k"] == "cd_flat"]

    def cps(self, name):
        """Casts per second in this build, from the measured rate scaled by the simulation."""
        return self._cps_scaled.get(name, 0.0)

    # --- statuses
    def _statuses(self):
        F = self.F
        import build_profile
        sts = build_profile.STATUSES
        dur_mult = {}
        for e in self.fx:
            if e["k"] == "status_dur":
                dur_mult[e["status"]] = dur_mult.get(e["status"], 1.0) + e["v"] / 100
        self._cps_scaled = {n: F.use[n]["cps"] for n in F.use}
        self.dur = {n: (st.get("duration_s") or 0) * dur_mult.get(n, 1.0) for n, st in sts.items()}
        self.rates = {}
        self.extra_cps = {}

    def _ability_rates(self):
        """How often your abilities put each status on an enemy (per enemy and second)."""
        import build_profile
        F = self.F
        for st_name, st in build_profile.STATUSES.items():
            r = 0.0
            for src in st.get("applied_by", []):
                if "ability" in src["kind"] and (src["name"] in F.use or src["name"] in self.extra_cps):
                    a = F.abil.get(src["name"])
                    if not a:
                        continue
                    p = 1.0
                    m = re.search(rf"(\d+)% chance to (?:inflict|apply) {re.escape(st_name)}", a.get("description", ""), re.I)
                    if m:
                        p = float(m.group(1)) / 100
                    r += self.cps(src["name"]) * coverage(a) * p * (bolts(a) if p < 1 else 1)
            self.rates[st_name] = r

    def finish(self, cur: "Model | None"):
        """Casts relative to the current build, then the statuses the talents add."""
        F = self.F
        if self.cps_sim is not None and cur is not None and cur.cps_sim is not None:
            for n in set(F.use) | set(self.cps_sim):
                c0, c1 = cur.cps_sim.get(n, 0.0), self.cps_sim.get(n, 0.0)
                if n in F.use and c0 > 0:
                    self._cps_scaled[n] = F.use[n]["cps"] * c1 / c0
                elif n not in F.use and c1 > 0:  # cast for free, not on the bar
                    self._cps_scaled[n] = c1
                    self.extra_cps[n] = c1
        elif cur is not None:  # no simulation: cooldown talents by their direct effect
            for n in F.use:
                f0 = cur.mods["cd"].get(n, 1.0)
                f1 = self.mods["cd"].get(n, 1.0)
                if F.abil[n].get("slot") != "Basic Attack":
                    self._cps_scaled[n] = F.use[n]["cps"] * f0 / f1
        for e in self.cd_flat:  # flat cooldown cuts per trigger cast
            trig = sum(self._cps_scaled.get(n, 0.0) for n in self._targets(e["trigger"]))
            for n in self._targets(e["target"]):
                if n in F.use and not match(e["trigger"], F.abil[n]):
                    cd = _seconds(F.abil[n].get("cooldown"))
                    if cd:
                        cut = min(trig * e["v"], 0.6)  # seconds of cooldown removed per second
                        self._cps_scaled[n] = self._cps_scaled.get(n, 0.0) / max(1 - cut, 0.4)
        # statuses: from the abilities, then from talents, now that casts are known
        self._ability_rates()
        for e in self.fx:
            if e["k"] != "apply" or not e.get("status"):
                continue
            src, st = e["src"], e["status"]
            if src.startswith("crit:"):
                chc = min(F.char.get("Critical Hit Chance", 0.0) + self.stats.get("Critical Hit Chance", 0.0),
                          item_eval.CAP) / 100
                r = sum(self.cps(n) * coverage(F.abil[n]) * bolts(F.abil[n]) for n in F.use
                        if match(src[5:], F.abil[n])) * chc
            elif src.startswith("status:"):
                r = self.rates.get(src[7:], 0.0)
            elif src == "kill":
                r = F.kills * self.uptime(st) / max(PROC_TARGETS, 1)
            else:
                r = sum(self.cps(n) * coverage(F.abil[n]) for n in F.use if match(src, F.abil[n])) / max(e["every"], 1)
            self.rates[st] = self.rates.get(st, 0.0) + r * e["p"]
        return self

    def _raw_uptime(self, status: str) -> float:
        r, d = self.rates.get(status, 0.0), self.dur.get(status, 0.0)
        return 1 - math.exp(-r * d) if r > 0 and d > 0 else 0.0

    def uptime(self, status: str) -> float:
        """Share of the time an enemy carries a status (or a condition several statuses cause)."""
        if status in COND_OF:
            miss = 1.0
            for s in COND_OF[status]:
                miss *= 1 - self._raw_uptime(s)
            return 1 - miss
        return self._raw_uptime(status)

    def stacks(self, status: str, cap: int = 5) -> float:
        return min(self.rates.get(status, 0.0) * self.dur.get(status, 0.0), cap)

    def conditions(self) -> dict:
        out = {k: self.uptime(v) for k, v in ITEM_CONDITIONS.items()}
        for e in self.F.talent_conds:  # "Damage vs <status>" of talents
            out[f"Damage vs {e}"] = self.uptime(e)
        return out

    # --- damage
    def ability_mult(self, name: str) -> float:
        F = self.F
        a = F.abil[name]
        dmg, cc, cd = 0.0, 0.0, 0.0
        vs_extra = 0.0
        for e in self.fx:
            if e["k"] == "dmg" and e.get("cond") and (e["target"] or "").lower() == "all":
                continue  # in the "Damage vs" sum of the damage formula (sheet())
            if e["k"] == "dmg" and e.get("cond") and match(e["target"], a):
                vs_extra += e["v"] * self.uptime(e["cond"])  # same sum, only for this ability
                continue
            if e["k"] == "dmg" and (e["target"] or "").lower() == "all":
                continue  # All Damage (sheet())
            if e["k"] in ("dmg", "crit_chance", "crit_dmg") and match(e["target"], a):
                v = e["v"]
                if e.get("cond"):
                    v *= self.uptime(e["cond"])
                if e.get("per_stack"):
                    v *= self.stacks(e["per_stack"])
                if e["k"] == "dmg":
                    dmg += v
                elif e["k"] == "crit_chance":
                    cc += v
                else:
                    cd += v
            elif e["k"] == "buff" and e["give"]["k"] == "dmg" and (e["give"]["target"] or "").lower() != "all"                     and match(e["give"]["target"], a):
                dmg += e["v"] * self.buff_level(e)
            elif e["k"] == "charge" and match(e["target"], a):
                basic = sum(self.cps(n) for n in F.use if F.abil[n].get("slot") == "Basic Attack")
                strong = sum(self.cps(n) for n in F.use if F.abil[n].get("slot") == "Strong Attack") or 1e-9
                dmg += e["v"] * min(basic / e["every"] / strong, 1.0)
        chc = min(F.char.get("Critical Hit Chance", 0.0), item_eval.CAP) / 100
        chd = F.char.get("Critical Hit Damage", 0.0) / 100
        crit = (1 + min(chc + cc / 100, item_eval.CAP / 100) * (chd + cd / 100)) / (1 + chc * chd)
        f = (1 + dmg / 100) * crit
        if vs_extra:
            S = F.vs_sum
            f *= (1 + S + vs_extra / 100) / (1 + S)
        if "lightning" in _tags(a):
            f *= 1 + ELECTROSTATIC_PER_STACK * self.stacks("Electrostatic") / 100
        return f

    def buff_level(self, e) -> float:
        """Uptime (or average stacks) of a buff."""
        F = self.F
        trig = e["trigger"] or ""
        if trig.startswith("kill:"):
            rate = F.kills * self.uptime(trig[5:])
        else:
            ns = [n for n in F.use if match(trig, F.abil[n])]
            rate = sum(self.cps(n) * (skills.targets(F.abil[n]) * bolts(F.abil[n]) if e["per"] == "hit" else 1)
                       for n in ns) / max(e["every"], 1)
        if e["stacks"] > 1:
            return min(rate * e["dur"], e["stacks"])
        return 1 - math.exp(-rate * e["dur"]) if rate > 0 else 0.0

    def proc_wd(self) -> float:
        """Extra weapon damage % per second from procs."""
        F = self.F
        out = 0.0
        for e in self.fx:
            if e["k"] != "proc":
                continue
            if e["src"] == "kill":
                rate = F.kills * (self.uptime(e["cond"]) if e.get("cond") else 1.0)
            elif e["per"] == "electro5":
                rate = self.rates.get("Electrostatic", 0.0) / 5
            else:
                ns = [n for n in list(F.use) + list(self.extra_cps) if n in F.abil and match(e["src"], F.abil[n])]
                rate = sum(self.cps(n) * (skills.targets(F.abil[n]) * bolts(F.abil[n]) if e["per"] == "hit" else 1)
                           for n in ns) / max(e["every"], 1)
            out += rate * e["p"] * e["wd"] * e["targets"]
        for n, c in self.extra_cps.items():  # abilities cast for free that are not on the bar
            out += c * weight(F.abil[n])
        return out

    def sheet(self) -> dict:
        """Character sheet numbers of this build (relative to the sheet, which holds the current build)."""
        F = self.F
        d = dict(self.stats)
        mm = F.char.get("Max Mana", 0.0) + d.get("Max Mana", 0.0)
        for e in self.fx:
            k = e["k"]
            if k == "dmg" and e.get("cond") and (e["target"] or "").lower() == "all":
                d[f"Damage vs {e['cond']}"] = d.get(f"Damage vs {e['cond']}", 0.0) + e["v"]
            elif k == "mana_to_int":
                d["Intelligence"] = d.get("Intelligence", 0.0) + mm * e["v"] / 100
            elif k == "mr_pct":
                d["Magic Resist"] = d.get("Magic Resist", 0.0) + F.char.get("Magic Resist", 0.0) * e["v"] / 100
            elif k == "hp_to_thorns":
                d["Thorns"] = d.get("Thorns", 0.0) + F.char.get("Max Health", 0.0) * e["v"] / 100
            elif k == "dodge_to_cd":
                d["Critical Hit Damage"] = d.get("Critical Hit Damage", 0.0) + F.char.get("Dodge Chance", 0.0) * e["v"] / 100
            elif k == "while":
                a = F.abil.get(e["ability"])
                up = 0.0
                if a and e["ability"] in F.use:
                    du, cd = duration(a), _seconds(a.get("cooldown"))
                    up = min(du / cd, 1.0) if du and cd else 0.0
                if e["stat"] == "Magic Resist %":
                    d["Magic Resist"] = d.get("Magic Resist", 0.0) + F.char.get("Magic Resist", 0.0) * e["v"] / 100 * up
                else:
                    d[e["stat"]] = d.get(e["stat"], 0.0) + e["v"] * up
            elif k == "heal_during":
                a = F.abil.get(e["ability"])
                if a and e["ability"] in F.use:
                    du, cd = duration(a) or 4.0, _seconds(a.get("cooldown")) or 30.0
                    d["Life Regeneration"] = d.get("Life Regeneration", 0.0) + \
                        F.char.get("Max Health", 0.0) * e["v"] / 100 * du / cd
            elif k == "heal_every":
                d["Life Regeneration"] = d.get("Life Regeneration", 0.0) + \
                    F.char.get("Max Health", 0.0) * e["v"] / 100 / e["every"] * e["share"]
            elif k == "heal_cast":
                casts = sum(self.cps(n) for n in F.use if match(e["target"], F.abil[n])) / e["every"]
                d["Life Regeneration"] = d.get("Life Regeneration", 0.0) + casts * F.char.get("Max Health", 0.0) * e["v"] / 100
            elif k == "heal_per_mana":
                spent = sum(self.cps(n) * float(F.abil[n].get("mana") or 0) for n in F.use)
                d["Life Regeneration"] = d.get("Life Regeneration", 0.0) + spent * e["v"]
            elif k == "shield_hit":
                hits = sum(self.cps(n) * skills.targets(F.abil[n]) for n in F.use if match(e["target"], F.abil[n]))
                d["Life Regeneration"] = d.get("Life Regeneration", 0.0) + \
                    min(hits * F.char.get("Max Health", 0.0) * e["v"] / 100, F.char.get("Max Health", 0.0) * 0.1) * 0.3
            elif k == "buff" and e["give"]["k"] == "stat":
                st = e["give"]["stat"]
                d[st] = d.get(st, 0.0) + e["v"] * self.buff_level(e)
            elif k == "buff" and e["give"]["k"] == "dmg" and (e["give"]["target"] or "").lower() == "all":
                # "+30% to all damage" adds to All Damage (one sum with the element bonus)
                d["All Damage"] = d.get("All Damage", 0.0) + e["v"] * self.buff_level(e)
            elif k == "dmg" and not e.get("cond") and (e.get("target") or "").lower() == "all":
                d["All Damage"] = d.get("All Damage", 0.0) + e["v"]
            elif k == "mana_dodge":
                pass
        return d


def _ctx_with(ctx, conditions: dict):
    import copy
    c = copy.copy(ctx)
    c.conditions = {**(ctx.conditions or {}), **conditions}
    c.sim = ctx.sim if ctx.sim is not None else object()  # attack speed works through the casts here
    return c


def evaluate(facts: Facts, build: dict, current: dict, mode: str, cur_model: Model | None = None):
    """(damage %, survival %, income %, score, Model) of build against the current build."""
    F = facts
    key = (tuple(sorted(build.items())), mode)
    if key in F._cache:
        return F._cache[key]
    cur = cur_model or Model(F, current).finish(None)
    new = Model(F, build).finish(cur)
    # damage: abilities x their talent bonuses x casts, global factor from the damage formula
    tot = 0.0
    for n, x in F.use.items():
        c0 = cur.cps(n) or x["cps"]
        tot += x["share"] * (new.cps(n) / c0 if c0 else 1.0) * new.ability_mult(n) / cur.ability_mult(n)
    tot += (new.proc_wd() - cur.proc_wd()) / F.wd_total
    s_cur, s_new = cur.sheet(), new.sheet()
    char = F.char
    v0 = dict(char)
    v1 = dict(char)
    for k in set(s_cur) | set(s_new):
        v1[k] = v1.get(k, 0.0) + s_new.get(k, 0.0) - s_cur.get(k, 0.0)
    # element bonuses count with the share of your damage of that element (the formula takes one element)
    if F.elem_share:
        main = F.ctx.elem
        el_tot = sum(F.elem_share.values()) or 1.0
        gain = 0.0
        for e in item_eval.ELEMENTS:
            k = f"{e} Damage"
            de = v1.get(k, 0.0) - v0.get(k, 0.0)
            if de:
                gain += de * F.elem_share.get(e, 0.0) / el_tot
                v1[k] = v0.get(k, 0.0)
        v1[f"{main} Damage"] = v0.get(f"{main} Damage", 0.0) + gain
    g0 = item_eval.dps_factor(v0, _ctx_with(F.ctx, cur.conditions()))
    g1 = item_eval.dps_factor(v1, _ctx_with(F.ctx, new.conditions()))
    vul = (1 + VULNERABLE_PCT / 100 * new.uptime("Vulnerable")) / (1 + VULNERABLE_PCT / 100 * cur.uptime("Vulnerable"))
    dps = (tot * g1 / g0 * vul - 1) * 100
    # survival: effective health incl. healing
    d0 = item_eval.defense(v0, char, F.ctx)
    d1 = item_eval.defense(v1, char, F.ctx)
    surv = (d1["ehp"] / d0["ehp"] - 1) * 100 if d0["ehp"] > 0 else 0.0
    # income: movement speed
    farm = (v1.get("Bonus Move Speed", 0.0) - v0.get("Bonus Move Speed", 0.0)) * \
        item_eval.OTHER_DEFAULTS["Bonus Move Speed"][1]
    w = item_eval.MODES.get(mode, item_eval.MODES["Damage"])
    score = w["dps"] * dps + w["surv"] * surv + w["farm"] * farm
    res = (dps, surv, farm, score, new)
    F._cache[key] = res
    return res


# ----------------------------------------------------------------------------- the best build

BUNDLE = 0.01   # % of score per talent used (tie-break only)
FILLER = 0.5    # % of score: a talent worth less than this only opens rows


def _caps(hero):
    rows = {}
    for t in talents.tree(hero):
        if t.get("capstone"):
            rows.setdefault(t["points"], []).append(t)
    return rows


def best_build(facts: Facts, points: int, current: dict, mode: str, progress=None) -> dict:
    """Best build for the points: every combination of capstones, each filled greedily point by point
    (a talent only counts once its row is open), then single points moved while that helps."""
    import itertools
    hero = facts.hero
    tree = talents.tree(hero)
    cur_model = Model(facts, current).finish(None)
    # a hair less per talent used: equal value -> points bundled in fewer, fuller talents
    score = lambda b: evaluate(facts, b, current, mode, cur_model)[3] - BUNDLE * sum(1 for v in b.values() if v)
    cap_rows = _caps(hero)
    rows_sorted = sorted(cap_rows)
    # capstones that are worth something on their own or open synergies (statuses): candidates per row
    options = [[None] + cap_rows[r] for r in rows_sorted]
    combos = list(itertools.product(*options))
    best, best_s = {}, None
    normal = [t for t in tree if not t.get("capstone")]
    for i, combo in enumerate(combos):
        if progress:
            progress(i / len(combos))
        want = [c for c in combo if c is not None]
        b = {}
        s = score(b)
        for _ in range(points):
            used = sum(b.values())
            if used >= points:
                break
            # a wanted capstone whose row is open goes in first
            cap = next((c for c in want if not b.get(talents.key(c)) and talents.can_add(b, c, hero)), None)
            if cap is not None:
                b[talents.key(cap)] = 1
                s = score(b)
                continue
            pick, pick_s = None, None
            for t in normal:
                if not talents.can_add(b, t, hero):
                    continue
                b2 = dict(b)
                b2[talents.key(t)] = b2.get(talents.key(t), 0) + 1
                s2 = score(b2) - t["points"] * 1e-6  # no value: cheap rows first (they open the next rows)
                if pick_s is None or s2 > pick_s:
                    pick, pick_s = t, s2
            if pick is None:
                break
            b[talents.key(pick)] = b.get(talents.key(pick), 0) + 1
            s = pick_s
        if not all(b.get(talents.key(c)) for c in want):
            continue  # a wanted capstone's row never opened
        if best_s is None or s > best_s:
            best, best_s = b, s
    # move single points while it helps
    build, sc = dict(best), score(best)
    for _ in range(60):
        improved = False
        for a in tree:
            if not build.get(talents.key(a)):
                continue
            b1 = dict(build)
            b1[talents.key(a)] -= 1
            if not b1[talents.key(a)]:
                del b1[talents.key(a)]
            if not talents.valid(b1, hero):
                continue
            for t in tree:
                if t is a or not talents.can_add(b1, t, hero):
                    continue
                b2 = dict(b1)
                b2[talents.key(t)] = b2.get(talents.key(t), 0) + 1
                s2 = score(b2)
                if s2 > sc + 1e-6:
                    build, sc, improved = b2, s2, True
                    break
            if improved:
                break
        if not improved:
            break
    if progress:
        progress(1.0)
    return build


def talent_values(facts: Facts, build: dict, current: dict, mode: str) -> list:
    """Per talent of the build: what removing all its points would cost (its share of the build's value)."""
    hero = facts.hero
    cur_model = Model(facts, current).finish(None)
    full = evaluate(facts, build, current, mode, cur_model)
    out = []
    for t in talents.tree(hero):
        r = build.get(talents.key(t), 0)
        if not r:
            continue
        b = dict(build)
        del b[talents.key(t)]
        v = evaluate(facts, b, current, mode, cur_model)
        out.append((t, r, full[0] - v[0], full[1] - v[1], full[3] - v[3]))
    out.sort(key=lambda x: -x[4])
    return out


# ----------------------------------------------------------------------------- synergies

def provides(t: dict, hero: str) -> set:
    """Statuses a talent puts on enemies."""
    return {e["status"] for e in rank_effects(t, hero) if e["k"] == "apply" and e.get("status")}


def needs(t: dict, hero: str) -> set:
    """Statuses a talent's bonus depends on."""
    out = set()
    for e in rank_effects(t, hero):
        for k in ("cond", "per_stack"):
            if e.get(k):
                out.add(e[k])
        if e["k"] == "apply" and str(e.get("src", "")).startswith("status:"):
            out.add(e["src"][7:])
        if e["k"] == "buff" and str(e.get("trigger", "")).startswith("kill:"):
            out.add(e["trigger"][5:])
    return out


def status_sources(F: Facts, m: Model, status: str) -> list:
    """Who puts a status on enemies in this build: your abilities and talents."""
    import build_profile
    subs = COND_OF.get(status, [status])
    out = []
    for st in subs:
        for src in build_profile.STATUSES.get(st, {}).get("applied_by", []):
            if "ability" in src["kind"] and (src["name"] in F.use or src["name"] in m.extra_cps):
                out.append(src["name"])
    for e in m.fx:
        if e["k"] == "apply" and e.get("status") in subs:
            out.append(e["talent"])
    return list(dict.fromkeys(out))


def explain(F: Facts, build: dict, current: dict, mode: str) -> dict:
    """Why a build fits together: {"packages": [text], "roles": {talent key: role}, "warnings": [text]}.
    Roles: "synergy" (gives or uses a status others use), "core" (damage), "survival", "filler" (only opens
    rows), "unused" (needs a status nobody applies)."""
    hero = F.hero
    cur = Model(F, current).finish(None)
    m = Model(F, build).finish(cur)
    vals = {talents.key(t): (d, sv, sc) for t, r, d, sv, sc in talent_values(F, build, current, mode)}
    tree = {talents.key(t): t for t in talents.tree(hero)}
    inb = [tree[k] for k, v in build.items() if v and k in tree]
    roles, packages, warnings = {}, [], []
    statuses = sorted({s for t in inb for s in needs(t, hero)} | {s for t in inb for s in provides(t, hero)})
    for st in statuses:
        users = [t for t in inb if st in needs(t, hero)]
        givers = [t for t in inb if provides(t, hero) & set(COND_OF.get(st, [st]))]
        if not users:
            continue
        srcs = status_sources(F, m, st)
        up = m.uptime(st)
        if st == "Electrostatic":
            level = f"{m.stacks(st):.1f} of 5 stacks on average"
        else:
            level = f"on enemies {up * 100:.0f} % of the time"
        if not srcs or up < 0.03:
            for t in users:
                other = [x for x in needs(t, hero) - {st}
                         if status_sources(F, m, x) and m.uptime(x) >= 0.03]
                if other:
                    warnings.append(f"{t['name']}: the {st} part of its bonus is idle – nothing in this build "
                                    f"applies {st} (its {', '.join(other)} part works).")
                else:
                    roles[talents.key(t)] = "unused"
                    warnings.append(f"{t['name']} needs {st}, but nothing in this build applies it.")
            continue
        for t in users + givers:
            roles[talents.key(t)] = "synergy"
        packages.append(f"{st}: {' + '.join(srcs)} → {level} → used by {', '.join(t['name'] for t in users)}")
    for t in inb:
        k = talents.key(t)
        if k in roles:
            continue
        d, sv, sc = vals.get(k, (0, 0, 0))
        if abs(sc) < FILLER and abs(d) < FILLER and abs(sv) < FILLER:
            roles[k] = "filler"
        elif sv > d:
            roles[k] = "survival"
        else:
            roles[k] = "core"
    for t in inb:  # a status talent whose status no other talent or item bonus uses
        k = talents.key(t)
        if provides(t, hero) and roles.get(k) != "synergy":
            p = provides(t, hero)
            own = any(e["k"] == "apply" for e in rank_effects(t, hero))
            if own and vals.get(k, (0, 0, 0))[2] < FILLER:
                roles[k] = "filler"
    return {"packages": packages, "roles": roles, "warnings": warnings, "model": m,
            "elements": set(F.elem_share)}

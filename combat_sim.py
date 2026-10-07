"""How often the hero casts each slotted ability, following the game's auto-combat rules
(wiki "Hero Auto-Combat"):

  - every frame the slots are tried 4, 3, 2, 1 (Specials, Strong Attack, Basic Attack); the first one that is
    ready is cast, one cast per frame,
  - Strong Attack / Special: needs its slot timer and the global cooldown at 0 and its mana (after Mana Cost
    Reduction); afterwards the slot waits Cooldown x (1 - CDR) and the global cooldown 0.75 s x (1 - CDR) starts
    (CDR capped at 85 %),
  - Basic Attack: waits 1 / attacks per second, no global cooldown, no mana,
  - a channelled ability keeps the hero busy for its channel time,
  - mana: Mana Regeneration per second and Mana on Kill per kill, up to Max Mana.

The kills per second are not known; calibrate() picks them so that the simulated casts match the casts the
skill tracking counted. Damage per second follows as casts x weapon damage % (skills.damage_weight), so the
value of Cooldown Reduction, Mana Cost Reduction, Mana on Kill, Mana Regeneration, Max Mana and Attack Speed
comes from more or fewer casts of the abilities you actually use.
"""
import re

import skills

DT = 0.02          # simulation step, s
SECONDS = 90.0     # simulated fight time
GCD = 0.75
CAP = 0.85
STATS = ("Attack Speed", "Attack Speed Bonus", "Cooldown Reduction", "Mana Cost Reduction", "Max Mana",
         "Mana Regeneration", "Mana on Kill")


def _seconds(v) -> float | None:
    m = re.search(r"([\d.]+)\s*s", str(v or ""))
    return float(m.group(1)) if m else None


class CombatSim:
    def __init__(self, slot_names: list, hero: str = ""):
        """slot_names: abilities in slot order 1..4 (Basic, Strong, Special, Special) as on the skill bar."""
        info = {a["name"]: a for a in skills.ABILITIES if not hero or a.get("hero") == hero}
        self.slots = []
        for name in slot_names:
            a = info.get(name)
            if not a:
                continue
            desc = a.get("description", "")
            ch = re.search(r"Channel[^.]*?for (\d+(?:\.\d+)?) seconds", desc, re.I)
            self.slots.append({
                "name": name, "basic": a.get("slot") == "Basic Attack",
                "cooldown": _seconds(a.get("cooldown")) or 0.0, "mana": float(a.get("mana") or 0),
                "channel": float(ch.group(1)) if ch else 0.0, "weight": skills.damage_weight(a)})
        self._cache = {}

    def ok(self) -> bool:
        return any(not s["basic"] for s in self.slots)

    def run(self, stats: dict, kills_per_s: float, mods: dict | None = None) -> dict:
        """Casts per second of every slotted ability (plus abilities cast for free by talents).

        mods (talents): {"cd": {ability: factor}, "mana": {ability: factor}, "gain": {ability: mana per cast},
        "free": [(source, ability, chance)]} - cooldown and mana cost factors, mana restored per cast, and
        abilities cast for free (no mana, no cooldown, no time) with a chance per cast of the source."""
        mods = mods or {}
        mkey = tuple((k, tuple(sorted((str(a), round(float(b), 4)) for a, b in mods[k].items())))
                     for k in ("cd", "mana", "gain") if mods.get(k)) +             tuple(sorted((a, b, round(float(c), 4)) for a, b, c in mods.get("free", [])))
        key = (tuple(round(float(stats.get(k, 0.0)), 3) for k in STATS), round(kills_per_s, 3), mkey)
        if key in self._cache:
            return self._cache[key]
        cd_f = [float(mods.get("cd", {}).get(s["name"], 1.0)) for s in self.slots]
        mana_f = [float(mods.get("mana", {}).get(s["name"], 1.0)) for s in self.slots]
        gain = [float(mods.get("gain", {}).get(s["name"], 0.0)) for s in self.slots]
        free = [[(dst, float(p)) for src, dst, p in mods.get("free", []) if src == s["name"]] for s in self.slots]
        extra, acc = {}, {}
        aps = max(float(stats.get("Attack Speed", 1.0)), 0.05)
        cdr = min(max(float(stats.get("Cooldown Reduction", 0.0)), 0.0) / 100, CAP)
        mcr = min(max(float(stats.get("Mana Cost Reduction", 0.0)), 0.0) / 100, CAP)
        max_mana = max(float(stats.get("Max Mana", 100.0)), 1.0)
        regen = float(stats.get("Mana Regeneration", 0.0))
        mok = float(stats.get("Mana on Kill", 0.0))
        order = list(reversed(range(len(self.slots))))  # slot 4 first
        timers = [0.0] * len(self.slots)
        casts = [0] * len(self.slots)
        mana, gcd, busy, kill_acc, t = max_mana, 0.0, 0.0, 0.0, 0.0
        while t < SECONDS:
            mana = min(max_mana, mana + regen * DT)
            kill_acc += kills_per_s * DT
            while kill_acc >= 1.0:
                kill_acc -= 1.0
                mana = min(max_mana, mana + mok)
            timers = [max(x - DT, 0.0) for x in timers]
            gcd = max(gcd - DT, 0.0)
            busy = max(busy - DT, 0.0)
            if busy <= 0 and gcd <= 0:
                for i in order:
                    s = self.slots[i]
                    if timers[i] > 0:
                        continue
                    if s["basic"]:
                        timers[i] = 1.0 / aps
                    else:
                        cost = s["mana"] * (1 - mcr) * mana_f[i]
                        if mana < cost:
                            continue
                        mana -= cost
                        timers[i] = s["cooldown"] * cd_f[i] * (1 - cdr)
                        gcd = GCD * (1 - cdr)
                        busy = s["channel"]
                    casts[i] += 1
                    mana = min(max_mana, mana + gain[i])
                    for dst, p in free[i]:
                        acc[dst] = acc.get(dst, 0.0) + p
                        while acc[dst] >= 1.0:
                            acc[dst] -= 1.0
                            extra[dst] = extra.get(dst, 0) + 1
                    break
            t += DT
        out = {s["name"]: casts[i] / SECONDS for i, s in enumerate(self.slots)}
        for dst, n in extra.items():
            out[dst] = out.get(dst, 0.0) + n / SECONDS
        self._cache[key] = out
        return out

    def wd_per_s(self, stats: dict, kills_per_s: float, mods: dict | None = None) -> float:
        cps = self.run(stats, kills_per_s, mods)
        return sum(cps[s["name"]] * s["weight"] for s in self.slots)

    def calibrate(self, stats: dict, measured_cps: dict) -> float:
        """Kills per second for which the simulated mana-hungry casts match the measured ones."""
        mana_slots = [s for s in self.slots if s["mana"] > 0 and measured_cps.get(s["name"])]
        if not mana_slots:
            return 1.0
        target = sum(measured_cps[s["name"]] * s["mana"] for s in mana_slots)  # measured mana spent per second

        def spent(k):
            cps = self.run(stats, k)
            return sum(cps[s["name"]] * s["mana"] for s in mana_slots)

        lo, hi = 0.0, 10.0
        if spent(hi) <= target:
            return hi
        for _ in range(14):
            mid = (lo + hi) / 2
            if spent(mid) < target:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

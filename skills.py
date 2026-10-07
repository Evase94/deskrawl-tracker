"""Count ability casts from the skill bar at the bottom of the game window.

When an ability is used, its icon flashes white for 70-180 ms, then the cooldown sweep starts.
The bar is found once by matching the hero's ability icons (data/abilities.json, pictures from
wikily.gg cached in cache/icons); after that each frame only needs the mean colour of four
small squares. A cast = the start of a flash (low saturation, high brightness).

Damage share estimate: casts x damage % of the ability x number of targets it names
("strikes 3 random enemies"); good enough to weigh talents for one ability against another.
"""
import base64
import hashlib
import json
import os
import re
import threading
import time
import urllib.request

import cv2
import numpy as np

import errlog
import paths

FLASH_S, FLASH_V = 0.35, 0.55   # mean saturation below / brightness above = white flash
MIN_GAP_S = 0.25                # one cast cannot flash twice within this time


def _abilities():
    try:
        with open(paths.res("data", "abilities.json"), encoding="utf-8") as f:
            return json.load(f)["abilities"]
    except Exception:
        return []


ABILITIES = _abilities()


def icon_path(url):
    return paths.user("cache", "icons", hashlib.sha1(url.encode()).hexdigest()[:16] + ".png")


def load_icon(url):
    """Ability picture as BGR array; downloaded once (wikily image server, 96 px)."""
    p = icon_path(url)
    if not os.path.exists(p):
        b64 = base64.urlsafe_b64encode(url.encode()).decode().rstrip("=")
        req = urllib.request.Request(f"https://img.wikily.gg/unsafe/w:128/{b64}",
                                     headers={"User-Agent": "Mozilla/5.0 (DeskrawlTracker)"})
        data = urllib.request.urlopen(req, timeout=20).read()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
    img = cv2.imread(p, cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.shape[2] == 4:  # flatten transparency onto dark slot colour
        a = img[:, :, 3:4] / 255.0
        img = (img[:, :, :3] * a + 20 * (1 - a)).astype(np.uint8)
    return img


LEARNED_DIR = paths.user("cache", "skill_icons")  # icons taken from the game's own skill bar
MAX_LEARNED = 6


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def templates(ability) -> list:
    """Pictures an ability is recognised by: icons the player confirmed from the game first (the wiki's
    pictures do not always match what the game shows), then the wiki icon."""
    out = []
    slug = _slug(ability["name"])
    if os.path.isdir(LEARNED_DIR):
        for fn in sorted(os.listdir(LEARNED_DIR)):
            if fn.startswith(slug + "-") and fn.endswith(".png"):
                img = cv2.imread(os.path.join(LEARNED_DIR, fn))
                if img is not None:
                    out.append(img)
    try:
        icon = load_icon(ability["icon_url"]) if ability.get("icon_url") else None
    except Exception:
        icon = None
    if icon is not None:
        out.append(icon)
    return out


def learn(name: str, crop) -> None:
    """Keep a picture of the game's own icon of this ability (the newest MAX_LEARNED)."""
    if crop is None or crop.size == 0:
        return
    os.makedirs(LEARNED_DIR, exist_ok=True)
    slug = _slug(name)
    have = sorted(fn for fn in os.listdir(LEARNED_DIR) if fn.startswith(slug + "-"))
    for fn in have[:max(len(have) - MAX_LEARNED + 1, 0)]:
        os.remove(os.path.join(LEARNED_DIR, fn))
    cv2.imwrite(os.path.join(LEARNED_DIR, f"{slug}-{int(time.time() * 1000)}.png"), crop)


def forget(name: str) -> None:
    if os.path.isdir(LEARNED_DIR):
        for fn in os.listdir(LEARNED_DIR):
            if fn.startswith(_slug(name) + "-"):
                os.remove(os.path.join(LEARNED_DIR, fn))


def targets(ability) -> int:
    m = re.search(r"strikes (\d+)", ability.get("description", ""))
    return int(m.group(1)) if m else 1


def damage_weight(ability) -> float:
    """Weapon damage % of one cast (all hits, all named targets); 0 for buffs without damage."""
    desc = ability.get("description", "")
    if ability.get("damage_l1"):  # level 1 values: known for most, comparable between abilities
        per_hit = ability["damage_l1"]
    else:
        pcts = [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)% weapon damage", desc)]
        per_hit = sum(pcts) if pcts else 0.0
    m = re.search(r"(\d+) [\w ]*?(?:bolts|hits|strikes|projectiles|arrows)[^.]*?each", desc)
    hits = int(m.group(1)) if m else 1
    return per_hit * hits * targets(ability)


class SkillBar:
    def __init__(self, hero):
        self.hero = hero
        self.slots = []        # [(name, x0, y0, size)]
        self.crops = []        # picture of each slot when the bar was found
        self.state = {}        # name -> (in_flash, last_event_t)
        self.located_for = None

    def locate(self, frame) -> bool:
        """Find the four ability icons in the lower part of the frame."""
        H, W = frame.shape[:2]
        y0 = int(H * 0.8)
        roi = frame[y0:, :int(W * 0.7)]  # the bar sits at the bottom left of the game window
        found = []
        lo, hi = max(16, int(H * 0.022)), int(H * 0.036)

        def match(icon, size):
            t = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
            _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED))
            return mx, loc

        for a in ABILITIES:
            if a.get("hero") != self.hero:
                continue
            best = (0, None, None)
            for icon in templates(a):
                coarse = max(((match(icon, sz), sz) for sz in range(lo, hi + 1, 3)), key=lambda r: r[0][0])
                for size in range(max(lo, coarse[1] - 2), min(hi, coarse[1] + 2) + 1):  # refine around it
                    mx, loc = match(icon, size)
                    if mx > best[0]:
                        best = (mx, loc, size)
            if best[0] > 0.55:
                found.append((best[0], a["name"], best[1][0], best[1][1] + y0, best[2]))
        found.sort(reverse=True)
        slots = []
        scores = {}
        for score, name, x, y, size in found:  # one ability per place, best match wins
            if all(abs(x - s[1]) > size * 0.6 or abs(y - s[2]) > size * 0.6 for s in slots):
                slots.append((name, x, y, size))
                scores[name] = score
        # the bar: icons on one line with an even spacing; drop matches elsewhere on the screen
        best_group = []
        for anchor in slots:  # four slots side by side fit into ~4.6 icon widths
            group = [s for s in slots if abs(s[2] - anchor[2]) < anchor[3] * 0.4
                     and 0 <= s[1] - anchor[1] < anchor[3] * 4.6]
            group = sorted(group, key=lambda g: -scores[g[0]])[:4]
            if sum(scores[g[0]] for g in group) > sum(scores[g[0]] for g in best_group):
                best_group = group
        slots = sorted(best_group, key=lambda s: s[1])
        if len(slots) >= 2:
            size = int(np.median([s[3] for s in slots]))
            # slot spacing: neighbours are about 1.2 icon widths apart; a stray match must not set it
            steps = [(b[1] - a[1]) / max(round((b[1] - a[1]) / (size * 1.22)), 1) for a, b in zip(slots, slots[1:])]
            steps = [d for d in steps if size * 1.05 <= d <= size * 1.45]
            pitch = float(np.median(steps)) if steps else size * 1.22
            # anchor on the best match and drop found icons that are off the slot grid
            anchor = max(slots, key=lambda s: scores[s[0]])
            slots = [s for s in slots if abs((s[1] - anchor[1]) / pitch - round((s[1] - anchor[1]) / pitch)) < 0.2]
            slots.sort(key=lambda s: s[1])
            y = int(np.median([s[2] for s in slots]))
            # positions of the four slots from the leftmost found one; a slot whose icon was dark
            # (cooldown) when the bar was searched is identified with a lower threshold
            left = anchor[1] - pitch * round((anchor[1] - slots[0][1]) / pitch)
            known = {round((s[1] - left) / pitch): s for s in slots}
            # every slot position around the found icons; the four ability slots are the four neighbours that
            # look most like abilities (the potion and scroll slots on the left do not)
            cand = {}
            for k in range(min(known) - 3, max(known) + 4):
                if k in known:
                    cand[k] = (scores[known[k][0]], known[k][0])
                    continue
                x = int(left + k * pitch)
                cell = frame[max(y - 3, 0):y + size + 3, max(x - 3, 0):x + size + 3]
                cand[k] = self.classify(cell) if cell.shape[:2] == (size + 6, size + 6) else (0.0, None)
            best_win, best_sum = None, -1.0
            for k0 in range(min(cand), max(cand) - 2):
                win = list(range(k0, k0 + 4))
                if not set(known) <= set(win) and len(known) <= 4:
                    continue
                total = sum(cand[k][0] for k in win if k in cand and cand[k][1])
                if total > best_sum:
                    best_win, best_sum = win, total
            out, used = [], set()
            for k in best_win or sorted(known):
                sc, name = cand.get(k, (0.0, None))
                if not name or sc < 0.25 or (name in used and k not in known):
                    continue
                used.add(name)
                out.append((name, int(left + k * pitch), y, size))
            slots = out[:4]
        self.slots = slots
        self.located_for = (H, W)
        self.crops = [frame[y:y + size, x:x + size].copy() for _, x, y, size in slots]
        self.state = {s[0]: (False, 0.0) for s in self.slots}
        return bool(self.slots)

    def classify(self, cell) -> tuple:
        """(best score, ability) for one slot picture of the size the bar was found with."""
        best = (0.0, None)
        if cell is None or cell.size == 0:
            return best
        h = cell.shape[0]
        for a in ABILITIES:
            if a.get("hero") != self.hero:
                continue
            for icon in templates(a):
                t = cv2.resize(icon, (h - 6, h - 6), interpolation=cv2.INTER_AREA) if h > 12 else icon
                sc = float(cv2.matchTemplate(cell, t, cv2.TM_CCOEFF_NORMED).max())
                if sc > best[0]:
                    best = (sc, a["name"])
        return best

    def update(self, frame, t) -> list:
        """Cast events [(t, ability name)] seen in this frame."""
        if self.located_for != frame.shape[:2] or not self.slots:
            if not self.locate(frame):
                return []
        out = []
        for name, x, y, size in self.slots:
            m = max(2, size // 8)  # inner square, away from the frame border
            c = frame[y + m:y + size - m, x + m:x + size - m]
            if c.size == 0:
                continue
            hsv = cv2.cvtColor(c, cv2.COLOR_BGR2HSV)
            s = float(hsv[..., 1].mean()) / 255
            v = float(hsv[..., 2].mean()) / 255
            flash = s < FLASH_S and v > FLASH_V
            was, last = self.state.get(name, (False, 0.0))
            if flash and not was and t - last > MIN_GAP_S:
                out.append((t, name))
                last = t
            self.state[name] = (flash, last)
        return out


def locate_stable(grab, hero: str, tries: int = 3):
    """Find the bar in several pictures and keep the layout most of them agree on, with the ability per
    slot that most pictures saw (a cast flash, a cooldown sweep or an effect can mislead one picture)."""
    reads = []
    for i in range(tries):
        frame = grab()
        if frame is None:
            break
        bar = SkillBar(hero)
        if bar.locate(frame) and bar.slots:
            reads.append(bar)
        if i + 1 < tries:
            time.sleep(0.25)
    if not reads:
        return None
    key = lambda b: (round(b.slots[0][1] / 6), len(b.slots))
    groups = {}
    for b in reads:
        groups.setdefault(key(b), []).append(b)
    same = max(groups.values(), key=len)
    best = same[0]
    slots, used = [], set()
    for i, (name, x, y, size) in enumerate(best.slots):
        votes = [b.slots[i][0] for b in same if i < len(b.slots)]
        ranked = sorted(set(votes), key=lambda n: -votes.count(n))
        pick = next((n for n in ranked if n not in used), name)
        used.add(pick)
        slots.append((pick, x, y, size))
    best.slots = slots
    best.state = {s[0]: (False, 0.0) for s in slots}
    return best


class SkillWatcher(threading.Thread):
    """Grabs the game window ~15 times a second while switched on and counts casts per run."""

    FPS = 15

    def __init__(self, state, capture):
        super().__init__(daemon=True)
        self.state, self.capture = state, capture
        self.enabled = threading.Event()
        self.bar = None
        self.status = "skill tracking off"
        self._run_id = None   # the skill bar is searched again at the start of every run (skills may change)

    def read_bar(self) -> str:
        """Search the skill bar now (button "Read skill bar"); works while tracking is off too."""
        if self.capture.grab() is None:
            return f"Game picture not available ({self.capture.status})."
        best = locate_stable(self.capture.grab, getattr(self.state, "hero", ""), tries=4)
        if best is None:
            return "No skill bar found – is the game showing a stage or town (not a full-screen menu)?"
        self.bar = best
        return "Skill bar read: " + ", ".join(s[0] for s in best.slots)

    def _recheck(self, hero):
        """Read the bar again (several pictures, takes some seconds) without pausing the counting."""
        try:
            bar = locate_stable(self.capture.grab, hero)
        except Exception:
            return
        old = self.bar
        if bar is None or old is None:
            return
        if [x[0] for x in bar.slots] != [x[0] for x in old.slots] or bar.located_for != old.located_for:
            bar.state = {name: old.state.get(name, (False, 0.0)) for name, *_ in bar.slots}
            self.bar = bar

    def confirm(self, names: list) -> None:
        """The player named the abilities on the bar (read-bar window): use them and remember the game's
        icons, so these abilities are recognised from now on."""
        bar = self.bar
        if bar is None:
            return
        for i, name in enumerate(names):
            if i < len(bar.slots) and name:
                _, x, y, size = bar.slots[i]
                bar.slots[i] = (name, x, y, size)
                if i < len(bar.crops):
                    learn(name, bar.crops[i])
        bar.state = {s[0]: (False, 0.0) for s in bar.slots}

    def run(self):
        while True:
            self.enabled.wait()
            if getattr(self.state, "ui_busy", lambda: False)():
                time.sleep(0.05)  # the tracker window is being moved/resized
                continue
            t0 = time.time()
            try:
                hero = self.state.hero
                if self.bar is None or self.bar.hero != hero:
                    self.bar = SkillBar(hero)
                cur = getattr(self.state, "current", None)
                run_id = cur.run_id if cur is not None else None
                if run_id and run_id != self._run_id:  # new run: abilities may have been swapped in town
                    self._run_id = run_id
                    if self.bar.slots:  # keep counting with the known bar; check it in the background
                        threading.Thread(target=self._recheck, args=(hero,), daemon=True).start()
                    else:
                        self.bar.located_for = None  # nothing known yet: the next update searches at once
                if not self.bar.slots:
                    self.status = "searching the skill bar…"
                frame = self.capture.grab()
                if frame is not None:
                    events = self.bar.update(frame, time.time())
                    if events:
                        self.state.add_casts(events)
                    self.status = ("skill bar: " + ", ".join(s[0] for s in self.bar.slots)) if self.bar.slots \
                        else "skill bar not found"
            except Exception as e:
                self.status = f"skill tracking error: {e}"
                errlog.report("skill_watcher", "skill tracking failed")
            time.sleep(max(0.0, 1 / self.FPS - (time.time() - t0)))

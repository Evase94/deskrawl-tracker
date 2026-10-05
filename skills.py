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
        self.state = {}        # name -> (in_flash, last_event_t)
        self.located_for = None

    def locate(self, frame) -> bool:
        """Find the four ability icons in the lower part of the frame."""
        H, W = frame.shape[:2]
        y0 = int(H * 0.75)
        roi = frame[y0:, :]
        found = []
        for a in ABILITIES:
            if a.get("hero") != self.hero or not a.get("icon_url"):
                continue
            try:
                icon = load_icon(a["icon_url"])
            except Exception:
                icon = None
            if icon is None:
                continue
            best = (0, None, None)
            for size in range(max(16, int(H * 0.022)), int(H * 0.036) + 1):
                t = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
                r = cv2.matchTemplate(roi, t, cv2.TM_CCOEFF_NORMED)
                _, mx, _, loc = cv2.minMaxLoc(r)
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
            pitch = min(b[1] - a[1] for a, b in zip(slots, slots[1:]))
            size = int(np.median([s[3] for s in slots]))
            y = int(np.median([s[2] for s in slots]))
            # positions of the four slots from the leftmost found one; a slot whose icon was dark
            # (cooldown) when the bar was searched is identified with a lower threshold
            left = slots[0][1] - pitch * round((slots[0][1] - min(s[1] for s in slots)) / max(pitch, 1))
            known = {round((s[1] - left) / pitch): s for s in slots}
            first = min(known)
            for k in range(first, first + 4):
                if k in known:
                    continue
                x = int(left + k * pitch)
                cell = frame[y:y + size, x:x + size]
                if cell.shape[:2] != (size, size):
                    continue
                best = (0.25, None)
                for a in ABILITIES:
                    if a.get("hero") != self.hero or a["name"] in [s[0] for s in known.values()]:
                        continue
                    try:
                        icon = load_icon(a["icon_url"])
                    except Exception:
                        continue
                    t = cv2.resize(icon, (size, size), interpolation=cv2.INTER_AREA)
                    sc = float(cv2.matchTemplate(cell, t, cv2.TM_CCOEFF_NORMED)[0][0])
                    if sc > best[0]:
                        best = (sc, a["name"])
                if best[1]:
                    known[k] = (best[1], x, y, size)
            slots = [known[k] for k in sorted(known)][:4]
        self.slots = slots
        self.located_for = (H, W)
        self.state = {s[0]: (False, 0.0) for s in self.slots}
        return bool(self.slots)

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


class SkillWatcher(threading.Thread):
    """Grabs the game window ~15 times a second while switched on and counts casts per run."""

    FPS = 15

    def __init__(self, state, capture):
        super().__init__(daemon=True)
        self.state, self.capture = state, capture
        self.enabled = threading.Event()
        self.bar = None
        self.status = "skill tracking off"

    def run(self):
        while True:
            self.enabled.wait()
            t0 = time.time()
            try:
                hero = self.state.hero
                if self.bar is None or self.bar.hero != hero:
                    self.bar = SkillBar(hero)
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
            time.sleep(max(0.0, 1 / self.FPS - (time.time() - t0)))

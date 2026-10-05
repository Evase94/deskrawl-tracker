"""Read the talent build from the game's talent window ("Combat Talents").

1. Find talent icons by matching the planner's talent pictures (afkmeta) against the frame.
2. The tree has the same layout as the planner, so the confident matches give a mapping from planner
   coordinates to the screen; every talent's position follows from it, also for icons that matched badly.
3. Read the "rank/max" badge under each visible icon (binarised, RapidOCR). The max is known, only the
   left number matters; capstones show a gold "1/1" or "0/1".
The window scrolls: talents outside the visible part are reported as missing, so a second read after
scrolling can complete the build.
"""
import re

import cv2
import numpy as np

import item_ocr
import skills
import talents

ANCHOR = 0.86     # match score of an icon that is certainly at that place
VISIBLE = 0.55    # match score at the expected place that counts as "icon visible"


def _inner(icon, size):
    """Grey centre of an icon (the ring around it differs between planner and game)."""
    g = cv2.resize(cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY), (size, size), interpolation=cv2.INTER_AREA)
    m = int(size * 0.2)
    return g[m:size - m, m:size - m], m


def _icons(hero):
    out = {}
    for t in talents.tree(hero):
        try:
            ic = skills.load_icon(t["icon_url"]) if t.get("icon_url") else None
        except Exception:
            ic = None
        if ic is not None:
            out[talents.key(t)] = ic
    return out


def _find_size(gray, icons, tree):
    best = (0.0, 40)
    hi = int(min(gray.shape[:2]) * 0.12)
    for t in tree[:6]:
        ic = icons.get(talents.key(t))
        if ic is None:
            continue
        for size in range(20, max(22, hi), 2):
            tt, _ = _inner(ic, size)
            if tt.shape[0] >= gray.shape[0] or tt.shape[1] >= gray.shape[1]:
                break
            mx = cv2.minMaxLoc(cv2.matchTemplate(gray, tt, cv2.TM_CCOEFF_NORMED))[1]
            if mx > best[0]:
                best = (mx, size)
    return best


def _parse_rank(text, ranks):
    t = text.replace(" ", "")
    t = t.translate(str.maketrans({"Ø": "0", "ø": "0", "O": "0", "o": "0", "D": "0", "Q": "0", "U": "0", "u": "0",
                                   "v": "0", "S": "5", "s": "5",
                                   "I": "1", "l": "1", "|": "1", "!": "1", "i": "1", "V": "1", "f": "/",
                                   "\\": "/", ">": "/"}))
    m = re.search(r"(\d+)/(\d+)", t)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        if b == ranks and 0 <= a <= ranks:
            return a
        if a == ranks and 0 <= b <= ranks and t.startswith(str(a)):  # rare swap
            return None
    digits = re.findall(r"\d+", t)
    if len(digits) == 1 and ranks == 1 and digits[0] in ("0", "1"):
        return int(digits[0])
    if len(digits) >= 2 and int(digits[-1]) == ranks and int(digits[0]) <= ranks:
        return int(digits[0])
    return None


def _badge(frame, x, y, s, ranks):
    crop = frame[int(y + s * 1.0):int(y + s * 1.36), int(x - s * 0.1):int(x + s * 1.1)]
    if crop.size == 0:
        return None
    g = cv2.resize(cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY), None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC)
    _, b = cv2.threshold(g, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    b = cv2.copyMakeBorder(255 - b, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    text = " ".join(l.text for l in item_ocr.ocr_rapid(cv2.cvtColor(b, cv2.COLOR_GRAY2BGR)))
    r = _parse_rank(text, ranks)
    if r is None and ranks == 1:
        # capstones: an invested one has the gold ornament and a gold badge text
        hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
        gold = ((hsv[..., 0] >= 15) & (hsv[..., 0] <= 35) & (hsv[..., 1] > 90) & (hsv[..., 2] > 150)).mean()
        r = 1 if gold > 0.04 else 0
    return r


def read(frame, hero):
    """{talent key: rank} of the talents visible in the frame, and [talent names not readable]."""
    tree = talents.tree(hero)
    icons = _icons(hero)
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    score, size = _find_size(gray, icons, tree)
    if score < 0.7:
        return None
    found = []
    for t in tree:
        ic = icons.get(talents.key(t))
        if ic is None:
            continue
        best = (0.0, None, size)
        for s in (size - 2, size, size + 2):
            tt, m = _inner(ic, s)
            _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(gray, tt, cv2.TM_CCOEFF_NORMED))
            if mx > best[0]:
                best = (mx, (loc[0] - m, loc[1] - m), s)
        found.append((best[0], t, best[1], best[2]))
    # anchors: confident matches, one talent per place
    anchors = []
    for sc, t, pos, s in sorted(found, key=lambda f: -f[0]):
        if sc < ANCHOR:
            break
        if all(abs(pos[0] - a[2][0]) > s * 0.5 or abs(pos[1] - a[2][1]) > s * 0.5 for a in anchors):
            anchors.append((sc, t, pos, s))
    xs = {round(a[1]["x"], 3) for a in anchors}
    ys = {round(a[1]["y"], 3) for a in anchors}
    if len(xs) < 2 or len(ys) < 2:
        return None
    ax, bx = np.polyfit([a[1]["x"] for a in anchors], [a[2][0] for a in anchors], 1)
    ay, by = np.polyfit([a[1]["y"] for a in anchors], [a[2][1] for a in anchors], 1)
    H, W = gray.shape[:2]
    build, unsure, seen = {}, [], set()
    for t in tree:
        ex, ey = ax * t["x"] + bx, ay * t["y"] + by
        if ey < -size * 0.2 or ey + size * 1.4 > H or ex < 0 or ex + size > W:
            continue  # outside the picture (scrolled away)
        ic = icons.get(talents.key(t))
        x0, y0 = int(max(ex - size * 0.4, 0)), int(max(ey - size * 0.4, 0))
        win = gray[y0:int(ey + size * 1.4), x0:int(ex + size * 1.4)]
        tt, m = _inner(ic, size)
        if win.shape[0] < tt.shape[0] or win.shape[1] < tt.shape[1]:
            continue
        _, mx, _, loc = cv2.minMaxLoc(cv2.matchTemplate(win, tt, cv2.TM_CCOEFF_NORMED))
        if mx < VISIBLE:
            continue  # covered by the window frame / not on screen
        x, y = x0 + loc[0] - m, y0 + loc[1] - m
        seen.add(talents.key(t))
        # talents without points are drawn darker: about half the brightness of the picture
        c = gray[int(y + size * 0.25):int(y + size * 0.75), int(x + size * 0.25):int(x + size * 0.75)]
        ref = cv2.cvtColor(cv2.resize(ic, (size, size)), cv2.COLOR_BGR2GRAY)[
            int(size * 0.25):int(size * 0.75), int(size * 0.25):int(size * 0.75)]
        if c.size == 0 or float(c.mean()) < 0.75 * max(float(ref.mean()), 1.0):
            continue
        if t["ranks"] == 1:
            build[talents.key(t)] = 1
            continue
        r = _badge(frame, x, y, size, t["ranks"])
        if r is None or r == 0:  # bright icon = at least one point; unreadable number: assume full
            unsure.append(t["name"])
            r = t["ranks"]
        build[talents.key(t)] = r
    return build, unsure, seen


def read_build(frame, hero):
    """For the tracker: (build, unsure names, seen keys) or a message."""
    r = read(frame, hero)
    if r is None:
        return "Talent window not found – open Combat Talents in the game and try again."
    return r

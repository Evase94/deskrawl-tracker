"""Read the talent build from the game's talent window ("Combat Talents").

The window shows about six of the nine rows at a time; the rest is reached by scrolling. Steps:
1. The window must be on screen: its tab "Combat Talents" or a "Capstone Talent" label is read (OCR).
2. Rank badges: red-brown plates under every talent ("10/10", "Ø/6"; a capstone with its point shows a gold
   "1/1"). They are found by colour and grouped into rows.
3. Columns: the badges' x positions map onto the planner's x positions (0.04 ... 0.96).
4. Rows: every visible row is matched against the rows of the tree with the same columns, by comparing the
   pictures above the badges with the talent pictures; the visible rows are consecutive, so the best
   offset wins.
5. Ranks: the badge is cut at its slash. The left number is compared with the "Ø" pictures (rank 0) and with
   the badge's own right number (full rank); anything else with digit pictures (data/talent_digits.npz),
   finally OCR. The number of free points ("0/70" top right) is read too.
Talents of rows outside the picture are reported as not seen, so a second read after scrolling completes the
build.
"""
import re

import cv2
import numpy as np

import item_ocr
import paths
import skills
import talents


# ----------------------------------------------------------------------------- badges

def _hsv(img):
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    return hsv[..., 0], hsv[..., 1], hsv[..., 2]


def badges(frame) -> list:
    """Rank badges: [(x, y, w, h)]."""
    H = frame.shape[0]
    h, s, v = _hsv(frame)
    plate = (((h <= 12) | (h >= 170)) & (s > 150) & (v > 50) & (v < 150)).astype(np.uint8)
    text = (((s < 70) & (v > 150)) | ((h >= 14) & (h <= 35) & (s > 70) & (v > 140))).astype(np.uint8)
    m = cv2.morphologyEx(plate, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, st, _ = cv2.connectedComponentsWithStats(m, 8)
    sc = H / 1152
    out = []
    for i in range(1, n):
        x, y, w, hh, _a = st[i]
        # a red icon right above can join the plate: keep the longest run of rows filled across the width
        rowfill = m[y:y + hh, x:x + w].mean(axis=1)
        fill = rowfill > 0.7
        best, cur, start = (0, 0), 0, 0
        for j, ok in enumerate(fill):
            if ok:
                start = j if cur == 0 else start
                cur += 1
                if cur > best[0]:
                    best = (cur, start)
            else:
                cur = 0
        if best[0]:
            a0, a1 = best[1], best[1] + best[0]  # rows [a0, a1) of the plate; widen over rows the digits thin out
            while a0 > 0 and rowfill[a0 - 1] > 0.45 and a1 - a0 < best[0] + 3:
                a0 -= 1
            while a1 < len(rowfill) and rowfill[a1] > 0.45 and a1 - a0 < best[0] + 3:
                a1 += 1
            y, hh = y + a0, a1 - a0
        if not (8 * sc <= hh <= 22 * sc and 24 * sc <= w <= 70 * sc and 2.0 <= w / hh <= 5.5):
            continue
        if plate[y:y + hh, x:x + w].mean() < 0.3 or text[y:y + hh, x:x + w].mean() < 0.05:
            continue
        out.append((int(x), int(y), int(w), int(hh)))
    return out


def _rows(bs: list) -> list:
    """Badges grouped by height on screen, top to bottom, each row left to right."""
    rows = []
    for b in sorted(bs, key=lambda b: b[1]):
        if rows and abs(rows[-1][0][1] - b[1]) < b[3]:
            rows[-1].append(b)
        else:
            rows.append([b])
    return [sorted(r) for r in rows]


# ----------------------------------------------------------------------------- numbers

def _text_mask(crop):
    """Digits of a badge (white, or gold on an invested capstone) - only where the red-brown plate is around
    them, so a bit of the icon above does not count."""
    h, s, v = _hsv(crop)
    white = (s < 90) & (v > 130)
    gold = (h >= 14) & (h <= 35) & (s > 70) & (v > 130)
    plate = (((h <= 12) | (h >= 170)) & (s > 150) & (v > 50) & (v < 150)).astype(np.uint8)
    rows = plate.mean(axis=1) > 0.25
    area = np.zeros_like(plate)
    if rows.any():
        r0, r1 = np.nonzero(rows)[0][[0, -1]]
        area[r0:r1 + 1, :] = 1
    text = (white | gold) & (area > 0)
    return text.astype(np.uint8), float((gold & (area > 0)).sum()) > float((white & (area > 0)).sum())


def _split(crop, ranks: int | None = None):
    """(left number mask, right number mask, gold text) of a badge, cut along its slash. The slash is a
    diagonal over the full text height; "Ø" has one inside too, so with the max rank known the cut whose right
    side looks most like that number wins."""
    m, gold = _text_mask(crop)
    H, W = m.shape
    ys, xs = np.nonzero(m)
    if len(xs) < 8:
        return None, None, gold
    y0, y1 = ys.min(), ys.max()
    ymid = (y0 + y1) / 2
    need = (y1 - y0 + 1) * 0.8
    tmpl = _digits().get(str(ranks), []) if ranks else []

    def cut(cx, k):
        num, den = m.copy(), m.copy()
        for y in range(H):
            x = cx + k * (y - ymid)
            num[y, int(max(x - 1.5, 0)):] = 0
            den[y, :int(min(x + 2.5, W))] = 0
        return num, den

    cands = []
    for k in (-0.5, -0.6, -0.7, -0.8):
        for cx in range(int(W * 0.25), int(W * 0.8)):
            hits = 0
            for y in range(y0, y1 + 1):
                x = int(round(cx + k * (y - ymid)))
                if 0 <= x < W and (m[y, x] or (x + 1 < W and m[y, x + 1])):
                    hits += 1
            if hits >= need:
                cands.append((hits, cx, k))
    if not cands:
        return None, None, gold
    if tmpl:
        def score(c):
            num, den = cut(c[1], c[2])
            d = _shape(den)
            return max(_sim(d, t) for t in tmpl) + 0.01 * c[0]
        best = max(cands, key=score)
    else:
        best = max(cands)
    num, den = cut(best[1], best[2])
    return num, den, gold


def _shape(mask, h=14):
    if mask is None:
        return None
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    c = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    w = max(int(round(c.shape[1] * h / c.shape[0])), 1)
    return cv2.resize(c.astype(np.float32), (w, h), interpolation=cv2.INTER_AREA)


def _sim(a, b) -> float:
    if a is None or b is None or abs(a.shape[1] - b.shape[1]) > max(a.shape[1], b.shape[1]) * 0.35:
        return 0.0
    w = max(a.shape[1], b.shape[1])
    a2 = cv2.resize(a, (w, a.shape[0])) - 0
    b2 = cv2.resize(b, (w, b.shape[0])) - 0
    a2, b2 = a2 - a2.mean(), b2 - b2.mean()
    d = np.sqrt((a2 * a2).sum() * (b2 * b2).sum())
    return float((a2 * b2).sum() / d) if d > 0 else 0.0


ZERO_MIN = 0.42  # match of "Ø" at the start of a badge from which it reads 0
TEXT_H = 14      # digit height the badge text and the pictures in data/talent_digits.npz are scaled to
_DIGITS = None
LAST_PAIRS = []


def _digits() -> dict:
    """Pictures of the numbers in the badges' font: {"0": [img, ...], "6": [...], ...}."""
    global _DIGITS
    if _DIGITS is None:
        _DIGITS = {}
        try:
            with np.load(paths.res("data", "talent_digits.npz")) as z:
                for k in z.files:
                    _DIGITS.setdefault(k.split("_")[0], []).append(z[k])
        except Exception:
            pass
    return _DIGITS


def _ocr_number(mask) -> int | None:
    if mask is None or not mask.any():
        return None
    big = cv2.resize((1 - mask) * 255, None, fx=5, fy=5, interpolation=cv2.INTER_CUBIC).astype(np.uint8)
    big = cv2.copyMakeBorder(big, 20, 20, 20, 20, cv2.BORDER_CONSTANT, value=255)
    text = "".join(l.text for l in item_ocr.ocr_rapid(cv2.cvtColor(big, cv2.COLOR_GRAY2BGR)))
    text = text.translate(str.maketrans({"Ø": "0", "ø": "0", "O": "0", "o": "0", "I": "1", "l": "1", "|": "1",
                                         "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2"}))
    m = re.fullmatch(r"\D*(\d{1,2})\D*", text)
    return int(m.group(1)) if m else None


def _text_band(m):
    """Rows of the digits: the longest run of rows with at least two text pixels (stray pixels drop out)."""
    rows = m.sum(axis=1) >= 2
    best, cur, start = (0, 0), 0, 0
    for j, ok in enumerate(rows):
        if ok:
            start = j if cur == 0 else start
            cur += 1
            if cur > best[0]:
                best = (cur, start)
        else:
            cur = 0
    return best[1], best[1] + best[0]


def _norm_text(crop):
    """Text mask of a badge scaled so the digits are TEXT_H pixels high, cropped to the text; and gold?"""
    m, gold = _text_mask(crop)
    r0, r1 = _text_band(m)
    if r1 - r0 < 4:
        return None, gold
    m = m[r0:r1]
    cols = np.nonzero(m.any(axis=0))[0]
    m = m[:, cols[0]:cols[-1] + 1]
    f = TEXT_H / m.shape[0]
    big = cv2.resize(m.astype(np.float32), (max(int(round(m.shape[1] * f)), 1), TEXT_H), interpolation=cv2.INTER_AREA)
    return big, gold


def _width(key) -> float:
    ts = _digits().get(key, [])
    return float(np.median([t.shape[1] for t in ts])) if ts else 0.0


def _left_match(txt, tmpl, slack=4) -> float:
    """How well the start of the badge text shows tmpl (a few pixels of play left/right and in width)."""
    best = -1.0
    W = txt.shape[1]
    for dw in (-1, 0, 1):
        w = tmpl.shape[1] + dw
        if w < 2 or w > W:
            continue
        t = cv2.resize(tmpl, (w, TEXT_H), interpolation=cv2.INTER_AREA)
        reg = txt[:, :min(w + slack, W)]
        if reg.shape[1] < w or t.std() == 0 or reg.std() == 0:
            continue
        best = max(best, float(cv2.minMaxLoc(cv2.matchTemplate(reg, t, cv2.TM_CCOEFF_NORMED))[1]))
    return best


def rank(crop, ranks: int):
    """(points of a badge, sure?, confidence 0..1). The badge reads "<points>/<ranks>": the text starts with "Ø" (0 points) or
    with the same number it ends with (full); other numbers are compared with digit pictures, then OCR."""
    txt, gold = _norm_text(crop)
    if ranks == 1 and gold:
        return 1, True, 1.0
    if txt is None:
        return (0, False, 0.1) if ranks == 1 else (None, False, 0.0)
    W = txt.shape[1]
    dig = _digits()
    zero = max((_left_match(txt, t) for t in dig.get("0", [])), default=-1.0)
    full = max((_left_match(txt, t) for t in dig.get(str(ranks), [])), default=-1.0)
    k = int(round(_width(str(ranks))))
    if k and 2 * k <= W + 2:  # the badge's own right-hand number
        full = max(full, _left_match(txt, np.ascontiguousarray(txt[:, W - k:])))
    # measured on real badges: "Ø" scores 0.48-0.95 at the start, any other number at most 0.34
    if zero >= ZERO_MIN:
        return 0, zero >= ZERO_MIN + 0.1, zero
    # not 0: full rank unless a smaller number fits clearly better (in-between ranks are rare)
    best, best_s = None, -1.0
    for key, ts in dig.items():
        if not key.isdigit() or not 0 < int(key) < ranks:
            continue
        sc = max(_left_match(txt, t) for t in ts)
        if sc > best_s:
            best, best_s = int(key), sc
    if best is not None and best_s >= 0.6 and best_s > full + 0.1:
        return best, best_s >= 0.8, best_s * 0.5  # in-between numbers: few pictures to compare with
    if full >= 0.3:
        return ranks, full >= 0.5, full
    num, _den, _g = _split(crop, ranks)
    r = _ocr_number(num)
    if r is not None and 0 <= r <= ranks:
        return r, False, 0.1
    return (0, False, 0.1) if ranks == 1 else (None, False, 0.0)


# ----------------------------------------------------------------------------- talents

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


def _icon_score(gray, b, ic) -> float:
    """How well the picture above badge b shows talent icon ic."""
    x, y, w, h = b
    size = int(w * 0.95)
    cx = x + w / 2
    win = gray[max(int(y - size * 1.25), 0):int(y + h * 0.2), max(int(cx - size * 0.75), 0):int(cx + size * 0.75)]
    t = cv2.resize(cv2.cvtColor(ic, cv2.COLOR_BGR2GRAY), (size, size), interpolation=cv2.INTER_AREA)
    mg = int(size * 0.22)
    t = t[mg:size - mg, mg:size - mg]
    if win.shape[0] < t.shape[0] or win.shape[1] < t.shape[1]:
        return 0.0
    return float(cv2.minMaxLoc(cv2.matchTemplate(win, t, cv2.TM_CCOEFF_NORMED))[1])


def window_open(lines) -> bool:
    """The talent window's tabs ("Combat Talents", "Life Skills", "Paragon") or capstone labels are on screen.
    The stylised font is read loosely ("CemBAT TALENTS", "CAFSTONE")."""
    txt = " ".join(re.sub(r"[^a-z]", "", l.text.lower()) for l in lines)
    return "talents" in txt or "capstone" in txt or "cafstone" in txt or         ("paragon" in txt and ("life" in txt or "skills" in txt))


def free_points(lines):
    """(free, total) from "0/70" at the top of the window, or None."""
    for l in lines:
        m = re.fullmatch(r"\W*(\d{1,3})\s*/\s*(\d{2,3})\W*", l.text)
        if m and 10 <= int(m.group(2)) <= 200:
            return int(m.group(1)), int(m.group(2))
    return None


def read(frame, hero, lines=None):
    """{talent key: rank}, [talent names not read for sure], {talent keys seen}, (free, total) or None,
    {talent key: confidence of its number}."""
    lines = item_ocr.ocr_windows(frame) if lines is None else lines
    if not window_open(lines):
        return None
    tree = talents.tree(hero)
    rows = _rows(badges(frame))
    # the talent grid: rows of the tree by threshold, each with its x positions
    by_row = {}
    for t in tree:
        by_row.setdefault(t["points"], []).append(t)
    thresholds = sorted(by_row)
    full = [r for r in rows if len(r) == 5]
    if not full:
        return None
    x04 = np.median([r[0][0] + r[0][2] / 2 for r in full])
    x96 = np.median([r[-1][0] + r[-1][2] / 2 for r in full])
    if x96 - x04 < 100:
        return None
    to_norm = lambda px: 0.04 + (px - x04) / (x96 - x04) * 0.92
    # rows of badges that sit on the grid (drops HUD and other red things)
    grid = []
    for r in rows:
        xs = [to_norm(b[0] + b[2] / 2) for b in r]
        if all(-0.05 <= x <= 1.05 for x in xs) and len(r) >= 2:
            grid.append((r, xs))
    if not grid:
        return None
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    icons = _icons(hero)

    def row_fit(r, xs, thr):
        """(score, [(badge, talent)]) of screen row r as tree row thr; None if the columns do not fit."""
        ts = by_row[thr]
        pairs, total = [], 0.0
        for b, x in zip(r, xs):
            t = min(ts, key=lambda t: abs(t.get("x", 0.5) - x))
            if abs(t.get("x", 0.5) - x) > 0.06:
                return None
            ic = icons.get(talents.key(t))
            total += _icon_score(gray, b, ic) if ic is not None else 0.0
            pairs.append((b, t))
        return total / len(r), pairs

    best = None
    for off in range(-len(grid) + 1, len(thresholds)):
        score, n, pairs = 0.0, 0, []
        for i, (r, xs) in enumerate(grid):
            k = off + i
            if not 0 <= k < len(thresholds):
                continue  # a row cut off at the window edge
            f = row_fit(r, xs, thresholds[k])
            if f is None:
                score = -1e9
                break
            score += f[0]
            n += 1
            pairs += f[1]
        if n and (best is None or score / n > best[0]):
            best = (score / n, pairs)
    if best is None or best[0] < 0.3:
        return None
    global LAST_PAIRS
    LAST_PAIRS = best[1]  # for building data/talent_digits.npz
    build, unsure, seen, conf = {}, [], set(), {}
    for (x, y, w, h), t in best[1]:
        k = talents.key(t)
        seen.add(k)
        pad = max(h // 4, 2)
        r, sure, c = rank(frame[max(y - pad, 0):y + h + pad, x:x + w], t["ranks"])
        conf[k] = c
        if r is None:
            unsure.append(t["name"])
            continue
        if not sure:
            unsure.append(t["name"])
        if r:
            build[k] = r
    return build, unsure, seen, free_points(lines), conf


def read_build(frame, hero):
    """For the tracker: (build, unsure names, seen keys, (free, total) or None, confidences) or a message."""
    r = read(frame, hero)
    if r is None:
        return "Talent window not found – open Combat Talents in the game and try again."
    return r

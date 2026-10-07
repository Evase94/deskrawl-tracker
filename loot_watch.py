"""Legendary drops on screen, the moment they hit the ground.

A dropped item shows its name on the ground: a dark plate with the name in its rarity colour (Rare yellow,
OpenCV hue ~30; Legendary orange, hue ~15-19). LootWatcher looks a few times per second for orange text on
such a plate, reads the name to be sure it is an item (not a damage number, not the "Legendary" row of the
stage end screen) and reports every new label once. Labels stay where they dropped until a minion picks the
item up, so a label seen again at the same spot is the same drop.

The exact count per run comes from the stage end screen ("Legendary 3 (+1)", see item_ocr.read_end_rarities);
this watcher is for the sound right when it drops.
"""
import re
import threading
import time

import cv2
import numpy as np

import errlog
import item_ocr

HUE = (8, 23)          # legendary orange; Rare yellow starts at ~25
SAT, VAL = 140, 170    # bright, saturated text
DARK = 75              # plate background (V)
ROI_Y = (0.35, 0.94)   # battle strip; below it the HUD
NOT_NAMES = {"legendary", "divine", "rare", "uncommon", "common", "runeset", "obtained", "sold"}


def find_labels(frame) -> list:
    """Boxes (x, y, w, h) in frame coordinates of orange text on a dark plate."""
    H, W = frame.shape[:2]
    s = H / 1152
    y0, y1 = int(ROI_Y[0] * H), int(ROI_Y[1] * H)
    roi = frame[y0:y1]
    hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
    h, sat, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    orange = ((h >= HUE[0]) & (h <= HUE[1]) & (sat > SAT) & (v > VAL)).astype(np.uint8)
    if orange.sum() < 30:
        return []
    dark = v < DARK
    joined = cv2.morphologyEx(orange, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (max(int(9 * s), 3), 3)))
    n, _, stats, _ = cv2.connectedComponentsWithStats(joined, 8)
    out = []
    tx0, ty0, tw, th = item_ocr.TOAST_ROI
    for i in range(1, n):
        x, y, w, hh, area = stats[i]
        if not (7 * s <= hh <= 26 * s and w >= 38 * s and w >= hh * 2.5):
            continue
        if tx0 * W <= x < (tx0 + tw) * W and ty0 * H <= y + y0 < (ty0 + th) * H:
            continue  # pop-ups bottom left ("Obtained [..]")
        box_o = orange[y:y + hh, x:x + w]
        fill = box_o.mean()
        if not 0.08 <= fill <= 0.55:
            continue  # lava: solid orange; sparks: a few pixels
        box_d = dark[y:y + hh, x:x + w]
        rest = box_d[box_o == 0]
        if rest.size == 0 or rest.mean() < 0.45:
            continue  # text needs a dark plate (or night sky) behind it
        pad = max(int(3 * s), 2)
        above = dark[max(y - pad, 0):y, x:x + w]
        below = dark[y + hh:y + hh + pad, x:x + w]
        if above.size and above.mean() < 0.4 or below.size and below.mean() < 0.4:
            continue
        out.append((int(x), int(y + y0), int(w), int(hh)))
    return out


def label_text(frame, box) -> str:
    x, y, w, h = box
    pad = max(h // 3, 3)
    crop = frame[max(y - pad, 0):y + h + pad, max(x - pad, 0):x + w + pad]
    if crop.size == 0:
        return ""
    lines = item_ocr.ocr_scaled(crop, 3, "win")
    return " ".join(l.text for l in sorted(lines, key=lambda l: l.x)).strip()


def is_item_name(text: str) -> bool:
    letters = sum(ch.isalpha() for ch in text)
    if letters < 4 or letters < len(text.replace(" ", "")) * 0.7:
        return False  # damage numbers ("848,237", "2.7M")
    return re.sub(r"[^a-z]", "", text.lower()) not in NOT_NAMES


class LootWatcher(threading.Thread):
    INTERVAL_S = 0.5
    SAME_SPOT = 40     # px (at 1152 height): a label here is the one seen before
    FORGET_S = 25.0    # a label not seen for this long is gone (picked up)

    def __init__(self, state, capture, on_drop):
        super().__init__(daemon=True)
        self.state, self.capture, self.on_drop = state, capture, on_drop
        self.known = []     # [cx, cy, last_seen, is_item]
        self.found = 0
        self.enabled = True

    def _match(self, cx, cy, s):
        for k in self.known:
            if abs(k[0] - cx) < self.SAME_SPOT * s and abs(k[1] - cy) < self.SAME_SPOT * s * 0.5:
                return k
        return None

    def step(self, frame, now):
        s = frame.shape[0] / 1152
        self.known = [k for k in self.known if now - k[2] < self.FORGET_S]
        new = []
        for box in find_labels(frame):
            cx, cy = box[0] + box[2] / 2, box[1] + box[3] / 2
            k = self._match(cx, cy, s)
            if k is not None:
                k[0], k[1], k[2] = cx, cy, now
                continue
            text = label_text(frame, box)
            item = is_item_name(text)
            self.known.append([cx, cy, now, item])
            if item:
                new.append((text, box))
        return new

    def run(self):
        while True:
            time.sleep(self.INTERVAL_S)
            if not self.enabled or self.state.ui_busy():
                continue
            try:
                frame = self.capture.grab()
                if frame is None:
                    continue
                for text, _box in self.step(frame, time.time()):
                    self.found += 1
                    self.on_drop(text)
            except Exception:
                errlog.report("loot_watcher", "legendary label reader failed")

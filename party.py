"""Party mode: the whole screen glows in wild, changing patterns and colours, with music if you like.

A see-through window over all monitors that lets every click through (the game and the tracker stay usable).
Patterns change every few seconds. Colours may jump, but never more than FLASH_HZ times a second: faster
flashing light can trigger seizures (the usual safety limit is 3 flashes a second).

Music, looped while the party runs: data/party.mp3 (copyright-free, ships with the tracker) or a .mp3 / .wav of
your own from the tracker's "sounds" folder, which stays on your PC.
"""
import colorsys
import ctypes
import math
import random
import time
import tkinter as tk

import sound

ALPHA = 0.5           # how strongly the colours cover the screen
FPS = 30
PATTERN_S = 5.0       # each pattern this long
HUE_SPEED = 0.25      # colour wheel turns per second
FLASH_HZ = 3          # colour jumps per second at most - never faster (seizure safety)


def _rgb(h, s=1.0, v=1.0) -> str:
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, s, v)
    return "#%02x%02x%02x" % (int(r * 255), int(g * 255), int(b * 255))


def _virtual_screen():
    """(x, y, w, h) of all monitors together."""
    u = ctypes.windll.user32
    return u.GetSystemMetrics(76), u.GetSystemMetrics(77), u.GetSystemMetrics(78), u.GetSystemMetrics(79)


class Party:
    def __init__(self, root):
        self.root = root
        self.win = None
        self.job = None

    @property
    def on(self) -> bool:
        return self.win is not None

    def start(self, music: str | None = None):
        if self.win is not None:
            return
        x, y, w, h = _virtual_screen()
        self.w, self.h = w, h
        win = tk.Toplevel(self.root)
        win.overrideredirect(True)
        win.geometry(f"{w}x{h}+{x}+{y}")
        win.attributes("-topmost", True)
        win.attributes("-alpha", ALPHA)
        win.configure(bg="black")
        self.c = tk.Canvas(win, width=w, height=h, bg="black", highlightthickness=0, bd=0)
        self.c.pack()
        self.win = win
        win.update_idletasks()
        try:  # click-through, no taskbar button
            u = ctypes.windll.user32
            hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
            ex = u.GetWindowLongW(hwnd, -20)
            u.SetWindowLongW(hwnd, -20, ex | 0x80000 | 0x20 | 0x80)  # LAYERED | TRANSPARENT | TOOLWINDOW
        except Exception:
            pass
        if music:
            sound.loop(music)
        self.t0 = time.time()
        self.pattern = None
        self._tick()

    def stop(self):
        if self.job:
            self.root.after_cancel(self.job)
            self.job = None
        if self.win is not None:
            self.win.destroy()
            self.win = None
            sound.stop_loop()

    # -- animation -------------------------------------------------------------
    PATTERNS = ("rings", "strobe", "stripes", "sunburst", "bubbles", "plasma")

    def _tick(self):
        if self.win is None:
            return
        t = time.time() - self.t0
        name = self.PATTERNS[int(t // PATTERN_S) % len(self.PATTERNS)]
        if name != self.pattern:
            self.c.delete("all")
            self.pattern = name
            getattr(self, f"_setup_{name}")()
        try:
            getattr(self, f"_draw_{name}")(t)
        except tk.TclError:
            return
        self.job = self.root.after(int(1000 / FPS), self._tick)

    @staticmethod
    def _beat(t) -> int:
        """Counts up FLASH_HZ times a second: colour jumps happen only when it changes."""
        return int(t * FLASH_HZ)

    # rings: colour circles racing outward from the middle
    def _setup_rings(self):
        self.n_rings = 22
        r_max = math.hypot(self.w, self.h) / 2
        cx, cy = self.w / 2, self.h / 2
        self.items = []
        for i in range(self.n_rings):
            r = r_max * (self.n_rings - i) / self.n_rings
            self.items.append(self.c.create_oval(cx - r, cy - r, cx + r, cy + r, width=0, fill="black"))

    def _draw_rings(self, t):
        for i, it in enumerate(self.items):
            self.c.itemconfigure(it, fill=_rgb(t * HUE_SPEED + i / self.n_rings * 2 - t * 0.8))

    # strobe: a grid of tiles jumping to new colours on every beat (FLASH_HZ)
    def _setup_strobe(self):
        self.cols, self.rows = 8, 5
        tw, th = self.w / self.cols, self.h / self.rows
        self.items = [self.c.create_rectangle(i * tw, j * th, (i + 1) * tw + 1, (j + 1) * th + 1, width=0,
                                              fill="black") for j in range(self.rows) for i in range(self.cols)]
        self.last_beat = None

    def _draw_strobe(self, t):
        beat = self._beat(t)
        if beat == self.last_beat:
            return
        self.last_beat = beat
        for it in self.items:
            self.c.itemconfigure(it, fill=_rgb(random.random(), 1.0, random.choice((0.55, 1.0))))

    # stripes: wide diagonal bands rushing across
    def _setup_stripes(self):
        self.band = max(self.w, self.h) / 12
        n = int((self.w + self.h) / self.band) + 3
        self.items = [self.c.create_polygon(0, 0, 0, 0, 0, 0, 0, 0, width=0, fill="black") for _ in range(n)]

    def _draw_stripes(self, t):
        b, h = self.band, self.h
        shift = (t * 420) % b
        for i, it in enumerate(self.items):
            x = (i - 1) * b + shift - h
            self.c.coords(it, x, 0, x + b, 0, x + b + h, h, x + h, h)
            self.c.itemconfigure(it, fill=_rgb(t * HUE_SPEED + i * 0.13))

    # sunburst: wedges spinning around the middle
    def _setup_sunburst(self):
        self.n_wedges = 16
        r = math.hypot(self.w, self.h)
        cx, cy = self.w / 2, self.h / 2
        self.box = (cx - r, cy - r, cx + r, cy + r)
        self.items = [self.c.create_arc(*self.box, start=0, extent=360 / self.n_wedges, width=0, fill="black",
                                        style="pieslice") for _ in range(self.n_wedges)]

    def _draw_sunburst(self, t):
        step = 360 / self.n_wedges
        spin = (t * 140) % 360
        for i, it in enumerate(self.items):
            self.c.itemconfigure(it, start=spin + i * step,
                                 fill=_rgb(t * HUE_SPEED + i / self.n_wedges, 1.0, 1.0 if i % 2 else 0.6))

    # bubbles: glowing discs flying around and bouncing off the edges
    def _setup_bubbles(self):
        self.balls = []
        for _ in range(45):
            r = random.uniform(0.03, 0.12) * min(self.w, self.h)
            x, y = random.uniform(r, self.w - r), random.uniform(r, self.h - r)
            a = random.uniform(0, 2 * math.pi)
            sp = random.uniform(220, 600)
            it = self.c.create_oval(x - r, y - r, x + r, y + r, width=0, fill="black")
            self.balls.append([it, x, y, r, math.cos(a) * sp, math.sin(a) * sp, random.random()])
        self.last = time.time()

    def _draw_bubbles(self, t):
        now = time.time()
        dt, self.last = min(now - self.last, 0.1), now
        for b in self.balls:
            it, x, y, r, vx, vy, hue = b
            x, y = x + vx * dt, y + vy * dt
            if not r <= x <= self.w - r:
                vx = -vx
            if not r <= y <= self.h - r:
                vy = -vy
            b[1:6] = [x, y, r, vx, vy]
            self.c.coords(it, x - r, y - r, x + r, y + r)
            self.c.itemconfigure(it, fill=_rgb(hue + t * HUE_SPEED * 2))

    # plasma: a grid of tiles whose colours roll in fast waves
    def _setup_plasma(self):
        self.cols, self.rows = 20, 11
        tw, th = self.w / self.cols, self.h / self.rows
        self.items = [[self.c.create_rectangle(i * tw, j * th, (i + 1) * tw + 1, (j + 1) * th + 1, width=0,
                                               fill="black") for i in range(self.cols)] for j in range(self.rows)]

    def _draw_plasma(self, t):
        for j, row in enumerate(self.items):
            for i, it in enumerate(row):
                v = (math.sin(i * 0.45 + t * 2.4) + math.sin(j * 0.6 - t * 1.9)
                     + math.sin((i + j) * 0.3 + t * 1.3)) / 6
                self.c.itemconfigure(it, fill=_rgb(t * HUE_SPEED + v, 1.0, 0.7 + 0.3 * math.sin(v * 6 + t * 2)))

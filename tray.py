"""Tray icon (the small icons at the right of the taskbar) for "minimize to tray", and the tracker's own icon.

The icon is drawn here - gold bars on a dark disc in the tracker's colours - so the tracker never looks like the
game in the taskbar or the tray. The tray runs in its own thread (pystray); its clicks are handed to the Tk
thread through the callbacks, which must only queue work.
"""
import threading

from PIL import Image, ImageDraw

import errlog

GOLD = (232, 162, 58, 255)
IRON = (27, 29, 34, 255)
LINE = (58, 63, 74, 255)


def icon_image(size: int = 64) -> Image.Image:
    """The tracker's icon: three rising gold bars on a dark disc with a gold ring."""
    s = size * 4  # drawn large, scaled down smoothly
    im = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.ellipse((2, 2, s - 3, s - 3), fill=IRON, outline=GOLD, width=max(s // 22, 2))
    base, w, gap = s * 0.70, s * 0.13, s * 0.06
    x0 = s / 2 - (3 * w + 2 * gap) / 2
    for i, h in enumerate((0.22, 0.34, 0.46)):
        x = x0 + i * (w + gap)
        d.rounded_rectangle((x, base - s * h, x + w, base), radius=s * 0.02, fill=GOLD)
    d.line((s * 0.24, base + s * 0.05, s * 0.76, base + s * 0.05), fill=LINE, width=max(s // 40, 2))
    return im.resize((size, size), Image.LANCZOS)


class Tray:
    def __init__(self, title: str, on_show, on_quit):
        self.title, self.on_show, self.on_quit = title, on_show, on_quit
        self.icon = None
        self.lock = threading.Lock()

    def available(self) -> bool:
        try:
            import pystray  # noqa: F401
            return True
        except Exception:
            return False

    def show(self) -> bool:
        with self.lock:
            if self.icon is not None:
                return True
            try:
                import pystray
                menu = pystray.Menu(pystray.MenuItem("Show Deskrawl Tracker", lambda *_: self.on_show(), default=True),
                                    pystray.MenuItem("Exit", lambda *_: self.on_quit()))
                self.icon = pystray.Icon("DeskrawlTracker", icon_image(64), self.title, menu)
                self.icon.run_detached()
                return True
            except Exception:
                errlog.report("tray", "tray icon could not be shown")
                self.icon = None
                return False

    def hide(self):
        with self.lock:
            if self.icon is not None:
                try:
                    self.icon.stop()
                except Exception:
                    pass
                self.icon = None

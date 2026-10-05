"""Look and feel of the tracker: palette, type, sidebar navigation, tooltips, tables.

Palette taken from the game's own UI: iron-grey panels, parchment text, coin gold as the one
accent, and the game's rarity colours reserved for items.
"""
import re
import tkinter as tk
from tkinter import ttk

# ----------------------------------------------------------------------------- tokens
BG = "#1b1d22"        # iron
PANEL = "#24272e"     # card surface
RAISED = "#2e323b"    # hover / selected / inputs
LINE = "#3a3f4a"      # borders, track of bars
FG = "#ece7dc"        # parchment text
MUTED = "#9a958a"     # secondary text
ACCENT = "#e8a23a"    # coin gold
EMBER = "#c8521f"     # start of the level bar gradient
GOOD = "#7bc47f"
BAD = "#e0645c"
WARN = "#d9b25c"
RARITY = {"Common": "#d8d4cc", "Uncommon": "#6ea8ff", "Rare": "#f2d16b", "Legendary": "#ff9a3c",
          "Divine": "#e5e0ff", "Gem": "#7dcfff"}

F_BODY = ("Segoe UI", 10)
F_SMALL = ("Segoe UI", 9)
F_LABEL = ("Segoe UI Semibold", 9)
F_HEAD = ("Bahnschrift SemiBold", 15)
F_NUM = ("Bahnschrift SemiBold", 20)
F_NUM_M = ("Bahnschrift SemiBold", 15)
F_NUM_XL = ("Bahnschrift SemiBold", 28)
F_NAV = ("Bahnschrift", 11)


def apply_style(root):
    st = ttk.Style(root)
    st.theme_use("clam")
    st.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, rowheight=26,
                 borderwidth=0, font=F_BODY)
    st.configure("Treeview.Heading", background=BG, foreground=MUTED, relief="flat", font=("Segoe UI Semibold", 9),
                 padding=(6, 4))
    st.map("Treeview.Heading", background=[("active", BG)])
    st.map("Treeview", background=[("selected", RAISED)], foreground=[("selected", FG)])
    st.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])  # no frame border
    st.configure("Icons.Treeview", rowheight=36)
    st.layout("Icons.Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    st.configure("Vertical.TScrollbar", background=RAISED, troughcolor=PANEL, borderwidth=0, arrowcolor=MUTED,
                 gripcount=0, arrowsize=10)
    st.map("Vertical.TScrollbar", background=[("active", LINE)])
    st.configure("TCombobox", fieldbackground=RAISED, background=RAISED, foreground=FG, arrowcolor=ACCENT,
                 borderwidth=0, padding=4)
    st.map("TCombobox", fieldbackground=[("readonly", RAISED)], foreground=[("readonly", FG)],
           selectbackground=[("readonly", RAISED)], selectforeground=[("readonly", FG)])
    root.option_add("*TCombobox*Listbox.background", RAISED)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", BG)
    root.option_add("*TCombobox*Listbox.font", F_BODY)


# ----------------------------------------------------------------------------- tooltip / info

class Tooltip:
    """Hover help. Closes itself when the pointer is no longer over the widget - Windows does not
    always send <Leave> (window minimized, other window activated), which left tooltips behind."""

    def __init__(self, widget, text, wrap=360):
        self.widget, self.text, self.wrap = widget, text, wrap
        self.tip = None
        self.after = None
        widget.bind("<Enter>", self._schedule, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<Unmap>", self._hide, add="+")
        widget.bind("<Destroy>", self._hide, add="+")

    def _schedule(self, _):
        if self.after:
            self.widget.after_cancel(self.after)
        self.after = self.widget.after(250, self._show)

    def _pointer_inside(self) -> bool:
        try:
            if not self.widget.winfo_viewable():
                return False
            x, y = self.widget.winfo_pointerxy()
            w = self.widget.winfo_containing(x, y)
            while w is not None:
                if w is self.widget:
                    return True
                w = w.master
        except Exception:
            pass
        return False

    def _watch(self):
        if self.tip is None:
            return
        if not self._pointer_inside():
            self._hide(None)
        else:
            self.widget.after(200, self._watch)

    def _show(self):
        self.after = None
        if self.tip is not None or not self._pointer_inside():
            return
        text = self.text() if callable(self.text) else self.text
        if not text:
            return
        x = self.widget.winfo_rootx() + 12
        y = self.widget.winfo_rooty() + self.widget.winfo_height() + 4
        self.tip = tk.Toplevel(self.widget)
        self.tip.overrideredirect(True)
        self.tip.attributes("-topmost", True)
        self.tip.configure(bg=LINE)
        tk.Label(self.tip, text=text, bg=RAISED, fg=FG, font=F_SMALL, justify="left", wraplength=self.wrap,
                 padx=10, pady=7).pack(padx=1, pady=1)
        self.tip.geometry(f"+{x}+{y}")
        self.widget.after(200, self._watch)

    def _hide(self, _):
        if self.after:
            try:
                self.widget.after_cancel(self.after)
            except Exception:
                pass
            self.after = None
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


def info(parent, text, bg=BG):
    """Small ⓘ that explains on hover - keeps explanations available without filling the screen."""
    lab = tk.Label(parent, text="ⓘ", bg=bg, fg=MUTED, font=("Segoe UI Symbol", 11), cursor="question_arrow")
    Tooltip(lab, text)
    return lab


def page_header(parent, title, info_text=None):
    """Title row of a page; returns the frame (put controls into it with side='right')."""
    row = tk.Frame(parent, bg=BG)
    row.pack(fill="x", padx=14, pady=(12, 6))
    tk.Label(row, text=title, bg=BG, fg=FG, font=F_HEAD).pack(side="left")
    if info_text:
        info(row, info_text).pack(side="left", padx=(6, 0), pady=(3, 0))
    return row


def button(parent, text, cmd, accent=False, small=False):
    bg, fg = (ACCENT, BG) if accent else (RAISED, FG)
    b = tk.Label(parent, text=text, bg=bg, fg=fg, font=F_SMALL if small else F_LABEL, padx=10, pady=4,
                 cursor="hand2")
    b.bind("<Button-1>", lambda e: cmd())
    b.bind("<Enter>", lambda e: b.configure(bg=ACCENT if accent else LINE))
    b.bind("<Leave>", lambda e: b.configure(bg=bg))
    return b


def card(parent, **pack):
    f = tk.Frame(parent, bg=PANEL, highlightthickness=0)
    if pack:
        f.pack(**pack)
    return f


# ----------------------------------------------------------------------------- table with zebra rows

def autowrap(label, pad=8):
    """Wrap a label's text at its current width (labels otherwise get cut off in narrow windows)."""
    label.bind("<Configure>", lambda e: label.configure(wraplength=max(e.width - pad, 80)), add="+")
    return label


_RE_THOUSANDS = re.compile(r"^-?\d{1,3}(\.\d{3})+$")


def sort_key(text):
    """Sort value of a cell: numbers (+8.1 %, 47.0k, 16.09M ★, 1.200, 1:32) before text, '-' last."""
    t = str(text).replace("★", "").replace("≈", "").replace("%", "").strip()
    if t in ("", "-", "?"):
        return (2, 0, "")
    m = re.fullmatch(r"(\d+):(\d{2})(?::(\d{2}))?", t)
    if m:
        a, b, c = m.groups()
        return (0, int(a) * 3600 + int(b) * 60 + int(c) if c else int(a) * 60 + int(b), "")
    if _RE_THOUSANDS.match(t):
        return (0, float(t.replace(".", "")), "")
    m = re.fullmatch(r"([+-]?\d+(?:[.,]\d+)?)\s*([kMB]?)", t)
    if m:
        v = float(m.group(1).replace(",", "."))
        return (0, v * {"": 1, "k": 1e3, "M": 1e6, "B": 1e9}[m.group(2)], "")
    m = re.match(r"([+-]?\d+(?:[.,]\d+)?)", t)  # "5/10", "4/10" etc.
    if m:
        return (0, float(m.group(1).replace(",", ".")), t.lower())
    return (1, 0, t.lower())


class ZTree(ttk.Treeview):
    """Treeview with zebra rows, columns that fit the visible width, and click-to-sort headers."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self._base = None
        self._sort = None  # (column, descending)
        self._titles = {}
        self._pending = None
        self.bind("<Configure>", self._fit, add="+")
        self.tag_configure("odd", background="#272a32")
        self.tag_configure("good", foreground=GOOD)
        self.tag_configure("bad", foreground=BAD)
        self.tag_configure("meh", foreground=MUTED)

    def heading(self, column, option=None, **kw):
        if "text" in kw and column != "#0":
            self._titles[column] = kw["text"]
            kw.setdefault("command", lambda c=column: self.sort_by(c))
        return super().heading(column, option, **kw)

    def sort_by(self, column, toggle=True):
        if toggle:
            desc = not self._sort[1] if self._sort and self._sort[0] == column else True
            self._sort = (column, desc)
        self._apply_sort()

    def _apply_sort(self):
        self._pending = None
        if not self._sort:
            return
        col, desc = self._sort
        rows = list(self.get_children(""))
        def key(i):
            k = sort_key(self.set(i, col))
            return (k[0], -k[1], k[2]) if desc and k[0] == 0 else k
        for idx, iid in enumerate(sorted(rows, key=key)):
            self.move(iid, "", idx)
            tags = tuple(t for t in self.item(iid, "tags") if t != "odd")
            self.item(iid, tags=tags + (("odd",) if idx % 2 else ()))
        for c, title in self._titles.items():
            arrow = ("▼ " if desc else "▲ ") if c == col else ""
            super().heading(c, text=arrow + title)

    def _fit(self, e):
        cols = self["columns"]
        if self._base is None:
            self._base = [max(self.column(c, "width"), 30) for c in cols]
        total = sum(self._base)
        if e.width < 50 or not total:
            return
        tree_col = self.column("#0", "width") if "tree" in str(self["show"]) else 0
        scale = (e.width - tree_col - 12) / total  # 12 px breathing room on the right
        for c, b in zip(cols, self._base):
            self.column(c, width=max(int(b * scale), 28), minwidth=20)

    def insert(self, parent, index, iid=None, **kw):
        n = len(self.get_children(parent))
        tags = tuple(kw.pop("tags", ()) or ())
        if isinstance(tags, str):
            tags = (tags,)
        if n % 2:
            tags = tags + ("odd",)
        r = super().insert(parent, index, iid=iid, tags=tags, **kw)
        if self._sort and self._pending is None:  # keep the chosen order when rows are refreshed
            self._pending = self.after_idle(self._apply_sort)
        return r


# ----------------------------------------------------------------------------- sidebar navigation

class SideNav(tk.Frame):
    """Sidebar + page area. Same calls as ttk.Notebook (add/select/tab), so pages are plain frames
    created with this widget as parent."""

    def __init__(self, master):
        super().__init__(master, bg=BG)
        self.side = tk.Frame(self, bg=PANEL, width=138)
        self.side.grid(row=0, column=0, sticky="ns")
        self.side.grid_propagate(False)
        self.nav = tk.Frame(self.side, bg=PANEL)
        self.nav.pack(fill="x", pady=(8, 0))
        self.bottom = tk.Frame(self.side, bg=PANEL)  # controls live here
        self.bottom.pack(side="bottom", fill="x", pady=8)
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)
        self.pages = {}
        self.current = None

    def add(self, page, text=""):
        page.grid(row=0, column=1, sticky="nsew")
        row = tk.Frame(self.nav, bg=PANEL, cursor="hand2")
        row.pack(fill="x")
        bar = tk.Frame(row, bg=PANEL, width=3)
        bar.pack(side="left", fill="y")
        lab = tk.Label(row, text=text, bg=PANEL, fg=MUTED, font=F_NAV, anchor="w", padx=12, pady=7)
        lab.pack(side="left", fill="x", expand=True)
        for w in (row, lab):
            w.bind("<Button-1>", lambda e, p=page: self.select(p))
            w.bind("<Enter>", lambda e, p=page: self._hover(p, True))
            w.bind("<Leave>", lambda e, p=page: self._hover(p, False))
        self.pages[page] = (row, bar, lab)
        if self.current is None:
            self.select(page)

    def _hover(self, page, on):
        if page is not self.current:
            row, bar, lab = self.pages[page]
            for w in (row, lab):
                w.configure(bg=RAISED if on else PANEL)

    def select(self, page):
        self.current = page
        page.tkraise()
        for p, (row, bar, lab) in self.pages.items():
            sel = p is page
            row.configure(bg=RAISED if sel else PANEL)
            lab.configure(bg=RAISED if sel else PANEL, fg=FG if sel else MUTED)
            bar.configure(bg=ACCENT if sel else PANEL)

    def tab(self, page, text=None):
        if text is not None:
            self.pages[page][2].configure(text=text)


class StatusDot(tk.Frame):
    """● label - colour shows state, hover shows the full status text."""

    def __init__(self, parent, label, bg=BG):
        super().__init__(parent, bg=bg)
        self.dot = tk.Label(self, text="●", bg=bg, fg=MUTED, font=("Segoe UI", 10))
        self.dot.pack(side="left")
        tk.Label(self, text=label, bg=bg, fg=MUTED, font=F_SMALL).pack(side="left", padx=(2, 0))
        self.detail = ""
        Tooltip(self, lambda: self.detail)

    def set(self, state, detail):
        self.dot.configure(fg={"ok": GOOD, "warn": WARN, "bad": BAD}.get(state, MUTED))
        self.detail = detail


class ScrollFrame(tk.Frame):
    """Vertically scrolling page: put content into .inner. Scrollbar only while needed,
    mouse wheel works while the pointer is over the page."""

    def __init__(self, master, bg=BG):
        super().__init__(master, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, highlightthickness=0, bd=0)
        self.sb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.inner = tk.Frame(self.canvas, bg=bg)
        self._win = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.canvas.configure(yscrollcommand=self._yset)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.bind_all("<MouseWheel>", self._wheel, add="+")

    def _yset(self, first, last):
        self.sb.set(first, last)
        if float(first) <= 0 and float(last) >= 1:
            self.sb.pack_forget()
        elif not self.sb.winfo_ismapped():
            self.sb.pack(side="right", fill="y", before=self.canvas)

    def _wheel(self, e):
        try:
            w = self.winfo_containing(e.x_root, e.y_root)
        except Exception:
            return
        # let tables scroll themselves; scroll the page everywhere else on it
        while w is not None and w is not self:
            if isinstance(w, ttk.Treeview) and tuple(round(x, 3) for x in w.yview()) != (0.0, 1.0):
                return  # table with its own scrolling
            w = w.master
        if w is self and self.sb.winfo_ismapped():
            self.canvas.yview_scroll(int(-e.delta / 120), "units")

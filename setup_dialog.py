"""First-run setup: find the game's log file and check that Windows can read English text.

Shown on the first start (and from "Change log file"). Everything it decides goes into the config:
  log_path    - full path of Game.log
  setup_done  - True once the player confirmed
"""
import os
import tkinter as tk
from tkinter import filedialog

import paths
import ui_theme as ui
from ui_theme import BG, PANEL, RAISED, FG, MUTED

OCR_COMMAND = 'Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"'


def ocr_languages() -> list:
    """Languages Windows' own text recognition can read (empty if it cannot be asked)."""
    try:
        from winrt.windows.media.ocr import OcrEngine
        return [l.language_tag for l in OcrEngine.available_recognizer_languages]
    except Exception:
        return []


def english_ocr_ok() -> bool:
    return any(t.lower().startswith("en") for t in ocr_languages())


def needs_setup(cfg: dict) -> bool:
    return not cfg.get("setup_done") or not os.path.isfile(cfg.get("log_path") or paths.DEFAULT_LOG)


class SetupDialog(tk.Toplevel):
    """Modal window. on_done(log_path) is called when the player confirms."""

    def __init__(self, root, cfg: dict, on_done, first_run=True):
        super().__init__(root, bg=BG)
        self.cfg, self.on_done = cfg, on_done
        self.title("Deskrawl Tracker – Setup")
        self.resizable(False, False)
        self.transient(root)
        self.protocol("WM_DELETE_WINDOW", self._close)
        self.var_path = tk.StringVar(value=cfg.get("log_path") or paths.DEFAULT_LOG)

        pad = dict(padx=18)
        tk.Label(self, text="Setup" if first_run else "Log file", bg=BG, fg=FG, font=ui.F_HEAD,
                 anchor="w").pack(fill="x", pady=(16, 2), **pad)
        tk.Label(self, text="The tracker reads Deskrawl's log file and the game picture. "
                            "This checks that both work.", bg=BG, fg=MUTED, font=ui.F_SMALL,
                 anchor="w", justify="left", wraplength=500).pack(fill="x", **pad)

        # --- log file
        c = ui.card(self, fill="x", pady=(14, 0), **pad)
        tk.Label(c, text="1  Deskrawl log file (Game.log)", bg=PANEL, fg=FG, font=ui.F_LABEL,
                 anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        row = tk.Frame(c, bg=PANEL)
        row.pack(fill="x", padx=12)
        e = tk.Entry(row, textvariable=self.var_path, bg=RAISED, fg=FG, insertbackground=FG, relief="flat",
                     font=ui.F_SMALL, width=58)
        e.pack(side="left", fill="x", expand=True, ipady=4)
        ui.button(row, "Browse…", self._browse, small=True).pack(side="left", padx=(6, 0))
        self.lbl_log = tk.Label(c, text="", bg=PANEL, font=ui.F_SMALL, anchor="w", justify="left", wraplength=500)
        self.lbl_log.pack(fill="x", padx=12, pady=(6, 10))
        self.var_path.trace_add("write", lambda *_: self._check())

        # --- OCR language
        c = ui.card(self, fill="x", pady=(10, 0), **pad)
        tk.Label(c, text="2  Windows text recognition (English)", bg=PANEL, fg=FG, font=ui.F_LABEL,
                 anchor="w").pack(fill="x", padx=12, pady=(10, 4))
        self.lbl_ocr = tk.Label(c, text="", bg=PANEL, font=ui.F_SMALL, anchor="w", justify="left", wraplength=500)
        self.lbl_ocr.pack(fill="x", padx=12)
        self.ocr_help = tk.Frame(c, bg=PANEL)
        tk.Label(self.ocr_help, text="Open PowerShell as administrator, run this command, "
                                     "then restart the tracker:", bg=PANEL, fg=MUTED, font=ui.F_SMALL,
                 anchor="w", justify="left", wraplength=500).pack(fill="x")
        cmd_row = tk.Frame(self.ocr_help, bg=PANEL)
        cmd_row.pack(fill="x", pady=(4, 0))
        tk.Label(cmd_row, text=OCR_COMMAND, bg=RAISED, fg=FG, font=("Consolas", 9), anchor="w",
                 padx=6, pady=4).pack(side="left", fill="x", expand=True)
        ui.button(cmd_row, "Copy", self._copy_cmd, small=True).pack(side="left", padx=(6, 0))
        tk.Frame(c, bg=PANEL, height=10).pack()

        # --- next steps
        c = ui.card(self, fill="x", pady=(10, 0), **pad)
        tk.Label(c, text="3  Then in the game", bg=PANEL, fg=FG, font=ui.F_LABEL, anchor="w").pack(
            fill="x", padx=12, pady=(10, 4))
        tk.Label(c, text="• Open the character window, press F9 at the top of the Attributes list, scroll down, "
                         "press F9 again.\n"
                         "• Hover your equipped weapon and press F8 – the tracker remembers its damage.\n"
                         "• F10 hides Deskrawl without minimizing it (a minimized game cannot be read).",
                 bg=PANEL, fg=MUTED, font=ui.F_SMALL, anchor="w", justify="left").pack(fill="x", padx=12, pady=(0, 10))

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", pady=16, **pad)
        self.btn_ok = ui.button(bar, "Done", self._done, accent=True)
        self.btn_ok.pack(side="right")
        if first_run:
            ui.button(bar, "Later", self._close).pack(side="right", padx=6)

        self._check()
        self.update_idletasks()
        x = root.winfo_rootx() + max((root.winfo_width() - self.winfo_width()) // 2, 0)
        y = root.winfo_rooty() + 60
        self.geometry(f"+{x}+{y}")
        self.grab_set()
        self.focus_force()

    def _browse(self):
        cur = self.var_path.get()
        start = os.path.dirname(cur) if os.path.isdir(os.path.dirname(cur)) else os.path.expanduser("~")
        p = filedialog.askopenfilename(parent=self, title="Select Deskrawl's Game.log", initialdir=start,
                                       filetypes=[("Deskrawl Log", "Game.log"), ("Log files", "*.log"),
                                                  ("All files", "*.*")])
        if p:
            self.var_path.set(os.path.normpath(p))

    def _check(self):
        p = self.var_path.get().strip()
        if os.path.isfile(p):
            size = os.path.getsize(p)
            self.lbl_log.configure(text=f"✓ found ({size / 1024:.0f} KB)", fg=ui.GOOD)
        elif os.path.isdir(os.path.dirname(p)):
            self.lbl_log.configure(text="The folder exists but has no Game.log. Start Deskrawl once "
                                        "and the game creates it.", fg=ui.WARN)
        else:
            self.lbl_log.configure(text="Not found. Use “Browse…” to select Game.log. "
                                        "It is usually in "
                                        "%USERPROFILE%\\AppData\\LocalLow\\First Day Games\\Deskrawl.", fg=ui.BAD)
        langs = ocr_languages()
        if english_ocr_ok():
            self.lbl_ocr.configure(text="✓ installed", fg=ui.GOOD)
            self.ocr_help.pack_forget()
        else:
            have = ", ".join(langs) if langs else "none"
            self.lbl_ocr.configure(text=f"Missing (installed: {have}). Without it item comparison, sales and drops "
                                        "do not work – EXP/h and Gold/h from the log still do.",
                                   fg=ui.WARN)
            self.ocr_help.pack(fill="x", padx=12, pady=(6, 0))

    def _copy_cmd(self):
        self.clipboard_clear()
        self.clipboard_append(OCR_COMMAND)

    def _done(self):
        p = self.var_path.get().strip()
        self.cfg["log_path"] = p
        self.cfg["setup_done"] = True
        self.grab_release()
        self.destroy()
        self.on_done(p)

    def _close(self):
        self.grab_release()
        self.destroy()

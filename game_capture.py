"""Capture the Deskrawl window directly (works while it is covered by other windows).

Uses PrintWindow with PW_RENDERFULLCONTENT, which asks DWM for the window's own surface instead
of copying pixels from the screen. Only a minimized window cannot be captured (it is not rendered).
"""
import ctypes
import ctypes.wintypes as W

import numpy as np

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

user32.FindWindowW.restype = W.HWND
user32.GetDC.restype = W.HDC
gdi32.CreateCompatibleDC.restype = W.HDC
gdi32.CreateCompatibleBitmap.restype = W.HBITMAP
gdi32.SelectObject.restype = W.HGDIOBJ
for fn, args in [(user32.PrintWindow, [W.HWND, W.HDC, W.UINT]),
                 (user32.GetDC, [W.HWND]),
                 (user32.ReleaseDC, [W.HWND, W.HDC]),
                 (gdi32.CreateCompatibleDC, [W.HDC]),
                 (gdi32.CreateCompatibleBitmap, [W.HDC, ctypes.c_int, ctypes.c_int]),
                 (gdi32.SelectObject, [W.HDC, W.HGDIOBJ]),
                 (gdi32.DeleteObject, [W.HGDIOBJ]),
                 (gdi32.DeleteDC, [W.HDC]),
                 (gdi32.GetDIBits, [W.HDC, W.HBITMAP, W.UINT, W.UINT, ctypes.c_void_p, ctypes.c_void_p, W.UINT])]:
    fn.argtypes = args

PW_CLIENTONLY_RENDERFULLCONTENT = 0x1 | 0x2


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", W.DWORD), ("biWidth", W.LONG), ("biHeight", W.LONG), ("biPlanes", W.WORD),
                ("biBitCount", W.WORD), ("biCompression", W.DWORD), ("biSizeImage", W.DWORD),
                ("biXPelsPerMeter", W.LONG), ("biYPelsPerMeter", W.LONG), ("biClrUsed", W.DWORD),
                ("biClrImportant", W.DWORD)]


class GameCapture:
    def __init__(self, title: str = "Deskrawl", wnd_class: str = "UnityWndClass"):
        self.title, self.wnd_class = title, wnd_class
        self.hwnd = None
        self.status = "game not found"

    def find(self):
        if self.hwnd and user32.IsWindow(self.hwnd):
            return self.hwnd
        self.hwnd = user32.FindWindowW(self.wnd_class, self.title)
        return self.hwnd

    def client_size(self):
        hwnd = self.find()
        if not hwnd:
            return None
        rc = W.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(rc))
        return rc.right, rc.bottom

    def grab(self):
        """Return the game's client area as a BGR numpy array, or None."""
        hwnd = self.find()
        if not hwnd:
            self.status = "game not found"
            return None
        if user32.IsIconic(hwnd):
            self.status = "game minimized"
            return None
        size = self.client_size()
        if not size or size[0] <= 0 or size[1] <= 0:
            return None
        w, h = size
        hdc = user32.GetDC(hwnd)
        mdc = gdi32.CreateCompatibleDC(hdc)
        bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
        old = gdi32.SelectObject(mdc, bmp)
        try:
            if not user32.PrintWindow(hwnd, mdc, PW_CLIENTONLY_RENDERFULLCONTENT):
                self.status = "PrintWindow fehlgeschlagen"
                return None
            bi = _BITMAPINFOHEADER(40, w, -h, 1, 32, 0, 0, 0, 0, 0, 0)
            buf = ctypes.create_string_buffer(w * h * 4)
            gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bi), 0)
            self.status = "game ok"
            return np.frombuffer(buf, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
        finally:
            gdi32.SelectObject(mdc, old)
            gdi32.DeleteObject(bmp)
            gdi32.DeleteDC(mdc)
            user32.ReleaseDC(hwnd, hdc)

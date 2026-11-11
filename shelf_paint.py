"""Present completed Tk window contents on Windows."""

from contextlib import contextmanager, ExitStack
import ctypes
from ctypes import wintypes
import sys
from _tkinter import DONT_WAIT, IDLE_EVENTS, TK_VERSION, WINDOW_EVENTS


class _Handle(ctypes.c_void_p):
    """Preserve pointer-sized Windows handles between native calls."""


if sys.platform == "win32":
    _user = ctypes.WinDLL("user32")
    _gdi = ctypes.WinDLL("gdi32")
    for function in (_user.CreateWindowExW, _user.GetDC, _user.GetForegroundWindow,
                     _gdi.CreateCompatibleDC, _gdi.CreateCompatibleBitmap, _gdi.SelectObject):
        function.restype = _Handle
    # Use the Tk DLL already loaded by this interpreter, including in the exe.
    _get_module = ctypes.WinDLL("kernel32").GetModuleHandleW
    _get_module.restype = wintypes.HMODULE
    _restrict_events = None
    for suffix in ("t", ""):
        name = f"tk{TK_VERSION.replace('.', '')}{suffix}.dll"
        if handle := _get_module(name):
            _restrict_events = ctypes.CDLL(name, handle=handle).Tk_RestrictEvents
            _restrict_events.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
            _restrict_events.restype = ctypes.c_void_p
            break

    @ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(ctypes.c_int))
    def _paint_events_only(_, event):
        # Tk XEvent types from Expose through ConfigureNotify. Defer user input.
        return int(12 <= event[0] <= 22)


def stage_window(window):
    if sys.platform == "win32":
        window.attributes("-alpha", 0)


def _finish_paint(window):
    window.update_idletasks()
    if not window.winfo_viewable():
        return
    # Mapped children receive native Expose events, which enqueue further idle
    # draws. Flush both queues while leaving clicks, closes and timers pending.
    if _restrict_events:
        previous_data = ctypes.c_void_p()
        previous = _restrict_events(_paint_events_only, None, ctypes.byref(previous_data))
        try:
            if not previous:
                while window.tk.dooneevent(WINDOW_EVENTS | IDLE_EVENTS | DONT_WAIT):
                    pass
        finally:
            _restrict_events(previous, previous_data, ctypes.byref(ctypes.c_void_p()))
    _gdi.GdiFlush()
    ctypes.windll.dwmapi.DwmFlush()


def show_window(window):
    if sys.platform == "win32":
        _finish_paint(window)
        window.attributes("-alpha", 1)


def _copy_client(owner, popup, origin, size):
    with ExitStack() as cleanup:
        dc = _user.GetDC(owner)
        if not dc:
            return False
        cleanup.callback(_user.ReleaseDC, owner, dc)
        memory = _gdi.CreateCompatibleDC(dc)
        if not memory:
            return False
        cleanup.callback(_gdi.DeleteDC, memory)
        bitmap = _gdi.CreateCompatibleBitmap(dc, size.cx, size.cy)
        if not bitmap:
            return False
        cleanup.callback(_gdi.DeleteObject, bitmap)
        previous = _gdi.SelectObject(memory, bitmap)
        if not previous:
            return False
        cleanup.callback(_gdi.SelectObject, memory, previous)
        # PW_CLIENTONLY | PW_RENDERFULLCONTENT copies only this window.
        if not _user.PrintWindow(owner, memory, 0x03):
            return False
        return _user.UpdateLayeredWindow(popup, None, ctypes.byref(origin), ctypes.byref(size),
                                         memory, ctypes.byref(wintypes.POINT()), 0, None, 0x04)  # ULW_OPAQUE


def _cover_client(window):
    owner = _Handle(int(window.tk.call("wm", "frame", window._w), 0))
    if _user.GetForegroundWindow().value != owner.value:
        return None
    rect, origin = wintypes.RECT(), wintypes.POINT()
    if not _user.GetClientRect(owner, ctypes.byref(rect)) or not _user.ClientToScreen(owner, ctypes.byref(origin)):
        return None
    size = wintypes.SIZE(rect.right, rect.bottom)
    # A layered, nonactivating, click-through tool window owned by the catalog.
    styles = 0x00080000 | 0x08000000 | 0x00000020 | 0x00000080
    popup = _user.CreateWindowExW(styles, "STATIC", None, 0x80000000, origin.x, origin.y,
                                  size.cx, size.cy, owner, None, None, None)
    if not popup:
        return None
    with ExitStack() as cleanup:
        cleanup.callback(_user.DestroyWindow, popup)
        if not _copy_client(owner, popup, origin, size) or _user.GetForegroundWindow().value != owner.value:
            return None
        _user.ShowWindow(popup, 4)  # SW_SHOWNOACTIVATE
        ctypes.windll.dwmapi.DwmFlush()
        cleanup.pop_all()
        return popup


@contextmanager
def keep_view_visible(window):
    """Keep the previous view on screen until Tk has painted the replacement."""
    if sys.platform != "win32" or not window.winfo_viewable():
        yield
        return
    popup = _cover_client(window)
    try:
        yield
        if popup:
            _finish_paint(window)
    finally:
        if popup:
            _user.DestroyWindow(popup)

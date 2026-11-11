"""Load catalog icons for disc images and locally downloaded Windows applications."""

from contextlib import ExitStack
import ctypes
from pathlib import Path
import struct
import sys
import tkinter as tk
import zlib

from fetch_sources import Recipe, filename
from fetch_store import read_state, stamp


ICON_SIZE = 16


class _Handle(ctypes.c_void_p):
    """Keep GDI handles pointer-sized when passing them back to Windows."""


if sys.platform == "win32":
    _gdi = ctypes.WinDLL("gdi32")
    for function in (_gdi.CreateCompatibleDC, _gdi.CreateDIBSection, _gdi.SelectObject):
        function.restype = _Handle


def _png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def _photo_from_backgrounds(master, black, white):
    # Comparing two backgrounds recovers coverage for both legacy AND-mask
    # icons and newer alpha-blended icons. PNG stores unpremultiplied RGBA.
    pixels = bytearray()
    for i in range(0, len(black), 4):
        if i % (ICON_SIZE * 4) == 0:
            pixels.append(0)  # PNG scanline with no filter
        blue, green, red = black[i:i + 3]
        alpha = min(255, 255 - white[i] + blue)
        if 0 < alpha < 255:
            red, green, blue = (min(255, round(channel * 255 / alpha)) for channel in (red, green, blue))
        pixels.extend((red, green, blue, alpha))
    header = struct.pack(">IIBBBBB", ICON_SIZE, ICON_SIZE, 8, 6, 0, 0, 0)
    png = (b"\x89PNG\r\n\x1a\n" + _png_chunk(b"IHDR", header)
           + _png_chunk(b"IDAT", zlib.compress(pixels)) + _png_chunk(b"IEND", b""))
    return tk.PhotoImage(master=master, data=png, format="png")


def _executable_icon(master, path):
    user = ctypes.windll.user32
    icon = ctypes.c_void_p()
    user.PrivateExtractIconsW(str(path), 0, ICON_SIZE, ICON_SIZE, ctypes.byref(icon), None, 1, 0)
    if not icon:
        return None
    with ExitStack() as cleanup:
        cleanup.callback(user.DestroyIcon, icon)
        dc = _gdi.CreateCompatibleDC(None)
        if not dc:
            return None
        cleanup.callback(_gdi.DeleteDC, dc)
        # BITMAPINFOHEADER for a top-down, uncompressed 32-bit DIB section.
        info = struct.pack("<IiiHHIIiiII", 40, ICON_SIZE, -ICON_SIZE, 1, 32, 0, 0, 0, 0, 0, 0)
        bits = ctypes.c_void_p()
        bitmap = _gdi.CreateDIBSection(dc, info, 0, ctypes.byref(bits), None, 0)
        if not bitmap:
            return None
        cleanup.callback(_gdi.DeleteObject, bitmap)
        previous = _gdi.SelectObject(dc, bitmap)
        if not previous:
            return None
        cleanup.callback(_gdi.SelectObject, dc, previous)
        size = ICON_SIZE * ICON_SIZE * 4
        backgrounds = []
        for fill in (0, 255):
            ctypes.memset(bits, fill, size)
            if not user.DrawIconEx(dc, 0, 0, icon, ICON_SIZE, ICON_SIZE, 0, None, 3):
                return None
            _gdi.GdiFlush()
            backgrounds.append(ctypes.string_at(bits, size))
        return _photo_from_backgrounds(master, *backgrounds)


def _disc_icon(master):
    return tk.PhotoImage(master=master, file=Path(__file__).parent / "assets" / "disc.png")


def load_app_icon(master, job, dest):
    if not isinstance(job, Recipe):
        return None
    try:
        if job.primary(job.archive or job.asset_pattern).lower().endswith(".iso"):
            return _disc_icon(master)
        if sys.platform != "win32" or dest.drive.startswith("\\\\"):
            return None
        root = job.cache_root(dest)
        state = read_state(root, "current.json") or read_state(root, "pending.json")
        if not state:
            return None
        primary = job.primary(filename(state.get("archive", job.archive)))
        if primary.lower().endswith(".iso"):
            return _disc_icon(master)
        candidates = []
        if primary.lower().endswith(".exe"):
            published = state.get("published", {})
            path = Path(published.get("path", ""))
            if path.is_absolute() and not path.drive.startswith("\\\\"):
                candidates.append((path, primary, published.get("stat")))
        members = dict.fromkeys([primary, *("files/" + name for name in job.extract)])
        for member in members:
            if member.lower().endswith(".exe"):
                path = root / "releases" / state["release"] / member
                candidates.append((path, member, state.get("stats", {}).get(member)))
        for path, member, recorded in candidates:
            # Use recorded file metadata here. Hashing changed executables on the
            # UI thread would make opening a large catalog unnecessarily slow.
            if state["sha256"].get(member) and path.is_file() and (not recorded or recorded == stamp(path)):
                icon = _executable_icon(master, path)
                if icon is not None:
                    return icon
    except (OSError, ValueError, TypeError, AttributeError, tk.TclError):
        pass  # A missing or unreadable local release can still be listed.
    return None

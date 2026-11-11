"""Themed 16- and 20-pixel command icons for Tk."""

from functools import lru_cache
from pathlib import Path
import struct
import tkinter as tk
from zlib import crc32


NAMES = ("library", "updates", "problems", "search", "add", "check",
         "download", "verify", "edit", "reload", "more", "close", "settings", "collapse", "expand",
         "system", "light", "dark", "language", "app_window", "hourglass", "save", "cancel",
         "window_close", "info", "folders", "list_checks", "diskette_x")


@lru_cache(maxsize=32)
def _icon_strip(color):
    # The indexed PNG stores line coverage in its transparency table. Tinting
    # its single palette preserves antialiasing against any background.
    data = (Path(__file__).parent / "assets" / "commands.png").read_bytes()
    start = data.index(b"PLTE")
    length = struct.unpack_from(">I", data, start - 4)[0]
    palette = b"PLTE" + bytes.fromhex(color.removeprefix("#")) * (length // 3)
    checksum = struct.pack(">I", crc32(palette))
    return data[:start] + palette + checksum + data[start + 4 + length + 4:]


def make_icon(master, name, color, size=16):
    y = {16: 0, 20: 16}[size]
    x = NAMES.index(name) * 20
    strip = tk.PhotoImage(master=master, data=_icon_strip(color), format="png")
    icon = tk.PhotoImage(master=master, width=size, height=size)
    icon.tk.call(str(icon), "copy", str(strip), "-from", x, y, x + size, y + size)
    return icon

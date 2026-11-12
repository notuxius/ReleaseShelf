"""Themed command and heading icons for Tk."""

from functools import lru_cache
from pathlib import Path
import re
import struct
import tkinter as tk
from zlib import crc32


NAMES = ("eye", "updates", "problems", "search", "add", "check",
         "download", "verify", "edit", "reload", "more", "close", "settings", "collapse", "expand",
         "system", "light", "dark", "language", "picture_frame", "hourglass", "save", "cancel",
         "window_close", "info", "folders", "list_checks", "diskette_x", "pause_circle", "folder",
         "folder_open", "tag", "file_x", "folder_x", "headphones", "code_xml", "monitor",
         "refresh_cw", "hard_drive", "gamepad", "cpu", "globe", "disc", "layers", "briefcase",
         "shield", "wrench", "package_minus", "paintbrush", "inbox", "tags")

CATEGORY_SYMBOLS = {
    "audio": "headphones", "video": "headphones", "development": "code_xml",
    "display": "monitor", "hdr": "monitor", "drivers": "refresh_cw", "updates": "refresh_cw",
    "files": "hard_drive", "storage": "hard_drive", "gaming": "gamepad", "games": "gamepad",
    "hardware": "cpu", "internet": "globe", "networking": "globe", "recovery": "disc",
    "setup": "layers", "virtualization": "layers", "productivity": "briefcase",
    "security": "shield", "utilities": "wrench", "tools": "wrench",
    "uninstallers": "package_minus", "customization": "paintbrush",
}


def category_icon(category):
    if not category:
        return "inbox"
    for word in re.findall(r"[^\W_]+", category.casefold()):
        if word in CATEGORY_SYMBOLS:
            return CATEGORY_SYMBOLS[word]
    return "folder"


@lru_cache(maxsize=32)
def tinted_strip(filename, color):
    # The indexed PNG stores line coverage in its transparency table. Tinting
    # its single palette preserves antialiasing against any background.
    data = (Path(__file__).parent / "assets" / filename).read_bytes()
    start = data.index(b"PLTE")
    length = struct.unpack_from(">I", data, start - 4)[0]
    palette = b"PLTE" + bytes.fromhex(color.removeprefix("#")) * (length // 3)
    checksum = struct.pack(">I", crc32(palette))
    return data[:start] + palette + checksum + data[start + 4 + length + 4:]


def make_icon(master, name, color, size=16):
    y, cell_width = {16: (0, 20), 20: (16, 20), 28: (36, 28)}[size]
    x = NAMES.index(name) * cell_width
    # Share decoded strips across widgets in the same Tk interpreter.
    root = master._root()
    if not hasattr(root, "_command_strips"):
        root._command_strips = {}
    if color not in root._command_strips:
        root._command_strips[color] = tk.PhotoImage(master=root, data=tinted_strip("commands.png", color), format="png")
    strip = root._command_strips[color]
    icon = tk.PhotoImage(master=master, width=size, height=size)
    icon.tk.call(str(icon), "copy", str(strip), "-from", x, y, x + size, y + size)
    return icon

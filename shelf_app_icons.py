"""Load catalog icons for disc images and locally downloaded Windows applications."""

from contextlib import ExitStack
import ctypes
from http.client import HTTPException
import json
import lzma
from pathlib import Path
import re
import shutil
import struct
import sys
import tempfile
import tkinter as tk
from urllib.parse import urlsplit
import zipfile
import zlib

from fetch_sources import Recipe, filename, open_url, read_page
from fetch_store import read_state, stamp
from shelf_checklist import LocalItem


ICON_SIZE = 16


def store_product_id(job):
    if isinstance(job, Recipe):
        match = re.fullmatch(r"https://get\.microsoft\.com/installer/download/([a-z0-9]{12})/?(?:\?[^#]*)?",
                             job.download, re.IGNORECASE | re.ASCII)
        if match:
            return match[1].upper()


def fetch_store_logo(product, results):
    """Fetch only the catalog product's logo. Tk images are created by the UI thread."""
    data = None
    try:
        catalog = read_page(f"https://displaycatalog.mp.microsoft.com/v7.0/products/{product}?market=US&languages=en-US")
        for localized in json.loads(catalog)["Product"]["LocalizedProperties"]:
            for image in localized.get("Images", []):
                if image.get("ImagePurpose") != "Logo":
                    continue
                uri = urlsplit(image.get("Uri", ""))
                if uri.hostname != "store-images.s-microsoft.com" or not uri.path.startswith("/image/"):
                    continue
                url = f"https://store-images.s-microsoft.com{uri.path}?w={ICON_SIZE}&h={ICON_SIZE}&format=png"
                with open_url(url) as response:
                    raw = response.read(65537)
                if (len(raw) <= 65536 and raw.startswith(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR")
                        and raw[16:24] == struct.pack(">II", ICON_SIZE, ICON_SIZE)):
                    data = raw
                    return
    except (OSError, HTTPException, ValueError, KeyError, TypeError, AttributeError):
        pass
    finally:
        results.put(data)


def cache_store_logo(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=path.parent) as staging:
        image = Path(staging) / "logo.png"
        image.write_bytes(data)
        image.replace(path)


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


def _msi_icon(master, path):
    # Open the installer database read-only and use the icon registered for Programs and Features.
    msi = ctypes.windll.msi
    db, view, record = (ctypes.c_uint() for _ in range(3))
    query = ("SELECT Icon.Data FROM Icon, Property WHERE Property.Property = 'ARPPRODUCTICON' "
             "AND Icon.Name = Property.Value")
    with ExitStack() as cleanup:
        if msi.MsiOpenDatabaseW(str(path), None, ctypes.byref(db)):
            return None
        cleanup.callback(msi.MsiCloseHandle, db)
        if msi.MsiDatabaseOpenViewW(db, query, ctypes.byref(view)):
            return None
        cleanup.callback(msi.MsiCloseHandle, view)
        if msi.MsiViewExecute(view, 0) or msi.MsiViewFetch(view, ctypes.byref(record)):
            return None
        cleanup.callback(msi.MsiCloseHandle, record)
        size = ctypes.c_uint(msi.MsiRecordDataSize(record, 1))
        if not 0 < size.value <= 16 * 1024 * 1024:
            return None
        raw = ctypes.create_string_buffer(size.value)
        if msi.MsiRecordReadStream(record, 1, raw, ctypes.byref(size)):
            return None
        folder = cleanup.enter_context(tempfile.TemporaryDirectory())
        icon_path = Path(folder) / ("product.exe" if raw[:2] == b"MZ" else "product.ico")
        icon_path.write_bytes(raw[:size.value])
        return _executable_icon(master, icon_path)


def _zip_icon(master, path):
    try:
        with zipfile.ZipFile(path) as package:
            members = [info for info in package.infolist() if info.filename.lower().endswith((".exe", ".ico"))]
            # Prefer the main EXE, then other EXEs, then packaged icon files.
            members.sort(key=lambda info: (info.filename.lower().endswith(".ico"), len(Path(info.filename).parts)))
            if not members:
                return None
            with tempfile.TemporaryDirectory() as folder:
                for info in members:
                    candidate = Path(folder) / ("candidate" + Path(info.filename).suffix.lower())
                    try:
                        with package.open(info) as src, candidate.open("wb") as dst:
                            shutil.copyfileobj(src, dst)
                    except (zipfile.BadZipFile, EOFError, RuntimeError, OSError, zlib.error, lzma.LZMAError):
                        continue
                    icon = _executable_icon(master, candidate)
                    if icon is not None:
                        return icon
    except (OSError, ValueError, zipfile.BadZipFile):
        pass
    return None


def load_app_icon(master, job, dest):
    try:
        if isinstance(job, LocalItem) and sys.platform == "win32" and not job.path.drive.startswith("\\\\"):
            loader = {".zip": _zip_icon, ".msi": _msi_icon, ".exe": _executable_icon}.get(job.path.suffix.lower())
            return loader(master, job.path) if loader else None
        if not isinstance(job, Recipe):
            return None
        if job.primary(job.archive or job.asset_pattern).lower().endswith(".iso"):
            return _disc_icon(master)
        if sys.platform != "win32" or dest.drive.startswith("\\\\"):
            return None
        root = job.cache_root(dest)
        state = read_state(root, "current.json") or read_state(root, "pending.json")
        if not state:
            return None
        job = job.versioned_members(state["version"])
        archive = filename(state.get("archive", job.archive))
        primary = job.primary(archive)
        if primary.lower().endswith(".iso"):
            return _disc_icon(master)
        candidates = []
        published = state.get("published", {})
        published_path = Path(published.get("path", ""))
        local_published = published_path.is_absolute() and not published_path.drive.startswith("\\\\")
        if primary.lower().endswith((".exe", ".msi")) and local_published:
            candidates.append((published_path, primary, published.get("stat")))
        members = dict.fromkeys([primary, *("files/" + name for name in job.extract)])
        for member in members:
            if member.lower().endswith((".exe", ".msi")):
                path = root / "releases" / state["release"] / member
                candidates.append((path, member, state.get("stats", {}).get(member)))
        if archive.lower().endswith(".zip"):
            path = root / "releases" / state["release"] / archive
            candidates.append((path, archive, state.get("stats", {}).get(archive)))
            if primary == archive and local_published:
                candidates.append((published_path, archive, published.get("stat")))
        for path, member, recorded in candidates:
            # Use recorded file metadata here. Hashing changed executables on the
            # UI thread would make opening a large catalog unnecessarily slow.
            if state["sha256"].get(member) and path.is_file() and (not recorded or recorded == stamp(path)):
                if member.lower().endswith(".zip"):
                    return _zip_icon(master, path)
                loader = _msi_icon if member.lower().endswith(".msi") else _executable_icon
                icon = loader(master, path)
                if icon is not None:
                    return icon
    except (OSError, ValueError, TypeError, AttributeError, tk.TclError):
        pass  # A missing or unreadable local release can still be listed.
    return None

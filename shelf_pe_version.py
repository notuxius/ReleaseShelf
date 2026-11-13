"""Read a Windows installer's file version using bounded reads of its PE resources."""

import re
import struct


def pe_file_version(read):
    """read(offset, count) returns exactly that range from a local or remote file."""
    header = read(0, 64)
    if header[:2] != b"MZ":
        raise ValueError("Missing Windows executable header")
    pe_offset, = struct.unpack_from("<I", header, 60)
    coff = read(pe_offset, 24)
    if coff[:4] != b"PE\0\0":
        raise ValueError("Missing PE signature")
    count, = struct.unpack_from("<H", coff, 6)
    optional_size, = struct.unpack_from("<H", coff, 20)
    if not 1 <= count <= 96 or optional_size < 2:
        raise ValueError("Invalid PE section headers")
    headers = read(pe_offset + 24, optional_size + 40 * count)
    magic, = struct.unpack_from("<H", headers)
    directory = {0x10b: 96, 0x20b: 112}.get(magic)
    if directory is None or optional_size < directory + 24:
        raise ValueError("Missing PE resource directory")
    rva, size = struct.unpack_from("<II", headers, directory + 16)
    for index in range(count):
        section = optional_size + index * 40
        base, raw_size, raw = struct.unpack_from("<III", headers, section + 12)
        if base <= rva < base + raw_size:
            break
    else:
        raise ValueError("Missing PE resource section")
    start = raw + rva - base
    limit = min(size, raw_size - (rva - base))
    # Directory tables normally fit in the first block, even for very large installers.
    tables = read(start, min(limit, 16384))

    def resource(offset, length):
        if offset < 0 or length < 1 or offset + length > limit:
            raise ValueError("Invalid Windows version resource offset")
        if offset + length <= len(tables):
            return tables[offset:offset + length]
        return read(start + offset, length)

    def entries(offset):
        names, ids = struct.unpack_from("<HH", resource(offset, 16), 12)
        if not 0 < names + ids <= 4096:
            raise ValueError("Invalid PE resource directory size")
        return list(struct.iter_unpack("<II", resource(offset + 16, 8 * (names + ids))))

    target = dict(entries(0)).get(16, 0)  # RT_VERSION
    for _ in range(2):  # Resource name, then language.
        if not target & 0x80000000:
            raise ValueError("Missing Windows file version resource")
        _, target = entries(target & 0x7fffffff)[0]
    address, length = struct.unpack_from("<II", resource(target, 16))
    if not 0 < length <= 65535:
        raise ValueError("Invalid Windows version resource size")
    block = resource(address - rva, length)
    key = "VS_VERSION_INFO\0".encode("utf-16le")
    fixed = (6 + len(key) + 3) & ~3
    if block[6:6 + len(key)] != key or len(block) < fixed + 16:
        raise ValueError("Invalid Windows version information")
    signature, _, high, low = struct.unpack_from("<IIII", block, fixed)
    if signature != 0xfeef04bd or not (high or low):
        raise ValueError("Missing fixed Windows file version")
    parts = [high >> 16, high & 65535, low >> 16, low & 65535]
    # Prefer the publisher's numeric spelling (e.g. 9.8.3 instead of 9.8.3.0).
    text = block.decode("utf-16le", errors="ignore")
    match = re.search(r"FileVersion\x00+\s*(\d+(?:\.\d+){1,3})\s*\x00", text)
    if match:
        return match[1]
    return ".".join(map(str, parts))

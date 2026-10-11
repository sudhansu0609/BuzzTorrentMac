"""Draws the BuzzTorrent logo (yellow rounded square + bolt) as PNG / ICO, stdlib only."""
from __future__ import annotations

import struct
import zlib
from pathlib import Path

BG = (255, 197, 49)
FG = (26, 19, 0)
# bolt from static/favicon.svg, in a 32x32 box
BOLT = [(17.5, 5), (8, 18), (15, 18), (13.5, 27), (24, 14), (17, 14)]
RADIUS = 8


def _in_poly(x: float, y: float, pts) -> bool:
    inside = False
    j = len(pts) - 1
    for i, (xi, yi) in enumerate(pts):
        xj, yj = pts[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _in_rrect(x: float, y: float) -> bool:
    cx = min(max(x, RADIUS), 32 - RADIUS)
    cy = min(max(y, RADIUS), 32 - RADIUS)
    return (x - cx) ** 2 + (y - cy) ** 2 <= RADIUS ** 2 and 0 <= x <= 32 and 0 <= y <= 32


def render(size: int, ss: int = 4) -> bytes:
    """RGBA pixels, anti-aliased with ss x ss supersampling."""
    out = bytearray()
    step = 32 / size
    offs = [(i + 0.5) / ss for i in range(ss)]
    n = ss * ss
    for py in range(size):
        for px in range(size):
            bg = fg = 0
            for oy in offs:
                y = (py + oy) * step
                for ox in offs:
                    x = (px + ox) * step
                    if _in_rrect(x, y):
                        if _in_poly(x, y, BOLT):
                            fg += 1
                        else:
                            bg += 1
            cover = bg + fg
            if not cover:
                out += b"\x00\x00\x00\x00"
                continue
            mix = [round((BG[k] * bg + FG[k] * fg) / cover) for k in range(3)]
            out += bytes(mix + [round(255 * cover / n)])
    return bytes(out)


def png(size: int) -> bytes:
    raw = render(size)
    rows = b"".join(b"\x00" + raw[y * size * 4:(y + 1) * size * 4] for y in range(size))

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows, 9)) + chunk(b"IEND", b""))


def ico(sizes=(16, 24, 32, 48, 64, 128, 256)) -> bytes:
    images = [png(s) for s in sizes]
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = 6 + 16 * len(sizes)
    entries = b""
    for s, img in zip(sizes, images):
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(img), offset)
        offset += len(img)
    return header + entries + b"".join(images)


def ensure(static_dir: Path) -> Path:
    """Write buzztorrent.ico and favicon.png if missing; returns the .ico path."""
    ico_path = static_dir / "buzztorrent.ico"
    if not ico_path.exists():
        ico_path.write_bytes(ico())
    png_path = static_dir / "favicon.png"
    if not png_path.exists():
        png_path.write_bytes(png(192))
    return ico_path

"""Video quality of a result: the real resolution when the source reports it (Internet
Archive lists each file's width and height), otherwise the release tags in its title."""
from __future__ import annotations

import re

# (pattern, effective height). Checked in order, so "1080p" wins over "DVDRip" in one title.
TITLE_TAGS = [
    (re.compile(r"\b(4320p|8k)\b", re.I), 4320),
    (re.compile(r"\b(2160p|4k|uhd)\b", re.I), 2160),
    (re.compile(r"\b1080[pi]\b", re.I), 1080),
    (re.compile(r"\b720p\b", re.I), 720),
    (re.compile(r"\b(576p|480p|dvd ?rip|dvdscr|dvd)\b", re.I), 480),
    (re.compile(r"\b(360p|240p|vhs ?rip|vhs|cam ?rip|hd ?cam|telesync|hdts)\b", re.I), 360),
]


def from_title(title: str) -> int | None:
    for rx, height in TITLE_TAGS:
        if rx.search(title or ""):
            return height
    return None


def from_dims(width, height) -> int | None:
    """Effective height: a widescreen 1920x800 film is still 1080p, 1280x694 is 720p."""
    try:
        w, h = int(width or 0), int(height or 0)
    except (TypeError, ValueError):
        return None
    if w <= 0 and h <= 0:
        return None
    return max(h, round(w * 9 / 16))


def label(height: int | None) -> str | None:
    if not height:
        return None
    if height >= 4000:
        return "8K"
    if height >= 2000:
        return "4K"
    if height >= 1000:
        return "1080p"
    if height >= 690:
        return "720p"
    if height >= 440:
        return "480p"
    return "Low"


def rank(height: int | None) -> int:
    """0 unknown, 1 low ... 6 8K. Used to sort equally relevant results."""
    return {None: 0, "Low": 1, "480p": 2, "720p": 3, "1080p": 4, "4K": 5, "8K": 6}[label(height)]

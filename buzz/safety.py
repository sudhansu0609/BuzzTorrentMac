"""Spam / fake / malware heuristics.

No tool can prove a torrent is clean without downloading it, so this is a risk
score built from signals that strongly correlate with fakes and malware:
executables hidden in media torrents, double extensions, "password"/"codec"
lures, sizes that can't be a real film, implausible seed counts, mismatched
hashes, and so on. Anything rated `danger` is hidden by default.
"""
from __future__ import annotations

import re

from .models import Result

MEDIA = {"movies", "tv", "music", "books"}
EXEC_EXT = {"exe", "scr", "bat", "cmd", "com", "msi", "vbs", "vbe", "js", "jse", "jar", "ps1",
            "lnk", "pif", "hta", "wsf", "cpl", "reg", "dll", "apk", "sh", "app", "dmg",
            "pkg", "mpkg", "command", "tool", "scpt"}
NEVER_LEGIT_EXT = {"scr", "pif", "lnk", "hta", "vbe", "jse", "wsf", "cpl"}
LURE_EXT = {"mp4", "mkv", "avi", "mov", "wmv", "m4v", "mp3", "flac", "wav", "pdf", "epub",
            "mobi", "azw3", "jpg", "jpeg", "png", "txt", "doc", "docx", "srt"}
ARCHIVE_EXT = {"rar", "zip", "7z"}
DRM_LURE_EXT = {"wmv", "wma", "asf"}
SHORTCUT_EXT = {"url", "website", "webloc", "html", "htm"}

_EXT = "|".join(sorted(EXEC_EXT))
TITLE_EXEC = re.compile(rf"\.({_EXT})\b", re.I)
DOUBLE_EXT = re.compile(rf"\.({'|'.join(sorted(LURE_EXT))})\s*\.({_EXT})$", re.I)
TITLE_DOUBLE_EXT = re.compile(rf"\.({'|'.join(sorted(LURE_EXT))})\s*\.({_EXT})\b", re.I)

TITLE_RULES = [
    (re.compile(r"\b(password|passwd)\b", re.I), "danger",
     "Mentions a password: classic 'complete a survey for the password' scam"),
    (re.compile(r"\b(codec|player|license)\s*(pack\s*)?(is\s*)?(required|needed|included)\b", re.I), "danger",
     "Claims a codec/player/licence is required: common malware lure"),
    (re.compile(r"\b(click here|free download|download now|full version free|100% working|no survey)\b", re.I),
     "warn", "Clickbait wording typical of spam uploads"),
    (re.compile(r"(https?://|www\.)", re.I), "warn",
     "Title contains a web address, often used to push ad or scam sites"),
    (re.compile(r"[\U0001F300-\U0001FAFF☀-➿]{2,}"), "warn", "Emoji-stuffed title (spam pattern)"),
]
CRACK = re.compile(r"\b(keygen|key\s*gen|activator|crack(ed)?|serial\s*generator)\b", re.I)

MB = 1024 * 1024
# category: (danger_below, warn_below). Games only warn: retro/indie games are tiny, but a
# "new AAA game" of a few MB is the classic fake "downloader" stub.
SIZE_FLOOR = {"movies": (20 * MB, 150 * MB), "tv": (5 * MB, 30 * MB), "games": (0, 5 * MB)}
# Curated sources host real files, so a tiny item there isn't a malware stub, but it isn't the
# film either: Internet Archive search turns up trailers, TCM clips and lone subtitle files.
EXTRA_BELOW = {"movies": 100 * MB, "tv": 20 * MB}
VIDEO_EXT = {"mp4", "mkv", "avi", "mov", "wmv", "m4v", "mpg", "mpeg", "webm", "ogv", "ts", "m2ts", "flv",
             "vob", "ifo", "iso", "divx", "3gp", "asf", "rm", "rmvb"}

# Files Internet Archive generates itself (speech-to-text transcripts, metadata).
# Exempted only for trusted sources; a .js in any other movie torrent stays flagged.
TRUSTED_DERIVATIVE = re.compile(
    r"(\.asr\.js|_meta\.xml|_files\.xml|_meta\.sqlite|_reviews\.xml|_hocr\.html(\.gz)?|_chocr\.html(\.gz)?"
    r"|_djvu\.(xml|txt)|_hocr_pageindex\.json(\.gz)?|_hocr_searchtext\.txt(\.gz)?|_page_numbers\.json"
    r"|_scandata\.xml|_jp2\.zip|_abbyy\.gz)$", re.I)   # the Archive's own OCR / scan derivatives
PADDING = re.compile(r"(^|/)\.?_{2,}padding_file(/|$)")

WEIGHT = {"danger": 45, "warn": 15, "info": 0}


def _flag(level: str, msg: str) -> dict:
    return {"level": level, "msg": msg}


def _human(n: int) -> str:
    return f"{n / MB:.0f} MB" if n >= MB else f"{max(1, n // 1024)} KB"


def _ext(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def title_flags(r: Result) -> list[dict]:
    flags = []
    t = r.title or ""
    if TITLE_DOUBLE_EXT.search(t):
        flags.append(_flag("danger", "Double file extension in title (e.g. movie.mp4.exe): disguised executable"))
    elif TITLE_EXEC.search(t) and r.category in MEDIA:
        flags.append(_flag("danger", "Media listing names an executable file"))
    for rx, level, msg in TITLE_RULES:
        if rx.search(t):
            flags.append(_flag(level, msg))
    if CRACK.search(t):
        if r.category in MEDIA:
            flags.append(_flag("danger", "Media listing mentions cracks/keygens"))
        else:
            flags.append(_flag("warn", "Includes crack/keygen tools, a leading source of malware. Scan before running"))
    return flags


def stats_flags(r: Result) -> list[dict]:
    flags = []
    floor = SIZE_FLOOR.get(r.category)
    if r.trusted_source:
        if r.expect_full and r.size and r.size < EXTRA_BELOW.get(r.category, 0):
            what = "the full movie" if r.category == "movies" else "a full episode"
            flags.append(_flag("warn", f"Only {_human(r.size)}: too small to be {what}. "
                                       "Probably a trailer, clip, short or subtitles"))
    elif floor and r.size:
        if r.size < floor[0]:
            flags.append(_flag("danger", f"Far too small for real {r.category} content ({_human(r.size)})"))
        elif r.size < floor[1]:
            flags.append(_flag("warn", f"Unusually small for {r.category} ({_human(r.size)})"))
    if r.seeders is not None:
        if r.seeders > 100_000:
            flags.append(_flag("warn", "Implausibly high seeder count (possible fake stats)"))
        elif r.seeders == 0:
            flags.append(_flag("info", "No seeders right now, may never finish"))
    if r.trusted_source:
        flags.append(_flag("info", "From a curated source (web-seeded, downloads even with no peers)"))
    return flags


def scan_files(files: list[tuple[str, int]], category: str,
               trusted: bool = False) -> tuple[list[dict], dict[int, str]]:
    """Inspect a torrent's file list. Returns (flags, {file_index: reason})."""
    flags, marks = [], {}
    exts = [_ext(p) for p, _ in files]
    for i, (path, _) in enumerate(files):
        name = path.rsplit("/", 1)[-1]
        if PADDING.search(path) or (trusted and TRUSTED_DERIVATIVE.search(name)):
            exts[i] = ""
            continue
        e = exts[i]
        if DOUBLE_EXT.search(name):
            marks[i] = "disguised executable (double extension)"
        elif e in NEVER_LEGIT_EXT:
            marks[i] = f".{e} files have no legitimate use in a torrent"
        elif e in EXEC_EXT and category in MEDIA:
            marks[i] = "executable inside a media torrent"
        elif e in SHORTCUT_EXT:
            marks[i] = "web shortcut/page (usually points to ads or scams)"
        elif "password" in name.lower():
            marks[i] = "'password' file (survey/paywall scam)"
    if any("disguised" in m or "no legitimate" in m or "media torrent" in m for m in marks.values()):
        flags.append(_flag("danger", "Contains disguised or unexpected executable files"))
    if any("shortcut" in m or "password" in m for m in marks.values()):
        flags.append(_flag("warn", "Contains web shortcuts or 'password' files"))

    if category in MEDIA and files:
        media_like = [e for e in exts if e in LURE_EXT or e in DRM_LURE_EXT]
        if not media_like and any(e in ARCHIVE_EXT for e in exts):
            flags.append(_flag("warn", "Media packed inside an archive, often password-protected bait"))
        if category in {"movies", "tv"} and media_like and all(e in DRM_LURE_EXT for e in media_like):
            flags.append(_flag("warn", "Only .wmv/.asf video, the format used by 'licence required' scams"))
        if category in {"movies", "tv"} and not any(e in VIDEO_EXT or e in ARCHIVE_EXT for e in exts):
            flags.append(_flag("warn", "Contains no video file at all (subtitles, images or audio only)"))
    if not files:
        flags.append(_flag("warn", "Torrent lists no files"))
    return flags, marks


def assess(r: Result, extra: list[dict] | None = None) -> Result:
    flags = title_flags(r) + stats_flags(r) + (extra or [])
    seen, unique = set(), []
    for f in flags:
        if f["msg"] not in seen:
            seen.add(f["msg"])
            unique.append(f)
    r.flags = unique
    r.score = max(0, 100 - sum(WEIGHT[f["level"]] for f in unique))
    levels = {f["level"] for f in unique}
    r.verdict = "danger" if "danger" in levels else "caution" if "warn" in levels else "safe"
    return r

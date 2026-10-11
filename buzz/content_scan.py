"""Content inspection of downloaded files.

Unlike the pre-download checks, this reads the actual bytes on disk. A file's
real type comes from its magic bytes, never its name, so a "movie.mp4" that is
really a Windows program is caught however it's named. Archives are opened
(central directory / headers only, nothing is extracted) to find programs and
password protection, and PDFs are checked for embedded scripts.

This complements, not replaces, the antivirus scan in `av.py`.
"""
from __future__ import annotations

import json
import os
import re
import struct
import zipfile
from pathlib import Path

HEAD = 64 * 1024
PDF_SCAN = 8 * 1024 * 1024
LNK_MAGIC = b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46"
ASF_MAGIC = b"\x30\x26\xb2\x75\x8e\x66\xcf\x11"

VIDEO_EXT = {"mkv", "mp4", "avi", "mov", "m4v", "wmv", "webm", "mpg", "mpeg", "ts", "m2ts", "ogv", "flv", "3gp", "vob"}
AUDIO_EXT = {"mp3", "flac", "wav", "m4a", "aac", "ogg", "opus", "wma", "ape", "alac"}
IMAGE_EXT = {"jpg", "jpeg", "png", "gif", "bmp", "webp"}
BOOK_EXT = {"pdf", "epub", "mobi", "azw3", "djvu", "cbz", "cbr"}
TEXT_EXT = {"txt", "nfo", "srt", "ass", "ssa", "sub", "vtt", "md", "sfv", "md5", "cue", "log"}
DOC_EXT = {"doc", "docx", "docm", "xls", "xlsx", "xlsm", "ppt", "pptx", "pptm", "rtf"}
NON_EXEC_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT | BOOK_EXT | TEXT_EXT
PROGRAM_EXT = {"exe", "msi", "com", "scr", "pif", "cpl", "dll", "sys", "jar", "appx", "msix", "msixbundle",
               "pkg", "mpkg", "dylib"}   # last three: macOS installers / libraries
SCRIPT_EXT = {"bat", "cmd", "ps1", "vbs", "vbe", "js", "jse", "wsf", "wsh", "hta", "lnk", "reg", "inf",
              "command", "sh", "zsh", "tool", "applescript", "scpt"}   # macOS: double-clickable scripts
NEVER_LEGIT = {"scr", "pif", "cpl", "lnk", "hta", "vbe", "jse", "wsf"}
DISK_IMAGE_EXT = {"iso", "img", "vhd", "vhdx", "dmg"}
ARCHIVE_TYPES = {"zip", "rar", "7z"}
PROGRAM_TYPES = {"exe", "elf", "macho", "lnk", "ole", "script"}
MEDIA_TYPES = {"video", "audio", "ebook", "pdf"}
PARTIAL_EXT = {".!qb", ".aria2", ".part", ".!ut", ".!bt", ".crdownload", ".tmp", ".bc!", ".partial", ".download"}
IGNORE_NAMES = {"desktop.ini", "thumbs.db", ".ds_store"}
PDF_RISK = re.compile(rb"/(JavaScript|JS|Launch|EmbeddedFile|RichMedia|XFA)\b")
DOUBLE_EXT = re.compile(r"\.(" + "|".join(sorted(NON_EXEC_EXT | DOC_EXT)) + r")\s*\.(" +
                        "|".join(sorted(PROGRAM_EXT | SCRIPT_EXT)) + r")$", re.I)


def ext_of(name: str) -> str:
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def sniff(head: bytes) -> str:
    """Identify a file by its leading bytes."""
    h = head
    if h[:2] == b"MZ":
        return "exe"
    if h[:4] == b"\x7fELF":
        return "elf"
    if h[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe", b"\xfe\xed\xfa\xcf", b"\xfe\xed\xfa\xce"):
        return "macho"
    if h[:20] == LNK_MAGIC:
        return "lnk"
    if h[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08"):
        return "zip"
    if h[:6] == b"Rar!\x1a\x07":
        return "rar"
    if h[:6] == b"7z\xbc\xaf\x27\x1c":
        return "7z"
    if h[:8] == b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1":
        return "ole"  # MSI installers and legacy Office files
    if b"%PDF-" in h[:1024]:
        return "pdf"
    if h[:4] == b"\x1a\x45\xdf\xa3" or h[4:8] == b"ftyp" or h[:3] == b"FLV" or \
            (h[:4] == b"RIFF" and h[8:12] == b"AVI ") or h[:4] in (b"\x00\x00\x01\xba", b"\x00\x00\x01\xb3"):
        return "video"
    if (len(h) > 188 and h[0] == 0x47 and h[188] == 0x47) or (len(h) > 196 and h[4] == 0x47 and h[196] == 0x47):
        return "video"  # MPEG transport stream / m2ts
    if h[:8] == ASF_MAGIC:
        return "video"  # wmv / wma
    if h[:3] == b"ID3" or h[:4] in (b"fLaC", b"OggS", b"MAC ") or (h[:4] == b"RIFF" and h[8:12] == b"WAVE") or \
            (len(h) > 1 and h[0] == 0xFF and (h[1] & 0xE0) == 0xE0):
        return "audio"
    if h[:3] == b"\xff\xd8\xff" or h[:8] == b"\x89PNG\r\n\x1a\n" or h[:6] in (b"GIF87a", b"GIF89a") or \
            h[:2] == b"BM" or (h[:4] == b"RIFF" and h[8:12] == b"WEBP"):
        return "image"
    if h[60:68] == b"BOOKMOBI" or h[:8] == b"AT&TFORM":
        return "ebook"
    if len(h) > 32774 and h[32769:32774] == b"CD001":
        return "iso"
    stripped = h.lstrip(b"\xef\xbb\xbf \t\r\n")[:200].lower()
    if stripped.startswith(b"#!") or stripped.startswith((b"@echo", b"powershell", b"createobject(", b"wscript.")):
        return "script"
    if stripped.startswith((b"<!doctype html", b"<html", b"<script", b"<hta:")):
        return "html"
    if not h:
        return "empty"
    sample = h[:4096]
    if sample and sum(32 <= b < 127 or b in (9, 10, 13) for b in sample) / len(sample) > 0.95:
        return "text"
    return "data"


# ----------------------------------------------------------------- archives
def inspect_zip(path: Path, max_entries: int = 20000) -> dict:
    out = {"names": [], "encrypted": False, "macros": False, "error": None}
    try:
        with zipfile.ZipFile(path) as z:
            for i, info in enumerate(z.infolist()):
                if i >= max_entries:
                    break
                out["names"].append(info.filename)
                if info.flag_bits & 0x1:
                    out["encrypted"] = True
                if info.filename.lower().endswith("vbaproject.bin"):
                    out["macros"] = True
    except (zipfile.BadZipFile, OSError, ValueError, NotImplementedError) as e:
        out["error"] = str(e)
    return out


def _vint(buf: bytes, pos: int) -> tuple[int, int]:
    value = shift = 0
    while pos < len(buf):
        b = buf[pos]
        value |= (b & 0x7F) << shift
        pos += 1
        if not b & 0x80:
            return value, pos
        shift += 7
    raise ValueError("truncated vint")


def inspect_rar(path: Path, max_blocks: int = 5000) -> dict:
    """Read RAR headers (no decompression): file names and password protection."""
    out = {"names": [], "encrypted": False, "error": None}
    try:
        with open(path, "rb") as f:
            sig = f.read(8)
            if sig[:7] == b"Rar!\x1a\x07\x00":          # RAR 4.x
                f.seek(7)
                for _ in range(max_blocks):
                    hdr = f.read(7)
                    if len(hdr) < 7:
                        break
                    _, htype, flags, size = struct.unpack("<HBHH", hdr)
                    if size < 7:
                        break
                    body = f.read(size - 7)
                    add = 0
                    if flags & 0x8000 and len(body) >= 4:
                        add = struct.unpack("<I", body[:4])[0]
                    if htype == 0x73 and flags & 0x0080:      # headers encrypted
                        out["encrypted"] = True
                        break
                    if htype == 0x74 and len(body) >= 25:     # file header
                        if flags & 0x04:
                            out["encrypted"] = True
                        name_size = struct.unpack("<H", body[19:21])[0]
                        start = 25 + (8 if flags & 0x100 else 0)
                        out["names"].append(body[start:start + name_size].decode("utf-8", "replace").split("\x00")[0])
                    elif htype == 0x7B:                       # end of archive
                        break
                    f.seek(add, os.SEEK_CUR)
            elif sig == b"Rar!\x1a\x07\x01\x00":           # RAR 5.x
                for _ in range(max_blocks):
                    pre = f.read(16)
                    if len(pre) < 6:
                        break
                    size, p = _vint(pre, 4)
                    f.seek(f.tell() - len(pre) + p)
                    block = f.read(size)
                    htype, q = _vint(block, 0)
                    hflags, q = _vint(block, q)
                    if htype == 4:                            # archive encryption header
                        out["encrypted"] = True
                        break
                    extra = data = 0
                    if hflags & 0x1:
                        extra, q = _vint(block, q)
                    if hflags & 0x2:
                        data, q = _vint(block, q)
                    if htype == 2:                            # file header
                        fflags, q = _vint(block, q)
                        _, q = _vint(block, q)                # unpacked size
                        _, q = _vint(block, q)                # attributes
                        if fflags & 0x2:
                            q += 4                            # mtime
                        if fflags & 0x4:
                            q += 4                            # crc
                        _, q = _vint(block, q)                # compression info
                        _, q = _vint(block, q)                # host os
                        nlen, q = _vint(block, q)
                        out["names"].append(block[q:q + nlen].decode("utf-8", "replace"))
                        pos = len(block) - extra
                        while extra and pos < len(block):     # extra records; type 1 = encryption
                            rsize, r = _vint(block, pos)
                            rtype, _ = _vint(block, r)
                            if rtype == 1:
                                out["encrypted"] = True
                            pos = r + rsize
                    elif htype == 5:
                        break
                    f.seek(data, os.SEEK_CUR)
    except (OSError, ValueError, struct.error) as e:
        out["error"] = str(e)
    return out


def pdf_risks(path: Path) -> list[str]:
    try:
        with open(path, "rb") as f:
            data = f.read(PDF_SCAN)
    except OSError:
        return []
    return sorted({m.decode() for m in PDF_RISK.findall(data)})


# -------------------------------------------------------------------- items
def list_files(root: Path, cap: int = 20000) -> list[Path]:
    if root.is_file():
        return [root]
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".____padding")]
        for n in filenames:
            if n.lower() not in IGNORE_NAMES:
                out.append(Path(dirpath) / n)
                if len(out) >= cap:
                    return out
    return out


def is_partial(path: Path) -> bool:
    name = path.name.lower()
    return any(name.endswith(e) for e in PARTIAL_EXT)


def _finding(rel: str, level: str, msg: str) -> dict:
    return {"file": rel, "level": level, "msg": msg}


def inspect_item(root: Path, base: Path) -> dict:
    """Inspect every file of one download. Returns findings plus the files worth
    sending to a hash-reputation service and signature checks."""
    files = list_files(root)
    findings: list[dict] = []
    programs: list[Path] = []      # real executables on disk (for signature checks)
    risky: list[Path] = []         # executables, scripts, archives, documents (for reputation lookup)
    types: dict[Path, str] = {}

    for p in files:
        rel = str(p.relative_to(base)).replace("\\", "/")
        e = ext_of(p.name)
        try:
            with open(p, "rb") as f:
                head = f.read(HEAD)
        except OSError as err:
            from .av import _open_error
            if _open_error(p) == 225:  # ERROR_VIRUS_INFECTED
                findings.append(_finding(rel, "danger", "Windows blocked this file: your antivirus flagged it as infected"))
            else:
                findings.append(_finding(rel, "warn", f"Could not read file ({err.strerror or err})"))
            continue
        real = sniff(head)
        types[p] = real

        if DOUBLE_EXT.search(p.name):
            findings.append(_finding(rel, "danger", "Double extension: a program pretending to be a media/document file"))
        if e in NON_EXEC_EXT and real in PROGRAM_TYPES | {"html"}:
            what = {"exe": "a Windows program", "elf": "a Linux program", "macho": "a Mac program",
                    "lnk": "a Windows shortcut", "ole": "an installer/Office container",
                    "script": "a script", "html": "a web page/HTA"}[real]
            findings.append(_finding(rel, "danger", f"Named .{e} but is actually {what} (disguised)"))
        elif e in VIDEO_EXT | AUDIO_EXT and real in ARCHIVE_TYPES:
            findings.append(_finding(rel, "warn", f"Named .{e} but is actually a {real.upper()} archive"))
        if e in NEVER_LEGIT or real == "lnk":
            findings.append(_finding(rel, "danger", f".{e or real} files run commands when opened and have no place in a download"))
        if real in ("exe", "elf", "macho", "ole") or e in PROGRAM_EXT:
            programs.append(p)
        if e in PROGRAM_EXT | SCRIPT_EXT or real in PROGRAM_TYPES | ARCHIVE_TYPES | {"pdf", "html"} or e in DOC_EXT:
            risky.append(p)

        if real == "zip":
            z = inspect_zip(p)
            _archive_findings(findings, rel, z, "ZIP")
            if z["macros"]:
                findings.append(_finding(rel, "warn", "Office document contains macros (a common malware carrier)"))
        elif real == "rar":
            _archive_findings(findings, rel, inspect_rar(p), "RAR")
        elif real == "7z":
            findings.append(_finding(rel, "info", "7-Zip archive: contents are checked by the antivirus scan only"))
        elif real == "pdf" or e == "pdf":
            risks = pdf_risks(p)
            if risks:
                findings.append(_finding(rel, "warn", "PDF contains active content: " + ", ".join("/" + r for r in risks)))
        if e in DISK_IMAGE_EXT or real == "iso":
            findings.append(_finding(rel, "warn", "Disk image: can hide programs and bypass Windows download warnings"))

    media = any(t in MEDIA_TYPES for t in types.values())
    if media:
        for p in programs:
            # Windows programs by name, and Mac/Linux programs by content (they often have no extension,
            # e.g. "Movie.app/Contents/MacOS/Movie")
            if types.get(p) in PROGRAM_TYPES and (ext_of(p.name) in PROGRAM_EXT or types.get(p) in ("macho", "elf")):
                rel = str(p.relative_to(base)).replace("\\", "/")
                findings.append(_finding(rel, "danger", "Program bundled with media files (\"player\"/\"codec\" lure)"))
        for p in files:
            e = ext_of(p.name)
            if e in SCRIPT_EXT - {"lnk"}:
                rel = str(p.relative_to(base)).replace("\\", "/")
                if e == "js" and _is_json_data(p):
                    # e.g. the Internet Archive's "<item>.asr.js" speech-to-text data: pure JSON can't run code
                    findings.append(_finding(rel, "info", "Data file with a .js name (plain JSON, nothing that runs)"))
                else:
                    findings.append(_finding(rel, "danger", "Script bundled with media files"))
    return {
        "files": files, "findings": _dedupe(findings), "programs": programs[:50], "risky": risky[:200],
        "media": media, "size": sum(_size(p) for p in files),
    }


def _is_json_data(p: Path, limit: int = 5 * 1024 * 1024) -> bool:
    """True when the whole file is one strict JSON object or array (no code can hide in that)."""
    try:
        if p.stat().st_size > limit:
            return False
        value = json.loads(p.read_bytes().decode("utf-8-sig"))
    except (OSError, ValueError):
        return False
    return isinstance(value, (dict, list))


def _archive_findings(findings: list, rel: str, info: dict, kind: str) -> None:
    if info["encrypted"]:
        findings.append(_finding(rel, "warn",
                                 f"Password-protected {kind}: antivirus can't see inside (classic 'pay for the password' scam)"))
    bad = [n for n in info["names"] if DOUBLE_EXT.search(n) or ext_of(n) in NEVER_LEGIT]
    progs = [n for n in info["names"] if ext_of(n) in PROGRAM_EXT | SCRIPT_EXT]
    if bad:
        findings.append(_finding(rel, "danger", f"{kind} contains disguised programs: {', '.join(bad[:3])}"))
    elif progs:
        findings.append(_finding(rel, "info", f"{kind} contains {len(progs)} program/script file(s), e.g. {progs[0]}"))
    if info["error"] and not info["names"]:
        findings.append(_finding(rel, "warn", f"Corrupt or unreadable {kind} archive"))


def _dedupe(findings: list[dict]) -> list[dict]:
    seen, out = set(), []
    for f in findings:
        key = (f["file"], f["msg"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def _size(p: Path) -> int:
    try:
        return p.stat().st_size
    except OSError:
        return 0

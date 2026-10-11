"""The PC library: videos in the download folder that the download scanner has checked and
found clean, ready to stream to the TV.

Only files the scanner rated "clean" are listed, and each must also *be* a video by its
leading bytes (a program renamed to .mp4 never shows up). Files are addressed by an opaque
id; the TV never sends a path."""
from __future__ import annotations

import hashlib
import re
import threading
import time
from pathlib import Path

from .content_scan import VIDEO_EXT, ext_of, is_partial, list_files, sniff
from .fuzzy import clean_release
from .quality import from_title
from .subtitles import describe, for_video, is_subtitle

CONTENT_TYPES = {"mkv": "video/x-matroska", "mp4": "video/mp4", "m4v": "video/mp4", "webm": "video/webm",
                 "avi": "video/x-msvideo", "mov": "video/quicktime", "ts": "video/mp2t", "m2ts": "video/mp2t",
                 "mpg": "video/mpeg", "mpeg": "video/mpeg", "wmv": "video/x-ms-wmv", "flv": "video/x-flv",
                 "ogv": "video/ogg", "3gp": "video/3gpp", "vob": "video/mpeg"}
CACHE_SECONDS = 30
# Formats TV players handle best come first among copies of the same video
PREFERRED = ("mp4", "m4v", "mkv", "webm")
GROUP_SUFFIX = re.compile(r"(_512kb|[ ._-]?(4320p|2160p|1440p|1080p|720p|576p|480p|360p|8k|4k|uhd|hd|sd))+$", re.I)


def file_id(rel: str) -> str:
    return hashlib.sha1(rel.encode("utf-8")).hexdigest()[:20]


class Library:
    def __init__(self, scanner, titles=None, private=None):
        self.scanner = scanner
        self.titles = titles or (lambda: {})        # download folder/file name -> real title
        self.private = private or (lambda: set())    # names of private (18+) downloads
        self.lock = threading.Lock()
        self._cache: tuple[float, str, list[dict], dict[str, Path]] | None = None
        self._sniffed: dict[tuple, bool] = {}

    def state(self) -> str:
        if not self.scanner.enabled or not self.scanner.folder:
            return "scanner_off"
        return "ok"

    def _is_video(self, f: Path) -> bool:
        try:
            st = f.stat()
        except OSError:
            return False
        key = (str(f), st.st_size, st.st_mtime)
        if key not in self._sniffed:
            try:
                with open(f, "rb") as fh:
                    head = fh.read(4096)
            except OSError:
                return False
            self._sniffed[key] = sniff(head) == "video"
        return self._sniffed[key]

    def _scan(self) -> tuple[list[dict], dict[str, Path]]:
        sc = self.scanner
        folder = sc.folder
        items, index = [], {}
        try:
            names = self.titles() or {}
            private = self.private() or set()
        except Exception:
            names, private = {}, set()
        with sc._lock:
            reports = [dict(r) for r in sc.reports.values()
                       if r.get("folder") == str(folder) and r.get("state") == "done" and r.get("verdict") == "clean"]
        for rep in reports:
            entry = sc._inside(folder, rep["name"])
            if not entry or not entry.exists():
                continue
            files = list_files(entry) if entry.is_dir() else [entry]
            videos = [f for f in files if ext_of(f.name) in VIDEO_EXT and not is_partial(f) and self._is_video(f)]
            subs_all = [f for f in files if is_subtitle(f.name)]
            # copies of one video (film.mp4, film.avi, film_512kb.mp4) become one entry
            groups: dict[tuple, list[Path]] = {}
            for v in videos:
                groups.setdefault((v.parent, GROUP_SUFFIX.sub("", v.stem).lower() or v.stem.lower()), []).append(v)
            known = names.get(rep["name"])
            for (parent, key), copies in groups.items():
                try:
                    copies.sort(key=lambda f: (ext_of(f.name) in PREFERRED, f.stat().st_size), reverse=True)
                    mtime = max(f.stat().st_mtime for f in copies)
                except OSError:
                    continue
                best = copies[0]
                versions = []
                for f in copies:
                    vid = file_id(f.relative_to(folder).as_posix())
                    index[vid] = f
                    versions.append({"id": vid, "ext": ext_of(f.name), "size": f.stat().st_size,
                                     "quality": from_title(f.name)})
                same_dir = [s.name for s in subs_all if s.parent == parent]
                subs = []
                for d in describe(for_video(best.name, same_dir, len(groups) == 1)):
                    sub_path = parent / d["file"]
                    sid = file_id(sub_path.relative_to(folder).as_posix())
                    index[sid] = sub_path
                    subs.append({**d, "id": sid})
                title, year = clean_release(best.stem)
                if known:
                    k_title, k_year = clean_release(known)
                    # name the extra videos of a download by what sets them apart ("film_edit" -> "edit")
                    base = Path(rep["name"]).stem.lower()
                    diff = key[len(base):].strip(" ._-") if key.startswith(base) else (clean_release(key)[0] or key)
                    extra = f" · {diff}" if diff and len(groups) > 1 else ""
                    title, year = (k_title or known) + extra, k_year or year
                items.append({"id": versions[0]["id"], "title": title or best.stem, "year": year, "file": best.name,
                              "folder": rep["name"] if entry.is_dir() else "", "size": versions[0]["size"],
                              "added": mtime, "quality": from_title(best.name) or from_title(rep["name"]),
                              "subs": subs, "versions": versions, "scanned": rep.get("scanned_at"),
                              "private": rep["name"] in private,
                              "ext": ext_of(best.name)})
        items.sort(key=lambda x: -x["added"])
        return items, index

    def items(self) -> list[dict]:
        if self.state() != "ok":
            return []
        with self.lock:
            folder = str(self.scanner.folder)
            if self._cache and time.time() - self._cache[0] < CACHE_SECONDS and self._cache[1] == folder:
                return self._cache[2]
            items, index = self._scan()
            self._cache = (time.time(), folder, items, index)
            return items

    def path(self, fid: str) -> Path | None:
        """The file behind an id, re-checked to still be inside the download folder."""
        self.items()
        with self.lock:
            p = self._cache[3].get(fid) if self._cache else None
        folder = self.scanner.folder
        if not p or not folder:
            return None
        try:
            p.resolve().relative_to(Path(folder).resolve())
        except (ValueError, OSError):
            return None
        return p if p.is_file() else None

"""Strict, bounded bencode decoder for .torrent files.

Only the metadata is read: the torrent's file list, never its contents. We
compute the real info-hash from the raw `info` dictionary so a listing that
advertises one hash but serves a different torrent is caught.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

MAX_DEPTH = 64
INT_RE = re.compile(rb"^-?\d{1,32}$")


class BencodeError(ValueError):
    pass


@dataclass
class TorrentInfo:
    infohash: str
    name: str
    files: list[tuple[str, int]] = field(default_factory=list)
    trackers: list[str] = field(default_factory=list)
    private: bool = False

    @property
    def total_size(self) -> int:
        return sum(size for _, size in self.files)


class _Decoder:
    def __init__(self, data: bytes):
        self.data = data
        self.pos = 0
        self.info_span: tuple[int, int] | None = None

    def value(self, depth: int):
        if depth > MAX_DEPTH:
            raise BencodeError("nesting too deep")
        c = self.data[self.pos]
        if c == ord("i"):
            end = self.data.index(b"e", self.pos)
            raw = self.data[self.pos + 1:end]
            if not INT_RE.match(raw):
                raise BencodeError("bad integer")
            self.pos = end + 1
            return int(raw)
        if c == ord("l"):
            self.pos += 1
            out = []
            while self.data[self.pos] != ord("e"):
                out.append(self.value(depth + 1))
            self.pos += 1
            return out
        if c == ord("d"):
            self.pos += 1
            out = {}
            while self.data[self.pos] != ord("e"):
                key = self.string()
                start = self.pos
                out[key] = self.value(depth + 1)
                if depth == 0 and key == b"info":
                    self.info_span = (start, self.pos)
            self.pos += 1
            return out
        if ord("0") <= c <= ord("9"):
            return self.string()
        raise BencodeError("unexpected byte")

    def string(self) -> bytes:
        colon = self.data.index(b":", self.pos)
        raw = self.data[self.pos:colon]
        if not raw.isdigit() or len(raw) > 10:
            raise BencodeError("bad string length")
        length = int(raw)
        start = colon + 1
        if start + length > len(self.data):
            raise BencodeError("string runs past end of data")
        self.pos = start + length
        return self.data[start:self.pos]


def _text(b) -> str:
    return b.decode("utf-8", errors="replace") if isinstance(b, bytes) else str(b)


def _walk_v2(tree: dict, prefix: str, out: list) -> None:
    for name, node in tree.items():
        if not isinstance(node, dict):
            continue
        if b"" in node and isinstance(node[b""], dict):
            out.append((prefix + _text(name), int(node[b""].get(b"length", 0))))
        else:
            _walk_v2(node, prefix + _text(name) + "/", out)


def parse_torrent(data: bytes) -> TorrentInfo:
    dec = _Decoder(data)
    try:
        root = dec.value(0)
    except (IndexError, ValueError) as e:
        raise BencodeError(f"not a valid torrent file ({e})") from e
    if dec.pos != len(data):
        raise BencodeError("trailing data after torrent")
    if not isinstance(root, dict) or not isinstance(root.get(b"info"), dict) or not dec.info_span:
        raise BencodeError("torrent has no info dictionary")

    info = root[b"info"]
    start, end = dec.info_span
    name = _text(info.get(b"name", b""))
    files: list[tuple[str, int]] = []
    try:
        if isinstance(info.get(b"files"), list):
            for f in info[b"files"]:
                path = "/".join(_text(p) for p in f.get(b"path", []))
                files.append((f"{name}/{path}", int(f.get(b"length", 0))))
        elif b"length" in info:
            files.append((name, int(info[b"length"])))
        elif isinstance(info.get(b"file tree"), dict):
            _walk_v2(info[b"file tree"], name + "/", files)
    except (AttributeError, TypeError, ValueError) as e:
        raise BencodeError("malformed file list") from e

    trackers = []
    if isinstance(root.get(b"announce"), bytes):
        trackers.append(_text(root[b"announce"]))
    for tier in root.get(b"announce-list", []) or []:
        if isinstance(tier, list):
            trackers.extend(_text(t) for t in tier if isinstance(t, bytes))

    return TorrentInfo(
        infohash=hashlib.sha1(data[start:end]).hexdigest(),
        name=name,
        files=files,
        trackers=list(dict.fromkeys(trackers)),
        private=info.get(b"private") == 1,
    )

"""Magnet link parsing and canonical rebuilding.

Magnets from third parties are never passed through as-is. We extract the
info-hash, display name, trackers and https web-seeds, validate each, and
rebuild a clean URI. Parameters like `as=` / `xs=` (which tell clients to
fetch from arbitrary HTTP hosts) are dropped.
"""
from __future__ import annotations

import base64
import ipaddress
import re
import urllib.parse

HEX40 = re.compile(r"^[0-9a-fA-F]{40}$")
B32 = re.compile(r"^[A-Za-z2-7]{32}$")
TRACKER_KEY = re.compile(r"^tr(\.\d+)?$")
TRACKER_SCHEMES = {"udp", "http", "https", "ws", "wss"}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
MAX_TRACKERS = 25
MAX_MAGNET_LEN = 8000


def normalize_infohash(value: str) -> str | None:
    v = (value or "").strip()
    if HEX40.match(v):
        return v.lower()
    if B32.match(v):
        return base64.b32decode(v.upper()).hex()
    return None


def clean_name(name: str) -> str:
    return " ".join(CONTROL.sub(" ", name or "").split())[:200]


def _host_ok(host: str | None) -> bool:
    if not host:
        return False
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return True  # a hostname; trackers are resolved by the torrent client
    return ip.is_global


def valid_tracker(url: str) -> bool:
    if len(url) > 300:
        return False
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return (parts.scheme in TRACKER_SCHEMES and not parts.username
            and _host_ok(parts.hostname))


def valid_webseed(url: str) -> bool:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return False
    return parts.scheme == "https" and not parts.username and _host_ok(parts.hostname) and len(url) <= 500


def parse_magnet(uri: str) -> dict | None:
    """Return {infohash, name, trackers, webseeds} or None if not a usable magnet."""
    if not uri or len(uri) > MAX_MAGNET_LEN or not uri[:8].lower() == "magnet:?":
        return None
    infohash, name, trackers, webseeds = None, "", [], []
    for key, value in urllib.parse.parse_qsl(uri[8:]):
        key = key.lower()
        if key == "xt" and value.lower().startswith("urn:btih:"):
            infohash = infohash or normalize_infohash(value[9:])
        elif key == "dn":
            name = clean_name(value)
        elif TRACKER_KEY.match(key) and valid_tracker(value):
            trackers.append(value)
        elif key == "ws" and valid_webseed(value):
            webseeds.append(value)
    if not infohash:
        return None
    return {"infohash": infohash, "name": name, "trackers": trackers, "webseeds": webseeds}


def build_magnet(infohash: str, name: str = "", trackers=(), webseeds=()) -> str:
    parts = [f"xt=urn:btih:{infohash}"]
    if name:
        parts.append("dn=" + urllib.parse.quote(clean_name(name), safe=""))
    seen = set()
    for tr in trackers:
        if tr not in seen and valid_tracker(tr) and len(seen) < MAX_TRACKERS:
            seen.add(tr)
            parts.append("tr=" + urllib.parse.quote(tr, safe=""))
    for ws in dict.fromkeys(webseeds):
        if valid_webseed(ws):
            parts.append("ws=" + urllib.parse.quote(ws, safe=""))
    return "magnet:?" + "&".join(parts)

"""Living-room TV access: pairing, the OTT "where to watch" list, playable Internet
Archive files, and sending a download to the PC's torrent app.

The TV reaches this server over the home network, so everything here assumes the
caller may be any device on the Wi-Fi: a paired TV can search, browse, play and
start a download on the PC, and nothing else (settings, scanner and quarantine
stay loopback-only; app.py enforces that)."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import os
import re
import secrets
import socket
import sys
import threading
import time
import urllib.parse
from pathlib import PurePosixPath

from . import osutil
from .providers.archive_org import is_video, item_files
from .quality import from_dims, label as quality_label
from .subtitles import describe, for_video, is_subtitle

COOKIE = "buzztv"
PIN_LEN = 6
MAX_TOKENS = 10

# Indian and global OTT services. "apps" are matched against the labels of apps installed
# on the TV; "site" drives the web fallback (a Google search limited to that site, which
# works even when a service changes its own search URL).
SERVICES = [
    {"id": "zee5", "name": "ZEE5", "apps": ["zee5"], "site": "zee5.com"},
    {"id": "hotstar", "name": "JioHotstar", "apps": ["jiohotstar", "hotstar", "disney+ hotstar"], "site": "hotstar.com"},
    {"id": "sonyliv", "name": "SonyLIV", "apps": ["sonyliv", "sony liv"], "site": "sonyliv.com"},
    {"id": "netflix", "name": "Netflix", "apps": ["netflix"], "site": "netflix.com"},
    {"id": "prime", "name": "Prime Video", "apps": ["prime video", "amazon prime video"], "site": "primevideo.com"},
    {"id": "youtube", "name": "YouTube", "apps": ["youtube"], "site": "youtube.com"},
    {"id": "mxplayer", "name": "MX Player", "apps": ["mx player", "amazon mx player"], "site": "mxplayer.in"},
    {"id": "waves", "name": "WAVES (Doordarshan)", "apps": ["waves", "prasar bharati"], "site": "waves.pb.gov.in"},
    {"id": "aha", "name": "aha (Telugu/Tamil)", "apps": ["aha"], "site": "aha.video"},
    {"id": "sunnxt", "name": "Sun NXT", "apps": ["sun nxt"], "site": "sunnxt.com"},
    {"id": "hoichoi", "name": "hoichoi (Bengali)", "apps": ["hoichoi"], "site": "hoichoi.tv"},
    {"id": "chaupal", "name": "Chaupal (Punjabi/Bhojpuri)", "apps": ["chaupal"], "site": "chaupal.tv"},
    {"id": "stage", "name": "STAGE (Bhojpuri/Haryanvi)", "apps": ["stage"], "site": "stage.in"},
    {"id": "shemaroo", "name": "ShemarooMe", "apps": ["shemaroome", "shemaroo"], "site": "shemaroome.com"},
    {"id": "etvwin", "name": "ETV Win", "apps": ["etv win"], "site": "etvwin.com"},
    {"id": "manorama", "name": "ManoramaMAX", "apps": ["manoramamax", "manorama max"], "site": "manoramamax.com"},
    {"id": "lionsgate", "name": "Lionsgate Play", "apps": ["lionsgate play", "lionsgateplay"], "site": "lionsgateplay.com"},
    {"id": "appletv", "name": "Apple TV", "apps": ["apple tv"], "site": "tv.apple.com"},
    {"id": "erosnow", "name": "Eros Now", "apps": ["eros now", "erosnow"], "site": "erosnow.com"},
    {"id": "hungama", "name": "Hungama Play", "apps": ["hungama play", "hungama"], "site": "hungama.com"},
    {"id": "discovery", "name": "discovery+", "apps": ["discovery+", "discovery plus"], "site": "discoveryplus.in"},
    {"id": "mubi", "name": "MUBI", "apps": ["mubi"], "site": "mubi.com"},
    {"id": "klikk", "name": "KLiKK (Bengali)", "apps": ["klikk"], "site": "klikk.tv"},
    {"id": "planetmarathi", "name": "Planet Marathi", "apps": ["planet marathi"], "site": "planetmarathi.com"},
    {"id": "tataplay", "name": "Tata Play Binge", "apps": ["tata play binge", "binge"], "site": "tataplaybinge.com"},
    {"id": "airtel", "name": "Airtel Xstream", "apps": ["airtel xstream", "xstream"], "site": "airtelxstream.in"},
]


# Services with a free tier (with ads, or a free section) are listed first. Paid-only titles
# still need the service's own subscription; nothing here gets around that.
FREE = {"youtube": "Free full movies on official channels", "mxplayer": "Free with ads", "waves": "Free",
        "zee5": "Some free", "hotstar": "Some free", "airtel": "Some free", "sonyliv": "Some free",
        "shemaroo": "Some free", "hungama": "Some free"}
# Words added to the search so the service lands on full films rather than clips
QUERY_SUFFIX = {"youtube": " full movie"}


def services_for(title: str) -> list[dict]:
    q = " ".join(str(title).split())[:120]
    out = []
    for s in SERVICES:
        query = q + QUERY_SUFFIX.get(s["id"], "")
        web = ("https://www.youtube.com/results?" + urllib.parse.urlencode({"search_query": query})
               if s["id"] == "youtube" else
               "https://www.google.com/search?" + urllib.parse.urlencode({"q": f"{q} site:{s['site']}"}))
        out.append({**s, "query": query, "web": web, "free": FREE.get(s["id"], "")})
    tier = lambda s: 0 if s["id"] == "youtube" else 1 if s["free"] in ("Free", "Free with ads") else 2 if s["free"] else 3
    out.sort(key=tier)
    return out


IDENT = re.compile(r"^[A-Za-z0-9._-]{1,120}$")     # an Internet Archive item id


def b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode("utf-8")).decode().rstrip("=")


def unb64(ref: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,800}", ref or ""):
        raise ValueError("bad reference")
    try:
        return base64.urlsafe_b64decode(ref + "=" * (-len(ref) % 4)).decode("utf-8")
    except (ValueError, UnicodeDecodeError) as e:
        raise ValueError("bad reference") from e


# ---------------------------------------------------------------- signed links
class Signer:
    """Short-lived signed links (stream, subtitles, progress) for the TV app's native player,
    which can't carry the pairing cookie. Only handed out to paired devices."""

    def __init__(self, secret: str):
        self.key = secret.encode()

    def sign(self, path: str, ttl: int = 12 * 3600) -> str:
        exp = int(time.time()) + ttl
        sig = hashlib.sha256(self.key + f"|{path}|{exp}".encode()).hexdigest()[:32]
        return f"{path}?t={exp}.{sig}"

    def check(self, path: str, token: str) -> bool:
        exp, _, sig = (token or "").partition(".")
        if not exp.isdigit() or int(exp) < time.time():
            return False
        good = hashlib.sha256(self.key + f"|{path}|{exp}".encode()).hexdigest()[:32]
        return secrets.compare_digest(sig, good)


# ---------------------------------------------------------------- pairing
def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_pin() -> str:
    return "".join(secrets.choice("0123456789") for _ in range(PIN_LEN))


class Pairing:
    """A 6-digit PIN shown on the PC pairs a TV once; the TV then keeps a long random
    token (stored here only as a hash). Wrong PINs lock pairing for a growing delay."""

    def __init__(self, cfg: dict, save):
        self.cfg, self.save = cfg, save            # cfg is the "tv" section; save() persists it
        self.lock = threading.Lock()
        self.fails, self.locked_until = 0, 0.0

    def check(self, token: str | None) -> bool:
        return bool(token) and _hash(token) in self.cfg.get("tokens", [])

    def pair(self, pin: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self.lock:
            if now < self.locked_until:
                raise PermissionError(f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s")
            if not secrets.compare_digest(str(pin), str(self.cfg.get("pin", ""))) or not self.cfg.get("pin"):
                self.fails += 1
                if self.fails >= 5:
                    self.locked_until = now + 30 * 2 ** min(self.fails - 5, 6)
                raise PermissionError("Wrong PIN. Check BuzzTorrent → Settings → TV on the PC")
            self.fails = 0
            token = secrets.token_urlsafe(32)
            self.cfg["tokens"] = (self.cfg.get("tokens", []) + [_hash(token)])[-MAX_TOKENS:]
            self.save()
            return token


# ---------------------------------------------------------------- phone remote
REMOTE_ACTIONS = {"search", "open", "play", "home"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def remote_command(body: dict, categories, langs) -> dict:
    """Validates a command from the phone remote (search on the TV, open/play a result)."""
    action = str(body.get("action", ""))
    if action not in REMOTE_ACTIONS:
        raise ValueError("unknown remote action")
    cmd: dict = {"action": action}
    if action == "search":
        q = " ".join(_CONTROL.sub(" ", str(body.get("q", ""))).split())[:200]
        if not q:
            raise ValueError("type something to search for")
        cmd["q"] = q
        if body.get("cat") in categories:
            cmd["cat"] = body["cat"]
        if body.get("lang") in langs:
            cmd["lang"] = body["lang"]
    elif action in ("open", "play"):
        rid = str(body.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", rid):
            raise ValueError("bad result id")
        cmd["id"] = rid
    return cmd


class RemoteChannel:
    """Commands from phones to every TV that is listening. TVs long-poll with the last
    sequence number they saw; a TV that has just connected only gets new commands."""
    KEEP_SECONDS = 120

    def __init__(self):
        self.cond = threading.Condition()
        self.seq = 0
        self.items: list[dict] = []

    def post(self, cmd: dict) -> int:
        with self.cond:
            self.seq += 1
            now = time.time()
            self.items = [c for c in self.items if now - c["at"] < self.KEEP_SECONDS][-49:]
            self.items.append({**cmd, "seq": self.seq, "at": now})
            self.cond.notify_all()
            return self.seq

    def wait(self, after: int, timeout: float = 25) -> dict:
        with self.cond:
            if after < 0 or after > self.seq:          # first poll (or server restarted): start from now
                return {"seq": self.seq, "commands": []}
            self.cond.wait_for(lambda: self.seq > after, timeout=timeout)
            cmds = [{k: v for k, v in c.items() if k != "at"} for c in self.items if c["seq"] > after]
            return {"seq": self.seq, "commands": cmds}


# ---------------------------------------------------------------- network
PRIVATE_NETS = [ipaddress.ip_network(n) for n in
                ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "169.254.0.0/16")]


def is_lan_ip(text: str) -> bool:
    try:
        ip = ipaddress.ip_address(text)
    except ValueError:
        return False
    return ip.version == 4 and any(ip in n for n in PRIVATE_NETS)


def host_is_lan_literal(host_header: str, port: int) -> bool:
    """Host header names us by a private IPv4 address. DNS-rebinding attacks need a
    hostname, so accepting IP literals keeps that protection intact."""
    host, _, p = host_header.rpartition(":")
    return p == str(port) and is_lan_ip(host)


def lan_addresses() -> list[str]:
    """This PC's home-network IPv4 addresses, the one with the default route first."""
    found = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))           # no packet is sent; just picks the outgoing interface
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out = []
    for a in found:
        if is_lan_ip(a) and not a.startswith("169.254.") and a not in out:
            out.append(a)
    return out


# ---------------------------------------------------------------- playback
PLAYABLE_EXT = (".mp4", ".m4v", ".webm")     # what the TV app's built-in player handles
QUALITY_SUFFIX = re.compile(r"([ ._-]?(4320p|2160p|1440p|1080p|720p|576p|480p|360p|240p|8k|4k|uhd|hd|sd|surround|stereo))+$",
                            re.I)
_NUM = re.compile(r"(\d+)")


def _natural(name: str):
    return [int(p) if p.isdigit() else p.lower() for p in _NUM.split(name)]


def _int(v) -> int | None:
    try:
        return int(v or 0) or None
    except (TypeError, ValueError):
        return None


def _rendition(ident: str, f: dict) -> dict:
    name = str(f["name"])
    height = from_dims(f.get("width"), f.get("height"))
    return {"file": name, "url": f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}",
            "size": _int(f.get("size")), "height": height, "label": quality_label(height),
            "format": str(f.get("format") or PurePosixPath(name).suffix.lstrip(".").upper())}


def pick_playable(ident: str, files: list[dict]) -> list[dict]:
    """One entry per video (episode order), each with the best copy the TV app can play
    itself and, when a sharper copy exists only in another format (often a 1080p .mkv
    upload next to the Archive's 480p .mp4), that one as "alt" for a player app such as VLC."""
    groups: dict[str, list[dict]] = {}
    for f in files:
        if not is_video(f):
            continue
        name = str(f["name"])
        source = name if f.get("source") == "original" else str(f.get("original") or name)
        key = re.sub(r"_512kb$", "", PurePosixPath(source).stem, flags=re.I)
        # "film_1080p" and "film_720p" are copies of one video, not two episodes
        key = QUALITY_SUFFIX.sub("", key) or key
        groups.setdefault(key, []).append(f)

    def score(f: dict):
        low = "512kb" in str(f.get("name", "")).lower() or "512Kb" in str(f.get("format", ""))
        return (from_dims(f.get("width"), f.get("height")) or 0, not low, f.get("source") == "original")

    sub_names = [str(f.get("name", "")) for f in files if is_subtitle(str(f.get("name", "")))]
    out = []
    for key, group in sorted(groups.items(), key=lambda kv: _natural(kv[0])):
        inapp = [f for f in group if str(f["name"]).lower().endswith(PLAYABLE_EXT)]
        best_in = max(inapp, key=score) if inapp else None
        best_all = max(group, key=score)
        main = _rendition(ident, best_in) if best_in else None
        alt = None
        if best_all is not best_in and (from_dims(best_all.get("width"), best_all.get("height")) or 0) > \
                ((main or {}).get("height") or 0):
            alt = _rendition(ident, best_all)
        if not main and not alt:
            continue
        lengths = [f.get("length") for f in group if f.get("length")]
        try:
            secs = float(lengths[0]) if lengths else None
        except ValueError:
            secs = None                                     # some items store "12:34" style lengths
        first = main or alt
        # Every copy, sharpest first: the TV app's native player starts at the top and
        # falls back down the list if the TV can't decode one.
        versions = [_rendition(ident, f) for f in sorted(group, key=score, reverse=True)]
        best = versions[0]
        out.append({"name": str(next((f.get("title") for f in group if f.get("title")), key)),
                    "file": main["file"] if main else None, "url": main["url"] if main else None,
                    "size": first["size"], "height": first["height"], "label": first["label"],
                    "seconds": secs, "alt": alt, "versions": versions, "key": f"ia:{ident}:{key}"[:400],
                    "subs": describe(for_video(key + ".x", sub_names, len(groups) == 1)),
                    "best": {"label": best["label"], "height": best["height"], "format": best["format"]}})
    return out[:500]


def playable_files(ident: str) -> list[dict]:
    return pick_playable(ident, item_files(ident))


# ---------------------------------------------------------------- send to PC
def magnet_handler() -> str | None:
    """The program Windows opens magnet links with, or None. (macOS doesn't say without
    extra tools; BuzzTorrent's own downloader is used there anyway.)"""
    if os.name != "nt":
        return "your torrent app" if sys.platform == "darwin" else None
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, r"magnet\shell\open\command") as k:
            cmd = str(winreg.QueryValue(k, None) or "")
    except OSError:
        return None
    m = re.match(r'\s*"([^"]+)"|\s*(\S+)', cmd)
    exe = (m.group(1) or m.group(2)) if m else ""
    return os.path.splitext(os.path.basename(exe))[0] or None


def open_on_pc(magnet: str) -> str:
    if not magnet.startswith("magnet:?xt=urn:btih:"):
        raise ValueError("not a magnet link")
    app = magnet_handler()
    if not app:
        raise ValueError("No torrent app on the PC is set up to open magnet links")
    osutil.open_path(magnet)          # a vetted magnet URI, opened by the user's own torrent app
    return app

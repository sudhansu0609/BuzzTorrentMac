"""BuzzTorrent local web app.  Run:  python app.py   then open http://127.0.0.1:8765"""
from __future__ import annotations

import argparse
import hashlib
import http.cookies
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from buzz import __version__
from buzz.engine import Engine, NotFound
from buzz.models import CATEGORIES
from buzz.providers import ArchiveOrgProvider, TorznabProvider
from buzz.safehttp import FetchError, FetchPolicy, fetch
from buzz.scanner import DownloadScanner
from buzz.smart import SmartSearch
from buzz.fuzzy import LANGUAGES
from buzz.providers.archive_org import GENRE_QUERY
from buzz import tv
from buzz import osutil
from buzz import subtitles as subs_mod
from buzz.library import CONTENT_TYPES, Library
from buzz.quality import label as quality_label
from buzz.tvstate import TvState
from buzz.bencode import BencodeError, parse_torrent
from buzz.downloader import Downloader, DownloaderError, find_aria2
from buzz.adult import COOKIE as ADULT_COOKIE, AdultGate
from buzz.details import IA_IDENT

LANG_IDS = {"any", *LANGUAGES}
GENRE_IDS = {"any", *GENRE_QUERY}

ROOT = Path(__file__).resolve().parent
CONFIG = ROOT / "config.json"
APK = ROOT / "android" / "build" / "BuzzTorrentTV.apk"
STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/style.css": ("style.css", "text/css; charset=utf-8"),
    "/favicon.svg": ("favicon.svg", "image/svg+xml"),
    "/favicon.png": ("favicon.png", "image/png"),
    "/tv": ("tv.html", "text/html; charset=utf-8"),
    "/tv.js": ("tv.js", "text/javascript; charset=utf-8"),
    "/tv.css": ("tv.css", "text/css; charset=utf-8"),
    "/remote": ("remote.html", "text/html; charset=utf-8"),
    "/remote.js": ("remote.js", "text/javascript; charset=utf-8"),
    "/remote.css": ("remote.css", "text/css; charset=utf-8"),
}
# What a device on the home network may use. Everything else (settings, sources, the
# download scanner, quarantine) only answers this PC.
REMOTE_OPEN = {"/tv", "/tv.js", "/tv.css", "/remote", "/remote.js", "/remote.css", "/favicon.svg", "/favicon.png",
               "/BuzzTorrentTV.apk", "/api/tv/hello", "/api/tv/pair"}
REMOTE_PAIRED = {"/api/search", "/api/suggest", "/api/details", "/api/image", "/api/browse/options", "/api/browse",
                 "/api/thumb", "/api/tv/play", "/api/tv/services", "/api/tv/send", "/api/tv/remote",
                 "/api/tv/remote/poll", "/api/tv/result", "/api/tv/library", "/api/tv/continue",
                 "/api/tv/watchlist", "/api/tv/watch", "/api/tv/alerts/seen", "/api/tv/progress",
                 "/api/tv/shelves", "/api/tv/shelf", "/api/adult/state", "/api/adult/unlock", "/api/adult/lock",
                 "/api/downloads", "/api/downloads/action"}
PUBLIC_CATEGORIES = tuple(c for c in CATEGORIES if c != "adult")
# Links the TV app's native player opens itself (it can't send the pairing cookie): they carry
# a short-lived signature instead, handed out only to paired devices.
SIGNED_PREFIXES = ("/api/tv/stream/", "/api/tv/subtitles/", "/api/tv/progress")
SUB_POLICY = FetchPolicy(("archive.org",), max_bytes=subs_mod.MAX_BYTES, timeout=15)
# The TV page streams Internet Archive videos straight from archive.org.
TV_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; "
          "media-src https://archive.org https://*.archive.org; base-uri 'none'; form-action 'self'; "
          "frame-ancestors 'none'")
FIREWALL_RULE = "BuzzTorrent TV"
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'none'; script-src 'self'; style-src 'self'; "
                                "img-src 'self' data:; connect-src 'self'; "
                                "media-src 'self' https://archive.org https://*.archive.org; base-uri 'none'; "
                                "form-action 'self'; frame-ancestors 'none'"),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Cache-Control": "no-store",
}
CONTROL = re.compile(r"[\x00-\x1f\x7f]")
NAME_RE = re.compile(r"^[\w .()\-]{1,40}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_\-]{0,128}$")
DEFAULTS = {
    "host": "127.0.0.1",
    "port": 8765,
    "archive_org": True,
    "torznab": [],
    "extra_trackers": ["udp://tracker.opentrackr.org:1337/announce", "udp://open.demonii.com:1337/announce"],
    "cache_ttl": 600,
    "scanner": {"enabled": False, "folder": None, "auto_quarantine": True, "virustotal_key": ""},
    "tv": {"enabled": False, "pin": "", "tokens": [], "autostart": False},
    # BuzzTorrent's own downloader (aria2). Shares back until ratio 1.0 or 30 minutes after finishing.
    "downloader": {"aria2": None, "folder": None, "seed_ratio": 1.0, "seed_minutes": 30},
    # Private 18+ section: off and hidden unless turned on with a PIN; per-device unlock.
    "adult": {"enabled": False, "pin_hash": "", "pin_salt": "", "on_tv": False},
}
MAX_BODY = 64 * 1024


def load_config() -> dict:
    # utf-8-sig: Notepad and PowerShell often add a BOM when the file is edited by hand
    cfg = json.loads(CONFIG.read_text(encoding="utf-8-sig")) if CONFIG.exists() else {}
    merged = {**DEFAULTS, **cfg}
    merged["scanner"] = {**DEFAULTS["scanner"], **cfg.get("scanner", {})}
    merged["tv"] = {**DEFAULTS["tv"], **cfg.get("tv", {})}
    merged["downloader"] = {**DEFAULTS["downloader"], **cfg.get("downloader", {})}
    merged["adult"] = {**DEFAULTS["adult"], **cfg.get("adult", {})}
    if not CONFIG.exists():
        save_config(merged)
    return merged


def save_config(cfg: dict) -> None:
    tmp = CONFIG.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    os.replace(tmp, CONFIG)


SMART = SmartSearch()   # shared, so its caches survive source changes


def build_engine(cfg: dict) -> Engine:
    providers = [ArchiveOrgProvider()] if cfg["archive_org"] else []
    for src in cfg["torznab"]:
        if src.get("enabled", True):
            providers.append(TorznabProvider(src["name"], src["url"], src.get("apikey", ""),
                                             src.get("allow_private", True)))
    if not providers:
        raise ValueError("enable at least one source")
    return Engine(providers, cfg["extra_trackers"], cfg["cache_ttl"], smart=SMART)


def validate_source(src: dict, old: dict | None) -> dict:
    name = str(src.get("name", "")).strip()
    url = str(src.get("url", "")).strip()
    key = str(src.get("apikey", "")).strip()
    if not NAME_RE.match(name):
        raise ValueError("Name: 1-40 letters, numbers, spaces, . ( ) -")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username:
        raise ValueError(f"{name}: URL must be http(s)://host/... without credentials")
    if not KEY_RE.match(key):
        raise ValueError(f"{name}: API key may only contain letters, numbers, - and _")
    if not key and old:
        key = old.get("apikey", "")  # blank in the form = keep the stored key
    return {"name": name, "url": url, "apikey": key, "enabled": bool(src.get("enabled", True)),
            "allow_private": bool(src.get("allow_private", True))}


def public_settings(cfg: dict) -> dict:
    return {
        "archive_org": cfg["archive_org"],
        "torznab": [{"name": s["name"], "url": s["url"], "enabled": s.get("enabled", True),
                     "has_key": bool(s.get("apikey"))} for s in cfg["torznab"]],
        "version": __version__,
    }


class Server(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second process silently share the port.
    allow_reuse_address = os.name != "nt"
    daemon_threads = True

    def handle_error(self, request, client_address):
        # The browser navigated away mid-response: normal, not worth a traceback.
        if isinstance(sys.exc_info()[1], (ConnectionAbortedError, ConnectionResetError, BrokenPipeError)):
            return
        super().handle_error(request, client_address)


class Handler(BaseHTTPRequestHandler):
    server_version = "BuzzTorrent"
    sys_version = ""
    engine: Engine
    scanner: DownloadScanner
    presence = None   # Presence, in app mode
    cfg: dict
    allowed_hosts: set[str]
    cfg_lock = threading.Lock()
    port = 8765
    lan_bound = False      # listening on the home network (TV access was on at start-up)
    app_mode = False       # started by the launcher, so it can restart itself
    pairing: tv.Pairing
    remote_channel = tv.RemoteChannel()
    downloader: Downloader
    adult: AdultGate
    library: Library
    tvstate: TvState
    signer: tv.Signer
    sub_cache: dict = {}

    def log_message(self, fmt, *args):
        # Request lines carry search words in their query string: log the path only (privacy)
        args = tuple(re.sub(r"\?\S*", "", a, count=1) if isinstance(a, str) and a.startswith(("GET ", "POST ")) else a
                     for a in args)
        sys.stderr.write("  %s\n" % (fmt % args))

    def _send(self, status: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, obj, extra: dict | None = None) -> None:
        self._send(status, json.dumps(obj).encode(), "application/json; charset=utf-8", extra)

    def _netloc_ok(self, netloc: str) -> bool:
        netloc = netloc.lower()
        return netloc in self.allowed_hosts or (self._tv_on() and tv.host_is_lan_literal(netloc, self.port))

    def _host_ok(self) -> bool:
        # Reject requests whose Host header isn't us: stops DNS-rebinding attacks
        # where a malicious web page tries to drive this local API.
        return self._netloc_ok(self.headers.get("Host") or "")

    @classmethod
    def _tv_on(cls) -> bool:
        return cls.lan_bound and cls.cfg["tv"]["enabled"]

    @property
    def remote(self) -> bool:
        try:
            return not ipaddress.ip_address(self.client_address[0]).is_loopback
        except ValueError:
            return True

    def _token(self) -> str | None:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        return jar[tv.COOKIE].value if tv.COOKIE in jar else None

    def _cookie(self, name: str) -> str | None:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie") or "")
        except http.cookies.CookieError:
            return None
        return jar[name].value if name in jar else None

    def _adult_ok(self) -> bool:
        """The private section is unlocked for this device (and allowed on TVs, if it's one)."""
        if self.remote and not self.adult.cfg.get("on_tv"):
            return False
        return self.adult.valid(self._cookie(ADULT_COOKIE))

    def _private_names(self) -> set[str]:
        return self.downloader.private_names() if hasattr(self, "downloader") else set()

    def _remote_refusal(self, path: str, query: str = "") -> tuple[int, dict] | None:
        """None if this request may proceed. Devices on the network only get the TV
        surface, and only after pairing with the PIN shown on the PC."""
        if not self.remote:
            return None
        if not self._tv_on():
            return 403, {"error": "TV access is off. Turn it on in BuzzTorrent settings on the PC"}
        if path in REMOTE_OPEN:
            return None
        if path.startswith(SIGNED_PREFIXES):
            token = (urllib.parse.parse_qs(query).get("t") or [""])[0]
            if self.signer.check(path, token) or self.pairing.check(self._token()):
                return None
            return 401, {"error": "link expired, open it again from the TV", "pair": not token}
        if path in REMOTE_PAIRED:
            return None if self.pairing.check(self._token()) else (401, {"error": "not paired", "pair": True})
        return 403, {"error": "only available on the PC"}

    def do_GET(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host header"})
        url = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(url.query)
        arg = lambda k: (qs.get(k) or [""])[0]
        if self.remote and url.path == "/" and self._tv_on():
            ua = self.headers.get("User-Agent") or ""
            phone = "Mobi" in ua and "BuzzTorrentTV" not in ua     # phones get the remote, TVs the TV page
            self.send_response(302)
            self.send_header("Location", "/remote" if phone else "/tv")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        refused = self._remote_refusal(url.path, url.query)
        if refused:
            return self._json(*refused)
        try:
            if url.path in STATIC:
                name, ctype = STATIC[url.path]
                extra = {"Content-Security-Policy": TV_CSP} if url.path == "/tv" else None
                return self._send(200, (ROOT / "static" / name).read_bytes(), ctype, extra)
            if url.path == "/api/player/info":
                return self._json(200, {"vlc": bool(find_vlc())})
            if url.path == "/api/adult/state":
                a = self.adult
                shown = a.enabled and (not self.remote or a.cfg.get("on_tv"))
                return self._json(200, {"enabled": shown, "unlocked": shown and self._adult_ok(),
                                        **({} if self.remote else {"configured": bool(a.cfg.get("pin_hash")),
                                                                   "on_tv": bool(a.cfg.get("on_tv")),
                                                                   "turned_on": bool(a.cfg.get("enabled"))})})
            if url.path == "/api/downloads":
                d = self.downloader
                open_ = self._adult_ok()
                items = [{**x, "title": "Private download", "name": ""} if x.get("private") and not open_ else x
                         for x in d.list()]
                return self._json(200, {"available": d.available, "running": d.running(),
                                        "folder": str(d.folder or self.cfg["downloader"].get("folder") or ""),
                                        "items": items})
            if url.path == "/api/scanner":
                status = self.scanner.status()
                private = self._private_names()
                if private:
                    open_ = self._adult_ok()
                    status["items"] = [{**it, "private": True, **({} if open_ else {"label": "Private download"})}
                                       if it.get("name") in private else it for it in status.get("items", [])]
                return self._json(200, status)
            if url.path.startswith("/api/tv/") or url.path == "/BuzzTorrentTV.apk":
                return self._tv_get(url.path, arg)
            if url.path == "/api/search":
                q = " ".join(CONTROL.sub(" ", arg("q")).split())[:200]
                cat = arg("cat") or "all"
                if not q or cat not in CATEGORIES:
                    return self._json(400, {"error": "need q (1-200 chars) and a valid cat"})
                lang, genre = arg("lang") or "any", arg("genre") or "any"
                if lang not in LANG_IDS or genre not in GENRE_IDS:
                    return self._json(400, {"error": "unknown language or genre"})
                if cat == "adult":
                    if not self._adult_ok():
                        return self._json(403, {"error": "The private section is locked", "locked": True})
                    # nothing leaves this PC: no Wikipedia lookups for private searches
                    return self._json(200, self.engine.search(q, cat, smart=False, lang=lang, genre=genre))
                return self._json(200, self.engine.search(q, cat, smart=arg("smart") != "0", lang=lang, genre=genre))
            if url.path == "/api/suggest":
                q = " ".join(CONTROL.sub(" ", arg("q")).split())[:100]
                cat = arg("cat") or "all"
                if cat not in PUBLIC_CATEGORIES or arg("smart") == "0":
                    return self._json(200, {"query": q, "suggestions": []})
                lang = arg("lang") if arg("lang") in LANG_IDS else "any"
                return self._json(200, self.engine.suggest(q, cat, lang=lang))
            if url.path == "/api/details":
                r = self.engine._get(arg("id"))
                if r.category == "adult":
                    if not self._adult_ok():
                        return self._json(403, {"error": "The private section is locked", "locked": True})
                    return self._json(200, self.engine.details.for_result(r, use_wiki=False))
                return self._json(200, self.engine.details.for_result(r, use_wiki=arg("smart") != "0"))
            if url.path == "/api/image":
                try:
                    data, kind = self.engine.details.image(arg("k")[:40])
                except FetchError:
                    return self._json(404, {"error": "no image"})
                return self._send(200, data, kind, {"Cache-Control": "private, max-age=86400"})
            if url.path == "/api/browse/options":
                return self._json(200, {"sources": self.engine.browse_options()})
            if url.path == "/api/browse":
                cat = arg("cat") or "all"
                page = arg("page") or "1"
                if cat not in CATEGORIES or not page.isdigit():
                    return self._json(400, {"error": "bad category or page"})
                if cat == "adult" and not self._adult_ok():
                    return self._json(403, {"error": "The private section is locked", "locked": True})
                source = arg("source") or next(iter(self.engine.browse_options()), "")
                try:
                    lang, genre = arg("lang") or "any", arg("genre") or "any"
                    if lang not in LANG_IDS or genre not in GENRE_IDS:
                        return self._json(400, {"error": "unknown language or genre"})
                    return self._json(200, self.engine.browse(source, cat, arg("col")[:60], arg("sort") or "popular",
                                                              arg("decade") or "any", int(page),
                                                              family=arg("family") != "0", lang=lang, genre=genre))
                except FetchError as e:
                    return self._json(502, {"error": f"{source}: {e}"})
            if url.path == "/api/thumb":
                try:
                    data, kind = self.engine.thumbnail(arg("id"))
                except (FetchError, NotFound):
                    return self._json(404, {"error": "no thumbnail"})
                return self._send(200, data, kind, {"Cache-Control": "private, max-age=86400"})
            if url.path == "/api/inspect":
                return self._json(200, self.engine.inspect(arg("id")))
            if url.path == "/api/torrent":
                name, data = self.engine.torrent_file(arg("id"), force=arg("force") == "1")
                quoted = urllib.parse.quote(name)
                return self._send(200, data, "application/x-bittorrent",
                                  {"Content-Disposition": f"attachment; filename*=UTF-8''{quoted}"})
            if url.path == "/api/settings":
                return self._json(200, public_settings(self.cfg))
            if url.path == "/api/ping":
                alive = self.presence.ping(arg("c")) if self.presence else False
                return self._json(200, {"app": bool(self.presence), "ok": alive})
            if url.path == "/api/sources":
                return self._json(200, {"sources": list(self.engine.providers), "categories": CATEGORIES})
            return self._json(404, {"error": "not found"})
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return  # client went away
        except NotFound as e:
            return self._json(404, {"error": e.args[0]})
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except Exception as e:  # never leak a traceback to the browser
            self.log_error("internal error: %r", e)
            return self._json(500, {"error": "internal error"})

    def _read_json(self) -> dict:
        # CSRF guard: a cross-site page can't set a custom header or a JSON
        # content type without a CORS preflight, which this server never approves.
        if self.headers.get("X-BuzzTorrent") != "1" or \
                "application/json" not in (self.headers.get("Content-Type") or ""):
            raise PermissionError("missing CSRF header")
        origin = self.headers.get("Origin")
        if origin and not self._netloc_ok(urllib.parse.urlsplit(origin).netloc):
            raise PermissionError("cross-origin request refused")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            raise ValueError("bad request body size")
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return data

    def do_POST(self):
        if not self._host_ok():
            return self._json(403, {"error": "bad host header"})
        parts = urllib.parse.urlsplit(self.path)
        path = parts.path
        refused = self._remote_refusal(path, parts.query)
        if refused:
            return self._json(*refused)
        if path == "/api/bye":
            # sendBeacon from a closing window can't add custom headers, but it only ever
            # removes the caller's own unguessable client id, so nothing else is affected
            if self.presence:
                q = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                self.presence.bye((q.get("c") or [""])[0])
            return self._json(200, {"ok": True})
        try:
            body = self._read_json()
            if path == "/api/settings":
                return self._save_settings(body)
            if path == "/api/sources/test":
                old = next((s for s in self.cfg["torznab"] if s["name"] == body.get("name")), None)
                src = validate_source(body, old)
                title = TorznabProvider(src["name"], src["url"], src["apikey"], src["allow_private"]).test()
                return self._json(200, {"ok": True, "message": f"Connected: {title}"})
            if path.startswith("/api/scanner/"):
                return self._scanner_action(path.rsplit("/", 1)[1], body)
            if path.startswith("/api/tv/"):
                return self._tv_post(path, body)
            if path == "/api/adult/unlock":
                if self.remote and not self.adult.cfg.get("on_tv"):
                    raise PermissionError("The private section isn't available on the TV")
                token = self.adult.unlock(str(body.get("pin", "")).strip())
                # a session cookie: gone when the app closes; the PC also forgets it after 30 idle minutes
                return self._json(200, {"ok": True}, {"Set-Cookie": f"{ADULT_COOKIE}={token}; Path=/; HttpOnly; SameSite=Strict"})
            if path == "/api/adult/lock":
                self.adult.lock_token(self._cookie(ADULT_COOKIE))
                return self._json(200, {"ok": True}, {"Set-Cookie": f"{ADULT_COOKIE}=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict"})
            if path == "/api/adult/config":
                if self.remote:
                    raise PermissionError("only available on the PC")
                with self.cfg_lock:
                    self.adult.configure(bool(body.get("enabled")), body.get("pin"), bool(body.get("on_tv")))
                return self._json(200, {"ok": True, "turned_on": self.adult.enabled, "on_tv": bool(self.adult.cfg.get("on_tv"))})
            if path == "/api/downloads/add":
                r = self.engine._get(str(body.get("id", "")))
                gid = self.start_download(r, force=bool(body.get("force")))
                return self._json(200, {"ok": True, "gid": gid, "folder": str(self.downloader.folder)})
            if path == "/api/library/open":
                # "Open in video player" on the PC: the file plays in whatever app Windows uses for it
                if self.remote:
                    raise PermissionError("only available on the PC")
                p = self.library.path(str(body.get("id", "")))
                if not p:
                    raise NotFound("not in the scanned library")
                osutil.open_path(str(p))          # a scanned, clean video from the library
                return self._json(200, {"ok": True})
            if path == "/api/play/external":
                if self.remote:
                    raise PermissionError("only available on the PC")
                link = str(body.get("url", ""))
                vlc = find_vlc()
                if not vlc or not re.fullmatch(r"https://([a-z0-9-]+\.)*archive\.org/download/[^\s]{1,600}", link):
                    raise ValueError("Install VLC to play this format on the PC")
                subprocess.Popen([vlc, "--no-video-title-show", link], creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                return self._json(200, {"ok": True})
            if path == "/api/downloads/action":
                self.downloader.action(str(body.get("gid", "")), str(body.get("action", "")))
                return self._json(200, {"ok": True})
            if path == "/api/downloads/open":
                folder = self.downloader.folder or Path(self.cfg["downloader"].get("folder") or "")
                if not folder or not Path(folder).is_dir():
                    raise ValueError("Nothing downloaded yet")
                osutil.open_path(str(folder))
                return self._json(200, {"ok": True})
            return self._json(404, {"error": "not found"})
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
            return  # client went away
        except PermissionError as e:
            return self._json(403, {"error": str(e)})
        except NotFound as e:
            return self._json(404, {"error": e.args[0]})
        except FetchError as e:
            return self._json(200, {"ok": False, "message": str(e)})
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        except Exception as e:
            self.log_error("internal error: %r", e)
            return self._json(500, {"error": "internal error"})

    def _save_settings(self, body: dict):
        with self.cfg_lock:
            old_by_name = {s["name"]: s for s in self.cfg["torznab"]}
            sources = [validate_source(s, old_by_name.get(str(s.get("name", "")).strip()))
                       for s in body.get("torznab", [])][:20]
            if len({s["name"] for s in sources}) != len(sources):
                raise ValueError("Each source needs a unique name")
            if sources and any(s["name"] == ArchiveOrgProvider.name for s in sources):
                raise ValueError(f"'{ArchiveOrgProvider.name}' is a reserved name")
            new_cfg = {**self.cfg, "archive_org": bool(body.get("archive_org", True)), "torznab": sources}
            engine = build_engine(new_cfg)  # raises ValueError if nothing is enabled
            save_config(new_cfg)
            old_engine, Handler.engine, Handler.cfg = Handler.engine, engine, new_cfg
            old_engine.close()
        return self._json(200, public_settings(new_cfg))


    def _scanner_action(self, action: str, body: dict):
        sc = self.scanner
        name = str(body.get("name", ""))
        if action == "config":
            old = self.cfg["scanner"]
            key = str(body.get("virustotal_key", "")).strip()
            if body.get("clear_virustotal"):
                key = ""
            elif not key:
                key = old.get("virustotal_key", "")
            elif not re.fullmatch(r"[0-9a-fA-F]{64}", key):
                raise ValueError("A VirusTotal API key is 64 hexadecimal characters")
            new = {"enabled": bool(body.get("enabled")), "folder": (body.get("folder") or "").strip() or None,
                   "auto_quarantine": bool(body.get("auto_quarantine", True)), "virustotal_key": key}
            sc.configure(new["enabled"], new["folder"], new["auto_quarantine"], key)
            new["folder"] = str(sc.folder) if sc.folder else None
            with self.cfg_lock:
                Handler.cfg = {**self.cfg, "scanner": new}
                save_config(Handler.cfg)
            return self._json(200, sc.status())
        if action == "scan":
            sc.request_scan(name)
            return self._json(200, sc.status())
        if action == "quarantine":
            done = sc.quarantine(name)
            return self._json(200, {"done": done, **sc.status()})
        if action == "forget":
            sc.forget(name)
            return self._json(200, sc.status())
        if action == "browse":
            return self._json(200, {"folder": browse_folder()})
        if action == "reveal":
            target = sc._inside(sc.folder, name) if sc.folder and name else sc.folder
            if not target or not target.exists():
                raise ValueError("Not found in the download folder")
            osutil.reveal(target)
            return self._json(200, {"ok": True})
        return self._json(404, {"error": "unknown scanner action"})

    # ------------------------------------------------------------------ TV
    def _tv_get(self, path: str, arg):
        if path == "/BuzzTorrentTV.apk":
            if not APK.is_file():
                return self._json(404, {"error": "the TV app hasn't been built yet"})
            return self._send(200, APK.read_bytes(), "application/vnd.android.package-archive",
                              {"Content-Disposition": "attachment; filename=BuzzTorrentTV.apk"})
        if path == "/api/tv/hello":
            # Lets the TV app find this PC on the network. Says nothing beyond "BuzzTorrent is here".
            return self._json(200, {"app": "BuzzTorrent", "version": __version__, "ui": tv_ui_version(),
                                    "apk": apk_version(),
                                    "paired": self.pairing.check(self._token()) if self.remote else True})
        if path == "/api/tv/remote/poll":
            # The TV waits here for searches sent from a phone (long poll)
            try:
                after = int(arg("after") or -1)
            except ValueError:
                after = -1
            return self._json(200, self.remote_channel.wait(after, timeout=25))
        if path == "/api/tv/result":
            return self._json(200, self.engine._get(arg("id")).to_json(self.engine.extra_trackers))
        if path == "/api/tv/services":
            return self._json(200, {"services": tv.services_for(arg("title"))})
        if path == "/api/tv/play":
            ident = arg("ident")
            if not ident:
                m = IA_IDENT.match(self.engine._get(arg("id")).details_url or "")
                if not m:
                    return self._json(200, {"files": [], "note": "Only free Internet Archive copies can be played on the TV. "
                                                                "Use Send to PC for this one."})
                ident = m.group(1)
            if not tv.IDENT.match(ident):
                return self._json(400, {"error": "bad identifier"})
            try:
                files = tv.playable_files(ident)
            except FetchError as e:
                return self._json(502, {"error": f"archive.org: {e}"})
            for f in files:
                f["resume"] = self.tvstate.position(f["key"])
                for sub in f["subs"]:
                    ref = tv.b64(f"ia:{ident}/{sub['file']}")
                    sub["url"] = self.signer.sign(f"/api/tv/subtitles/{ref}")
            note = "" if files else "This upload has no video the TV can play (it may be subtitles, audio or a disc image)."
            return self._json(200, {"ident": ident, "files": files, "note": note,
                                    "progress": self.signer.sign("/api/tv/progress")})
        if path == "/api/tv/library":
            items = []
            open_ = self._adult_ok()
            for it in self.library.items():
                if it.get("private") and not open_:
                    continue
                key = f"lib:{it['id']}"
                items.append({**it, "key": key, "resume": self.tvstate.position(key),
                              "label": quality_label(it["quality"]),
                              "stream": self.signer.sign(f"/api/tv/stream/{it['id']}"),
                              "versions": [{**v, "label": quality_label(v["quality"]) or v["ext"].upper(),
                                            "stream": self.signer.sign(f"/api/tv/stream/{v['id']}")}
                                           for v in it["versions"]],
                              "subs": [{**s, "url": self.signer.sign(f"/api/tv/subtitles/{tv.b64('lib:' + s['id'])}")}
                                       for s in it["subs"]]})
            downloading = [{**{k: d[k] for k in ("title", "status", "done", "total", "down", "eta")},
                            **({"title": "Private download"} if d.get("private") and not open_ else {})}
                           for d in self.downloader.list() if d["status"] in ("active", "metadata", "waiting", "paused")]
            return self._json(200, {"state": self.library.state(), "items": items, "downloading": downloading,
                                    "progress": self.signer.sign("/api/tv/progress")})
        if path.startswith("/api/tv/stream/"):
            p = self.library.path(path.rsplit("/", 1)[1])
            if not p:
                return self._json(404, {"error": "not in the scanned library"})
            return self._stream_file(p, CONTENT_TYPES.get(p.suffix.lstrip(".").lower(), "application/octet-stream"))
        if path.startswith("/api/tv/subtitles/"):
            return self._json(200, {"cues": self._subtitles(path.rsplit("/", 1)[1])})
        if path == "/api/tv/continue":
            return self._json(200, {"items": self.tvstate.continue_list()})
        if path == "/api/tv/watchlist":
            return self._json(200, {"items": self.tvstate.watchlist(), "alerts": self.tvstate.alerts()})
        if path == "/api/tv/shelves":
            return self._json(200, {"shelves": self.engine.shelves(arg("cat") or "movies")})
        if path == "/api/tv/shelf":
            page = arg("page") or "1"
            if not page.isdigit():
                return self._json(400, {"error": "bad page"})
            try:
                return self._json(200, self.engine.shelf(arg("source") or "Internet Archive", arg("id"), int(page)))
            except FetchError as e:
                return self._json(502, {"error": str(e)})
        if path == "/api/tv/status" and not self.remote:
            return self._json(200, tv_status(type(self)))
        return self._json(404, {"error": "not found"})

    def _tv_post(self, path: str, body: dict):
        if path == "/api/tv/pair":
            token = self.pairing.pair(str(body.get("pin", "")).strip())
            cookie = f"{tv.COOKIE}={token}; Path=/; Max-Age=31536000; HttpOnly; SameSite=Strict"
            return self._json(200, {"ok": True}, {"Set-Cookie": cookie})
        if path == "/api/tv/progress":
            key = str(body.get("key", ""))
            private = {f"lib:{it['id']}" for it in self.library.items() if it.get("private")}
            if key not in private:                 # private videos leave no Continue watching trace
                self.tvstate.report(body)
            return self._json(200, {"ok": True})
        if path == "/api/tv/watch":
            if body.get("category") == "adult":
                raise ValueError("Private titles aren't kept on the watchlist")
            if body.get("action") == "remove":
                self.tvstate.watch_remove(str(body.get("id", ""))[:32])
                return self._json(200, {"ok": True})
            return self._json(200, {"ok": True, "item": self.tvstate.watch_add(body)})
        if path == "/api/tv/alerts/seen":
            self.tvstate.mark_seen()
            return self._json(200, {"ok": True})
        if path == "/api/tv/remote":
            cmd = tv.remote_command(body, PUBLIC_CATEGORIES, LANG_IDS)
            return self._json(200, {"ok": True, "seq": self.remote_channel.post(cmd)})
        if path == "/api/tv/send":
            r = self.engine._get(str(body.get("id", "")))
            if r.verdict == "danger":
                raise PermissionError("Rated risky. If you really want it, download it from the PC")
            if not r.infohash and r.torrent_url:
                self.engine.inspect(r.id)            # fetches and vets the .torrent, which fills in the hash
            if self.downloader.available:
                self.start_download(r)
                return self._json(200, {"ok": True, "app": "BuzzTorrent"})
            magnet = r.magnet(self.engine.extra_trackers)
            if not magnet:
                raise ValueError("This result has no magnet link")
            app = tv.open_on_pc(magnet)
            return self._json(200, {"ok": True, "app": app})
        if self.remote:
            raise PermissionError("only available on the PC")
        if path == "/api/tv/config":
            return self._tv_config(body)
        if path == "/api/tv/firewall":
            allow_firewall(self.port)
            return self._json(200, tv_status(type(self)))
        return self._json(404, {"error": "not found"})

    @classmethod
    def download_folder(cls) -> Path:
        """Where the built-in downloader saves: the folder the download scanner watches, so
        every finished download gets virus-scanned. If the scanner isn't set up yet, it is
        turned on for Downloads\\BuzzTorrent."""
        sc = cls.scanner
        if sc.enabled and sc.folder:
            return Path(sc.folder)
        folder = Path(cls.cfg["downloader"].get("folder") or Path.home() / "Downloads" / "BuzzTorrent")
        folder.mkdir(parents=True, exist_ok=True)
        old = cls.cfg["scanner"]
        try:
            sc.configure(True, str(folder), old.get("auto_quarantine", True), old.get("virustotal_key", ""))
        except ValueError:
            pass
        with cls.cfg_lock:
            cls.cfg = {**cls.cfg, "scanner": {**old, "enabled": sc.enabled, "folder": str(sc.folder) if sc.folder else None},
                       "downloader": {**cls.cfg["downloader"], "folder": str(folder)}}
            save_config(cls.cfg)
        return folder

    def start_download(self, r, force: bool = False) -> str:
        """Downloads a result inside BuzzTorrent. Same safety rules as everywhere else: a
        risky result needs an explicit confirmation, and a .torrent is only used once its
        hash has been verified."""
        if r.verdict == "danger" and not force:
            raise PermissionError("Rated risky. Confirm to download it anyway")
        if not self.downloader.available:
            raise DownloaderError("The download engine (aria2) isn't installed")
        torrent = None
        if r.torrent_url or r.trusted_source:
            try:
                _, torrent = self.engine.torrent_file(r.id, force=force)
            except (NotFound, FetchError, PermissionError):
                torrent = None
        magnet = r.magnet(self.engine.extra_trackers)
        if not torrent and not magnet:
            raise ValueError("This result has no magnet link or verified .torrent")
        info = {"result": r.id, "verdict": r.verdict, "adult": r.category == "adult"}
        infohash = r.infohash or ""
        if torrent:
            try:
                parsed = parse_torrent(torrent)
                info["name"] = parsed.name          # the folder it lands in, so the library shows the real title
                infohash = parsed.infohash
            except BencodeError:
                torrent = None
        return self.downloader.add(self.download_folder(), infohash, r.title, torrent=torrent, magnet=magnet, info=info)

    def _stream_file(self, path: Path, ctype: str):
        """Sends a file with HTTP Range support, so the TV can seek without downloading it all."""
        size = path.stat().st_size
        start, end, status = 0, size - 1, 200
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", (self.headers.get("Range") or "").strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), size - 1) if m.group(2) else size - 1
            else:
                start = max(0, size - int(m.group(2)))
            if start >= size or start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            status = 206
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(end - start + 1))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        if status == 206:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        remaining = end - start + 1
        with open(path, "rb") as fh:
            fh.seek(start)
            while remaining > 0:
                chunk = fh.read(min(256 * 1024, remaining))
                if not chunk:
                    break
                self.wfile.write(chunk)
                remaining -= len(chunk)

    def _subtitles(self, ref: str) -> list[dict]:
        try:
            spec = tv.unb64(ref)
        except ValueError:
            raise NotFound("bad subtitle reference")
        if spec in self.sub_cache:
            return self.sub_cache[spec]
        if spec.startswith("lib:"):
            p = self.library.path(spec[4:])
            if not p or not subs_mod.is_subtitle(p.name) or p.stat().st_size > subs_mod.MAX_BYTES:
                raise NotFound("subtitle not found")
            data = p.read_bytes()
        elif spec.startswith("ia:"):
            ident, _, name = spec[3:].partition("/")
            if not tv.IDENT.match(ident) or not subs_mod.is_subtitle(name) or \
                    name not in {str(f.get("name")) for f in tv.item_files(ident)}:
                raise NotFound("subtitle not found")
            data = fetch(f"https://archive.org/download/{ident}/{urllib.parse.quote(name)}", SUB_POLICY)
        else:
            raise NotFound("bad subtitle reference")
        cues = subs_mod.parse(subs_mod.decode(data))
        if len(self.sub_cache) > 200:
            self.sub_cache.clear()
        self.sub_cache[spec] = cues
        return cues

    def _tv_config(self, body: dict):
        with self.cfg_lock:
            t = self.cfg["tv"]
            if body.get("new_pin") or (body.get("enabled") and not t.get("pin")):
                t["pin"] = tv.new_pin()
            if body.get("forget"):
                t["tokens"] = []
            if "autostart" in body:
                t["autostart"] = bool(body["autostart"])
                from buzz.shortcuts import set_autostart
                set_autostart(ROOT, t["autostart"])
            restart = "enabled" in body and bool(body["enabled"]) != self.lan_bound
            if "enabled" in body:
                t["enabled"] = bool(body["enabled"])
            save_config(self.cfg)
        status = tv_status(type(self))
        if restart and self.app_mode:
            status["restarting"] = True
            self._json(200, status)
            restart_in_background(self.server)
            return
        status["restart_needed"] = restart
        return self._json(200, status)


def apk_version() -> dict | None:
    """Version of the TV app this PC serves (written by android/build_apk.py)."""
    try:
        v = json.loads((APK.parent / "version.json").read_text(encoding="utf-8"))
        return {"code": int(v["code"]), "name": str(v["name"])} if APK.is_file() else None
    except (OSError, ValueError, KeyError, TypeError):
        return {"code": 1, "name": "1.0"} if APK.is_file() else None


_ui_cache: dict = {}


def tv_ui_version() -> str:
    """Changes whenever the TV page's files or the server version change, so an open TV
    reloads itself after an update on the PC."""
    files = [ROOT / "static" / n for n in ("tv.html", "tv.js", "tv.css")]
    key = tuple(f.stat().st_mtime_ns for f in files if f.exists())
    if _ui_cache.get("key") != key:
        h = hashlib.sha256(__version__.encode())
        for f in files:
            if f.exists():
                h.update(f.read_bytes())
        _ui_cache.update(key=key, value=h.hexdigest()[:16])
    return _ui_cache["value"]


def find_vlc() -> str | None:
    """VLC, which plays every format, for "Open in video player" on the PC / Mac."""
    return osutil.vlc()


def jackett_settings() -> Path | None:
    """Jackett's ServerConfig.json: ProgramData / AppData on Windows, ~/.config/Jackett on macOS and Linux."""
    cands = [Path(os.environ.get("ProgramData", r"C:\ProgramData")) / "Jackett" / "ServerConfig.json",
             Path(os.environ.get("LOCALAPPDATA", "")) / "Jackett" / "ServerConfig.json",
             Path.home() / ".config" / "Jackett" / "ServerConfig.json",
             Path.home() / "Library" / "Application Support" / "Jackett" / "ServerConfig.json"]
    return next((p for p in cands if p.is_file()), None)


def connect_jackett() -> int:
    """Command line: add (or refresh) the local Jackett as a BuzzTorrent source."""
    path = jackett_settings()
    if not path:
        print("Jackett's settings weren't found. Start Jackett once, then run this again.")
        return 1
    try:
        js = json.loads(path.read_text(encoding="utf-8-sig"))
        key, port = str(js["APIKey"]), int(js.get("Port") or 9117)
    except (OSError, ValueError, KeyError) as e:
        print(f"Couldn't read Jackett's settings: {e}")
        return 1
    src = validate_source({"name": "Jackett", "apikey": key, "enabled": True, "allow_private": True,
                           "url": f"http://127.0.0.1:{port}/api/v2.0/indexers/all/results/torznab/api"}, None)
    try:
        TorznabProvider(src["name"], src["url"], src["apikey"], True).test()
        note = "connected"
    except FetchError as e:
        note = f"saved (Jackett didn't answer yet: {e})"
    cfg = load_config()
    cfg["torznab"] = [s for s in cfg["torznab"] if s.get("name") != "Jackett"] + [src]
    save_config(cfg)
    print(f"Jackett {note}. Add sites in Jackett's page: http://127.0.0.1:{port}  (restart BuzzTorrent if it's running)")
    return 0


def warm_shelves() -> None:
    """The Archive answers these queries slowly; load the TV's classics rows ahead of time
    (and again before the hourly cache runs out) so the TV home opens quickly."""
    while True:
        for sh in Handler.engine.shelves("all"):
            try:
                Handler.engine.shelf(sh["source"], sh["id"])
            except Exception:
                pass
        time.sleep(50 * 60)


def tv_status(h: type | None = None) -> dict:
    h = h or Handler
    t = h.cfg["tv"]
    return {"enabled": t["enabled"], "listening": h.lan_bound, "port": h.port, "pin": t.get("pin", ""),
            "addresses": tv.lan_addresses(), "paired": len(t.get("tokens", [])), "autostart": t["autostart"],
            "firewall": firewall_allowed(), "apk": APK.is_file(), "apk_version": apk_version(),
            "magnet_app": tv.magnet_handler(),
            "app_mode": h.app_mode}


def firewall_allowed() -> bool | None:
    """Whether Windows Firewall lets the TV in on a private network: our own port rule, or an
    allow rule for this Python (Windows adds one when you click "Allow" on its prompt).
    None when it can't be told (non-Windows, or netsh output in another language)."""
    if os.name != "nt":
        return None
    try:
        r = subprocess.run(["netsh", "advfirewall", "firewall", "show", "rule", "name=all", "dir=in", "verbose"],
                           capture_output=True, text=True, timeout=20, errors="replace",
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        return None
    if "Rule Name:" not in r.stdout:
        return None
    exes = {os.path.normcase(os.path.abspath(sys.executable))}
    exes.add(os.path.normcase(os.path.abspath(str(Path(sys.executable).with_name("pythonw.exe")))))
    allowed = blocked = False
    for block in r.stdout.split("Rule Name:")[1:]:
        f = dict((k.strip().lower(), v.strip()) for k, _, v in
                 (line.partition(":") for line in block.splitlines()[1:]) if _)
        if f.get("enabled") != "Yes" or not any(p in f.get("profiles", "") for p in ("Private", "Any")):
            continue
        ours = block.splitlines()[0].strip() == FIREWALL_RULE or \
            os.path.normcase(f.get("program", "")) in exes
        if ours and f.get("protocol", "TCP") in ("TCP", "Any"):
            if f.get("action") == "Block":
                blocked = True
            elif f.get("action") == "Allow":
                allowed = True
    return allowed and not blocked


def allow_firewall(port: int) -> None:
    """Asks Windows (with its admin prompt) to let the TV reach this port on private networks only."""
    if os.name != "nt":
        raise ValueError("Only needed on Windows")
    args = (f'advfirewall firewall add rule name="{FIREWALL_RULE}" dir=in action=allow protocol=TCP '
            f'localport={int(port)} profile=private')
    script = f"Start-Process netsh -Verb RunAs -WindowStyle Hidden -Wait -ArgumentList '{args}'"
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True,
                   timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def restart_in_background(server) -> None:
    """Start a fresh copy (it waits for the port), then stop this one. Used when TV access
    is switched on or off, because that changes which network the server listens on."""
    subprocess.Popen([osutil.background_python(), str(ROOT / "launcher.pyw"), "--no-window", "--wait-port"], cwd=ROOT,
                     **osutil.detached())
    threading.Thread(target=server.shutdown, daemon=True).start()


def browse_folder() -> str | None:
    """The system's folder picker (on this computer, the same machine as the app window)."""
    return osutil.choose_folder("Choose the folder your downloads are saved to")


class Presence:
    """App mode: shut the server down once its window is closed.

    Each open page pings every few seconds and sends a "bye" beacon when it closes.
    The server exits a few seconds after the last window says bye (a reload pings
    again right away, so it survives), or if no window has pinged for a long time
    (crashed browser). Hidden windows can be throttled to one ping a minute, so
    "stale" is generous."""
    CLIENT = re.compile(r"^[A-Za-z0-9-]{8,64}$")

    def __init__(self, server, grace=4.0, stale=180.0, first_wait=120.0):
        self.server, self.grace, self.stale, self.first_wait = server, grace, stale, first_wait
        self.clients: dict[str, float] = {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.seen_any = False
        self.bye_at: float | None = None

    def ping(self, cid: str) -> bool:
        if not self.CLIENT.match(cid or ""):
            return False
        with self.lock:
            self.clients[cid] = time.time()
            self.seen_any, self.bye_at = True, None
        return True

    def bye(self, cid: str) -> None:
        with self.lock:
            if self.clients.pop(cid, None) is not None and not self.clients:
                self.bye_at = time.time()

    def should_stop(self, now: float) -> bool:
        with self.lock:
            for cid, t in list(self.clients.items()):
                if now - t > self.stale:
                    del self.clients[cid]
                    if not self.clients:
                        self.bye_at = now - self.grace
            if not self.seen_any:
                return now - self.started > self.first_wait
            return not self.clients and self.bye_at is not None and now - self.bye_at >= self.grace

    def watch(self) -> None:
        while not self.should_stop(time.time()):
            time.sleep(1)
        print("App window closed; shutting down.", flush=True)
        self.server.shutdown()


def find_app_browser() -> str | None:
    """Edge, Chrome or Brave, which can show a page as a standalone app window."""
    return osutil.app_browser()


def open_app_window(url: str) -> None:
    exe = find_app_browser()
    if not exe:
        webbrowser.open(url)
        return
    # Your normal browser profile, shown as a standalone app window. (A fresh profile would
    # make policy-installed extensions pop up their own sign-up / welcome pages.)
    subprocess.Popen([exe, f"--app={url}", "--window-size=1380,900"],
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def message_box(text: str) -> None:
    osutil.message_box(text)


def already_running(port: int) -> bool:
    import urllib.request
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/sources", timeout=2) as r:
            return r.status == 200 and b"sources" in r.read(2000)
    except OSError:
        return False


def _log_to_file() -> None:
    """pythonw has no console: send output to data/buzztorrent.log instead."""
    log = ROOT / "data" / "buzztorrent.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    if log.exists() and log.stat().st_size > 1_000_000:
        log.replace(log.with_suffix(".old.log"))
    stream = open(log, "a", encoding="utf-8", buffering=1)
    sys.stdout = sys.stderr = stream
    print(f"\n--- {time.strftime('%Y-%m-%d %H:%M:%S')} ---")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="BuzzTorrent safe torrent metasearch")
    ap.add_argument("--port", type=int, help="override the port from config.json")
    ap.add_argument("--open", action="store_true", help="open the app in the default browser once ready")
    ap.add_argument("--app", action="store_true",
                    help="desktop-app mode: no console, own window, quit when the window closes")
    ap.add_argument("--install-shortcuts", action="store_true", help="create Desktop and Start Menu shortcuts")
    ap.add_argument("--no-window", action="store_true", help="app mode without opening a window (TV server)")
    ap.add_argument("--wait-port", action="store_true", help="wait up to 20 s for the port to free up (restart)")
    ap.add_argument("--connect-jackett", action="store_true",
                    help="add the Jackett on this computer as a source (reads its API key from Jackett's settings)")
    args = ap.parse_args(argv)

    if args.install_shortcuts:
        from buzz.shortcuts import install
        for path in install(ROOT):
            print("created", path)
        return 0
    if args.connect_jackett:
        return connect_jackett()
    if args.app or sys.stdout is None:
        _log_to_file()

    try:
        cfg = load_config()
    except ValueError as e:
        message_box(f"config.json is not valid JSON:\n{e}")
        return 1
    if args.port:
        cfg["port"] = args.port
    host, port = cfg["host"], int(cfg["port"])
    url = f"http://127.0.0.1:{port}/"
    tv_on = bool(cfg["tv"]["enabled"])
    if tv_on and host in ("127.0.0.1", "localhost"):
        host = "0.0.0.0"              # the TV needs to reach us over the home network
    if args.app and not args.wait_port and already_running(port):
        if not args.no_window:
            open_app_window(url)      # second launch: just bring up another window
        return 0
    try:
        Handler.engine = build_engine(cfg)
    except ValueError:  # every source disabled by hand-editing: fall back to the built-in one
        cfg["archive_org"] = True
        Handler.engine = build_engine(cfg)
    Handler.cfg = cfg
    Handler.scanner = DownloadScanner(ROOT / "data", ROOT / "quarantine")
    sc = cfg["scanner"]
    try:
        Handler.scanner.configure(sc["enabled"], sc["folder"], sc["auto_quarantine"], sc["virustotal_key"])
    except ValueError as e:
        print(f"Download scanner is off: {e}")
    Handler.allowed_hosts = {f"{h}:{port}" for h in ("127.0.0.1", "localhost", host)}
    Handler.port, Handler.app_mode = port, bool(args.app)
    Handler.lan_bound = host not in ("127.0.0.1", "localhost")
    Handler.pairing = tv.Pairing(cfg["tv"], lambda: save_config(Handler.cfg))
    if not cfg["tv"].get("secret"):
        cfg["tv"]["secret"] = secrets.token_hex(32)       # signs the TV player's stream links
        save_config(cfg)
    Handler.signer = tv.Signer(cfg["tv"]["secret"])
    dl = cfg["downloader"]
    Handler.downloader = Downloader(ROOT / "data", find_aria2(dl.get("aria2")), cfg["extra_trackers"],
                                    float(dl.get("seed_ratio", 1.0)), int(dl.get("seed_minutes", 30)))
    Handler.library = Library(Handler.scanner, titles=Handler.downloader.titles,
                              private=Handler.downloader.private_names)
    Handler.adult = AdultGate(cfg["adult"], lambda: save_config(Handler.cfg))
    if Handler.downloader.available and Handler.downloader.meta and dl.get("folder"):
        # pick up unfinished downloads from last time
        threading.Thread(target=lambda: Handler.downloader.ensure(Handler.download_folder()), daemon=True).start()
    Handler.tvstate = TvState(ROOT / "data" / "tv_state.json")
    Handler.tvstate.start_checker(lambda title, cat: Handler.engine.search(title, cat, smart=False)["results"])
    if cfg["tv"]["enabled"]:
        threading.Thread(target=warm_shelves, name="shelves", daemon=True).start()
    if tv_on and not cfg["tv"].get("pin"):
        cfg["tv"]["pin"] = tv.new_pin()
        save_config(cfg)
    deadline = time.time() + (20 if args.wait_port else 0)
    while True:
        try:
            server = Server((host, port), Handler)
            break
        except OSError:
            if time.time() < deadline:
                time.sleep(0.5)
                continue
            server = None
            break
    if server is None:
        msg = (f"Port {port} is already in use by another program.\n\n"
               f"Close it, or change \"port\" in config.json.")
        if args.app:
            message_box(msg)
            return 1
        sys.exit(msg)
    print(f"BuzzTorrent {__version__} running at {url}")
    print(f"Sources: {', '.join(Handler.engine.providers)}")
    eng = Handler.scanner.engine
    print(f"Download scanner: {'watching ' + str(Handler.scanner.folder) if Handler.scanner.enabled else 'off'}"
          f" (antivirus: {eng.name if eng else 'none found'})")
    if Handler.lan_bound:
        print(f"TV access on: http://{(tv.lan_addresses() or ['<this-pc-ip>'])[0]}:{port}/tv")
    if args.app:
        if not tv_on:   # with TV access on, keep serving the TV after the PC window closes
            Handler.presence = Presence(server)
            threading.Thread(target=Handler.presence.watch, name="presence", daemon=True).start()
        if not args.no_window:
            threading.Thread(target=open_app_window, args=(url,), daemon=True).start()
    else:
        print("Close this window or press Ctrl+C to stop.", flush=True)
        if args.open:  # the socket is already listening, so the page will load
            threading.Thread(target=webbrowser.open, args=(url,), daemon=True).start()
    pid_file = ROOT / ".buzztorrent.pid"
    pid_file.write_text(str(os.getpid()))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        pid_file.unlink(missing_ok=True)
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

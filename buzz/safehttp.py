"""Hardened HTTP fetching.

Every outbound request goes through `fetch`, which enforces:
  * http/https only, no credentials embedded in URLs
  * a per-source host allowlist, re-checked on every redirect hop
  * a hard cap on redirect hops (redirect chains are how ad/scam funnels work)
  * no requests to private / loopback / link-local addresses (SSRF guard),
    unless the source is explicitly a local service such as Jackett
  * content-type checks, response size caps and decompression-bomb limits
  * redirects to magnet: URIs are surfaced, never followed blindly
"""
from __future__ import annotations

import ipaddress
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request
import zlib
from dataclasses import dataclass

USER_AGENT = "BuzzTorrent/1.0 (metasearch)"
REDIRECT_CODES = {301, 302, 303, 307, 308}


class FetchError(Exception):
    """A request was refused by policy or failed."""

    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


class MagnetRedirect(Exception):
    """The server redirected to a magnet: URI instead of returning content."""

    def __init__(self, uri: str):
        super().__init__("redirected to a magnet link")
        self.uri = uri


@dataclass(frozen=True)
class FetchPolicy:
    allowed_hosts: tuple[str, ...]      # exact host or parent domain, e.g. "archive.org"
    allow_private: bool = False         # permit LAN/loopback (local Jackett/Prowlarr only)
    max_redirects: int = 3
    max_bytes: int = 4 * 1024 * 1024
    timeout: float = 12.0
    content_types: tuple[str, ...] = ()  # accepted Content-Type substrings; empty = any

    def host_allowed(self, host: str) -> bool:
        host = host.lower().rstrip(".")
        return any(host == h or host.endswith("." + h) for h in self.allowed_hosts)


# A bare opener with only the HTTP(S) handlers: no automatic redirects, no
# file:/ftp:/data: handlers, no error processing. We vet every hop ourselves.
_opener = urllib.request.OpenerDirector()
_opener.add_handler(urllib.request.HTTPHandler())
def _tls_context() -> ssl.SSLContext:
    """Normal certificate checking. Python from python.org on a Mac ships without CA
    certificates until "Install Certificates" is run; macOS keeps its own bundle at
    /etc/ssl/cert.pem, so use that instead of failing every HTTPS request."""
    ctx = ssl.create_default_context()
    if sys.platform == "darwin" and not ctx.cert_store_stats().get("x509_ca") and os.path.exists("/etc/ssl/cert.pem"):
        ctx.load_verify_locations("/etc/ssl/cert.pem")
    return ctx


_opener.add_handler(urllib.request.HTTPSHandler(context=_tls_context()))


def _assert_public(host: str, port: int) -> None:
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise FetchError(f"cannot resolve {host}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if not ip.is_global or ip.is_multicast:
            raise FetchError(f"{host} resolves to a non-public address ({ip}); blocked")


def check_url(url: str, policy: FetchPolicy) -> None:
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise FetchError(f"scheme not allowed: {parts.scheme or 'none'}")
    if parts.username or parts.password:
        raise FetchError("credentials embedded in URL; blocked")
    host = parts.hostname
    if not host:
        raise FetchError("URL has no host")
    if not policy.host_allowed(host):
        raise FetchError(f"off-site host blocked: {host}")
    if not policy.allow_private:
        _assert_public(host, parts.port or (443 if parts.scheme == "https" else 80))


def _decompress(raw: bytes, encoding: str, limit: int) -> bytes:
    encoding = encoding.strip().lower()
    if encoding in ("", "identity"):
        return raw
    if encoding not in ("gzip", "deflate"):
        raise FetchError(f"unsupported content encoding: {encoding}")
    d = zlib.decompressobj(32 + zlib.MAX_WBITS)  # auto-detect gzip/zlib header
    try:
        out = d.decompress(raw, limit + 1)
    except zlib.error as e:
        raise FetchError("corrupt compressed response") from e
    if len(out) > limit or d.unconsumed_tail:
        raise FetchError("decompressed response too large; blocked")
    return out


def fetch(url: str, policy: FetchPolicy, accept: str = "*/*", headers: dict | None = None) -> bytes:
    for _ in range(policy.max_redirects + 1):
        check_url(url, policy)
        req = urllib.request.Request(url, headers={
            "User-Agent": USER_AGENT,
            "Accept": accept,
            "Accept-Encoding": "gzip, deflate",
            **(headers or {}),
        })
        try:
            resp = _opener.open(req, timeout=policy.timeout)
        except (urllib.error.URLError, OSError, ValueError) as e:
            raise FetchError(f"network error: {e}") from e
        with resp:
            if resp.status in REDIRECT_CODES:
                location = (resp.headers.get("Location") or "").strip()
                if not location:
                    raise FetchError("redirect without a target")
                nxt = urllib.parse.urljoin(url, location)
                if nxt.lower().startswith("magnet:"):
                    raise MagnetRedirect(nxt)
                url = nxt
                continue
            if resp.status != 200:
                raise FetchError(f"HTTP {resp.status}", status=resp.status)
            ctype = (resp.headers.get("Content-Type") or "").lower()
            if policy.content_types and not any(t in ctype for t in policy.content_types):
                raise FetchError(f"unexpected content type: {ctype or 'none'}")
            declared = resp.headers.get("Content-Length") or ""
            if declared.isdigit() and int(declared) > policy.max_bytes:
                raise FetchError("response too large; blocked")
            raw = resp.read(policy.max_bytes + 1)
            if len(raw) > policy.max_bytes:
                raise FetchError("response too large; blocked")
            return _decompress(raw, resp.headers.get("Content-Encoding") or "", policy.max_bytes)
    raise FetchError(f"more than {policy.max_redirects} redirects; blocked")

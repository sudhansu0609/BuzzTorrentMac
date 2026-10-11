"""Torznab connector: works with Jackett, Prowlarr, or any Torznab-compatible indexer.

Point it at the Torznab endpoint your Jackett/Prowlarr instance shows, e.g.
  Jackett:  http://127.0.0.1:9117/api/v2.0/indexers/all/results/torznab/api
  Prowlarr: http://127.0.0.1:9696/<indexer-id>/api
Requests stay on that host only; torrent downloads that bounce elsewhere are refused.
"""
from __future__ import annotations

import urllib.parse
import xml.etree.ElementTree as ET

from ..fuzzy import LANGUAGES, detect_languages
from ..magnet import clean_name, normalize_infohash, parse_magnet
from ..models import Result
from ..safehttp import FetchError, FetchPolicy, fetch
from .base import Provider

NS = {"torznab": "http://torznab.com/schemas/2015/feed"}
CATS = {"movies": "2000", "tv": "5000", "music": "3000", "books": "7000",
        "games": "1000,4050", "software": "4000", "adult": "6000"}
BY_THOUSAND = {"1": "games", "2": "movies", "3": "music", "4": "software", "5": "tv", "6": "adult", "7": "books"}


def _with_language(query: str, lang: str) -> str:
    if lang in LANGUAGES and lang != "en":
        word = LANGUAGES[lang][1][0]
        if word not in query.lower():
            return f"{query} {word}".strip()
    return query


def _only_language(results: list[Result], lang: str) -> list[Result]:
    if lang not in LANGUAGES or lang == "en":
        return results
    return [r for r in results if lang in r.language or not r.language]


def _category(code: str | None) -> str:
    code = (code or "").strip()
    if not code.isdigit():
        return "other"
    if code.startswith("405"):
        return "games"
    return BY_THOUSAND.get(code[0], "other") if len(code) == 4 else "other"


def _int(v) -> int | None:
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


class TorznabProvider(Provider):
    def __init__(self, name: str, url: str, apikey: str = "", allow_private: bool = True):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError(f"invalid Torznab URL for {name!r}")
        self.name = name
        self.endpoint = url
        self.apikey = apikey
        self.host = parts.hostname
        self.api_policy = FetchPolicy((self.host,), allow_private, content_types=("xml", "rss"),
                                      max_bytes=8 * 1024 * 1024, timeout=38)
        self.torrent_policy = FetchPolicy((self.host,), allow_private,
                                          content_types=("bittorrent", "octet-stream"))

    def _same_host(self, url: str | None) -> str | None:
        if url and url.lower().startswith(("http://", "https://")) \
                and self.api_policy.host_allowed(urllib.parse.urlsplit(url).hostname or ""):
            return url
        return None

    def _url(self, params: dict) -> str:
        if self.apikey:
            params = {**params, "apikey": self.apikey}
        return self.endpoint + ("&" if "?" in self.endpoint else "?") + urllib.parse.urlencode(params)

    def test(self) -> str:
        """Ask the indexer for its capabilities; raise FetchError on any problem."""
        body = fetch(self._url({"t": "caps"}), self.api_policy)
        if b"<!DOCTYPE" in body[:2048].upper() or b"<!ENTITY" in body:
            raise FetchError("response contains a DTD; refused")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise FetchError("not a Torznab endpoint (bad XML)") from e
        if root.tag == "error":
            raise FetchError(f"indexer error: {root.get('description', 'unknown')}")
        if root.tag != "caps":
            raise FetchError("not a Torznab endpoint")
        server = root.find("server")
        return (server.get("title") if server is not None else None) or "Torznab indexer"

    def search(self, query: str, category: str, limit: int, lang: str = "any", genre: str = "any") -> list[Result]:
        # Torznab has no language parameter, but Indian releases name their language
        # ("… Hindi 1080p", "… Bangla Movie"): add the word, then keep matching titles.
        params = {"t": "search", "q": _with_language(query, lang), "limit": str(limit)}
        if category in CATS:
            params["cat"] = CATS[category]
        return _only_language(self._fetch_feed(params)[0], lang)

    def browse_options(self) -> dict:
        all_cats = [{"id": "", "label": "Latest releases"}]
        return {"collections": {c: all_cats for c in ("all", *CATS)},
                "sorts": [{"id": "newest", "label": "Newest first"}],
                "decades": [{"id": "any", "label": "Any year"}],
                "languages": [{"id": "any", "label": "Any language"}] +
                             [{"id": c, "label": name} for c, (name, _) in LANGUAGES.items()],
                "genres": [{"id": "any", "label": "All genres"}]}

    def browse(self, category: str, collection: str, sort: str, decade: str,
               page: int, per_page: int, family: bool = True, lang: str = "any",
               genre: str = "any") -> tuple[list[Result], int | None]:
        """An empty Torznab query returns the indexers' latest releases."""
        params = {"t": "search", "q": _with_language("", lang), "limit": str(per_page),
                  "offset": str((page - 1) * per_page)}
        if category in CATS:
            params["cat"] = CATS[category]
        elif family:  # everything except Torznab's adult category (6000)
            params["cat"] = ",".join(sorted({c for k, v in CATS.items() if k != "adult" for c in v.split(",")}))
        results, total = self._fetch_feed(params)
        return _only_language(results, lang), total

    def _fetch_feed(self, params: dict) -> tuple[list[Result], int | None]:
        body = fetch(self._url(params), self.api_policy)
        if b"<!DOCTYPE" in body[:2048].upper() or b"<!ENTITY" in body:
            raise FetchError("feed contains a DTD; refused (XML entity attack guard)")
        try:
            root = ET.fromstring(body)
        except ET.ParseError as e:
            raise FetchError("malformed Torznab feed") from e
        if root.tag == "error":
            raise FetchError(f"indexer error: {root.get('description', 'unknown')}")

        total = None
        for el in root.iter():
            if el.tag.endswith("}response") and (el.get("total") or "").isdigit():
                total = int(el.get("total"))
                break
        out = []
        for item in root.iter("item"):
            attrs = {a.get("name"): a.get("value") for a in item.findall("torznab:attr", NS)}
            link = (item.findtext("link") or "").strip()
            enclosure = item.find("enclosure")
            enc_url = (enclosure.get("url") if enclosure is not None else "") or ""

            magnet_src = attrs.get("magneturl") or next(
                (u for u in (link, enc_url) if u.lower().startswith("magnet:")), None)
            parsed = parse_magnet(magnet_src) if magnet_src else None
            infohash = normalize_infohash(attrs.get("infohash") or "")
            if parsed:
                if infohash and parsed["infohash"] != infohash:
                    continue  # listing contradicts itself: drop it
                infohash = parsed["infohash"]
            torrent_url = self._same_host(link) or self._same_host(enc_url)
            if not infohash and not torrent_url:
                continue

            seeders = _int(attrs.get("seeders"))
            peers = _int(attrs.get("peers"))
            leechers = _int(attrs.get("leechers"))
            if leechers is None and peers is not None and seeders is not None:
                leechers = max(0, peers - seeders)
            details = item.findtext("comments") or item.findtext("guid") or ""
            out.append(Result(
                title=clean_name(item.findtext("title") or "untitled"),
                provider=self.name,
                category=_category(attrs.get("category") or item.findtext("category")),
                infohash=infohash,
                size=_int(item.findtext("size")) or _int(attrs.get("size")),
                seeders=seeders,
                leechers=leechers,
                published=(item.findtext("pubDate") or "")[:25] or None,
                details_url=details if details.lower().startswith(("http://", "https://")) else None,
                torrent_url=torrent_url,
                trackers=parsed["trackers"] if parsed else [],
                language=detect_languages(item.findtext("title") or ""),
            ))
        return out, total

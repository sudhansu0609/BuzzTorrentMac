"""Fans a query out to every source in parallel, merges duplicates by
info-hash, scores each result, and handles on-demand deep scans."""
from __future__ import annotations

import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout, wait

from .bencode import BencodeError, TorrentInfo, parse_torrent
from .magnet import parse_magnet
from .models import Result
from .providers import Provider
from .safehttp import FetchError, MagnetRedirect, fetch
from .details import DetailsService
from .fuzzy import clean_release, norm, romanize, script_language, similarity
from .safety import PADDING, assess, scan_files
from .smart import SmartSearch
from .adult import is_adult
from . import quality as Q

VERDICT_RANK = {"safe": 0, "caution": 1, "unknown": 2, "danger": 3}
SEARCH_TIMEOUT = 40      # many Jackett sites: give slow ones a chance
MAX_RESULTS_KEPT = 5000
MAX_TORRENTS_KEPT = 200
MAX_FILES_SHOWN = 500
BROWSE_PER_PAGE = 15
BROWSE_MAX_PAGES = 40
BROWSE_TTL = 3600
MAX_THUMBS = 600
HIRES_TAGS = ("2160p", "4K", "8K")
QUALITY_CHECKS = 24     # strong video matches whose real resolution is looked up per search
QUALITY_WAIT = 8        # seconds; slower lookups just leave the title's guess in place
STRONG = 0.8        # a result this similar to the query counts as a real match
LOOSE_MIN = 0.72    # typo-tolerant candidates must be at least this close to be shown
CATEGORY_NOUN = {"movies": "Film", "tv": "TV", "games": "Game", "software": "Software", "books": "Book", "music": "Music"}


class NotFound(KeyError):
    pass


def _bucket(rel: float) -> int:
    return 3 if rel >= 0.9 else 2 if rel >= STRONG else 1 if rel >= 0.65 else 0


def _max(a: int | None, b: int | None) -> int | None:
    return max((v for v in (a, b) if v is not None), default=None)


class Engine:
    def __init__(self, providers: list[Provider], extra_trackers=(), cache_ttl: int = 600,
                 smart: SmartSearch | None = None):
        self.providers = {p.name: p for p in providers}
        self.smart = smart
        self.details = DetailsService(smart or SmartSearch())
        self.extra_trackers = list(extra_trackers)
        self.cache_ttl = cache_ttl
        self._pool = ThreadPoolExecutor(max_workers=max(8, len(providers) * 4))
        self._lock = threading.Lock()
        self._cache: dict[tuple[str, str], tuple[float, dict]] = {}
        self._results: OrderedDict[str, Result] = OrderedDict()
        self._scan_flags: dict[str, list[dict]] = {}
        self._torrents: OrderedDict[str, tuple[TorrentInfo, bytes]] = OrderedDict()
        self._thumbs: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    # ---------------------------------------------------------------- search
    def search(self, query: str, category: str, limit: int = 50, smart: bool = True,
               lang: str = "any", genre: str = "any") -> dict:
        """Exact search on every source, plus (smart) typo-tolerant candidates and
        Wikipedia-based spelling correction. Results are ranked by safety, then by how
        closely they match what the user meant."""
        smart = smart and self.smart is not None
        key = (query.lower(), category, smart, lang, genre)
        flt = {"lang": lang, "genre": genre}
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < self.cache_ttl:
                return hit[1]

        understand = self._pool.submit(self.smart.understand, query, category) if smart else None
        loose_futs = [self._pool.submit(p.search_loose, query, category, 100, **flt) for p in self.providers.values()
                      if smart and hasattr(p, "search_loose")]
        # Indic-script query: also search its romanized spelling (most Archive titles are in Latin script)
        rom = romanize(query) if script_language(query) not in (None, "ur") else ""
        queries = [query] + ([rom] if rom and norm(rom) != norm(query) else [])
        # High-resolution copies are often outside the top hits of a plain search: ask for them too
        hires = [f"{query} {tag}" for tag in HIRES_TAGS] if category in ("movies", "tv", "all") else []
        found, errors, stats, _ = self._fan_out_many(queries + hires, category, limit, hires=set(hires), **flt)

        loose: list[Result] = []
        for fut in loose_futs:
            try:
                loose.extend(fut.result(timeout=SEARCH_TIMEOUT))
            except Exception:
                pass                                   # best effort; the exact search already reported errors
        try:
            info = understand.result(timeout=12) if understand else {"suggestion": None, "works": []}
        except Exception:
            info = {"suggestion": None, "works": []}

        targets = queries + ([info["suggestion"]] if info["suggestion"] else [])
        rel = lambda r, t=targets: max(max(similarity(x, r.title), similarity(x, clean_release(r.title)[0])) for x in t)
        loose = [r for r in loose if rel(r) >= LOOSE_MIN]
        strong = {r.id for r in [*found, *loose] if rel(r) >= STRONG}

        corrected, extra = None, []
        if smart and len(strong) < 5:
            alts = ([info["suggestion"]] if info["suggestion"] else []) + [w["title"] for w in info["works"][:2]]
            alts = [a for a in dict.fromkeys(alts) if a and similarity(a, query) < 0.97][:2]
            best_gain = 0
            per_alt = self._fan_out_many(alts, category, limit, **flt)[3] if alts else {}
            for alt in alts:
                items = per_alt.get(alt, [])
                gain = sum(1 for r in items if similarity(alt, clean_release(r.title)[0]) >= STRONG)
                extra.extend(items)
                if gain > best_gain:
                    corrected, best_gain = alt, gain
            targets += alts

        merged = self._separate_adult(self._merge(found + loose + extra), category)
        with self._lock:
            for r in merged:
                r.relevance = round(rel(r, targets), 3)
                assess(r, self._scan_flags.get(r.id))
                self._remember(r)
        self._measure_quality(merged)
        # Equally good matches: higher resolution first, then popularity
        merged.sort(key=lambda r: (VERDICT_RANK[r.verdict], -_bucket(r.relevance), -round(r.relevance, 2),
                                   -Q.rank(r.quality), -r.score, -(r.seeders or r.downloads or 0)))
        did_you_mean = [{"title": w["title"], "year": w["year"], "description": w["description"]}
                        for w in info["works"] if similarity(w["title"], query) < 0.98][:5]
        best = max((r.relevance for r in merged), default=0.0)
        close_only = bool(merged) and best < 0.95
        if close_only and not did_you_mean:     # no Wikipedia help: offer the closest titles we found
            seen = set()
            for r in sorted(merged, key=lambda r: -r.relevance):
                t = clean_release(r.title)[0]
                if norm(t) not in seen and r.relevance >= LOOSE_MIN:
                    seen.add(norm(t))
                    did_you_mean.append({"title": t, "year": r.year, "description": None})
                if len(did_you_mean) >= 3:
                    break
        payload = {
            "query": query,
            "category": category,
            "results": [r.to_json(self.extra_trackers) for r in merged],
            "errors": errors,
            "sources": sorted(stats, key=lambda s: s["name"]),
            "corrected": corrected if corrected and not len(strong) else None,
            "also_searched": corrected if corrected and len(strong) else None,
            "suggestion": info["suggestion"],
            "did_you_mean": did_you_mean,
            "close_matches_only": close_only,
            "lang": lang,
            "genre": genre,
        }
        with self._lock:
            if not errors:  # don't pin a transient source failure in the cache
                self._cache[key] = (time.time(), payload)
        return payload

    @staticmethod
    def _separate_adult(results: list[Result], category: str) -> list[Result]:
        """Adult content only ever shows in its own (PIN-locked) section, never in normal results."""
        for r in results:
            if is_adult(r.title, r.category):
                r.category = "adult"
        if category == "adult":
            return [r for r in results if r.category == "adult"]
        return [r for r in results if r.category != "adult"]

    def _measure_quality(self, results: list[Result]) -> None:
        """Title tags give a first guess; for the strongest video matches on sources that list
        their files (Internet Archive), the real resolution replaces it. Popular uploads are
        often low-resolution, so this is what lets a 1080p copy rise above them."""
        for r in results:
            if r.quality is None:
                r.quality = Q.from_title(r.title)
        todo = [r for r in results if r.category in ("movies", "tv") and r.relevance >= STRONG
                and r.verdict != "danger" and not r.quality_exact
                and hasattr(self.providers.get(r.provider), "quality")]
        # Safe, full-size copies first: trailers and clips mustn't use up the lookups
        todo.sort(key=lambda r: (r.verdict != "safe", -r.relevance, -(r.size or 0)))
        todo = todo[:QUALITY_CHECKS]
        futs = {self._pool.submit(self.providers[r.provider].quality, r): r for r in todo}
        done, _ = wait(futs, timeout=QUALITY_WAIT)
        for fut in done:
            try:
                got = fut.result()
            except Exception:
                continue
            height, exact = got if isinstance(got, tuple) else (got, True)
            if height and (exact or not futs[fut].quality):
                futs[fut].quality, futs[fut].quality_exact = height, exact

    def suggest(self, query: str, category: str, lang: str = "any") -> dict:
        """Search-as-you-type: closest known titles (Wikipedia works + typo-tolerant source titles)."""
        if self.smart is None or len(norm(query)) < 3:
            return {"query": query, "suggestions": []}
        loose_futs = [self._pool.submit(p.search_loose, query, category, 40, lang=lang) for p in self.providers.values()
                      if hasattr(p, "search_loose")]
        try:
            info = self.smart.understand(query, category)
        except Exception:
            info = {"suggestion": None, "works": []}
        out, seen = [], set()
        for w in info["works"]:
            key = norm(w["title"])
            if key not in seen:
                seen.add(key)
                out.append({"title": w["title"], "year": w["year"], "description": w["description"],
                            "thumb": self.details.register_image(w.get("thumb")), "from": "wikipedia"})
        target = info["suggestion"] or query
        found = []
        for fut in loose_futs:
            try:
                found.extend(fut.result(timeout=10))
            except Exception:
                pass
        found.sort(key=lambda r: (-max(similarity(query, r.title), similarity(target, r.title)), -(r.downloads or 0)))
        for r in found:
            t = clean_release(r.title)[0]
            if norm(t) in seen or max(similarity(query, t), similarity(target, t)) < LOOSE_MIN:
                continue
            seen.add(norm(t))
            out.append({"title": t, "year": r.year, "description": CATEGORY_NOUN.get(r.category),
                        "thumb": r.thumb, "from": r.provider})
            if len(out) >= 8:
                break
        return {"query": query, "suggestion": info["suggestion"], "suggestions": out[:8]}

    def _fan_out(self, query: str, category: str, limit: int, lang: str = "any",
                 genre: str = "any") -> tuple[list[Result], list[dict], list[dict]]:
        found, errors, stats, _ = self._fan_out_many([query], category, limit, lang, genre)
        return found, errors, stats

    def _fan_out_many(self, queries: list[str], category: str, limit: int, lang: str = "any", genre: str = "any",
                      hires: set[str] = frozenset()):
        """Every (query, source) pair in one flat batch, submitted from the calling thread
        (pool tasks never wait on other pool tasks, so the pool can't deadlock). The extra
        "2160p/4K/8K" queries only go to sources that need them: a Jackett search already
        covers every site, and repeating it four times would only slow searches down."""
        futures = {self._pool.submit(self._timed, p, q, category, limit, lang, genre): (q, p)
                   for q in queries for p in self.providers.values()
                   if q not in hires or getattr(p, "hires_queries", False)}
        done, pending = wait(futures, timeout=SEARCH_TIMEOUT)
        found, errors, stats, by_query = [], [], [], {q: [] for q in queries}
        for fut in done:
            q, prov = futures[fut]
            name = prov.name
            try:
                items, ms = fut.result()
                found.extend(items)
                by_query[q].extend(items)
                stats.append({"name": name, "ok": True, "count": len(items), "ms": ms})
            except (FetchError, ValueError, OSError) as e:
                errors.append({"source": name, "error": str(e)})
                stats.append({"name": name, "ok": False, "count": 0, "error": str(e)})
        for fut in pending:
            fut.cancel()
            name = futures[fut][1].name
            errors.append({"source": name, "error": "timed out"})
            stats.append({"name": name, "ok": False, "count": 0, "error": "timed out"})
        # one line per source, however many query variants were sent to it
        merged: dict[str, dict] = {}
        for st in stats:
            m = merged.setdefault(st["name"], {**st, "count": 0, "ms": 0})
            m["count"] += st.get("count", 0)
            m["ms"] = max(m.get("ms") or 0, st.get("ms") or 0)
            m["ok"] = m["ok"] or st["ok"]
        seen_err = set()
        errors = [e for e in errors if not (e["source"] in seen_err or seen_err.add(e["source"]))]
        return found, errors, list(merged.values()), by_query

    # ---------------------------------------------------------------- browse
    def browse_options(self) -> dict:
        out = {}
        for name, p in self.providers.items():
            opts = p.browse_options()
            if opts:
                out[name] = opts
        return out

    def shelves(self, category: str) -> list[dict]:
        out = []
        for name, p in self.providers.items():
            if hasattr(p, "shelves"):
                out += [{**s, "source": name} for s in p.shelves(category)]
        return out

    def shelf(self, source: str, sid: str, page: int = 1) -> dict:
        provider = self.providers.get(source)
        if provider is None or not hasattr(provider, "shelf") or sid not in getattr(provider, "SHELF_IDS", ()):
            raise NotFound("unknown shelf")
        page = max(1, min(page, BROWSE_MAX_PAGES))
        key = ("shelf", source, sid, page)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < BROWSE_TTL:
                return hit[1]
        results, total = provider.shelf(sid, page, BROWSE_PER_PAGE)
        with self._lock:
            for r in results:
                assess(r, self._scan_flags.get(r.id))
                self._remember(r)
        payload = {"source": source, "shelf": sid, "page": page, "total": total,
                   "results": [r.to_json(self.extra_trackers) for r in results]}
        with self._lock:
            self._cache[key] = (time.time(), payload)
        return payload

    def browse(self, source: str, category: str, collection: str, sort: str, decade: str, page: int,
               family: bool = True, lang: str = "any", genre: str = "any") -> dict:
        provider = self.providers.get(source)
        if provider is None or provider.browse_options() is None:
            raise NotFound("that source can't be browsed")
        page = max(1, min(page, BROWSE_MAX_PAGES))
        key = ("browse", source, category, collection, sort, decade, page, family, lang, genre)
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < BROWSE_TTL:
                return hit[1]
        results, total = provider.browse(category, collection, sort, decade, page, BROWSE_PER_PAGE, family,
                                         lang=lang, genre=genre)
        results = self._separate_adult(results, category)
        with self._lock:
            for r in results:
                r.expect_full = False   # browsing cartoons, shorts and commercials: small is expected
                assess(r, self._scan_flags.get(r.id))
                self._remember(r)
        if total is None:  # source doesn't report totals: allow one more page while pages are full
            total = (page - 1) * BROWSE_PER_PAGE + len(results) + (BROWSE_PER_PAGE if len(results) == BROWSE_PER_PAGE else 0)
        pages = max(1, min(BROWSE_MAX_PAGES, -(-total // BROWSE_PER_PAGE)))
        payload = {"source": source, "category": category, "collection": collection, "sort": sort,
                   "decade": decade, "family": family, "lang": lang, "genre": genre, "page": page, "pages": pages, "per_page": BROWSE_PER_PAGE, "total": total,
                   "results": [r.to_json(self.extra_trackers) for r in results]}
        with self._lock:
            self._cache[key] = (time.time(), payload)
        return payload

    def thumbnail(self, ident: str) -> tuple[bytes, str]:
        with self._lock:
            hit = self._thumbs.get(ident)
            if hit:
                self._thumbs.move_to_end(ident)
                return hit
        provider = next((p for p in self.providers.values() if hasattr(p, "thumbnail")), None)
        if provider is None:
            raise NotFound("no thumbnails")
        thumb = provider.thumbnail(ident)
        with self._lock:
            self._thumbs[ident] = thumb
            while len(self._thumbs) > MAX_THUMBS:
                self._thumbs.popitem(last=False)
        return thumb

    @staticmethod
    def _timed(p: Provider, query: str, category: str, limit: int, lang: str = "any",
               genre: str = "any") -> tuple[list[Result], int]:
        start = time.monotonic()
        items = p.search(query, category, limit, lang=lang, genre=genre)
        return items, int((time.monotonic() - start) * 1000)

    def close(self) -> None:
        self._pool.shutdown(wait=False, cancel_futures=True)

    @staticmethod
    def _merge(results: list[Result]) -> list[Result]:
        by_id: dict[str, Result] = {}
        for r in results:
            cur = by_id.get(r.id)
            if cur is None:
                by_id[r.id] = r
                continue
            cur.sources = list(dict.fromkeys(cur.sources + r.sources))
            cur.seeders = _max(cur.seeders, r.seeders)
            cur.leechers = _max(cur.leechers, r.leechers)
            cur.trackers = list(dict.fromkeys(cur.trackers + r.trackers))
            cur.webseeds = list(dict.fromkeys(cur.webseeds + r.webseeds))
            if r.trusted_source and not cur.trusted_source:
                cur.trusted_source, cur.provider, cur.torrent_url = True, r.provider, r.torrent_url
            cur.torrent_url = cur.torrent_url or r.torrent_url
            cur.size = cur.size or r.size
        return list(by_id.values())

    def _remember(self, r: Result) -> None:
        old = self._results.get(r.id)
        if old is not None:
            r.inspected = old.inspected
            r.infohash = r.infohash or old.infohash
        self._results[r.id] = r
        self._results.move_to_end(r.id)
        while len(self._results) > MAX_RESULTS_KEPT:
            self._results.popitem(last=False)

    def _get(self, rid: str) -> Result:
        with self._lock:
            r = self._results.get(rid)
        if r is None:
            raise NotFound("unknown result; run the search again")
        return r

    # ------------------------------------------------------------- deep scan
    def inspect(self, rid: str) -> dict:
        """Fetch the .torrent metadata (never the content) and scan its file list."""
        r = self._get(rid)
        with self._lock:
            cached = self._torrents.get(rid)
        if cached:
            return self._inspection(r, cached[0])

        if not r.torrent_url:
            return self._finish(r, [], status="magnet-only", note=(
                "Only a magnet link is available, so the file list can't be checked in advance. "
                "Your torrent client shows the files before downloading: never run .exe/.scr/.lnk "
                "files from a movie, music or book torrent."))

        provider = self.providers[r.provider]
        try:
            data = fetch(r.torrent_url, provider.torrent_policy, accept="application/x-bittorrent")
        except MagnetRedirect as m:
            parsed = parse_magnet(m.uri)
            if not parsed:
                return self._finish(r, [{"level": "danger", "msg": "Download link redirected to a malformed magnet"}],
                                    status="error")
            r.infohash = r.infohash or parsed["infohash"]
            r.trackers = list(dict.fromkeys(r.trackers + parsed["trackers"]))
            r.torrent_url = None
            return self.inspect(rid)
        except FetchError as e:
            return self._finish(r, [], status="error", note=f"Could not fetch torrent metadata: {e}")

        try:
            info = parse_torrent(data)
        except BencodeError as e:
            return self._finish(r, [{"level": "danger", "msg": f"Download is not a real torrent file ({e})"}],
                                status="error")

        flags = []
        if r.infohash and r.infohash != info.infohash:
            if r.trusted_source:
                flags.append({"level": "info", "msg": "Index hash was stale; magnet updated from the official .torrent"})
            else:
                flags.append({"level": "danger", "msg": "Torrent file doesn't match the advertised hash (bait-and-switch)"})
        if not r.infohash or r.trusted_source:
            r.infohash = info.infohash
        r.trackers = list(dict.fromkeys(r.trackers + info.trackers))
        if not r.size:
            r.size = info.total_size
        file_flags, _ = scan_files(info.files, r.category, r.trusted_source)
        with self._lock:
            self._torrents[rid] = (info, data)
            while len(self._torrents) > MAX_TORRENTS_KEPT:
                self._torrents.popitem(last=False)
        self._finish(r, flags + file_flags, status="ok")
        return self._inspection(r, info)

    def _finish(self, r: Result, flags: list[dict], status: str, note: str = "") -> dict:
        with self._lock:
            self._scan_flags[r.id] = flags
            r.inspected = True
            assess(r, flags)
            self._cache.clear()  # cached searches now hold stale verdicts
        return {"status": status, "note": note, "result": r.to_json(self.extra_trackers), "files": []}

    def _inspection(self, r: Result, info: TorrentInfo) -> dict:
        _, marks = scan_files(info.files, r.category, r.trusted_source)
        real = [(i, p, s) for i, (p, s) in enumerate(info.files) if not PADDING.search(p)]
        files = [{"path": p, "size": s, "warning": marks.get(i)} for i, p, s in real[:MAX_FILES_SHOWN]]
        return {
            "status": "ok",
            "note": "",
            "result": r.to_json(self.extra_trackers),
            "files": files,
            "file_count": len(real),
            "flagged_files": len(marks),
            "private": info.private,
        }

    def torrent_file(self, rid: str, force: bool = False) -> tuple[str, bytes]:
        """Return a verified .torrent (filename, bytes). Danger-rated ones need force."""
        r = self._get(rid)
        with self._lock:
            cached = self._torrents.get(rid)
        if not cached:
            self.inspect(rid)
            with self._lock:
                cached = self._torrents.get(rid)
        if not cached:
            raise NotFound("no verified .torrent available for this result")
        if r.verdict == "danger" and not force:
            raise PermissionError("rated dangerous; download blocked")
        safe_name = "".join(c if c.isalnum() or c in " ._-()[]" else "_" for c in r.title)[:120] or "download"
        return f"{safe_name}.torrent", cached[1]

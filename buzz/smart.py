"""Query understanding via Wikipedia's search (no API key).

Wikipedia's search engine is typo-tolerant and full-text. "pride and prejudise",
"nightofthe living dead" or even "zombies attack farmhouse 1968" all lead to the
right title. We use it only to *understand* the query (spelling suggestion +
matching works). The actual torrent search still runs against your sources.
"""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from collections import OrderedDict

from .fuzzy import YEAR, norm, script_language, similarity, strip_disambiguation
from .safehttp import FetchError, FetchPolicy, fetch

API = "https://{}.wikipedia.org/w/api.php?"
POLICY = FetchPolicy(("wikipedia.org",), content_types=("json",), timeout=8)
# Wikipedia editions used for queries written in that script (Latin -> English Wikipedia)
WIKIS = {"en", "hi", "bn", "ur", "ta", "te", "mr", "pa", "gu", "or", "kn", "ml"}
# Wikimedia's API etiquette asks for a descriptive User-Agent; generic ones get throttled.
UA = {"User-Agent": "BuzzTorrent/1.0 (local desktop app for personal use; "
                    "https://www.mediawiki.org/wiki/API:Etiquette) Python-urllib"}
MIN_INTERVAL = 0.12      # seconds between Wikipedia requests from this app
BACKOFF = 30             # pause after being rate-limited twice
HINT = {"movies": "film", "tv": "television series", "games": "video game", "books": "novel",
        "music": "album", "software": "software"}
KIND = {
    "movies": r"\bfilm\b|\bmovie\b|documentary|animated short|cartoon",
    "tv": r"television|\btv\b|sitcom|miniseries|series|\bshow\b|soap opera|anime",
    "games": r"video game|\bgame\b|arcade",
    "books": r"novel|\bbook\b|novella|poem|\bplay\b|short story|comic|manga|essay|memoir|treatise",
    "music": r"album|\bsong\b|single|\bband\b|musician|singer|rapper|composer|symphony|opera|soundtrack|\bep\b",
    "software": r"software|operating system|\bprogram\b|application|utility|web browser|\bapp\b|game engine",
}
KIND["all"] = "|".join(KIND.values())
CACHE_TTL = 6 * 3600


class SmartSearch:
    def __init__(self):
        self._cache: OrderedDict[tuple, tuple[float, object]] = OrderedDict()
        self._lock = threading.Lock()
        self._rate = threading.Lock()
        self._last = 0.0
        self._paused_until = 0.0

    # ------------------------------------------------------------ plumbing
    @staticmethod
    def wiki_for(text: str) -> str:
        lang = script_language(text)
        return lang if lang in WIKIS else "en"

    def _api(self, wiki: str = "en", **params) -> dict:
        if wiki not in WIKIS:
            wiki = "en"
        params.update(format="json", formatversion=2)
        key = (wiki, *sorted(params.items()))
        with self._lock:
            hit = self._cache.get(key)
            if hit and time.time() - hit[0] < CACHE_TTL:
                return hit[1]
        if time.time() < self._paused_until:
            raise FetchError("Wikipedia is rate-limiting us; smart search paused briefly")
        url = API.format(wiki) + urllib.parse.urlencode(params)
        for attempt in range(2):
            with self._rate:
                wait = MIN_INTERVAL - (time.monotonic() - self._last)
                if wait > 0:
                    time.sleep(wait)
                self._last = time.monotonic()
            try:
                data = json.loads(fetch(url, POLICY, accept="application/json", headers=UA))
                break
            except FetchError as e:
                if e.status != 429:
                    raise
                if attempt == 1:
                    self._paused_until = time.time() + BACKOFF
                    raise
                time.sleep(1.5)
        with self._lock:
            self._cache[key] = (time.time(), data)
            while len(self._cache) > 2000:
                self._cache.popitem(last=False)
        return data

    def _search(self, text: str, limit: int = 8, wiki: str = "en") -> tuple[list[str], str | None]:
        d = self._api(wiki, action="query", list="search", srsearch=text, srlimit=limit, srinfo="suggestion", srprop="")
        q = d.get("query", {})
        return [h["title"] for h in q.get("search", [])], q.get("searchinfo", {}).get("suggestion")

    def describe(self, titles: list[str], thumb: int = 160, wiki: str = "en") -> list[dict]:
        if not titles:
            return []
        d = self._api(wiki, action="query", prop="description|pageimages|pageprops", piprop="thumbnail",
                      pithumbsize=thumb, titles="|".join(titles[:20]), redirects=1)
        pages = {p["title"]: p for p in d.get("query", {}).get("pages", []) if "missing" not in p}
        redirects = {r["from"]: r["to"] for r in d.get("query", {}).get("redirects", [])}
        for n in d.get("query", {}).get("normalized", []):
            redirects.setdefault(n["from"], n["to"])
        out = []
        for t in titles:
            p = pages.get(redirects.get(t, t))
            if not p:
                continue
            name, year = strip_disambiguation(p["title"])
            desc = p.get("description") or ""
            if not year:
                y = YEAR.search(desc)
                year = int(y.group(1)) if y else None
            out.append({"title": name, "wiki_title": p["title"], "wiki": wiki, "year": year, "description": desc,
                        "thumb": (p.get("thumbnail") or {}).get("source"),
                        "qid": (p.get("pageprops") or {}).get("wikibase_item")})
        return out

    @staticmethod
    def is_kind(description: str, category: str, wiki: str = "en") -> bool:
        if wiki != "en":
            return True     # descriptions are in that language; rely on title similarity instead
        return bool(re.search(KIND.get(category, KIND["all"]), description or "", re.I))

    # --------------------------------------------------------------- public
    def understand(self, query: str, category: str) -> dict:
        """-> {"suggestion": corrected text or None, "works": [{title, year, description, thumb, qid}]}"""
        wiki = self.wiki_for(query)
        hint = HINT.get(category, "") if wiki == "en" else ""
        text = f"{query} {hint}".strip()
        try:
            titles, sugg = self._search(text, wiki=wiki)
            if titles and sugg and max(similarity(query, strip_disambiguation(t)[0]) for t in titles) < 0.6:
                titles = []                              # hits don't resemble the query: trust the spelling fix
            rounds = 0
            while not titles and sugg and rounds < 2:   # typo-heavy queries: follow the suggestion
                rounds += 1
                titles, sugg2 = self._search(sugg, wiki=wiki)
                if titles or not sugg2 or sugg2 == sugg:
                    break
                sugg = sugg2
            works = [w for w in self.describe(titles, wiki=wiki) if self.is_kind(w["description"], category, wiki)]
            if wiki != "en":
                works = [w for w in works if similarity(query, w["title"]) >= 0.45]
        except (FetchError, ValueError, KeyError):
            return {"suggestion": None, "works": []}
        suggestion = None
        if sugg:
            s = norm(sugg)
            if hint and s.endswith(" " + norm(hint)):
                s = s[: -len(norm(hint)) - 1]
            if s and s != norm(query):
                suggestion = s
        # Wikipedia's order already reflects meaning; nudge it towards titles that look like the query.
        n = len(works)
        for i, w in enumerate(works):
            target = suggestion or query
            w["score"] = round(0.55 * max(similarity(query, w["title"]), similarity(target, w["title"]))
                               + 0.45 * (1 - i / max(1, n)), 4)
        works.sort(key=lambda w: -w["score"])
        return {"suggestion": suggestion, "works": works[:6]}

    def lookup(self, title: str, year: int | None, category: str) -> dict | None:
        """Best Wikipedia article for a known title (+year): used for the details page."""
        wiki = self.wiki_for(title)
        text = f"{title} {year or ''} {HINT.get(category, '') if wiki == 'en' else ''}".strip()
        try:
            titles, sugg = self._search(text, limit=6, wiki=wiki)
            if not titles and sugg:
                titles, _ = self._search(sugg, limit=6, wiki=wiki)
            cands = [w for w in self.describe(titles, wiki=wiki) if self.is_kind(w["description"], category, wiki)]
        except (FetchError, ValueError, KeyError):
            return None
        best, best_score = None, 0.0
        for w in cands:
            s = similarity(title, w["title"])
            if year and w["year"] and abs(year - w["year"]) > 1:
                continue                     # remake / different work with the same name
            if year and w["year"] == year:
                s += 0.05
            if s > best_score:
                best, best_score = w, s
        return best if best and best_score >= 0.78 else None

    def article(self, wiki_title: str, wiki: str = "en") -> dict:
        d = self._api(wiki, action="query", prop="extracts|pageimages|pageprops|info|description", exintro=1,
                      explaintext=1, piprop="thumbnail", pithumbsize=600, inprop="url", titles=wiki_title, redirects=1)
        pages = d.get("query", {}).get("pages", [])
        if not pages or "missing" in pages[0]:
            return {}
        p = pages[0]
        extract = re.sub(r"\n{3,}", "\n\n", (p.get("extract") or "").strip())
        return {"extract": extract[:3000], "url": p.get("fullurl"), "thumb": (p.get("thumbnail") or {}).get("source"),
                "qid": (p.get("pageprops") or {}).get("wikibase_item"), "description": p.get("description")}

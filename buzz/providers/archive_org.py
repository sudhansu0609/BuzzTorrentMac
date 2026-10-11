"""Internet Archive: public-domain films, books, music and software, all web-seeded."""
from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from collections import OrderedDict

from ..fuzzy import STOP, detect_languages, norm
from ..magnet import clean_name, normalize_infohash
from ..models import Result
from ..quality import from_dims
from ..safehttp import FetchError, FetchPolicy, fetch
from .base import Provider

MEDIATYPES = {
    "all": ["movies", "texts", "audio", "etree", "software"],
    "movies": ["movies"], "tv": ["movies"], "books": ["texts"],
    "music": ["audio", "etree"], "games": ["software"], "software": ["software"],
}
TO_CATEGORY = {"movies": "movies", "texts": "books", "audio": "music", "etree": "music", "software": "software"}
TRACKERS = ["http://bt1.archive.org:6969/announce", "http://bt2.archive.org:6969/announce"]
LUCENE_SPECIAL = re.compile(r'([+\-&|!(){}\[\]^"~*?:\\/])')
IDENT = re.compile(r"^[A-Za-z0-9._-]{1,120}$")
FIELDS = ("identifier", "title", "item_size", "btih", "mediatype", "publicdate", "year", "downloads", "language",
          "collection", "subject")
# The Archive has no "TV" mediatype: series sit among the movies. These mark a series or episode.
TV_COLLECTIONS = ("classic_tv", "television", "television_inbox", "tvshows")
TV_SUBJECTS = ('television OR "tv series" OR "tv show" OR "tv serial" OR "tv shows" OR episode OR episodes '
               'OR serial OR doordarshan OR sitcom OR season')
TV_TITLE_WORDS = "episode OR episodes OR season OR seasons OR serial OR ep OR epi OR S01 OR S02 OR S03 OR S1 OR complete"
TV_FILTER = (f"(collection:({' OR '.join(TV_COLLECTIONS)}) OR subject:({TV_SUBJECTS}) "
             f"OR title:({TV_TITLE_WORDS}))")
TV_FEW = 5          # fewer series hits than this: also show ordinary videos with the same title
TV_TITLE = re.compile(r"\b(s\d{1,2}\s?e\d{1,3}|s\d{1,2}|season\s*\d+|episodes?\s*[-#:]?\s*\d+|ep\.?\s*\d+"
                      r"|epi\s*\d+|full series|complete series|tv series|tv serial)\b", re.I)
TV_SUBJECT = re.compile(r"\b(television|tv (series|serials?|shows?)|episodes?|sitcom|doordarshan|serials?)\b", re.I)

# Curated collections per category for browsing (id, label). "" = whole category.
COLLECTIONS = {
    "all": [("", "Everything")],
    "movies": [("", "All movies"), ("feature_films", "Feature films"), ("Comedy_Films", "Comedy"),
               ("SciFi_Horror", "Sci-fi & horror"), ("Film_Noir", "Film noir"), ("silent_films", "Silent films"),
               ("animationandcartoons", "Animation & cartoons")],
    "tv": [("classic_tv", "Classic TV"), ("classic_tv_commercials", "Vintage commercials")],
    "games": [("", "All games"), ("consolelivingroom", "Console games"), ("internetarcade", "Arcade"),
              ("softwarelibrary_msdos_games", "MS-DOS games"), ("softwarelibrary_win3_games", "Windows 3.x games"),
              ("softwarelibrary_flash_games", "Flash games")],
    "software": [("", "All software"), ("tucows", "Tucows shareware"),
                 ("softwarelibrary_msdos", "MS-DOS"), ("softwarelibrary_win3", "Windows 3.x"),
                 ("softwarelibrary_apple", "Apple")],
    "books": [("", "All books"), ("gutenberg", "Project Gutenberg"), ("americana", "American libraries"),
              ("sciencefiction", "Science fiction"), ("digitallibraryindia", "Digital Library of India")],
    "music": [("", "All music & audio"), ("etree", "Live concerts"), ("78rpm", "78 RPM records"),
              ("netlabels", "Netlabel albums"), ("librivoxaudio", "Audiobooks (LibriVox)"),
              ("oldtimeradio", "Old-time radio"), ("MudritSangeet", "Old Indian records (film songs, ghazals)")],
}
# Audiobooks and radio are hugely popular and would crowd out music in the "All" mixes.
NOT_IN_MIX = {"librivoxaudio", "oldtimeradio", "classic_tv_commercials", "digitallibraryindia"}

# Language filter: how the Archive spells each language in item metadata.
LANG_QUERY = {
    "en": "(eng OR English OR en)", "hi": "(hin OR Hindi OR hi)", "bn": "(ben OR Bengali OR Bangla OR bn)",
    "ur": "(urd OR Urdu OR ur)", "bho": "(bho OR Bhojpuri)", "ta": "(tam OR Tamil OR ta)",
    "te": "(tel OR Telugu OR te)", "mr": "(mar OR Marathi OR mr)", "pa": "(pan OR Punjabi OR Panjabi OR pa)",
}
LANG_VALUES = {"eng": "en", "english": "en", "en": "en", "hin": "hi", "hindi": "hi", "hi": "hi",
               "ben": "bn", "bengali": "bn", "bangla": "bn", "bn": "bn", "urd": "ur", "urdu": "ur", "ur": "ur",
               "bho": "bho", "bhojpuri": "bho", "tam": "ta", "tamil": "ta", "ta": "ta", "tel": "te", "telugu": "te",
               "te": "te", "mar": "mr", "marathi": "mr", "mr": "mr", "pan": "pa", "punjabi": "pa", "panjabi": "pa", "pa": "pa"}
LANG_LABELS = [("any", "Any language"), ("en", "English"), ("hi", "Hindi · हिंदी"), ("bn", "Bengali · বাংলা"),
               ("ur", "Urdu · اردو"), ("bho", "Bhojpuri · भोजपुरी"), ("ta", "Tamil · தமிழ்"), ("te", "Telugu · తెలుగు"),
               ("mr", "Marathi · मराठी"), ("pa", "Punjabi · ਪੰਜਾਬੀ")]

# Book genres, matched against subjects and titles in English, Hindi, Urdu and Bengali.
RELIGIOUS = ('(religion OR religious OR spirituality OR hinduism OR islam OR islamic OR sikhism OR buddhism OR jainism '
             'OR quran OR hadith OR hadees OR tafsir OR tafseer OR fiqh OR gita OR purana OR puran OR ramayan OR ramayana '
             'OR mahabharat OR bible OR "धर्म" OR "पुराण" OR "اسلام" OR "قرآن" OR "حدیث" OR "ধর্ম" OR "কুরআন")')
GENRE_QUERY = {
    "stories": '(stories OR "short stories" OR tales OR kahani OR kahaniyan OR kahaniya OR "कहानी" OR "कहानियाँ" '
               'OR "कहानियां" OR "कथा" OR afsana OR afsane OR "افسانہ" OR "افسانے" OR "کہانی" OR golpo OR "গল্প")',
    "poetry": '(poetry OR poems OR poem OR kavita OR "कविता" OR "काव्य" OR shayari OR "शायरी" OR ghazal OR ghazals '
              'OR "ग़ज़ल" OR nazm OR "شاعری" OR "غزل" OR "نظم" OR "دیوان" OR kobita OR "কবিতা")',
    "fiction": '(fiction OR novel OR novels OR upanyas OR "उपन्यास" OR naval OR "ناول" OR uponnash OR "উপন্যাস")',
    "nonfiction": '(history OR biography OR autobiography OR memoir OR essays OR science OR politics OR economics '
                  'OR travel OR "इतिहास" OR "जीवनी" OR "आत्मकथा" OR "تاریخ" OR "سوانح" OR "ইতিহাস" OR "জীবনী")',
    "religion": RELIGIOUS,
}
GENRE_LABELS = [("any", "All genres"), ("stories", "Stories · कहानियाँ"), ("poetry", "Poetry & shayari · कविता"),
                ("fiction", "Fiction & novels · उपन्यास"), ("nonfiction", "Non-fiction"),
                ("religion", "Religion & spirituality")]


def lang_filters(category: str, lang: str, genre: str) -> list[str]:
    parts = []
    if lang in LANG_QUERY:
        parts.append(f"language:{LANG_QUERY[lang]}")
    if genre in GENRE_QUERY and category in ("books", "all"):
        g = GENRE_QUERY[genre]
        parts.append(f"(subject:{g} OR title:{g})")
        if genre != "religion":   # religious texts dominate popularity in several languages
            parts.append(f"NOT subject:{RELIGIOUS}")
    return parts


# ---------------------------------------------------------------- an item's files
FILES_POLICY = FetchPolicy(("archive.org",), content_types=("json",), max_bytes=24 * 1024 * 1024, timeout=20)
VIDEO_EXT = (".mp4", ".m4v", ".webm", ".mkv", ".avi", ".mov", ".mpg", ".mpeg", ".ogv", ".wmv", ".flv", ".ts")
_files_cache: OrderedDict[str, tuple[float, list]] = OrderedDict()
_files_lock = threading.Lock()


def item_files(ident: str, ttl: float = 3600) -> list[dict]:
    """The file list of an Archive item (name, format, size, width, height, length...), cached."""
    if not IDENT.match(ident):
        raise FetchError("bad identifier")
    with _files_lock:
        hit = _files_cache.get(ident)
        if hit and time.time() - hit[0] < ttl:
            _files_cache.move_to_end(ident)
            return hit[1]
    try:
        meta = json.loads(fetch(f"https://archive.org/metadata/{ident}/files", FILES_POLICY))
    except ValueError as e:
        raise FetchError("unexpected response from archive.org") from e
    files = meta.get("result") if isinstance(meta, dict) else None
    files = files if isinstance(files, list) else []
    with _files_lock:
        _files_cache[ident] = (time.time(), files)
        while len(_files_cache) > 1500:
            _files_cache.popitem(last=False)
    return files


def is_video(f: dict) -> bool:
    name = str(f.get("name", ""))
    return name.lower().endswith(VIDEO_EXT) and not name.startswith("history/") and "/." not in "/" + name


def best_resolution(files: list[dict]) -> int | None:
    """Highest effective video height among an item's files (originals and the Archive's copies)."""
    return max((h for h in (from_dims(f.get("width"), f.get("height")) for f in files if is_video(f)) if h),
               default=None)


def _as_list(value) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else [str(value)] if value else []


def looks_tv(doc: dict) -> bool:
    """A series or an episode rather than a film, judged from the Archive's own metadata."""
    if any(c.startswith(TV_COLLECTIONS) for c in _as_list(doc.get("collection"))):
        return True
    title = doc.get("title")
    title = title[0] if isinstance(title, list) and title else title
    return bool(TV_TITLE.search(str(title or ""))) or any(TV_SUBJECT.search(s) for s in _as_list(doc.get("subject")))


def doc_languages(value) -> list[str]:
    vals = value if isinstance(value, list) else [value] if value else []
    out = []
    for v in vals:
        for part in re.split(r"[;,/]", str(v)):
            code = LANG_VALUES.get(part.strip().lower())
            if code and code not in out:
                out.append(code)
    return out
CURATED = {cat: [c for c, _ in cols if c and c not in NOT_IN_MIX] for cat, cols in COLLECTIONS.items() if cat != "all"}
CURATED["all"] = [c for cat in ("movies", "tv", "games", "books", "music") for c in CURATED[cat]]
SORTS = [("popular", "Most popular", "downloads desc"), ("trending", "Trending this week", "week desc"),
         ("newest", "Recently added", "addeddate desc"), ("oldest", "Oldest first", "year asc"),
         ("az", "Title A–Z", "titleSorter asc")]
# Kept out of suggestions when "family-friendly" is on (the default). Popularity on IA
# otherwise surfaces a lot of vintage exploitation films. Search is never filtered.
ADULT_TERMS = ("sex OR sexual OR sexy OR nude OR nudes OR nudist OR nudism OR naked OR erotic OR erotica OR porn "
               "OR porno OR xxx OR stripper OR striptease OR burlesque OR orgy OR fetish OR lingerie OR molester "
               "OR hentai OR playboy")
# TV home rows of Indian classics. Films and records old enough to be public domain in India
# (60 years after release), so these rows only feature what is free to share.
PUBLIC_DOMAIN = "year:[1900 TO 1964]"
SHELVES = {
    "hindi-classics": ("Golden-age Hindi films", "movies",
                       f"mediatype:movies AND language:(hin OR Hindi OR Hindustani) AND {PUBLIC_DOMAIN} "
                       "AND item_size:[100000000 TO *]"),
    "bengali-classics": ("Bengali classics: Satyajit Ray & more", "movies",
                         f"mediatype:movies AND language:(ben OR Bengali OR Bangla) AND {PUBLIC_DOMAIN} "
                         "AND item_size:[100000000 TO *]"),
    "regional-classics": ("Tamil, Telugu, Marathi & regional classics", "movies",
                          "mediatype:movies AND language:(tam OR Tamil OR tel OR Telugu OR mal OR Malayalam OR kan "
                          "OR Kannada OR mar OR Marathi OR pan OR Punjabi OR guj OR Gujarati) "
                          f"AND {PUBLIC_DOMAIN} AND item_size:[100000000 TO *]"),
    "old-records": ("Old Indian film songs & records", "music", "mediatype:(audio OR etree) AND collection:MudritSangeet"),
}
SHELF_ORDER = {"movies": ["hindi-classics", "bengali-classics", "regional-classics"],
               "all": ["hindi-classics", "bengali-classics", "regional-classics", "old-records"],
               "music": ["old-records"], "tv": []}

DECADES = [("any", "Any year")] + [(str(y), f"{y}s") for y in range(2020, 1890, -10)] + [("older", "Before 1900")]


class ArchiveOrgProvider(Provider):
    name = "Internet Archive"
    trusted = True
    hires_queries = True        # also gets the "<title> 2160p / 4K / 8K" searches
    SHELF_IDS = tuple(SHELVES)

    def __init__(self):
        self.api_policy = FetchPolicy(("archive.org",), content_types=("json",))
        # Torrent downloads hop to ia8xxxx.us.archive.org; the suffix rule allows that, nothing else.
        self.torrent_policy = FetchPolicy(("archive.org",), content_types=("bittorrent", "octet-stream"))
        self.thumb_policy = FetchPolicy(("archive.org",), content_types=("image/",), max_bytes=512 * 1024, timeout=10)

    def browse_options(self) -> dict:
        return {"collections": {k: [{"id": c, "label": l} for c, l in v] for k, v in COLLECTIONS.items()},
                "sorts": [{"id": s, "label": l} for s, l, _ in SORTS],
                "decades": [{"id": d, "label": l} for d, l in DECADES],
                "languages": [{"id": i, "label": l} for i, l in LANG_LABELS],
                "genres": [{"id": i, "label": l} for i, l in GENRE_LABELS]}

    def _query(self, params: list, category: str, force: bool = True) -> tuple[list[Result], int]:
        params = params + [("output", "json")] + [("fl[]", f) for f in FIELDS]
        body = fetch("https://archive.org/advancedsearch.php?" + urllib.parse.urlencode(params), self.api_policy)
        try:
            resp = json.loads(body)["response"]
            docs, total = resp["docs"], int(resp["numFound"])
        except (ValueError, KeyError, TypeError) as e:
            raise FetchError("unexpected response from archive.org") from e
        return [r for r in (self._to_result(d, category, force) for d in docs) if r], total

    def _to_result(self, doc: dict, category: str, force: bool = True) -> Result | None:
        ident = str(doc.get("identifier", ""))
        infohash = normalize_infohash(str(doc.get("btih", "")))
        if not infohash or not IDENT.match(ident):
            return None
        title = doc.get("title") or ident
        if isinstance(title, list):
            title = title[0] if title else ident
        cat = TO_CATEGORY.get(doc.get("mediatype"), "other")
        if category == "games" and cat == "software":
            cat = category
        elif cat == "movies" and ((category == "tv" and force) or looks_tv(doc)):
            cat = "tv"
        size, year, dls = doc.get("item_size"), doc.get("year"), doc.get("downloads")
        try:
            year = int(str(year)[:4]) if year else None
        except ValueError:
            year = None
        return Result(
            title=clean_name(str(title)),
            provider=self.name,
            category=cat,
            infohash=infohash,
            size=size if isinstance(size, int) else None,
            published=str(doc.get("publicdate", ""))[:10] or None,
            details_url=f"https://archive.org/details/{ident}",
            torrent_url=f"https://archive.org/download/{ident}/{ident}_archive.torrent",
            trusted_source=True,
            trackers=list(TRACKERS),
            webseeds=[f"https://archive.org/download/{ident}/"],
            thumb=f"/api/thumb?id={urllib.parse.quote(ident)}",
            year=year,
            downloads=dls if isinstance(dls, int) else None,
            language=doc_languages(doc.get("language")) or detect_languages(str(title)),
        )

    def search(self, query: str, category: str, limit: int, lang: str = "any", genre: str = "any") -> list[Result]:
        # connecting words ("the", "of", Hindi "ki/ka/ke") are often left out of titles: don't require them
        kept = [w for w in query.split() if norm(w) not in STOP] or query.split()
        words = [LUCENE_SPECIAL.sub(r"\\\1", w) for w in kept]
        if not words:
            return []
        mts = MEDIATYPES.get(category, MEDIATYPES["all"])
        base = [f"title:({' AND '.join(words)})", f"mediatype:({' OR '.join(mts)})", *lang_filters(category, lang, genre)]
        if category != "tv":
            return self._query([("q", " AND ".join(base)), ("sort[]", "downloads desc"), ("rows", str(limit))],
                               category)[0]
        results, _ = self._query([("q", " AND ".join([*base, TV_FILTER])), ("sort[]", "downloads desc"),
                                  ("rows", str(limit))], category)
        if len(results) < TV_FEW:
            # Many series are uploaded as plain videos ("Hum Log 12"). Show those too, labelled by
            # what they look like, so a film with the same name isn't passed off as a show.
            seen = {r.id for r in results}
            more, _ = self._query([("q", " AND ".join(base)), ("sort[]", "downloads desc"),
                                   ("rows", str(limit))], category, force=False)
            results += [r for r in more if r.id not in seen]
        return results

    def search_loose(self, query: str, category: str, limit: int = 100, lang: str = "any",
                     genre: str = "any") -> list[Result]:
        """Typo-tolerant candidates. The Archive has no fuzzy search, but typos rarely hit the
        first letters, so match word *prefixes* and let the engine rank by similarity."""
        words = [w for w in norm(query).split() if len(w) >= 4 and w not in STOP]
        if not words:
            words = [w for w in norm(query).split() if len(w) >= 3][:1]
        if not words:
            return []
        if len(words) == 1:
            prefixes = [words[0][:4] if len(words[0]) >= 8 else words[0][:3]]  # "nightofthelivingdead" -> nigh*
        else:
            prefixes = [w[:2] for w in words[:4]]
        mts = MEDIATYPES.get(category, MEDIATYPES["all"])
        prefixes = [LUCENE_SPECIAL.sub(r"\\\1", p) for p in prefixes]
        q = " AND ".join([f"title:({' AND '.join(p + '*' for p in prefixes)})", f"mediatype:({' OR '.join(mts)})",
                          "btih:[* TO *]", *lang_filters(category, lang, genre),
                          *([TV_FILTER] if category == "tv" else [])])
        results, _ = self._query([("q", q), ("sort[]", "downloads desc"), ("rows", str(limit))], category)
        return results

    def browse(self, category: str, collection: str, sort: str, decade: str,
               page: int, per_page: int, family: bool = True, lang: str = "any",
               genre: str = "any") -> tuple[list[Result], int]:
        if category == "adult":
            return [], 0                      # the Archive isn't a source for the private section
        mts = MEDIATYPES.get(category, MEDIATYPES["all"])
        allowed = {c for c, _ in COLLECTIONS.get(category, [])}
        if collection not in allowed:
            collection = COLLECTIONS[category][0][0] if category in COLLECTIONS else ""
        # mediatype excludes the sub-collections themselves; btih keeps only torrentable items
        parts = [f"mediatype:({' OR '.join(mts)})", "btih:[* TO *]"]
        # "All …" means the union of the curated collections. The raw mediatype is mostly
        # random personal uploads, which make poor suggestions.
        # With a non-English language chosen, the language itself is the curation (the curated
        # collections are mostly English).
        if collection or lang in ("any", "en"):
            chosen = [collection] if collection else CURATED.get(category, CURATED["all"])
            parts.append(f"collection:({' OR '.join(chosen)})")
        parts += lang_filters(category, lang, genre)
        if category in ("movies", "tv", "all"):
            parts.append("item_size:[1000000 TO *]")  # drops broken/placeholder items
        if family:
            parts.append(f"NOT title:({ADULT_TERMS}) AND NOT subject:({ADULT_TERMS})")
        if decade == "older":
            parts.append("year:[1 TO 1899]")
        elif decade.isdigit():
            parts.append(f"year:[{decade} TO {int(decade) + 9}]")
        sort_expr = next((e for s, _, e in SORTS if s == sort), SORTS[0][2])
        if sort == "oldest" and decade == "any":
            parts.append("year:[1 TO 3000]")  # undated items would otherwise sort first
        if sort == "az":
            parts.append("titleSorter:[a TO zzzz]")  # skip titles that start with punctuation
        return self._query([("q", " AND ".join(parts)), ("sort[]", sort_expr),
                            ("rows", str(per_page)), ("page", str(page))], category)

    def shelves(self, category: str) -> list[dict]:
        return [{"id": sid, "label": SHELVES[sid][0]} for sid in SHELF_ORDER.get(category, [])]

    def shelf(self, sid: str, page: int, per_page: int) -> tuple[list[Result], int]:
        label, category, q = SHELVES[sid]
        q = f"{q} AND btih:[* TO *] AND NOT title:({ADULT_TERMS}) AND NOT subject:({ADULT_TERMS})"
        return self._query([("q", q), ("sort[]", "downloads desc"), ("rows", str(per_page)), ("page", str(page))],
                           category)

    def quality(self, r: Result) -> tuple[int, bool] | None:
        """(effective height, measured) from the item's video files."""
        m = re.match(r"^https://archive\.org/details/([A-Za-z0-9._-]{1,120})$", r.details_url or "")
        if not m:
            return None
        # No playable video file (e.g. a raw .y4m.xz master): leave it unrated rather than
        # rank an unwatchable 67 GB file above copies people can actually play.
        measured = best_resolution(item_files(m.group(1)))
        return (measured, True) if measured else None

    def thumbnail(self, ident: str) -> tuple[bytes, str]:
        if not IDENT.match(ident):
            raise FetchError("bad identifier")
        data = fetch(f"https://archive.org/services/img/{ident}", self.thumb_policy, accept="image/*")
        kind = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/gif" if data[:3] == b"GIF"
                else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else None)
        if not kind:
            raise FetchError("not an image")
        return data, kind

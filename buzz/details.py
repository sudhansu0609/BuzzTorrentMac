"""Rich details for a result: poster, year, runtime, genres, director, cast,
summary. Sources (all keyless): the source's own metadata (Internet Archive),
Wikipedia (summary + poster) and Wikidata (structured facts).
"""
from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from collections import OrderedDict
from html.parser import HTMLParser

from .fuzzy import clean_release
from .models import Result
from .safehttp import FetchError, FetchPolicy, fetch
from .smart import UA, SmartSearch

WIKIDATA = FetchPolicy(("www.wikidata.org",), content_types=("json",), timeout=10)
IA_META = FetchPolicy(("archive.org",), content_types=("json",), timeout=10)
IMAGES = FetchPolicy(("upload.wikimedia.org", "thumb.wikimedia.org", "commons.wikimedia.org", "archive.org"), content_types=("image/",), max_bytes=4 * 1024 * 1024, timeout=12)
IA_IDENT = re.compile(r"^https://archive\.org/details/([A-Za-z0-9._-]{1,120})$")

# Wikidata properties shown per kind of work: (property, label)
FACTS = {
    "movies": [("P57", "Directed by"), ("P58", "Written by"), ("P86", "Music by"), ("P162", "Produced by")],
    "tv": [("P170", "Created by"), ("P57", "Directed by"), ("P86", "Music by")],
    "books": [("P50", "Author"), ("P110", "Illustrated by"), ("P123", "Publisher")],
    "games": [("P178", "Developer"), ("P123", "Publisher"), ("P400", "Platforms"), ("P404", "Game modes")],
    "music": [("P175", "Performed by"), ("P86", "Composer"), ("P264", "Record label")],
    "software": [("P178", "Developer"), ("P306", "Runs on"), ("P277", "Written in")],
}
FACTS["other"] = FACTS["all"] = FACTS["movies"]
CAST = "P161"
MINUTE, SECOND, HOUR = "Q7727", "Q11574", "Q25235"


class _Text(HTMLParser):
    BLOCK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "blockquote"}

    def __init__(self):
        super().__init__()
        self.parts: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip = max(0, self.skip - 1)
        elif tag in self.BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)


def html_to_text(html: str, limit: int = 4000) -> str:
    p = _Text()
    try:
        p.feed(html or "")
    except Exception:
        return ""
    text = "".join(p.parts)
    text = re.sub(r"[ \t\r\f\v]+", " ", text)
    text = re.sub(r"\n\s*\n\s*(\n\s*)+", "\n\n", text)
    return "\n".join(line.strip() for line in text.strip().splitlines())[:limit]


def _first(v):
    return v[0] if isinstance(v, list) and v else v


class DetailsService:
    def __init__(self, smart: SmartSearch):
        self.smart = smart
        self._lock = threading.Lock()
        self._cache: OrderedDict[str, dict] = OrderedDict()
        self._images: OrderedDict[str, str] = OrderedDict()       # key -> URL we got from an API
        self._image_data: OrderedDict[str, tuple[bytes, str]] = OrderedDict()

    # ---------------------------------------------------------------- images
    def register_image(self, url: str | None) -> str | None:
        if not url or not url.startswith(("https://upload.wikimedia.org/",
                                          "https://commons.wikimedia.org/wiki/Special:FilePath/")):
            return None
        key = hashlib.sha1(url.encode()).hexdigest()[:24]
        with self._lock:
            self._images[key] = url
            while len(self._images) > 5000:
                self._images.popitem(last=False)
        return f"/api/image?k={key}"

    def image(self, key: str) -> tuple[bytes, str]:
        with self._lock:
            if key in self._image_data:
                return self._image_data[key]
            url = self._images.get(key)
        if not url:
            raise FetchError("unknown image")
        data = fetch(url, IMAGES, accept="image/*", headers=UA)
        kind = ("image/png" if data[:8] == b"\x89PNG\r\n\x1a\n" else "image/gif" if data[:3] == b"GIF"
                else "image/jpeg" if data[:3] == b"\xff\xd8\xff" else "image/webp" if data[8:12] == b"WEBP" else None)
        if not kind:
            raise FetchError("not an image")
        with self._lock:
            self._image_data[key] = (data, kind)
            while len(self._image_data) > 300:
                self._image_data.popitem(last=False)
        return data, kind

    # ----------------------------------------------------------- the lookup
    def for_result(self, r: Result, use_wiki: bool = True) -> dict:
        key = f"{r.id}|{use_wiki}"
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        out = self._build(r, use_wiki)
        with self._lock:
            self._cache[key] = out
            while len(self._cache) > 500:
                self._cache.popitem(last=False)
        return out

    def _build(self, r: Result, use_wiki: bool) -> dict:
        title, year = clean_release(r.title)
        year = r.year or year
        out = {"title": title, "year": year, "category": r.category, "poster": r.thumb, "summary": None,
               "tagline": None, "facts": [], "cast": [], "genres": [], "runtime": None, "language": None,
               "country": None, "links": [], "upload": None, "matched": False}

        m = IA_IDENT.match(r.details_url or "")
        if m:
            out["upload"] = self._archive(m.group(1))
            if out["upload"]:
                out["title"] = out["upload"].pop("title") or title
                year = year or out["upload"].pop("year", None)
                out["year"] = year
        if not use_wiki:
            return out

        wiki = self.smart.lookup(out["title"], year, r.category)
        if not wiki:
            return out
        art = self.smart.article(wiki["wiki_title"], wiki.get("wiki", "en"))
        out.update(matched=True, summary=art.get("extract") or None, tagline=art.get("description") or wiki["description"])
        if art.get("url"):
            out["links"].append({"label": "Wikipedia", "url": art["url"]})
        poster = self.register_image(art.get("thumb") or wiki.get("thumb"))
        if poster:
            out["poster"] = poster
        qid = art.get("qid") or wiki.get("qid")
        if qid:
            try:
                self._wikidata(qid, r.category, out, wiki.get("wiki", "en"))
            except (FetchError, ValueError, KeyError):
                pass
        out["year"] = out["year"] or wiki.get("year")
        return out

    def _archive(self, ident: str) -> dict | None:
        try:
            meta = json.loads(fetch(f"https://archive.org/metadata/{ident}/metadata", IA_META)).get("result") or {}
        except (FetchError, ValueError):
            return None
        subjects = meta.get("subject") or []
        if isinstance(subjects, str):
            subjects = re.split(r"\s*[;,]\s*", subjects)
        year = str(_first(meta.get("year")) or _first(meta.get("date")) or "")[:4]
        lic = _first(meta.get("licenseurl"))
        return {
            "title": str(_first(meta.get("title")) or "") or None,
            "year": int(year) if year.isdigit() else None,
            "description": html_to_text(str(_first(meta.get("description")) or "")) or None,
            "creator": str(_first(meta.get("creator")) or "") or None,
            "runtime": str(_first(meta.get("runtime")) or "") or None,
            "language": str(_first(meta.get("language")) or "") or None,
            "subjects": [s for s in subjects if s][:12],
            "license": lic if isinstance(lic, str) and lic.startswith(("http://", "https://")) else None,
        }

    def _wikidata(self, qid: str, category: str, out: dict, lang: str = "en") -> None:
        if not re.fullmatch(r"Q\d{1,12}", qid):
            return
        url = f"https://www.wikidata.org/w/api.php?action=wbgetentities&ids={qid}&props=claims&format=json"
        claims = json.loads(fetch(url, WIKIDATA, headers=UA))["entities"][qid]["claims"]

        def items(prop, limit=8):
            ids = []
            for c in claims.get(prop, [])[: limit * 2]:
                v = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
                if isinstance(v, dict) and v.get("id") and v["id"] not in ids:
                    ids.append(v["id"])
            return ids[:limit]

        wanted = {CAST: items(CAST, 12), "P136": items("P136", 5), "P364": items("P364", 2), "P495": items("P495", 2)}
        for prop, _ in FACTS.get(category, FACTS["movies"]):
            wanted[prop] = items(prop, 6)
        labels = self._labels([i for ids in wanted.values() for i in ids], lang)
        name = lambda ids: [labels[i] for i in ids if i in labels]

        out["cast"] = name(wanted[CAST]) if category in ("movies", "tv", "other", "all") else []
        out["genres"] = [g.removesuffix(" film").removesuffix(" video game") for g in name(wanted["P136"])]
        out["language"] = ", ".join(name(wanted["P364"])) or (out.get("upload") or {}).get("language")
        out["country"] = ", ".join(name(wanted["P495"])) or None
        out["facts"] = [{"label": label, "values": name(wanted[prop])}
                        for prop, label in FACTS.get(category, FACTS["movies"]) if name(wanted[prop])]
        for c in claims.get("P2047", [])[:1]:                        # duration
            v = (c.get("mainsnak", {}).get("datavalue") or {}).get("value") or {}
            try:
                amount = float(v.get("amount", "0"))
            except ValueError:
                amount = 0
            unit = str(v.get("unit", "")).rsplit("/", 1)[-1]
            minutes = amount / 60 if unit == SECOND else amount * 60 if unit == HOUR else amount
            if minutes > 0:
                out["runtime"] = f"{int(minutes // 60)} h {int(minutes % 60)} min" if minutes >= 60 else f"{int(minutes)} min"
        if not out["year"]:
            for c in claims.get("P577", [])[:1]:
                t = str(((c.get("mainsnak", {}).get("datavalue") or {}).get("value") or {}).get("time", ""))
                if t[1:5].isdigit():
                    out["year"] = int(t[1:5])
        # Poster: Wikidata's "film poster" (freely licensed, on Commons) beats the article image;
        # then logo (games/software), then the generic image.
        order = ("P3383", "P154", "P18") if category in ("movies", "tv", "other", "all") else ("P154", "P18", "P3383")
        for prop in order:
            files = [((c.get("mainsnak", {}).get("datavalue") or {}).get("value")) for c in claims.get(prop, [])[:1]]
            if files and isinstance(files[0], str) and re.fullmatch(r"[^/\:*?\"<>|]{1,200}\.(jpe?g|png|gif|webp|tiff?)", files[0], re.I):
                url = "https://commons.wikimedia.org/wiki/Special:FilePath/" + urllib.parse.quote(files[0].replace(" ", "_")) + "?width=600"
                if prop == "P3383" or not str(out.get("poster") or "").startswith("/api/image"):
                    out["poster"] = self.register_image(url) or out["poster"]
                break
        for c in claims.get("P345", [])[:1]:                         # IMDb id
            v = (c.get("mainsnak", {}).get("datavalue") or {}).get("value")
            if isinstance(v, str) and re.fullmatch(r"tt\d{5,10}", v):
                out["links"].append({"label": "IMDb", "url": f"https://www.imdb.com/title/{v}/"})
        out["links"].append({"label": "Wikidata", "url": f"https://www.wikidata.org/wiki/{qid}"})

    def _labels(self, ids: list[str], lang: str = "en") -> dict[str, str]:
        ids = list(dict.fromkeys(i for i in ids if re.fullmatch(r"Q\d{1,12}", i)))[:50]
        if not ids:
            return {}
        langs = "en" if lang == "en" or not re.fullmatch(r"[a-z]{2,3}", lang) else f"en|{lang}"
        url = (f"https://www.wikidata.org/w/api.php?action=wbgetentities&props=labels&languages={langs}&format=json&ids="
               + urllib.parse.quote("|".join(ids)))
        ents = json.loads(fetch(url, WIKIDATA, headers=UA)).get("entities", {})
        out = {}
        for k, v in ents.items():
            labels = v.get("labels", {})
            pick = labels.get("en") or labels.get(lang)     # Latin name if there is one, else the native one
            if pick:
                out[k] = pick["value"]
        return out

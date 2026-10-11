from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .magnet import build_magnet
from .quality import label as quality_label

CATEGORIES = ("all", "movies", "tv", "games", "software", "books", "music", "adult")   # adult: PIN-locked


@dataclass
class Result:
    title: str
    provider: str
    category: str = "other"
    infohash: str | None = None
    size: int | None = None
    seeders: int | None = None
    leechers: int | None = None
    published: str | None = None
    details_url: str | None = None
    torrent_url: str | None = None      # server-side only; never sent to the browser
    trusted_source: bool = False
    trackers: list[str] = field(default_factory=list)
    webseeds: list[str] = field(default_factory=list)
    sources: list[str] = field(default_factory=list)
    flags: list[dict] = field(default_factory=list)
    verdict: str = "unknown"
    score: int = 100
    inspected: bool = False
    thumb: str | None = None            # same-origin thumbnail URL (proxied, never hot-linked)
    year: int | None = None
    downloads: int | None = None        # popularity on the source, when it reports one
    relevance: float = 1.0              # 0..1 closeness to what the user searched for
    language: list[str] = field(default_factory=list)   # language codes, e.g. ["hi"], ["hi", "en"]
    quality: int | None = None          # effective video height (1080 = 1080p), when known
    quality_exact: bool = False         # measured from the files, not guessed from the title
    expect_full: bool = True            # searched-for title, so a trailer-sized item is worth a warning
    id: str = ""

    def __post_init__(self):
        if not self.sources:
            self.sources = [self.provider]
        if not self.id:
            self.id = self.infohash or "t-" + hashlib.sha1(
                f"{self.provider}|{self.torrent_url}".encode()).hexdigest()[:24]

    def magnet(self, extra_trackers=()) -> str | None:
        if not self.infohash:
            return None
        return build_magnet(self.infohash, self.title, [*self.trackers, *extra_trackers], self.webseeds)

    def to_json(self, extra_trackers=()) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "category": self.category,
            "infohash": self.infohash,
            "magnet": self.magnet(extra_trackers),
            "size": self.size,
            "seeders": self.seeders,
            "leechers": self.leechers,
            "published": self.published,
            "details_url": self.details_url,
            "has_torrent": bool(self.torrent_url),
            "trusted_source": self.trusted_source,
            "sources": self.sources,
            "flags": self.flags,
            "verdict": self.verdict,
            "score": self.score,
            "inspected": self.inspected,
            "thumb": self.thumb,
            "year": self.year,
            "downloads": self.downloads,
            "relevance": self.relevance,
            "language": self.language,
            "quality": {"height": self.quality, "label": quality_label(self.quality), "exact": self.quality_exact}
                       if self.quality else None,
        }

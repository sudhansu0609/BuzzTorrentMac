from __future__ import annotations

from ..models import Result
from ..safehttp import FetchPolicy


class Provider:
    """A search source. Subclasses set `name`, `torrent_policy` and implement `search`."""

    name: str = "provider"
    trusted: bool = False
    torrent_policy: FetchPolicy

    def search(self, query: str, category: str, limit: int, lang: str = "any", genre: str = "any") -> list[Result]:
        raise NotImplementedError

    def browse_options(self) -> dict | None:
        """{collections: {category: [{id,label}]}, sorts: [...], decades: [...]}, or None if unsupported."""
        return None

    def browse(self, category: str, collection: str, sort: str, decade: str,
               page: int, per_page: int, family: bool = True, lang: str = "any",
               genre: str = "any") -> tuple[list[Result], int | None]:
        """One page of suggestions without a query. Returns (results, total or None)."""
        raise NotImplementedError

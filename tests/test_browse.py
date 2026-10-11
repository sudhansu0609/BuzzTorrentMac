import json
import unittest
import urllib.parse
from unittest import mock

from buzz.engine import BROWSE_MAX_PAGES, BROWSE_PER_PAGE, Engine, NotFound
from buzz.models import Result
from buzz.providers import ArchiveOrgProvider, archive_org
from buzz.providers.base import Provider


class StubProvider(Provider):
    name = "Stub"

    def __init__(self, total):
        self.total = total
        self.calls = []

    def browse_options(self):
        return {"collections": {"all": [{"id": "", "label": "All"}]}, "sorts": [], "decades": []}

    def browse(self, category, collection, sort, decade, page, per_page, family=True, **filters):
        self.calls.append(page)
        start = (page - 1) * per_page
        n = max(0, min(per_page, self.total - start))
        return [Result(title=f"Item {start + i}", provider=self.name, infohash=f"{start + i:040x}") for i in range(n)], self.total


class EngineBrowseTests(unittest.TestCase):
    def test_paging_and_cap(self):
        e = Engine([StubProvider(total=100_000)])
        d = e.browse("Stub", "all", "", "popular", "any", 2)
        self.assertEqual((len(d["results"]), d["page"], d["pages"]), (BROWSE_PER_PAGE, 2, BROWSE_MAX_PAGES))
        self.assertEqual(d["results"][0]["title"], f"Item {BROWSE_PER_PAGE}")
        self.assertEqual(e.browse("Stub", "all", "", "popular", "any", 999)["page"], BROWSE_MAX_PAGES)

    def test_small_total_and_cache(self):
        p = StubProvider(total=20)
        e = Engine([p])
        self.assertEqual(e.browse("Stub", "all", "", "popular", "any", 1)["pages"], 2)
        e.browse("Stub", "all", "", "popular", "any", 1)
        self.assertEqual(p.calls, [1])  # second call served from cache

    def test_results_are_scored_and_inspectable(self):
        e = Engine([StubProvider(total=5)])
        r = e.browse("Stub", "all", "", "popular", "any", 1)["results"][0]
        self.assertEqual(r["verdict"], "safe")
        self.assertEqual(e._get(r["id"]).title, "Item 0")

    def test_unknown_source(self):
        with self.assertRaises(NotFound):
            Engine([StubProvider(total=1)]).browse("Nope", "all", "", "popular", "any", 1)


class ArchiveBrowseQueryTests(unittest.TestCase):
    def run_browse(self, *args, **kw):
        seen = {}

        def fake_fetch(url, policy, accept="*/*", headers=None):
            seen["q"] = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
            return json.dumps({"response": {"numFound": 1, "docs": [
                {"identifier": "night_film", "title": "Night Film", "btih": "a" * 40, "mediatype": "movies",
                 "year": "1968", "downloads": 1234, "item_size": 5_000_000}]}}).encode()

        with mock.patch.object(archive_org, "fetch", fake_fetch):
            results, total = ArchiveOrgProvider().browse(*args, **kw)
        return seen["q"], results

    def test_curated_union_and_filters(self):
        q, results = self.run_browse("movies", "", "popular", "1950", 3, 15)
        self.assertIn("collection:(feature_films OR Comedy_Films", q["q"])
        self.assertIn("year:[1950 TO 1959]", q["q"])
        self.assertIn("NOT title:(sex", q["q"])
        self.assertIn("btih:[* TO *]", q["q"])
        self.assertEqual((q["page"], q["rows"], q["sort[]"]), ("3", "15", "downloads desc"))
        r = results[0]
        self.assertEqual((r.year, r.downloads, r.thumb), (1968, 1234, "/api/thumb?id=night_film"))

    def test_family_off_and_bad_collection_ignored(self):
        q, _ = self.run_browse("books", "evil) OR (mediatype:*", "az", "any", 1, 15, family=False)
        self.assertNotIn("NOT title", q["q"])
        self.assertNotIn("evil", q["q"])
        self.assertIn("titleSorter:[a TO zzzz]", q["q"])


if __name__ == "__main__":
    unittest.main()

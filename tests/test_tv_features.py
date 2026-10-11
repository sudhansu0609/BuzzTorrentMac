import http.client
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

import app
from buzz import subtitles, tv
from buzz.engine import Engine
from buzz.library import Library
from buzz.providers.archive_org import SHELF_ORDER, SHELVES
from buzz.scanner import DownloadScanner
from buzz.tvstate import TvState
from tests.test_tv import StubProvider

MKV_HEAD = b"\x1a\x45\xdf\xa3" + b"\x00" * 4092


class FakeScanner:
    """Just what the library reads from the download scanner."""
    _inside = staticmethod(DownloadScanner._inside)

    def __init__(self, folder: Path, reports: dict):
        self.enabled, self.folder, self.reports = True, folder, reports
        self._lock = threading.RLock()


def report(folder: Path, name: str, verdict: str, state: str = "done") -> dict:
    return {"name": name, "folder": str(folder), "state": state, "verdict": verdict, "scanned_at": 1}


class SubtitleTests(unittest.TestCase):
    def test_srt_and_vtt_become_cues(self):
        srt = "1\n00:00:01,500 --> 00:00:03,000\n<i>Hello</i> there\n\n2\n00:01:02,000 --> 00:01:04,250\nSecond\nline\n"
        self.assertEqual(subtitles.parse(srt), [
            {"start": 1.5, "end": 3.0, "text": "Hello there"},
            {"start": 62.0, "end": 64.25, "text": "Second\nline"}])
        vtt = "WEBVTT\n\n00:05.000 --> 00:07.000 align:start\nNamaste\n"
        self.assertEqual(subtitles.parse(vtt), [{"start": 5.0, "end": 7.0, "text": "Namaste"}])

    def test_languages_and_matching(self):
        self.assertEqual(subtitles.language("Film.2004.eng.srt")[1], "English")
        self.assertEqual(subtitles.language("Sholay [Hindi].srt")[1], "Hindi")
        subs = ["Show Ep 1.en.srt", "Show Ep 2.en.srt"]
        self.assertEqual(subtitles.for_video("Show Ep 1.mp4", subs, only_video=False), ["Show Ep 1.en.srt"])
        self.assertEqual(subtitles.for_video("Film.mkv", ["english.srt"], only_video=True), ["english.srt"])
        self.assertEqual(subtitles.decode("नमस्ते".encode("utf-8")), "नमस्ते")


class LibraryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.folder = Path(self.tmp.name)
        good = self.folder / "Sholay.1975.1080p"
        good.mkdir()
        (good / "Sholay.1975.1080p.mkv").write_bytes(MKV_HEAD)
        (good / "Sholay.1975.1080p.en.srt").write_text("1\n00:00:01,000 --> 00:00:02,000\nHi\n")
        fake = self.folder / "Fake.Movie"
        fake.mkdir()
        (fake / "Fake.Movie.mp4").write_bytes(b"MZ" + b"\x00" * 100)       # a program wearing a .mp4 name
        risky = self.folder / "Risky.Movie"
        risky.mkdir()
        (risky / "Risky.Movie.mkv").write_bytes(MKV_HEAD)
        new = self.folder / "Not.Scanned.Yet.mkv"
        new.write_bytes(MKV_HEAD)
        self.scanner = FakeScanner(self.folder, {
            "Sholay.1975.1080p": report(self.folder, "Sholay.1975.1080p", "clean"),
            "Fake.Movie": report(self.folder, "Fake.Movie", "clean"),
            "Risky.Movie": report(self.folder, "Risky.Movie", "suspicious"),
            "Not.Scanned.Yet.mkv": report(self.folder, "Not.Scanned.Yet.mkv", None, state="queued"),
        })
        self.lib = Library(self.scanner)

    def tearDown(self):
        self.tmp.cleanup()

    def test_only_clean_real_videos_are_listed(self):
        items = self.lib.items()
        self.assertEqual([(i["title"], i["year"], i["quality"]) for i in items], [("Sholay", 1975, 1080)])
        self.assertEqual([s["label"] for s in items[0]["subs"]], ["English"])
        self.assertIsNotNone(self.lib.path(items[0]["id"]))
        self.assertIsNone(self.lib.path("0" * 20))

    def test_scanner_off_means_empty(self):
        self.scanner.enabled = False
        self.assertEqual((self.lib.state(), self.lib.items()), ("scanner_off", []))


class TvStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.st = TvState(Path(self.tmp.name) / "tv_state.json")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resume_rules(self):
        self.st.report({"key": "ia:film:Film", "pos": 600, "dur": 6000, "title": "Film"})
        self.st.report({"key": "lib:" + "a" * 20, "pos": 10, "dur": 6000, "title": "Barely started"})
        self.st.report({"key": "ia:done:Done", "pos": 5900, "dur": 6000, "title": "Finished"})
        self.assertEqual(self.st.position("ia:film:Film"), 600)
        self.assertEqual(self.st.position("lib:" + "a" * 20), 0)        # under 30 s: start over
        self.assertEqual([c["title"] for c in self.st.continue_list()], ["Film"])
        with self.assertRaises(ValueError):
            self.st.report({"key": "../etc", "pos": 1, "dur": 2})
        reloaded = TvState(self.st.path)
        self.assertEqual(reloaded.position("ia:film:Film"), 600)          # survives a restart

    def test_watchlist_alerts_on_sharper_or_free_copy(self):
        w = self.st.watch_add({"title": "Sholay", "category": "movies", "quality": 480})
        results = [{"relevance": 1.0, "verdict": "safe", "quality": {"height": 480}, "sources": ["Jackett"]}]
        self.st.check_one(w, lambda t, c: results)                        # first check: just records
        self.assertEqual(self.st.alerts(), [])
        results.append({"relevance": 1.0, "verdict": "safe", "quality": {"height": 1080}, "sources": ["Internet Archive"]})
        self.st.check_one(self.st.watchlist()[0], lambda t, c: results)
        texts = [a["text"] for a in self.st.alerts()]
        self.assertIn("A 1080p copy of Sholay is now available", texts)
        self.assertIn("Sholay is now free to watch", texts)
        self.assertTrue(self.st.watchlist()[0]["new"])
        self.st.mark_seen()
        self.assertEqual(self.st.alerts(), [])
        self.st.watch_remove(w["id"])
        self.assertEqual(self.st.watchlist(), [])


class SignerAndShelfTests(unittest.TestCase):
    def test_signed_links_expire_and_bind_to_path(self):
        s = tv.Signer("secret")
        url = s.sign("/api/tv/stream/abc", ttl=60)
        path, token = url.split("?t=")
        self.assertTrue(s.check(path, token))
        self.assertFalse(s.check("/api/tv/stream/abd", token))
        self.assertFalse(tv.Signer("other").check(path, token))
        self.assertFalse(s.check(path, s.sign(path, ttl=-5).split("?t=")[1]))

    def test_shelves_are_public_domain_era(self):
        for sid in SHELF_ORDER["movies"]:
            self.assertIn("year:[1900 TO 1964]", SHELVES[sid][2])


class TestHandler(app.Handler):
    @property
    def remote(self):
        return self.headers.get("X-Test-Remote") == "1"

    def log_message(self, *a):
        pass


class StreamTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        folder = Path(cls.tmp.name)
        cls.body = MKV_HEAD + bytes(range(256)) * 40
        (folder / "Film.mkv").write_bytes(cls.body)
        scanner = FakeScanner(folder, {"Film.mkv": report(folder, "Film.mkv", "clean")})
        cls.server = app.Server(("127.0.0.1", 0), TestHandler)
        cls.port = cls.server.server_address[1]
        cfg = {**app.DEFAULTS, "tv": {"enabled": True, "pin": "111222", "tokens": [], "autostart": False}}
        TestHandler.engine = Engine([StubProvider()])
        TestHandler.cfg, TestHandler.port, TestHandler.lan_bound = cfg, cls.port, True
        TestHandler.allowed_hosts = {f"127.0.0.1:{cls.port}"}
        TestHandler.pairing = tv.Pairing(cfg["tv"], lambda: None)
        TestHandler.signer = tv.Signer("test-secret")
        TestHandler.library = Library(scanner)
        TestHandler.tvstate = TvState(folder / "state.json")
        cls.fid = TestHandler.library.items()[0]["id"]
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()

    def get(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", path, headers={"Host": f"192.168.1.20:{self.port}", "X-Test-Remote": "1", **(headers or {})})
        r = c.getresponse()
        data = r.read()
        c.close()
        return r, data

    def test_signed_stream_supports_seeking(self):
        url = TestHandler.signer.sign(f"/api/tv/stream/{self.fid}")
        r, data = self.get(url, {"Range": "bytes=4096-4100"})
        self.assertEqual((r.status, data), (206, self.body[4096:4101]))
        self.assertEqual(r.getheader("Content-Range"), f"bytes 4096-4100/{len(self.body)}")
        r, data = self.get(url)
        self.assertEqual((r.status, len(data)), (200, len(self.body)))
        self.assertEqual(self.get(url, {"Range": f"bytes={len(self.body) + 5}-"})[0].status, 416)

    def test_stream_needs_a_valid_signature(self):
        self.assertEqual(self.get(f"/api/tv/stream/{self.fid}")[0].status, 401)
        self.assertEqual(self.get(f"/api/tv/stream/{self.fid}?t=9999999999.deadbeef")[0].status, 401)
        other = TestHandler.signer.sign("/api/tv/stream/" + "0" * 20)
        self.assertEqual(self.get(other)[0].status, 404)                  # signed, but not in the library

    def test_library_and_progress_need_pairing(self):
        self.assertEqual(self.get("/api/tv/library")[0].status, 401)
        self.assertEqual(self.get("/api/tv/continue")[0].status, 401)
        self.assertEqual(self.get("/api/tv/watchlist")[0].status, 401)


if __name__ == "__main__":
    unittest.main()

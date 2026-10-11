"""Run:  python -m unittest discover tests"""
import hashlib
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from buzz.bencode import BencodeError, parse_torrent
from buzz.magnet import build_magnet, normalize_infohash, parse_magnet
from buzz.models import Result
from buzz.safehttp import FetchError, FetchPolicy, MagnetRedirect, check_url, fetch
from buzz.safety import assess, scan_files

H = "a" * 40
MB = 1024 * 1024


def benc(x) -> bytes:
    if isinstance(x, int):
        return b"i%de" % x
    if isinstance(x, str):
        x = x.encode()
    if isinstance(x, bytes):
        return b"%d:%s" % (len(x), x)
    if isinstance(x, list):
        return b"l" + b"".join(benc(i) for i in x) + b"e"
    return b"d" + b"".join(benc(k) + benc(v) for k, v in sorted(x.items())) + b"e"


class MagnetTests(unittest.TestCase):
    def test_strips_dangerous_params_and_bad_trackers(self):
        m = parse_magnet(f"magnet:?xt=urn:btih:{H.upper()}&dn=Film&as=http://evil.example/x.exe"
                         "&xs=http://evil.example/y&tr=udp://tracker.example:1337"
                         "&tr=http://192.168.1.1/announce&tr=javascript:alert(1)&ws=http://plain.example/")
        self.assertEqual(m["infohash"], H)
        self.assertEqual(m["trackers"], ["udp://tracker.example:1337"])
        self.assertEqual(m["webseeds"], [])  # http webseed rejected, https only
        rebuilt = build_magnet(m["infohash"], m["name"], m["trackers"])
        self.assertNotIn("evil", rebuilt)

    def test_rejects_non_magnets(self):
        for bad in ("http://ads.example/magnet", "magnet:?xt=urn:btih:xyz", "javascript:alert(1)", ""):
            self.assertIsNone(parse_magnet(bad))

    def test_base32_hash(self):
        self.assertEqual(normalize_infohash("MFQWCYLBMFQWCYLBMFQWCYLBMFQWCYLB"), "61" * 20)


class BencodeTests(unittest.TestCase):
    def test_real_infohash_and_files(self):
        info = {"name": "Film", "piece length": 16384, "pieces": b"x" * 20,
                "files": [{"length": 900 * MB, "path": ["Film.mkv"]}, {"length": 10, "path": ["a.txt"]}]}
        data = benc({"announce": "udp://t.example:1/a", "info": info})
        t = parse_torrent(data)
        self.assertEqual(t.infohash, hashlib.sha1(benc(info)).hexdigest())
        self.assertEqual([p for p, _ in t.files], ["Film/Film.mkv", "Film/a.txt"])

    def test_garbage_rejected(self):
        for bad in (b"<html>ad page</html>", b"d4:infoi1ee", b"d4:info", b"l" * 200 + b"e" * 200):
            with self.assertRaises(BencodeError):
                parse_torrent(bad)


class SafetyTests(unittest.TestCase):
    def r(self, title, cat="movies", size=1500 * MB, seeders=50):
        return assess(Result(title=title, provider="t", category=cat, infohash=H, size=size, seeders=seeders))

    def test_clean_result_is_safe(self):
        self.assertEqual(self.r("Night of the Living Dead 1968 1080p").verdict, "safe")

    def test_lures_are_dangerous(self):
        for t in ("Movie 2026 1080p.mp4.exe", "Movie 2026 [PASSWORD in readme]", "Movie codec required",
                  "Movie 2026 keygen"):
            self.assertEqual(self.r(t).verdict, "danger", t)

    def test_tiny_movie_is_dangerous(self):
        self.assertEqual(self.r("Big Movie 2026", size=2 * MB).verdict, "danger")

    def test_fake_seeds_and_spam_url(self):
        self.assertEqual(self.r("Movie www.free-movies.example", seeders=900_000).verdict, "caution")

    def test_crack_in_game_is_caution_not_danger(self):
        self.assertEqual(self.r("Some Game v1.2 crack", cat="games", size=4000 * MB).verdict, "caution")

    def test_file_scan(self):
        flags, marks = scan_files([("Film/Film.mkv", 1), ("Film/Film.avi.exe", 1), ("Film/Get Password.url", 1),
                                   ("Film/setup.lnk", 1)], "movies")
        self.assertEqual(set(marks), {1, 2, 3})
        self.assertIn("danger", {f["level"] for f in flags})
        flags, marks = scan_files([("Game/setup.exe", 1), ("Game/data.bin", 1)], "games")
        self.assertEqual(marks, {})

    def test_trusted_trailer_sized_item_is_not_marked_safe(self):
        def ia(size, expect_full=True):
            r = Result(title="Eternal Sunshine of the Spotless Mind", provider="ia", category="movies",
                       infohash=H, size=size, trusted_source=True)
            r.expect_full = expect_full
            return assess(r)
        self.assertEqual(ia(1021 * MB).verdict, "safe")
        clip = ia(21 * MB)
        self.assertEqual(clip.verdict, "caution")              # a trailer, not malware
        self.assertTrue(any("trailer" in f["msg"] for f in clip.flags))
        self.assertEqual(ia(140 * 1024).verdict, "caution")    # lone subtitle file
        self.assertEqual(ia(21 * MB, expect_full=False).verdict, "safe")   # browsing shorts/cartoons

    def test_movie_torrent_without_video_is_flagged(self):
        flags, _ = scan_files([("F/F.srt", 140_000), ("F/__ia_thumb.jpg", 9000)], "movies", trusted=True)
        self.assertTrue(any("no video" in f["msg"] for f in flags))
        flags, _ = scan_files([("F/F.mkv", 1), ("F/F.srt", 1)], "movies")
        self.assertFalse(any("no video" in f["msg"] for f in flags))

    def test_archive_derivatives_only_exempt_for_trusted_source(self):
        files = [("F/F.mp4", 1), ("F/F.asr.js", 1), ("F/.____padding_file/3", 1)]
        self.assertEqual(scan_files(files, "movies", trusted=True)[1], {})
        self.assertEqual(set(scan_files(files, "movies", trusted=False)[1]), {1})


class _Redirector(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        port = self.server.server_address[1]
        routes = {
            "/offsite": "http://evil.example/payload",
            "/magnet": f"magnet:?xt=urn:btih:{H}",
            "/hop1": "/hop2", "/hop2": "/hop3", "/hop3": "/hop4", "/hop4": "/ok",
            "/short": "/ok",
            "/scheme": "file:///C:/Windows/win.ini",
            "/rebind": f"http://localhost:{port}/ok",
        }
        if self.path in routes:
            self.send_response(302)
            self.send_header("Location", routes[self.path])
            self.end_headers()
        elif self.path == "/ok":
            body = b'{"ok": true}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path == "/big":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"x" * 5000)
        elif self.path == "/html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<script>ads</script>")


class FetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Redirector)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_address[1]}"
        cls.local = FetchPolicy(("127.0.0.1",), allow_private=True, content_types=("json",), max_bytes=1000)

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_ssrf_and_scheme_blocked_by_default(self):
        public = FetchPolicy(("127.0.0.1", "localhost", "archive.org"))
        for url in ("http://127.0.0.1/", "http://localhost/", "file:///etc/passwd",
                    "https://user:pw@archive.org/", "https://evil.example/"):
            with self.assertRaises(FetchError, msg=url):
                check_url(url, public)

    def test_follows_short_same_host_redirect(self):
        self.assertEqual(fetch(self.base + "/short", self.local), b'{"ok": true}')

    def test_blocks_offsite_redirect(self):
        with self.assertRaisesRegex(FetchError, "off-site"):
            fetch(self.base + "/offsite", self.local)

    def test_blocks_host_switch_redirect(self):
        with self.assertRaisesRegex(FetchError, "off-site"):
            fetch(self.base + "/rebind", self.local)

    def test_blocks_long_redirect_chain(self):
        with self.assertRaisesRegex(FetchError, "redirects"):
            fetch(self.base + "/hop1", self.local)

    def test_blocks_scheme_change_redirect(self):
        with self.assertRaisesRegex(FetchError, "scheme"):
            fetch(self.base + "/scheme", self.local)

    def test_magnet_redirect_surfaced(self):
        with self.assertRaises(MagnetRedirect) as cm:
            fetch(self.base + "/magnet", self.local)
        self.assertTrue(cm.exception.uri.startswith("magnet:"))

    def test_size_and_type_limits(self):
        with self.assertRaisesRegex(FetchError, "too large"):
            fetch(self.base + "/big", self.local)
        with self.assertRaisesRegex(FetchError, "content type"):
            fetch(self.base + "/html", self.local)


if __name__ == "__main__":
    unittest.main()

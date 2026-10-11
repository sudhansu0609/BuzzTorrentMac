import http.client
import json
import threading
import unittest

import app
from buzz import tv
from buzz.engine import Engine
from buzz.models import Result
from buzz.providers.archive_org import looks_tv
from buzz.providers.base import Provider


class StubProvider(Provider):
    name = "Stub"

    def search(self, query, category, limit, **filters):
        return [Result(title=f"{query} 2004", provider=self.name, category="movies", infohash="b" * 40,
                       size=900 * 1024 * 1024, seeders=10)]


class PlayableTests(unittest.TestCase):
    def test_one_playable_copy_per_video_in_episode_order(self):
        files = [
            {"name": "Show Ep 10.avi", "source": "original"},
            {"name": "Show Ep 10.mp4", "source": "derivative", "original": "Show Ep 10.avi", "format": "h.264"},
            {"name": "Show Ep 10_512kb.mp4", "source": "derivative", "original": "Show Ep 10.avi", "format": "512Kb MPEG4"},
            {"name": "Show Ep 2.mp4", "source": "original", "size": "1000", "length": "1500.5"},
            {"name": "history/files/Show Ep 2.mp4.~1~", "source": "original"},
            {"name": "Show Ep 2.srt", "source": "original"},
            {"name": "__ia_thumb.jpg", "source": "original"},
        ]
        got = tv.pick_playable("show", files)
        self.assertEqual([f["file"] for f in got], ["Show Ep 2.mp4", "Show Ep 10.mp4"])
        self.assertEqual(got[0]["url"], "https://archive.org/download/show/Show%20Ep%202.mp4")
        self.assertEqual(got[0]["seconds"], 1500.5)

    def test_sharper_mkv_is_offered_for_a_player_app(self):
        files = [
            {"name": "Film.1080p.mkv", "source": "original", "format": "Matroska", "width": "1920", "height": "1040", "size": "2500000000"},
            {"name": "Film.1080p.mp4", "source": "derivative", "original": "Film.1080p.mkv", "format": "h.264", "width": "854", "height": "462"},
        ]
        (f,) = tv.pick_playable("film", files)
        self.assertEqual((f["file"], f["label"]), ("Film.1080p.mp4", "480p"))     # plays inside the TV app
        self.assertEqual((f["alt"]["file"], f["alt"]["label"]), ("Film.1080p.mkv", "1080p"))
        # the native TV player gets every copy, sharpest first, and starts with the best
        self.assertEqual([v["label"] for v in f["versions"]], ["1080p", "480p"])
        self.assertEqual(f["best"]["label"], "1080p")

    def test_no_alt_when_the_playable_copy_is_already_best(self):
        files = [{"name": "Film.mp4", "source": "original", "width": "1280", "height": "720"},
                 {"name": "Film.ia.mp4", "source": "derivative", "original": "Film.mp4", "width": "1280", "height": "720"}]
        (f,) = tv.pick_playable("film", files)
        self.assertEqual((f["file"], f["alt"]), ("Film.mp4", None))

    def test_low_bitrate_copy_is_the_fallback(self):
        files = [{"name": "Film_512kb.mp4", "source": "derivative", "original": "Film.mpeg", "format": "512Kb MPEG4"}]
        self.assertEqual([f["file"] for f in tv.pick_playable("x", files)], ["Film_512kb.mp4"])


class QualityTests(unittest.TestCase):
    def test_labels_from_real_dimensions(self):
        from buzz import quality as Q
        self.assertEqual(Q.label(Q.from_dims(1920, 800)), "1080p")      # widescreen is still 1080p
        self.assertEqual(Q.label(Q.from_dims(1280, 694)), "720p")
        self.assertEqual(Q.label(Q.from_dims(852, 460)), "480p")
        self.assertEqual(Q.label(Q.from_dims(640, 368)), "Low")
        self.assertIsNone(Q.from_dims(None, ""))

    def test_labels_from_release_names(self):
        from buzz import quality as Q
        self.assertEqual(Q.from_title("Eternal.Sunshine.2004.1080p.BluRay"), 1080)
        self.assertEqual(Q.from_title("Film 2004 DVDRip x264"), 480)
        self.assertEqual(Q.from_title("Film 2160p HDR"), 2160)
        self.assertIsNone(Q.from_title("Eternal Sunshine of the Spotless Mind (2004)"))
        self.assertEqual(Q.label(Q.from_title("Planet Earth 8K 4320p")), "8K")
        self.assertEqual(Q.label(Q.from_dims(7680, 3200)), "8K")
        self.assertGreater(Q.rank(4320), Q.rank(2160))

    def test_search_also_asks_for_4k_and_8k_copies(self):
        asked = []

        class P(Provider):
            name = "IA"
            hires_queries = True

            def search(self, query, category, limit, **f):
                asked.append((self.name, query))
                return []

        class J(P):
            name = "Jackett"            # one search already covers every site: no repeats
            hires_queries = False

        Engine([P(), J()]).search("Some Film", "movies", smart=False)
        self.assertEqual(sorted(q for n, q in asked if n == "IA"),
                         sorted(["Some Film", "Some Film 2160p", "Some Film 4K", "Some Film 8K"]))
        self.assertEqual([q for n, q in asked if n == "Jackett"], ["Some Film"])
        asked.clear()
        asked.clear()
        Engine([P()]).search("Some Book", "books", smart=False)
        self.assertEqual(asked, [("IA", "Some Book")])

    def test_search_ranks_sharper_copies_first(self):
        class P(Provider):
            name = "IA"

            def search(self, query, category, limit, **f):
                mk = lambda t, dl: Result(title=t, provider=self.name, category="movies", infohash=f"{dl:040x}",
                                          size=2 * 1024 ** 3, downloads=dl, trusted_source=True,
                                          details_url=f"https://archive.org/details/x{dl}")
                return [mk("Some Film (2004)", 9000), mk("Some Film (2004)", 50), mk("Some Film (2004)", 400)]

            def quality(self, r):
                return {9000: (694, True), 50: 1040, 400: None}[r.downloads]

        got = Engine([P()]).search("Some Film", "movies", smart=False)["results"]
        self.assertEqual([r["quality"] and r["quality"]["label"] for r in got], ["1080p", "720p", None])


class PairingTests(unittest.TestCase):
    def test_pin_gives_a_token_stored_only_as_hash(self):
        cfg, saved = {"pin": "123456", "tokens": []}, []
        p = tv.Pairing(cfg, lambda: saved.append(1))
        token = p.pair("123456")
        self.assertTrue(p.check(token))
        self.assertNotIn(token, cfg["tokens"])
        self.assertFalse(p.check("guess"))
        self.assertFalse(p.check(None))
        self.assertEqual(saved, [1])

    def test_wrong_pins_lock_pairing(self):
        p = tv.Pairing({"pin": "123456", "tokens": []}, lambda: None)
        for _ in range(5):
            with self.assertRaises(PermissionError):
                p.pair("000000", now=1000)
        with self.assertRaisesRegex(PermissionError, "Too many"):
            p.pair("123456", now=1001)                 # even the right PIN waits out the lock
        self.assertTrue(p.pair("123456", now=1100))

    def test_no_pin_set_never_pairs(self):
        with self.assertRaises(PermissionError):
            tv.Pairing({"pin": "", "tokens": []}, lambda: None).pair("")


class NetworkTests(unittest.TestCase):
    def test_only_private_ip_literals_count_as_lan_hosts(self):
        self.assertTrue(tv.host_is_lan_literal("192.168.1.9:8765", 8765))
        self.assertTrue(tv.host_is_lan_literal("10.0.0.4:8765", 8765))
        self.assertFalse(tv.host_is_lan_literal("192.168.1.9:80", 8765))
        self.assertFalse(tv.host_is_lan_literal("evil.example:8765", 8765))     # DNS rebinding needs a name
        self.assertFalse(tv.host_is_lan_literal("8.8.8.8:8765", 8765))

    def test_services_cover_indian_ott_with_search_links(self):
        s = {x["id"]: x for x in tv.services_for("Hum Log")}
        for sid in ("zee5", "hotstar", "sonyliv", "netflix", "prime", "youtube", "hoichoi", "chaupal", "stage", "lionsgate"):
            self.assertIn(sid, s)
        self.assertTrue(s["zee5"]["web"].startswith("https://www.google.com/search?"))
        self.assertIn("site%3Azee5.com", s["zee5"]["web"])


class TvDetectionTests(unittest.TestCase):
    def test_series_and_episodes_vs_films(self):
        self.assertTrue(looks_tv({"title": "Ramayan Episode 12", "collection": "opensource_movies"}))
        self.assertTrue(looks_tv({"title": "The Twilight Zone S01E04"}))
        self.assertTrue(looks_tv({"title": "Doctor Who", "collection": ["television_inbox", "television"]}))
        self.assertTrue(looks_tv({"title": "Byomkesh", "subject": ["Bengali TV Series"]}))
        self.assertFalse(looks_tv({"title": "Sholay 1975", "collection": "opensource_movies"}))
        self.assertFalse(looks_tv({"title": "Mars2 Rover footage"}))


class TestHandler(app.Handler):
    """Lets a test request pose as a device on the home network."""
    @property
    def remote(self):
        return self.headers.get("X-Test-Remote") == "1"

    def log_message(self, *a):
        pass


class TvServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.Server(("127.0.0.1", 0), TestHandler)
        cls.port = cls.server.server_address[1]
        cfg = {**app.DEFAULTS, "tv": {"enabled": True, "pin": "654321", "tokens": [], "autostart": False}}
        TestHandler.engine = Engine([StubProvider()])
        TestHandler.cfg = cfg
        TestHandler.port = cls.port
        TestHandler.lan_bound = True
        TestHandler.allowed_hosts = {f"127.0.0.1:{cls.port}", f"localhost:{cls.port}"}
        TestHandler.pairing = tv.Pairing(cfg["tv"], lambda: None)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, body=None, remote=True, host=None, cookie=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": host or (f"192.168.1.20:{self.port}" if remote else f"127.0.0.1:{self.port}")}
        if remote:
            headers["X-Test-Remote"] = "1"
        if cookie:
            headers["Cookie"] = cookie
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            headers.update({"Content-Type": "application/json", "X-BuzzTorrent": "1"})
        c.request(method, path, body=data, headers=headers)
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, r.getheader("Set-Cookie"), r.getheader("Content-Security-Policy"), raw

    def test_tv_page_and_hello_are_open_but_everything_else_needs_pairing(self):
        status, _, csp, _ = self.req("GET", "/tv")
        self.assertEqual(status, 200)
        self.assertIn("media-src https://archive.org", csp)
        status, _, _, raw = self.req("GET", "/api/tv/hello")
        hello = json.loads(raw)
        self.assertEqual((status, hello["paired"]), (200, False))
        self.assertRegex(hello["ui"], r"^[0-9a-f]{16}$")      # the TV reloads when this changes
        self.assertIn("apk", hello)
        self.assertEqual(self.req("GET", "/api/search?q=film&cat=movies&smart=0")[0], 401)

    def test_pc_only_endpoints_refuse_the_network_even_when_paired(self):
        _, cookie, _, _ = self.req("POST", "/api/tv/pair", {"pin": "654321"})
        token = cookie.split(";")[0]
        self.assertIn("HttpOnly", cookie)
        self.assertEqual(self.req("GET", "/api/search?q=film&cat=movies&smart=0", cookie=token)[0], 200)
        for path in ("/api/settings", "/api/scanner", "/api/tv/status", "/", "/app.js", "/api/player/info"):
            status = self.req("GET", path, cookie=token)[0]
            self.assertIn(status, (302, 403), path)
        self.assertEqual(self.req("POST", "/api/tv/config", {"enabled": False}, cookie=token)[0], 403)
        self.assertEqual(self.req("POST", "/api/scanner/quarantine", {"name": "x"}, cookie=token)[0], 403)
        self.assertEqual(self.req("POST", "/api/settings", {"torznab": []}, cookie=token)[0], 403)
        # opening files / VLC on the PC is for the PC itself only
        self.assertEqual(self.req("POST", "/api/library/open", {"id": "0" * 20}, cookie=token)[0], 403)
        self.assertEqual(self.req("POST", "/api/play/external", {"url": "https://archive.org/download/x/y.mkv"}, cookie=token)[0], 403)
        self.assertEqual(self.req("POST", "/api/downloads/open", {}, cookie=token)[0], 403)

    def test_phone_search_reaches_a_listening_tv(self):
        _, cookie, _, _ = self.req("POST", "/api/tv/pair", {"pin": "654321"})
        token = cookie.split(";")[0]
        first = json.loads(self.req("GET", "/api/tv/remote/poll?after=-1", cookie=token)[3])   # TV connects
        got = {}

        def tv_waits():
            got["d"] = json.loads(self.req("GET", f"/api/tv/remote/poll?after={first['seq']}", cookie=token)[3])

        t = threading.Thread(target=tv_waits)
        t.start()
        status = self.req("POST", "/api/tv/remote", {"action": "search", "q": "sholay", "cat": "movies", "lang": "hi"},
                          cookie=token)[0]
        t.join(10)
        self.assertEqual(status, 200)
        self.assertEqual([(c["action"], c["q"], c["lang"]) for c in got["d"]["commands"]], [("search", "sholay", "hi")])

    def test_phone_remote_needs_pairing_and_valid_commands(self):
        self.assertEqual(self.req("POST", "/api/tv/remote", {"action": "search", "q": "x"})[0], 401)
        _, cookie, _, _ = self.req("POST", "/api/tv/pair", {"pin": "654321"})
        token = cookie.split(";")[0]
        self.assertEqual(self.req("POST", "/api/tv/remote", {"action": "shutdown"}, cookie=token)[0], 400)
        self.assertEqual(self.req("POST", "/api/tv/remote", {"action": "open", "id": "../../x"}, cookie=token)[0], 400)
        self.assertEqual(self.req("GET", "/remote")[0], 200)

    def test_phones_land_on_the_remote_page(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("GET", "/", headers={"Host": f"192.168.1.20:{self.port}", "X-Test-Remote": "1",
                                       "User-Agent": "Mozilla/5.0 (Linux; Android 14) Mobile Safari"})
        r = c.getresponse()
        r.read()
        c.close()
        self.assertEqual((r.status, r.getheader("Location")), (302, "/remote"))

    def test_wrong_pin_is_refused(self):
        status, cookie, _, _ = self.req("POST", "/api/tv/pair", {"pin": "111111"})
        self.assertEqual((status, cookie), (403, None))
        TestHandler.pairing.fails = 0          # don't leave the lock-out armed for other tests

    def test_host_header_must_be_us(self):
        self.assertEqual(self.req("GET", "/tv", host=f"evil.example:{self.port}")[0], 403)

    def test_this_pc_keeps_full_access(self):
        status, _, _, raw = self.req("GET", "/api/tv/status", remote=False)
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["pin"], "654321")
        self.assertEqual(self.req("GET", "/api/settings", remote=False)[0], 200)

    def test_tv_access_off_refuses_the_network(self):
        TestHandler.cfg["tv"]["enabled"] = False
        try:
            self.assertEqual(self.req("GET", "/tv", host=f"127.0.0.1:{self.port}")[0], 403)
        finally:
            TestHandler.cfg["tv"]["enabled"] = True


if __name__ == "__main__":
    unittest.main()

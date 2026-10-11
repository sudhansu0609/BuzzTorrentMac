import http.client
import io
import json
import sys
import threading
import unittest

import app
from buzz import tv
from buzz.adult import AdultGate, is_adult
from buzz.engine import Engine
from buzz.models import Result
from buzz.providers.base import Provider
from buzz.providers.torznab import _category

H = lambda i: f"{i:040x}"


class Mixed(Provider):
    name = "Jackett"

    def search(self, query, category, limit, **f):
        mk = lambda i, t, c: Result(title=t, provider=self.name, category=c, infohash=H(i), size=2 * 1024 ** 3, seeders=5)
        return [mk(1, f"{query} 2004 1080p", "movies"), mk(2, f"{query} XXX 2004", "movies"),
                mk(3, f"{query} scene", "adult"), mk(4, f"{query} ABC-123 1080p", "other")]


class GateTests(unittest.TestCase):
    def setUp(self):
        self.cfg = {"enabled": False, "pin_hash": "", "pin_salt": "", "on_tv": False}
        self.g = AdultGate(self.cfg, lambda: None)

    def test_needs_a_pin_and_unlocks_only_with_it(self):
        with self.assertRaises(ValueError):
            self.g.configure(True, None, False)              # no PIN yet
        with self.assertRaises(ValueError):
            self.g.configure(True, "12", False)              # too short
        self.g.configure(True, "4321", False)
        self.assertNotIn("4321", json.dumps(self.cfg))      # stored only as a salted hash
        with self.assertRaises(PermissionError):
            self.g.unlock("0000")
        token = self.g.unlock("4321")
        self.assertTrue(self.g.valid(token))
        self.assertFalse(self.g.valid(token, now=10 ** 12))  # expires
        self.g.lock_token(token)
        self.assertFalse(self.g.valid(token))

    def test_wrong_pins_lock_it_and_turning_off_relocks(self):
        self.g.configure(True, "4321", False)
        for _ in range(5):
            with self.assertRaises(PermissionError):
                self.g.unlock("1111", now=100)
        with self.assertRaisesRegex(PermissionError, "Too many"):
            self.g.unlock("4321", now=101)
        token = self.g.unlock("4321", now=200)
        self.g.configure(False, None, False)
        self.assertFalse(self.g.valid(token, now=201))


class SeparationTests(unittest.TestCase):
    def test_classification(self):
        self.assertEqual(_category("6000"), "adult")
        self.assertEqual(_category("6045"), "adult")
        self.assertTrue(is_adult("Some Video XXX 1080p"))
        self.assertTrue(is_adult("Studio SSIS-123 Uncensored"))
        self.assertFalse(is_adult("Sex Education S01 1080p"))
        self.assertFalse(is_adult("Naked Lunch 1991"))

    def test_adult_never_mixes_into_normal_results(self):
        e = Engine([Mixed()])
        normal = [r["title"] for r in e.search("Film", "movies", smart=False)["results"]]
        self.assertEqual(normal, ["Film 2004 1080p"])
        private = sorted(r["title"] for r in e.search("Film", "adult", smart=False)["results"])
        self.assertEqual(private, ["Film ABC-123 1080p", "Film XXX 2004", "Film scene"])


class Handler(app.Handler):
    @property
    def remote(self):
        return self.headers.get("X-Test-Remote") == "1"

    def log_message(self, *a):
        pass


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = app.Server(("127.0.0.1", 0), Handler)
        cls.port = cls.server.server_address[1]
        cfg = {**app.DEFAULTS, "tv": {"enabled": True, "pin": "222333", "tokens": [], "autostart": False},
               "adult": {"enabled": False, "pin_hash": "", "pin_salt": "", "on_tv": False}}
        Handler.engine = Engine([Mixed()])
        Handler.cfg, Handler.port, Handler.lan_bound = cfg, cls.port, True
        Handler.allowed_hosts = {f"127.0.0.1:{cls.port}"}
        Handler.pairing = tv.Pairing(cfg["tv"], lambda: None)
        Handler.adult = AdultGate(cfg["adult"], lambda: None)
        Handler.adult.configure(True, "4321", False)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def req(self, method, path, body=None, cookie=None, remote=False):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Host": f"192.168.1.30:{self.port}" if remote else f"127.0.0.1:{self.port}"}
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
        return r.status, r.getheader("Set-Cookie"), json.loads(raw or b"{}")

    def test_locked_until_the_pin_is_given(self):
        status, _, data = self.req("GET", "/api/search?q=Film&cat=adult")
        self.assertEqual((status, data.get("locked")), (403, True))
        status, cookie, _ = self.req("POST", "/api/adult/unlock", {"pin": "4321"})
        self.assertEqual(status, 200)
        self.assertNotIn("Max-Age", cookie)                     # a session cookie: gone when the app closes
        token = cookie.split(";")[0]
        status, _, data = self.req("GET", "/api/search?q=Film&cat=adult", cookie=token)
        self.assertEqual(status, 200)
        self.assertTrue(all(r["category"] == "adult" for r in data["results"]))
        _, cleared, _ = self.req("POST", "/api/adult/lock", {}, cookie=token)
        self.assertIn("Max-Age=0", cleared)
        self.assertEqual(self.req("GET", "/api/search?q=Film&cat=adult", cookie=token)[0], 403)

    def test_tv_needs_permission_from_the_pc(self):
        _, pair, _ = self.req("POST", "/api/tv/pair", {"pin": "222333"}, remote=True)
        tv_cookie = pair.split(";")[0]
        state = self.req("GET", "/api/adult/state", cookie=tv_cookie, remote=True)[2]
        self.assertFalse(state["enabled"])                        # not even shown on the TV
        self.assertEqual(self.req("POST", "/api/adult/unlock", {"pin": "4321"}, cookie=tv_cookie, remote=True)[0], 403)
        self.assertEqual(self.req("POST", "/api/adult/config", {"enabled": True}, cookie=tv_cookie, remote=True)[0], 403)

    def test_private_titles_stay_off_the_watchlist(self):
        status, _, data = self.req("POST", "/api/tv/watch", {"title": "x", "category": "adult"})
        self.assertEqual(status, 400)


class LogTests(unittest.TestCase):
    def test_search_words_never_reach_the_log(self):
        h = app.Handler.__new__(app.Handler)
        buf, old = io.StringIO(), sys.stderr
        sys.stderr = buf
        try:
            h.log_message('"%s" %s %s', "GET /api/search?q=secret+words&cat=adult HTTP/1.1", "200", "-")
        finally:
            sys.stderr = old
        self.assertIn("/api/search", buf.getvalue())
        self.assertNotIn("secret", buf.getvalue())


if __name__ == "__main__":
    unittest.main()

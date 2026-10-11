import struct
import unittest
from unittest import mock

import app
from buzz import icon
from buzz.details import html_to_text
from buzz.fuzzy import clean_release, similarity, strip_disambiguation

TITLE = "Night of the Living Dead"


class FuzzyTests(unittest.TestCase):
    def test_typos_spacing_and_word_order_still_match(self):
        for q in ("nite of teh livng ded", "nightofthe living dead", "nightofthelivingdead", "living dead night",
                  "Night Of The Living Dead!!"):
            self.assertGreaterEqual(similarity(q, TITLE), 0.8, q)

    def test_different_titles_do_not(self):
        self.assertLess(similarity("dawn of the dead", TITLE), 0.72)
        self.assertLess(similarity("nightofthelivingdead", "Night"), 0.72)
        self.assertLess(similarity("pride and prejudice", TITLE), 0.5)

    def test_exact_beats_contains(self):
        self.assertGreater(similarity("casablanca", "Casablanca"), similarity("casablanca", "Radio Casablanca 6.94 MHz"))

    def test_release_names(self):
        self.assertEqual(clean_release("Night.of.the.Living.Dead.1968.REMASTERED.1080p.BluRay.x264-GRP"), (TITLE, 1968))
        self.assertEqual(clean_release("2001.A.Space.Odyssey.1968.2160p"), ("2001 A Space Odyssey", 1968))
        self.assertEqual(clean_release("The Office US S05E02 720p WEB")[0], "The Office US")
        self.assertEqual(strip_disambiguation("Doom (1993 video game)"), ("Doom", 1993))

    def test_html_description_becomes_plain_text(self):
        text = html_to_text("<p>A <b>classic</b> film.</p><script>alert(1)</script><br>Starring <a href='x'>Duane Jones</a>")
        self.assertEqual(text, "A classic film.\n\nStarring Duane Jones")   # </p> + <br> = paragraph break


class PresenceTests(unittest.TestCase):
    def make(self):
        p = app.Presence(server=None, grace=4, stale=180, first_wait=120)
        p.started = 1000.0
        return p

    def test_waits_for_first_window(self):
        p = self.make()
        self.assertFalse(p.should_stop(1100))
        self.assertTrue(p.should_stop(1121))          # no window ever connected

    def test_closing_the_last_window_stops_after_grace(self):
        p = self.make()
        with mock.patch.object(app.time, "time", return_value=1001.0):
            p.ping("client-aaaaaaaa")
            p.bye("client-aaaaaaaa")
        self.assertFalse(p.should_stop(1003))
        self.assertTrue(p.should_stop(1005.5))

    def test_reload_survives(self):
        p = self.make()
        with mock.patch.object(app.time, "time", return_value=1001.0):
            p.ping("client-aaaaaaaa")
            p.bye("client-aaaaaaaa")                   # page unloads…
            p.ping("client-bbbbbbbb")                  # …and the reloaded page pings again
        self.assertFalse(p.should_stop(1010))

    def test_other_window_keeps_it_alive_and_bad_ids_ignored(self):
        p = self.make()
        with mock.patch.object(app.time, "time", return_value=1001.0):
            p.ping("client-aaaaaaaa")
            p.ping("client-bbbbbbbb")
            p.bye("client-aaaaaaaa")
            p.bye("not-a-known-client")
            self.assertFalse(p.ping("<script>"))
        self.assertFalse(p.should_stop(1010))
        self.assertTrue(p.should_stop(1001 + 181 + 4))  # crashed window: stale, then stop


class IconTests(unittest.TestCase):
    def test_png_and_ico_are_well_formed(self):
        data = icon.png(16)
        self.assertTrue(data.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(struct.unpack(">II", data[16:24]), (16, 16))
        ico = icon.ico(sizes=(16, 32))
        self.assertEqual(struct.unpack("<HHH", ico[:6]), (0, 1, 2))


if __name__ == "__main__":
    unittest.main()

import unittest

from buzz.fuzzy import clean_release, detect_languages, norm, romanize, script_language, similarity
from buzz.models import Result
from buzz.providers.archive_org import doc_languages, lang_filters
from buzz.providers.torznab import _only_language, _with_language


class ScriptTests(unittest.TestCase):
    def test_norm_keeps_indic_and_urdu_letters(self):
        self.assertEqual(norm("प्रेमचंद की कहानियाँ!"), "प्रेमचंद की कहानियां")      # chandrabindu unified
        self.assertEqual(norm("দেবদাস"), "দেবদাস")
        self.assertEqual(norm("دیوانِ غالب"), "دیوان غالب")                       # harakat removed
        self.assertEqual(norm("Deewān-e-Ghālib"), "deewan e ghalib")

    def test_romanize(self):
        self.assertEqual(romanize("गोदान"), "godan")
        self.assertEqual(romanize("मधुशाला"), "madhushala")
        self.assertEqual(romanize("ग़ज़ल"), "ghazal")
        self.assertEqual(romanize("हरिवंश राय बच्चन"), "harivansh ray bachchan")

    def test_cross_script_matching(self):
        self.assertGreaterEqual(similarity("premchand ki kahaniyan", "प्रेमचंद की कहानियाँ"), 0.9)
        self.assertGreaterEqual(similarity("madhushala", "मधुशाला"), 0.9)
        self.assertGreaterEqual(similarity("jibanananda das", "জীবনানন্দ দাশ"), 0.85)
        self.assertLess(similarity("गोदान", "कमल"), 0.5)

    def test_script_detection(self):
        self.assertEqual(script_language("मधुशाला"), "hi")
        self.assertEqual(script_language("দেবদাস"), "bn")
        self.assertEqual(script_language("دیوان غالب"), "ur")
        self.assertIsNone(script_language("Sholay"))


class ReleaseTests(unittest.TestCase):
    def test_language_tags(self):
        self.assertEqual(detect_languages("Pushpa 2021 [Hindi-Tamil] Dual Audio 1080p"), ["hi", "ta"])
        self.assertEqual(detect_languages("Chokher Bali 2003 Bangla Movie"), ["bn"])
        self.assertEqual(detect_languages("Nirahua Hindustani 3 Bhojpuri"), ["bho"])
        self.assertEqual(detect_languages("Hindi Medium 2017"), ["hi"])

    def test_clean_release_strips_language_and_junk(self):
        self.assertEqual(clean_release("Sholay Hindi Full Movie")[0], "Sholay")
        self.assertEqual(clean_release("Nirahua Hindustani 3 Bhojpuri Full Movie")[0], "Nirahua Hindustani 3")
        self.assertEqual(clean_release("Hindi Medium 2017 HDRip"), ("Hindi Medium", 2017))


class FilterTests(unittest.TestCase):
    def test_archive_language_and_genre_query(self):
        q = " AND ".join(lang_filters("books", "ur", "poetry"))
        self.assertIn("language:(urd OR Urdu OR ur)", q)
        self.assertIn("shayari", q)
        self.assertIn("NOT subject:", q)                         # religious texts kept out of poetry
        self.assertNotIn("NOT subject", " ".join(lang_filters("books", "hi", "religion")))
        self.assertEqual(lang_filters("movies", "any", "poetry"), [])   # genres only apply to books

    def test_archive_language_values(self):
        self.assertEqual(doc_languages(["Hindi", "eng"]), ["hi", "en"])
        self.assertEqual(doc_languages("ben; Bengali"), ["bn"])

    def test_torznab_language_word_and_filter(self):
        self.assertEqual(_with_language("Pushpa", "hi"), "Pushpa hindi")
        self.assertEqual(_with_language("Pushpa Hindi", "hi"), "Pushpa Hindi")
        self.assertEqual(_with_language("Pushpa", "en"), "Pushpa")
        rs = [Result(title=t, provider="J", infohash=f"{i:040x}", language=detect_languages(t))
              for i, t in enumerate(["Pushpa 2021 Hindi", "Pushpa 2021 Telugu", "Pushpa 2021 1080p"])]
        self.assertEqual([r.title for r in _only_language(rs, "hi")], ["Pushpa 2021 Hindi", "Pushpa 2021 1080p"])


if __name__ == "__main__":
    unittest.main()

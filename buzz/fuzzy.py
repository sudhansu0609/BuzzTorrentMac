"""Approximate title matching: tolerant of typos, missing/extra spaces, word
order and release-name noise ("Night.of.the.Living.Dead.1968.1080p.BluRay")."""
from __future__ import annotations

import re
import unicodedata
from difflib import SequenceMatcher

YEAR = re.compile(r"(?<!\d)(19[0-9]{2}|20[0-9]{2})(?!\d)")
RELEASE_TAGS = re.compile(
    r"\b(4320p|8k|2160p|1080p|1080i|720p|576p|480p|4k|uhd|hdr10? ?plus|hdr10?|hdr|dolby vision|dovi|sdr|10 ?bit|"
    r"60fps|hfr|dv|bluray|blu ray|brrip|bdrip|dvdrip|dvdscr|webrip|web dl|webdl|"
    r"web|hdtv|hdrip|camrip|cam|ts|telesync|x264|x265|h 264|h 265|hevc|avc|xvid|divx|aac|ac3|dts|ddp?5 1|atmos|"
    r"remux|repack|proper|extended|unrated|remastered|internal|limited|multi|dual audio|subbed|dubbed|"
    r"complete|season|s\d{1,2}(e\d{1,3})?|e\d{1,3}|part \d+|vol \d+|cd\d|disc \d|pc|iso|gog|repack|"
    r"full movie|full film|full hd movie|hd movie|bangla movie|hq|org|pre ?dvd|hdcam|hd ?rip|esubs?|msubs?|"
    r"hindi dubbed|south dubbed)\b", re.I)
STOP = {"the", "a", "an", "of", "and", "&", "in", "on", "to", "for", "ki", "ka", "ke", "aur", "की", "का", "के", "और"}

# --------------------------------------------------------------- languages
# code -> (English name, words that mark it in release names / metadata)
LANGUAGES = {
    "en": ("English", ("english", "eng")),
    "hi": ("Hindi", ("hindi", "hin")),
    "bn": ("Bengali", ("bengali", "bangla")),
    "ur": ("Urdu", ("urdu",)),
    "bho": ("Bhojpuri", ("bhojpuri",)),
    "ta": ("Tamil", ("tamil",)),
    "te": ("Telugu", ("telugu",)),
    "mr": ("Marathi", ("marathi",)),
    "pa": ("Punjabi", ("punjabi",)),
}
_LANG_WORDS = {w: code for code, (_, words) in LANGUAGES.items() for w in words}
_LANG_TAG = re.compile(r"\b(" + "|".join(sorted(_LANG_WORDS, key=len, reverse=True)) + r")\b", re.I)

# Unicode blocks of the Indic scripts that share the ISCII layout with Devanagari
# (a letter's offset inside its block is the same), plus Arabic script for Urdu.
INDIC_BLOCKS = {0x0900: "hi", 0x0980: "bn", 0x0A00: "pa", 0x0A80: "gu", 0x0B00: "or",
                0x0B80: "ta", 0x0C00: "te", 0x0C80: "kn", 0x0D00: "ml"}


def script_language(text: str) -> str | None:
    """Wikipedia language edition matching the script of `text` (None = Latin/other)."""
    for ch in text or "":
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFEFF:
            return "ur"
        base = cp & ~0x7F
        if base in INDIC_BLOCKS and 0x0900 <= cp <= 0x0D7F:
            return INDIC_BLOCKS[base]
    return None


def detect_languages(title: str) -> list[str]:
    """Language codes named in a release title ('Pushpa 2021 Hindi Dual Audio' -> ['hi'])."""
    found = []
    for m in _LANG_TAG.finditer(re.sub(r"[._\-\[\]()]+", " ", title or "")):
        code = _LANG_WORDS[m.group(1).lower()]
        if code not in found:
            found.append(code)
    sl = script_language(title)
    if sl and sl in LANGUAGES and sl not in found:
        found.append(sl)
    return found


# ------------------------------------------------------- romanization (Indic)
_CONS = {"क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n", "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
         "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n", "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n",
         "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m", "य": "y", "र": "r", "ल": "l", "ळ": "l", "व": "v",
         "श": "sh", "ष": "sh", "स": "s", "ह": "h", "ऱ": "r", "ऴ": "l", "य़": "y"}
_NUKTA = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "r", "ढ": "rh", "फ": "f"}
_VOW = {"अ": "a", "आ": "a", "इ": "i", "ई": "i", "उ": "u", "ऊ": "u", "ऋ": "ri", "ए": "e", "ऐ": "ai", "ओ": "o",
        "औ": "au", "ऍ": "e", "ऑ": "o", "ऎ": "e", "ऒ": "o"}
_MATRA = {"ा": "a", "ि": "i", "ी": "i", "ु": "u", "ू": "u", "ृ": "ri", "े": "e", "ै": "ai", "ो": "o", "ौ": "au",
          "ॅ": "e", "ॉ": "o", "ॆ": "e", "ॊ": "o"}
_VIRAMA, _NUKTA_SIGN = "्", "़"


def _to_devanagari(ch: str) -> str:
    cp = ord(ch)
    base = cp & ~0x7F
    if base in INDIC_BLOCKS and base != 0x0900 and 0x0980 <= cp <= 0x0D7F:
        return chr(0x0900 + (cp - base))
    return ch


def romanize(text: str) -> str:
    """Approximate Hinglish-style romanization of Indic text (Devanagari and sister scripts),
    good enough to match 'premchand' against 'प्रेमचंद'. Other characters pass through."""
    chars = [_to_devanagari(c) for c in unicodedata.normalize("NFC", text or "")]
    out, i, n = [], 0, len(chars)
    while i < n:
        c = chars[i]
        if c in _CONS:
            nxt = chars[i + 1] if i + 1 < n else ""
            if nxt == _NUKTA_SIGN:
                out.append(_NUKTA.get(c, _CONS[c]))
                i += 1
                nxt = chars[i + 1] if i + 1 < n else ""
            else:
                out.append(_CONS[c])
            if nxt in _MATRA:
                out.append(_MATRA[nxt])
                i += 1
            elif nxt == _VIRAMA:
                i += 1
            else:
                # inherent vowel, dropped at the end of a word (schwa deletion): कमल -> kamal
                after = chars[i + 1] if i + 1 < n else " "
                if not (after.isspace() or not (after in _CONS or after in _VOW or after in "ंँः")):
                    out.append("a")
                elif after in "ंँः":
                    out.append("a")
        elif c in _VOW:
            out.append(_VOW[c])
        elif c in "ंँ":
            out.append("n")
        elif c == "ः":
            out.append("h")
        elif c in (_VIRAMA, _NUKTA_SIGN):
            pass
        elif "०" <= c <= "९":
            out.append(str(ord(c) - 0x0966))
        else:
            out.append(c)
        i += 1
    return "".join(out)


def norm(s: str) -> str:
    """Lowercase; strip punctuation and Latin accents; collapse spaces. Letters of every script
    are kept (and Devanagari/Arabic spelling variants unified), so Hindi, Bengali and Urdu work."""
    s = unicodedata.normalize("NFKC", s or "").lower().replace("&", " and ")
    out = []
    for ch in s:
        if ch.isascii():
            out.append(ch if ch.isalnum() else " ")
            continue
        cat = unicodedata.category(ch)
        if cat[0] not in "LNM":
            out.append(" ")
        elif "ً" <= ch <= "ٟ" or ch == "ٰ" or ch == "़":
            continue                                   # Arabic harakat, Devanagari nukta: often omitted
        elif ch == "ँ":
            out.append("ं")                       # chandrabindu ~ anusvara (कहानियाँ / कहानियां)
        else:
            d = unicodedata.normalize("NFKD", ch)
            out.append(d[0] if d[0].isascii() and d[0].isalpha() else ch)   # é -> e, keep other scripts
    return " ".join("".join(out).split())


def compact(s: str) -> str:
    return norm(s).replace(" ", "")


def clean_release(title: str) -> tuple[str, int | None]:
    """'Night.of.the.Living.Dead.1968.1080p.BluRay.x264-GRP' -> ('Night of the Living Dead', 1968)"""
    t = re.sub(r"[._]+", " ", title or "")
    t = re.sub(r"\[[^\]]*\]|\{[^}]*\}", " ", t)
    year = None
    m = YEAR.search(t[1:]) if t else None          # a title may *start* with a year ("2001 …")
    if m:
        year = int(m.group(1))
        t = t[:m.start() + 1]
    tag = RELEASE_TAGS.search(t)
    if tag and tag.start() > 0:
        t = t[:tag.start()]
    t = re.sub(r"[\s\-–(]+$", "", t.strip())
    t = re.sub(r"(\s+(" + _LANG_TAG.pattern[3:-3] + r"))+$", "", t, flags=re.I)   # trailing "Hindi", "Bangla"…
    return (" ".join(t.split()) or title), year


def strip_disambiguation(title: str) -> tuple[str, int | None]:
    """Wikipedia style 'Night of the Living Dead (1968 film)' -> ('Night of the Living Dead', 1968)"""
    m = re.match(r"^(.*?)\s*\(([^)]*)\)\s*$", title or "")
    if not m:
        return title, None
    y = YEAR.search(m.group(2))
    return m.group(1), int(y.group(1)) if y else None


def _partial(short: str, long: str) -> float:
    """Best ratio of `short` against any equal-length window of `long`."""
    if not short or not long:
        return 0.0
    if len(short) > len(long):
        short, long = long, short
    best = 0.0
    sm = SequenceMatcher(None, short, long, autojunk=False)
    for block in sm.get_matching_blocks():
        start = max(0, block.b - block.a)
        window = long[start:start + len(short)]
        r = SequenceMatcher(None, short, window, autojunk=False).ratio()
        if r > best:
            best = r
            if r > 0.995:
                break
    return best


def _token_set(a: str, b: str) -> float:
    ta = {w for w in a.split() if w not in STOP} or set(a.split())
    tb = {w for w in b.split() if w not in STOP} or set(b.split())
    if not ta or not tb:
        return 0.0
    if ta <= tb:
        # every query word is in the title: great match, slightly less so the more extra words it has
        return 1.0 - 0.025 * min(len(tb - ta), 6)
    inter = " ".join(sorted(ta & tb))
    sa = " ".join(sorted(ta - tb))
    sb = " ".join(sorted(tb - ta))
    base = [SequenceMatcher(None, inter, (inter + " " + sa).strip()).ratio(),
            SequenceMatcher(None, inter, (inter + " " + sb).strip()).ratio(),
            SequenceMatcher(None, (inter + " " + sa).strip(), (inter + " " + sb).strip()).ratio()]
    return max(base) if inter else base[2] * 0.9


def similarity(query: str, title: str) -> float:
    """0..1, how well `title` answers `query`. Robust to typos, spacing and extra words in the title,
    and across scripts: 'premchand ki kahaniyan' matches 'प्रेमचंद की कहानियाँ'."""
    direct = _similarity(query, title)
    if direct < 0.995 and (script_language(query) or script_language(title)):
        rq, rt = romanize(query), romanize(title)
        if (rq, rt) != (query, title):
            return max(direct, round(_similarity(rq, rt) * 0.98, 4))
    return direct


def _similarity(query: str, title: str) -> float:
    q, t = norm(query), norm(title)
    if not q or not t:
        return 0.0
    if q == t:
        return 1.0
    qc, tc = q.replace(" ", ""), t.replace(" ", "")
    if qc == tc:
        return 0.99                                   # only spacing differs
    whole = SequenceMatcher(None, qc, tc, autojunk=False).ratio()
    part = _partial(qc, tc)
    longer = len(tc) / max(1, len(qc))
    if longer >= 1:
        # a title longer than the query ("… (1968) restored edition") is still a good answer
        part *= 0.97 if longer < 1.5 else 0.93 if longer < 3 else 0.85
    else:
        part *= longer ** 0.5          # a title that covers only part of the query ("Night") is not
    tokens = _token_set(q, t) * 0.96
    return round(max(whole, part, tokens), 4)

"""The private 18+ section.

Off and invisible by default. Once turned on (Settings, with a PIN), it still stays hidden:
each device unlocks it with the PIN, and the unlock lasts 30 minutes or until the app is
closed. Adult results never mix into normal searches, and nothing about them is kept:
no history, no Continue watching, no watchlist, no Wikipedia lookups, no search words in
the log, and adult downloads are listed as "Private download" while locked."""
from __future__ import annotations

import hashlib
import re
import secrets
import threading
import time

COOKIE = "buzzadult"
UNLOCK_SECONDS = 30 * 60

# Titles that are adult content whatever category a site filed them under. Strong terms only,
# so ordinary films ("Sex Education", "Naked Lunch") aren't caught.
ADULT_TITLE = re.compile(
    r"\b(porn|porno|pornographic|xxx|hentai|erotica|jav|onlyfans|brazzers|bangbros|naughty ?america|"
    r"reality ?kings|blacked|tushy|vixen|playboy tv|stag films?|adult films?|sex tapes?)\b", re.I)
JAV_CODE = re.compile(r"\b[A-Z]{2,6}-\d{3,5}\b")      # e.g. "ABC-123" style adult video codes


def is_adult(title: str, category: str = "") -> bool:
    return category == "adult" or bool(ADULT_TITLE.search(title or "")) or bool(JAV_CODE.search(title or ""))


def _hash(pin: str, salt: str) -> str:
    return hashlib.pbkdf2_hmac("sha256", pin.encode(), salt.encode(), 200_000).hex()


class AdultGate:
    def __init__(self, cfg: dict, save):
        self.cfg, self.save = cfg, save          # cfg is the "adult" config section
        self.lock = threading.Lock()
        self.tokens: dict[str, float] = {}       # in memory only: a restart locks everything
        self.fails, self.locked_until = 0, 0.0

    @property
    def enabled(self) -> bool:
        return bool(self.cfg.get("enabled") and self.cfg.get("pin_hash"))

    def configure(self, enabled: bool, pin: str | None, on_tv: bool) -> None:
        if pin is not None and pin != "":
            if not re.fullmatch(r"\d{4,8}", pin):
                raise ValueError("The PIN must be 4 to 8 digits")
            salt = secrets.token_hex(16)
            self.cfg.update(pin_salt=salt, pin_hash=_hash(pin, salt))
        if enabled and not self.cfg.get("pin_hash"):
            raise ValueError("Set a PIN first")
        self.cfg.update(enabled=bool(enabled), on_tv=bool(on_tv))
        if not enabled:
            with self.lock:
                self.tokens.clear()
        self.save()

    def unlock(self, pin: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        with self.lock:
            if not self.enabled:
                raise PermissionError("The private section is turned off")
            if now < self.locked_until:
                raise PermissionError(f"Too many wrong PINs. Try again in {int(self.locked_until - now) + 1} s")
            good = self.cfg.get("pin_hash") or ""
            if not secrets.compare_digest(_hash(str(pin), self.cfg.get("pin_salt", "")), good):
                self.fails += 1
                if self.fails >= 5:
                    self.locked_until = now + 30 * 2 ** min(self.fails - 5, 6)
                raise PermissionError("Wrong PIN")
            self.fails = 0
            token = secrets.token_urlsafe(24)
            self.tokens = {t: exp for t, exp in self.tokens.items() if exp > now}
            self.tokens[token] = now + UNLOCK_SECONDS
            return token

    def valid(self, token: str | None, now: float | None = None) -> bool:
        now = time.time() if now is None else now
        if not token or not self.enabled:
            return False
        with self.lock:
            exp = self.tokens.get(token)
            if not exp or exp < now:
                self.tokens.pop(token, None)
                return False
            self.tokens[token] = now + UNLOCK_SECONDS     # stays open while in use
            return True

    def lock_token(self, token: str | None) -> None:
        with self.lock:
            self.tokens.pop(token or "", None)

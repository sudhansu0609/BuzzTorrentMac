"""What the TV remembers, kept on the PC so every TV and phone shares it:
- Continue watching: where each video was stopped.
- Watchlist: titles to keep an eye on, re-checked in the background; when a sharper or a
  free copy turns up, an alert says so ("1080p copy of Sholay is now available")."""
from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path

from . import quality as Q

MAX_PROGRESS = 300
MAX_WATCH = 200
MAX_ALERTS = 100
FINISHED = 0.94             # watched this far = finished, drops out of Continue watching
MIN_RESUME = 30             # seconds; earlier stops start from the beginning
CHECK_EVERY = 6 * 3600
KEY = re.compile(r"^(ia:[A-Za-z0-9._-]{1,120}:.{1,300}|lib:[0-9a-f]{20})$")


def _clean(s, n: int) -> str:
    return " ".join(str(s or "").split())[:n]


class TvState:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"progress": {}, "watchlist": {}, "alerts": []}
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            for k in self.data:
                if isinstance(loaded.get(k), type(self.data[k])):
                    self.data[k] = loaded[k]
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError:
            pass

    # ------------------------------------------------------------ progress
    def report(self, body: dict) -> None:
        key = str(body.get("key", ""))
        if not KEY.match(key):
            raise ValueError("bad progress key")
        try:
            pos, dur = float(body.get("pos") or 0), float(body.get("dur") or 0)
        except (TypeError, ValueError):
            raise ValueError("bad position")
        with self.lock:
            p = self.data["progress"]
            old = p.get(key, {})
            p[key] = {"key": key, "pos": max(0.0, pos), "dur": max(0.0, dur),
                      "title": _clean(body.get("title") or old.get("title"), 200),
                      "name": _clean(body.get("name") or old.get("name"), 200),
                      "thumb": _clean(body.get("thumb") or old.get("thumb"), 300),
                      "quality": _clean(body.get("quality") or old.get("quality"), 12),
                      "updated": time.time()}
            if len(p) > MAX_PROGRESS:
                for k, _ in sorted(p.items(), key=lambda kv: kv[1]["updated"])[:len(p) - MAX_PROGRESS]:
                    del p[k]
            self._save()

    def position(self, key: str) -> float:
        """Where to resume, or 0 to start from the beginning."""
        with self.lock:
            e = self.data["progress"].get(key)
        if not e or e["pos"] < MIN_RESUME or (e["dur"] and e["pos"] / e["dur"] >= FINISHED):
            return 0.0
        return e["pos"]

    def continue_list(self, limit: int = 20) -> list[dict]:
        with self.lock:
            items = list(self.data["progress"].values())
        out = [e for e in items if e["pos"] >= MIN_RESUME and not (e["dur"] and e["pos"] / e["dur"] >= FINISHED)]
        out.sort(key=lambda e: -e["updated"])
        return out[:limit]

    def forget_progress(self, key: str) -> None:
        with self.lock:
            if self.data["progress"].pop(key, None) is not None:
                self._save()

    # ----------------------------------------------------------- watchlist
    @staticmethod
    def watch_id(title: str, category: str) -> str:
        return hashlib.sha1(f"{title.lower()}|{category}".encode()).hexdigest()[:16]

    def watch_add(self, body: dict) -> dict:
        title = _clean(body.get("title"), 200)
        category = body.get("category") if body.get("category") in ("movies", "tv", "music", "all") else "movies"
        if not title:
            raise ValueError("need a title")
        wid = self.watch_id(title, category)
        with self.lock:
            w = self.data["watchlist"]
            if wid not in w and len(w) >= MAX_WATCH:
                raise ValueError("Your watchlist is full (200 titles)")
            w[wid] = {**w.get(wid, {}), "id": wid, "title": title, "category": category,
                      "year": body.get("year") if isinstance(body.get("year"), int) else None,
                      "thumb": _clean(body.get("thumb"), 300), "added": w.get(wid, {}).get("added", time.time()),
                      "best": int(body.get("quality") or 0) or w.get(wid, {}).get("best", 0),
                      "free": bool(body.get("free")) or w.get(wid, {}).get("free", False),
                      "checked": w.get(wid, {}).get("checked", 0)}
            self._save()
            return w[wid]

    def watch_remove(self, wid: str) -> None:
        with self.lock:
            if self.data["watchlist"].pop(wid, None) is not None:
                self.data["alerts"] = [a for a in self.data["alerts"] if a.get("watch") != wid]
                self._save()

    def watchlist(self) -> list[dict]:
        with self.lock:
            unseen = {a["watch"] for a in self.data["alerts"] if not a.get("seen")}
            items = [{**w, "new": w["id"] in unseen} for w in self.data["watchlist"].values()]
        items.sort(key=lambda w: (not w["new"], -w["added"]))
        return items

    def alerts(self, unseen_only: bool = True) -> list[dict]:
        with self.lock:
            return [dict(a) for a in self.data["alerts"] if not (unseen_only and a.get("seen"))][::-1]

    def mark_seen(self) -> None:
        with self.lock:
            changed = False
            for a in self.data["alerts"]:
                if not a.get("seen"):
                    a["seen"], changed = True, True
            if changed:
                self._save()

    # ----------------------------------------------------- background check
    def check_one(self, w: dict, search) -> None:
        """search(title, category) -> results (as JSON dicts). Records an alert when a copy is
        sharper than any seen before, or the first free copy appears."""
        res = search(w["title"], w["category"])
        strong = [r for r in res if (r.get("relevance") or 0) >= 0.9 and r.get("verdict") == "safe"]
        best = max(((r.get("quality") or {}).get("height") or 0 for r in strong), default=0)
        free = any("Internet Archive" in (r.get("sources") or []) for r in strong)
        msgs = []
        if best and Q.rank(best) > Q.rank(w.get("best") or 0) and w.get("checked"):
            msgs.append(f"A {Q.label(best)} copy of {w['title']} is now available")
        if free and not w.get("free") and w.get("checked"):
            msgs.append(f"{w['title']} is now free to watch")
        with self.lock:
            cur = self.data["watchlist"].get(w["id"])
            if not cur:
                return
            cur.update(best=max(best, cur.get("best") or 0), free=cur.get("free") or free, checked=time.time())
            for m in msgs:
                self.data["alerts"].append({"watch": w["id"], "title": w["title"], "text": m, "at": time.time(),
                                            "seen": False})
            self.data["alerts"] = self.data["alerts"][-MAX_ALERTS:]
            self._save()

    def check_all(self, search, due_only: bool = True) -> int:
        with self.lock:
            items = [dict(w) for w in self.data["watchlist"].values()]
        n = 0
        for w in items:
            if due_only and time.time() - (w.get("checked") or 0) < CHECK_EVERY:
                continue
            try:
                self.check_one(w, search)
                n += 1
            except Exception:
                continue
            time.sleep(2)            # be gentle with the sources
        return n

    def start_checker(self, search, first_delay: float = 120) -> None:
        def loop():
            time.sleep(first_delay)
            while True:
                self.check_all(search)
                time.sleep(15 * 60)
        threading.Thread(target=loop, name="watchlist", daemon=True).start()

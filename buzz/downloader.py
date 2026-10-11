"""BuzzTorrent's own torrent downloader, so nothing needs another app.

The work is done by aria2 (https://aria2.github.io), a long-standing open-source download
engine with full BitTorrent support (magnets, DHT, web seeds). BuzzTorrent starts it hidden,
talks to it over its JSON-RPC interface on 127.0.0.1 with a random secret, and stops it
when BuzzTorrent exits. Downloads go into the folder the download scanner watches, so every
finished download is virus-scanned and then shows up in "My PC" on the TV."""
from __future__ import annotations

import base64
import json
import os
import secrets
import shutil
import socket
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

from . import osutil

KEYS = ["gid", "status", "totalLength", "completedLength", "uploadLength", "downloadSpeed", "uploadSpeed",
        "connections", "numSeeders", "infoHash", "errorMessage", "dir", "bittorrent", "followedBy", "following",
        "seeder", "files"]


def find_aria2(configured: str | None = None) -> str | None:
    """aria2c.exe: the configured path, then PATH, then where winget puts it."""
    for cand in (configured, osutil.which("aria2c")):          # Homebrew's folders too, on a Mac
        if cand and Path(cand).is_file():
            return str(cand)
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages"
    if base.is_dir():
        for p in sorted(base.glob("aria2.aria2*/**/aria2c.exe"), reverse=True):
            return str(p)
    return None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class DownloaderError(ValueError):
    pass


class Downloader:
    def __init__(self, data_dir: Path, aria2: str | None, trackers: list[str], seed_ratio: float = 1.0,
                 seed_minutes: int = 30):
        self.data_dir = data_dir
        self.aria2 = aria2
        self.trackers = [t for t in trackers if t.startswith(("udp://", "http://", "https://"))]
        self.seed_ratio, self.seed_minutes = seed_ratio, seed_minutes
        self.lock = threading.RLock()
        self.proc: subprocess.Popen | None = None
        self.port = 0
        self.secret = ""
        self.folder: Path | None = None
        self.meta_file = data_dir / "downloads.json"
        try:
            self.meta: dict[str, dict] = json.loads(self.meta_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.meta = {}

    @property
    def available(self) -> bool:
        return bool(self.aria2)

    # ------------------------------------------------------------- process
    def ensure(self, folder: Path) -> None:
        """Starts aria2 (once) saving into folder; restarts it if the folder changed."""
        if not self.aria2:
            raise DownloaderError("The download engine (aria2) isn't installed")
        folder.mkdir(parents=True, exist_ok=True)
        with self.lock:
            if self.proc and self.proc.poll() is None and self.folder == folder:
                return
            self.stop()
            session = self.data_dir / "aria2.session"
            session.touch(exist_ok=True)
            self.port, self.secret, self.folder = _free_port(), secrets.token_hex(16), folder
            args = [self.aria2, "--enable-rpc", "--rpc-listen-all=false", f"--rpc-listen-port={self.port}",
                    f"--rpc-secret={self.secret}", f"--dir={folder}", f"--input-file={session}",
                    f"--save-session={session}", "--save-session-interval=20", "--continue=true",
                    "--max-concurrent-downloads=3", "--file-allocation=none", "--bt-save-metadata=false",
                    "--enable-dht=true", "--bt-enable-lpd=true", "--follow-torrent=mem",
                    f"--seed-ratio={self.seed_ratio}", f"--seed-time={self.seed_minutes}",
                    "--console-log-level=warn", "--summary-interval=0", f"--stop-with-process={os.getpid()}",
                    "--disk-cache=32M", "--auto-file-renaming=true", "--allow-overwrite=false"]
            if self.trackers:
                args.append("--bt-tracker=" + ",".join(self.trackers))
            self.proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                         stdin=subprocess.DEVNULL, cwd=str(self.data_dir),
                                         creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        for _ in range(50):                      # wait for the RPC port
            try:
                self.call("aria2.getVersion")
                return
            except DownloaderError:
                time.sleep(0.1)
        raise DownloaderError("The download engine didn't start")

    def stop(self) -> None:
        with self.lock:
            if self.proc and self.proc.poll() is None:
                try:
                    self.call("aria2.saveSession")
                    self.call("aria2.shutdown")
                    self.proc.wait(5)
                except Exception:
                    self.proc.kill()
            self.proc = None

    def running(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def call(self, method: str, *params):
        body = json.dumps({"jsonrpc": "2.0", "id": "buzz", "method": method,
                           "params": [f"token:{self.secret}", *params]}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{self.port}/jsonrpc", data=body,
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                msg = json.loads(e.read()).get("error", {}).get("message")
            except Exception:
                msg = None
            raise DownloaderError(msg or f"download engine error {e.code}") from e
        except (OSError, ValueError) as e:
            raise DownloaderError("download engine not reachable") from e
        if "error" in data:
            raise DownloaderError(data["error"].get("message", "download engine error"))
        return data.get("result")

    # ---------------------------------------------------------------- jobs
    def _remember(self, infohash: str, info: dict) -> None:
        with self.lock:
            self.meta[infohash.lower()] = {**self.meta.get(infohash.lower(), {}), **info}
            tmp = self.meta_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.meta, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, self.meta_file)

    def add(self, folder: Path, infohash: str, title: str, torrent: bytes | None = None,
            magnet: str | None = None, info: dict | None = None) -> str:
        """Starts a download from a verified .torrent (preferred: it carries web seeds) or a
        clean magnet. Returns aria2's id. Adding the same torrent twice is a no-op."""
        self.ensure(folder)
        for item in self.list(raw=True):
            if (item.get("infoHash") or "").lower() == infohash.lower() and item["status"] != "removed":
                return item["gid"]
        if torrent:
            gid = self.call("aria2.addTorrent", base64.b64encode(torrent).decode(), [], {})
        elif magnet:
            gid = self.call("aria2.addUri", [magnet], {})
        else:
            raise DownloaderError("nothing to download")
        self._remember(infohash, {"title": title, "added": time.time(), **(info or {})})
        return gid

    def list(self, raw: bool = False) -> list[dict]:
        if not self.running():
            return []
        items = []
        for method, args in (("aria2.tellActive", ()), ("aria2.tellWaiting", (0, 100)), ("aria2.tellStopped", (0, 100))):
            try:
                items += self.call(method, *args, KEYS) or []
            except DownloaderError:
                continue
        if raw:
            return items
        out = []
        for it in items:
            if it.get("followedBy"):            # a magnet's metadata step; the real download follows
                continue
            ih = (it.get("infoHash") or "").lower()
            meta = self.meta.get(ih, {})
            bt = it.get("bittorrent") or {}
            bt_name = (bt.get("info") or {}).get("name")
            if ih and bt_name and meta and meta.get("name") != bt_name:
                self._remember(ih, {"name": bt_name})        # lets the library show the real title
                meta = self.meta[ih]
            total, done = int(it.get("totalLength") or 0), int(it.get("completedLength") or 0)
            status = it["status"]
            if status == "active" and total and done >= total:
                status = "seeding"
            elif status == "active" and not total:
                status = "metadata"
            out.append({
                "gid": it["gid"], "infohash": ih, "status": status,
                "title": meta.get("title") or (bt.get("info") or {}).get("name") or "Download",
                "name": (bt.get("info") or {}).get("name") or "",
                "total": total, "done": done, "uploaded": int(it.get("uploadLength") or 0),
                "down": int(it.get("downloadSpeed") or 0), "up": int(it.get("uploadSpeed") or 0),
                "peers": int(it.get("connections") or 0), "seeders": int(it.get("numSeeders") or 0),
                "error": it.get("errorMessage") or "", "added": meta.get("added", 0),
                "eta": int((total - done) / int(it["downloadSpeed"])) if int(it.get("downloadSpeed") or 0) and total else None,
                "result": meta.get("result"), "verdict": meta.get("verdict"), "private": bool(meta.get("adult")),
            })
        out.sort(key=lambda d: (d["status"] not in ("active", "metadata", "waiting", "paused"), -d["added"]))
        return out

    def private_names(self) -> set[str]:
        """Folder/file names of downloads from the private 18+ section."""
        with self.lock:
            return {m["name"] for m in self.meta.values() if m.get("adult") and m.get("name")}

    def titles(self) -> dict[str, str]:
        """Downloaded folder/file name -> the title it was found under ("AboutBan1935" -> "About Bananas")."""
        with self.lock:
            return {m["name"]: m["title"] for m in self.meta.values() if m.get("name") and m.get("title")}

    def action(self, gid: str, what: str) -> None:
        if not gid.isalnum() or len(gid) > 32:
            raise DownloaderError("bad download id")
        if what == "pause":
            self.call("aria2.pause", gid)
        elif what == "resume":
            self.call("aria2.unpause", gid)
        elif what == "remove":          # keeps what was downloaded; just stops it
            try:
                self.call("aria2.remove", gid)
            except DownloaderError:
                pass
            try:
                self.call("aria2.removeDownloadResult", gid)
            except DownloaderError:
                pass
        else:
            raise DownloaderError("unknown action")

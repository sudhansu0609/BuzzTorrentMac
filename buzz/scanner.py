"""Download scanner: watches the torrent client's download folder and scans each
finished download with the antivirus engine plus content inspection.

Each top-level entry in the folder (a file or a torrent's folder) is one item.
An item counts as finished when it has no partial files (.!qB, .part…) and
nothing in it changed for SETTLE seconds. Torrent clients pre-allocate files,
so size alone can't tell. Items already in the folder when watching starts are
listed as "not scanned" with a Scan button rather than scanned all at once.
"""
from __future__ import annotations

import json
import re
import os
import queue
import shutil
import threading
import time
from pathlib import Path

from . import av
from .content_scan import inspect_item, is_partial, list_files

ARIA2_TORRENT = re.compile(r"[0-9a-f]{40}\.torrent")
SETTLE = 90          # seconds without changes before a download counts as finished
POLL = 15
MAX_REPORTS = 300
VT_POSITIVES = 3     # engines that must agree before a VirusTotal hit counts as a threat


def validate_folder(folder: str) -> Path:
    p = Path(os.path.expandvars(os.path.expanduser(folder.strip().strip('"')))).resolve()
    if not p.is_dir():
        raise ValueError(f"Folder not found: {p}")
    windir = Path(os.environ.get("WINDIR", r"C:\Windows")).resolve()
    if p == Path(p.anchor) or p == windir or windir in p.parents or p == Path.home():
        raise ValueError("Pick your torrent app's download folder, not a drive root, Windows or your whole user folder")
    return p


def suggest_folders() -> list[str]:
    """Where popular torrent apps save by default."""
    out = []
    ini = Path(os.environ.get("APPDATA", "")) / "qBittorrent" / "qBittorrent.ini"
    if ini.is_file():
        for line in ini.read_text(encoding="utf-8", errors="replace").splitlines():
            key, _, val = line.partition("=")
            if key.strip() in ("Session\\DefaultSavePath", "Downloads\\SavePath") and val.strip():
                out.append(val.strip().replace("/", "\\"))
    tr = Path(os.environ.get("LOCALAPPDATA", "")) / "transmission" / "settings.json"
    if tr.is_file():
        try:
            d = json.loads(tr.read_text(encoding="utf-8")).get("download-dir")
            if d:
                out.append(d)
        except ValueError:
            pass
    out.append(str(Path.home() / "Downloads"))
    return [p for p in dict.fromkeys(out) if Path(p).is_dir()]


class DownloadScanner:
    def __init__(self, data_dir: Path, quarantine_dir: Path):
        self.data_dir = data_dir
        self.quarantine_dir = quarantine_dir
        self.reports_file = data_dir / "scan_reports.json"
        self.engine = av.best_engine()
        self.folder: Path | None = None
        self.enabled = False
        self.auto_quarantine = True
        self.vt: av.VirusTotal | None = None
        self._lock = threading.RLock()
        self._queue: queue.Queue[str] = queue.Queue()
        self._queued: set[str] = set()
        self._first_seen: dict[str, float] = {}
        self.reports: dict[str, dict] = {}
        self.known_folders: set[str] = set()   # folders already baselined once
        self._load()
        threading.Thread(target=self._watch_loop, name="scanner-watch", daemon=True).start()
        threading.Thread(target=self._work_loop, name="scanner-work", daemon=True).start()

    # ---------------------------------------------------------- persistence
    def _load(self) -> None:
        try:
            data = json.loads(self.reports_file.read_text(encoding="utf-8"))
            self.reports = data.get("reports", {})
            self.known_folders = set(data.get("known_folders", []))
            for r in self.reports.values():  # scans interrupted by a restart
                if r["state"] in ("queued", "scanning"):
                    r["state"] = "waiting"
        except (OSError, ValueError):
            self.reports = {}

    def _save(self) -> None:
        # Watcher and worker threads both save; on Windows concurrent replace() calls
        # collide, so the whole write happens under the lock.
        with self._lock:
            items = sorted(self.reports.items(), key=lambda kv: kv[1].get("seen", 0))[-MAX_REPORTS:]
            self.reports = dict(items)
            payload = json.dumps({"reports": self.reports, "known_folders": sorted(self.known_folders)}, indent=1)
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self.reports_file.with_suffix(".tmp")
            tmp.write_text(payload, encoding="utf-8")
            for attempt in range(5):
                try:
                    os.replace(tmp, self.reports_file)
                    break
                except PermissionError:  # antivirus/indexer briefly holding the file
                    if attempt == 4:
                        raise
                    time.sleep(0.1)

    # -------------------------------------------------------------- config
    def configure(self, enabled: bool, folder: str | None, auto_quarantine: bool, vt_key: str) -> None:
        new_folder = validate_folder(folder) if folder else None
        if enabled and not new_folder:
            raise ValueError("Choose a download folder to watch")
        with self._lock:
            changed = new_folder != self.folder
            self.folder, self.enabled, self.auto_quarantine = new_folder, enabled and bool(new_folder), auto_quarantine
            if not vt_key:
                self.vt = None
            elif not self.vt or self.vt.api_key != vt_key:
                self.vt = av.VirusTotal(vt_key)
            if changed:
                self.reports = {k: v for k, v in self.reports.items() if v.get("folder") == str(new_folder)}
                if new_folder and str(new_folder) not in self.known_folders:
                    self._baseline()   # first time only; later arrivals get scanned automatically
                    self.known_folders.add(str(new_folder))
        self._save()

    def _baseline(self) -> None:
        """Mark what's already in the folder as 'not scanned' instead of scanning it all."""
        for entry in self._entries():
            if entry.name not in self.reports:
                self.reports[entry.name] = self._new_report(entry, "unscanned", self._signature(entry))

    # ------------------------------------------------------------- watching
    def _entries(self) -> list[Path]:
        try:
            # aria2 (the built-in downloader) keeps "<name>.aria2" progress files and "<info-hash>.torrent"
            # copies (so downloads resume after a restart) next to the downloads: those aren't downloads
            return [e for e in self.folder.iterdir() if e.name.lower() not in ("desktop.ini", "thumbs.db")
                    and not e.name.startswith(".") and not e.name.lower().endswith(".aria2")
                    and not ARIA2_TORRENT.fullmatch(e.name.lower())]
        except OSError:
            return []

    @staticmethod
    def _signature(entry: Path) -> dict:
        files = list_files(entry)
        latest, total, partial = 0.0, 0, False
        for f in files:
            try:
                st = f.stat()
            except OSError:
                continue
            latest, total = max(latest, st.st_mtime), total + st.st_size
            partial = partial or is_partial(f)
        # BuzzTorrent's downloader (aria2) keeps "<name>.aria2" next to a download until it finishes
        partial = partial or entry.with_name(entry.name + ".aria2").exists()
        return {"mtime": latest, "size": total, "count": len(files), "partial": partial}

    def _new_report(self, entry: Path, state: str, sig: dict) -> dict:
        return {"name": entry.name, "folder": str(self.folder), "state": state, "verdict": None,
                "size": sig["size"], "file_count": sig["count"], "sig": [sig["size"], sig["mtime"], sig["count"]],
                "seen": time.time(), "scanned_at": None, "engine": None, "findings": [],
                "virustotal": None, "signatures": [], "quarantined": [], "error": None}

    def _watch_loop(self) -> None:
        while True:
            try:
                if self.enabled and self.folder:
                    self._poll()
            except Exception as e:  # keep the watcher alive no matter what
                print("  scanner watch error:", repr(e))
            time.sleep(POLL)

    def _poll(self) -> None:
        now = time.time()
        present = set()
        dirty = False
        for entry in self._entries():
            present.add(entry.name)
            sig = self._signature(entry)
            key = [sig["size"], sig["mtime"], sig["count"]]
            with self._lock:
                rep = self.reports.get(entry.name)
                if rep and rep["state"] in ("queued", "scanning"):
                    continue
                if rep and rep["sig"] == key and rep["state"] not in ("waiting", "gone"):
                    continue  # unchanged since last scan / listing
                if rep and rep["state"] == "done" and sig["count"] < rep["file_count"] and \
                        (rep.get("quarantined") or (rep.get("engine") or {}).get("threats")):
                    # files vanished after a detection: the antivirus removed them, not a new download
                    rep["sig"], rep["size"], rep["file_count"] = key, sig["size"], sig["count"]
                    dirty = True
                    continue
                if rep is None or rep["state"] != "waiting":
                    history = rep.get("quarantined", []) if rep else []
                    rep = self._new_report(entry, "waiting", sig)
                    rep["quarantined"] = history
                    self.reports[entry.name] = rep
                    dirty = True
                rep["sig"], rep["size"], rep["file_count"] = key, sig["size"], sig["count"]
                settled = not sig["partial"] and sig["count"] > 0 and now - sig["mtime"] >= SETTLE
                if settled:
                    self._enqueue(entry.name)
                    dirty = True
        with self._lock:
            for name in [n for n in self.reports if ARIA2_TORRENT.fullmatch(n.lower()) or n.lower().endswith(".aria2")]:
                del self.reports[name]          # the downloader's own bookkeeping files, not downloads
                dirty = True
            for name, rep in self.reports.items():
                if name not in present and rep["state"] not in ("gone", "queued", "scanning") \
                        and rep.get("folder") == str(self.folder) and not rep.get("quarantined"):
                    rep["state"] = "gone"
                    dirty = True
        if dirty:
            self._save()

    def _enqueue(self, name: str) -> None:
        with self._lock:
            if name in self._queued:
                return
            self._queued.add(name)
            if name in self.reports:
                self.reports[name]["state"] = "queued"
        self._queue.put(name)

    def request_scan(self, name: str) -> None:
        if not self.folder or not (self.folder / name).exists() or "/" in name or "\\" in name:
            raise ValueError("That download is no longer in the folder")
        with self._lock:
            entry = self.folder / name
            if name not in self.reports:
                self.reports[name] = self._new_report(entry, "queued", self._signature(entry))
        self._enqueue(name)
        self._save()

    # ------------------------------------------------------------- scanning
    def _work_loop(self) -> None:
        while True:
            name = self._queue.get()
            try:
                self._scan(name)
            except Exception as e:
                with self._lock:
                    if name in self.reports:
                        self.reports[name].update(state="error", error=f"scan failed: {e!r}"[:300])
            finally:
                with self._lock:
                    self._queued.discard(name)
                self._save()

    def _scan(self, name: str) -> None:
        folder = self.folder
        entry = folder / name if folder else None
        if not entry or not entry.exists():
            with self._lock:
                if name in self.reports:
                    self.reports[name]["state"] = "gone"
            return
        with self._lock:
            rep = self.reports[name]
            # quarantine history survives a rescan; everything else is recomputed
            rep.update(state="scanning", error=None, findings=[], virustotal=None, signatures=[])
            rep.setdefault("quarantined", [])
        self._save()

        # 1. content inspection (real file types, archives, PDFs)
        info = inspect_item(entry, folder)
        findings = info["findings"]

        # 2. antivirus engine
        if self.engine:
            result = self.engine.scan(entry)
            for t in result["threats"]:
                t["file"] = self._rel(t["file"], folder) or name
        else:
            result = {"engine": None, "ok": False, "threats": [], "error": "No antivirus engine found on this PC"}

        # 3. publisher signatures for programs
        sigs = []
        for path, s in av.signatures(info["programs"]).items():
            rel = self._rel(path, folder)
            sigs.append({"file": rel, **s})
            if s["status"] == "HashMismatch":
                findings.append({"file": rel, "level": "danger",
                                 "msg": "Digital signature is broken: the program was modified after it was signed"})
            elif s["status"] == "NotTrusted":
                findings.append({"file": rel, "level": "warn", "msg": "Signed with an untrusted certificate"})
        unsigned = [s for s in sigs if s["status"] == "NotSigned"]
        if unsigned:
            findings.append({"file": unsigned[0]["file"], "level": "info",
                             "msg": f"{len(unsigned)} program(s) have no publisher signature. That's normal for many "
                                    "games and mods, but nobody vouches for them"})

        # 4. optional VirusTotal hash reputation
        vt_report = None
        if self.vt and info["risky"]:
            vt_report = {"checked": 0, "unknown": 0, "hits": [], "error": None}
            for p in info["risky"][:20]:
                try:
                    if p.stat().st_size > av.VirusTotal.MAX_FILE:
                        continue
                    res = self.vt.lookup(av.sha256(p))
                except PermissionError as e:
                    vt_report["error"] = str(e)
                    break
                except OSError:
                    continue
                if "error" in res:
                    vt_report["error"] = res["error"]
                    continue
                vt_report["checked"] += 1
                if not res["found"]:
                    vt_report["unknown"] += 1
                elif res["malicious"] + res["suspicious"] > 0:
                    vt_report["hits"].append({"file": self._rel(str(p), folder), **res})

        # 5. verdict
        threats = [{"engine": result["engine"], **t} for t in result["threats"]]
        if vt_report:
            threats += [{"engine": "VirusTotal", "name": f"{h['malicious']}/{h['total']} engines flag it",
                         "file": h["file"]} for h in vt_report["hits"] if h["malicious"] >= VT_POSITIVES]
            for h in vt_report["hits"]:
                if h["malicious"] < VT_POSITIVES:
                    findings.append({"file": h["file"], "level": "warn",
                                     "msg": f"VirusTotal: {h['malicious'] + h['suspicious']}/{h['total']} engines flag it"})
        levels = {f["level"] for f in findings}
        if threats or "danger" in levels:
            verdict = "dangerous"
        elif "warn" in levels:
            verdict = "suspicious"
        elif not result["ok"] and not vt_report:
            verdict = "unverified"
        else:
            verdict = "clean"

        with self._lock:
            rep.update(state="done", verdict=verdict, scanned_at=time.time(), findings=findings,
                       engine={"name": result["engine"], "ok": result["ok"], "error": result["error"],
                               "threats": threats},
                       virustotal=vt_report, signatures=sigs[:50], size=info["size"], file_count=len(info["files"]))
        if threats and self.auto_quarantine:
            self.quarantine(name, auto=True)

    @staticmethod
    def _rel(path: str, folder: Path) -> str:
        try:
            return str(Path(path).resolve().relative_to(folder)).replace("\\", "/")
        except (ValueError, OSError):
            return str(path)

    # ----------------------------------------------------------- quarantine
    def quarantine(self, name: str, auto: bool = False) -> list[dict]:
        """Neutralise flagged files. Antivirus-confirmed threats go to Defender's own
        quarantine (restorable from Windows Security). Files flagged by content checks
        are moved into BuzzTorrent's quarantine folder and renamed *.blocked so they
        can't be opened by accident."""
        folder = self.folder
        with self._lock:
            rep = self.reports.get(name)
            if not rep or not folder or rep.get("state") != "done":
                raise ValueError("Nothing to quarantine for that download")
            av_files = {t["file"] for t in rep["engine"]["threats"] if t.get("file")} if rep.get("engine") else set()
            heur_files = {f["file"] for f in rep["findings"] if f["level"] == "danger"}
        done = []
        if av_files and isinstance(self.engine, av.Defender):
            for rel in sorted(av_files):
                target = self._inside(folder, rel)
                if target and target.exists():
                    self.engine.scan(target, remediate=True)
                    outcome = av.neutralised(target)
                    done.append({"file": rel, "by": "Microsoft Defender", "ok": bool(outcome),
                                 "outcome": outcome or "still present"})
        stamp = time.strftime("%Y%m%d-%H%M%S")
        for rel in sorted((heur_files | av_files) - {d["file"] for d in done if d["ok"]}):
            src = self._inside(folder, rel)
            if not src or not src.is_file():
                continue
            dest = self.quarantine_dir / stamp / (rel + ".blocked")
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dest))
                (dest.parent / "_restore_info.txt").open("a", encoding="utf-8").write(f"{dest.name}  <-  {src}\n")
                done.append({"file": rel, "by": "BuzzTorrent", "ok": True, "to": str(dest)})
            except OSError as e:
                done.append({"file": rel, "by": "BuzzTorrent", "ok": False, "error": str(e)})
        with self._lock:
            rep["quarantined"] = rep.get("quarantined", []) + [{**d, "auto": auto, "at": time.time()} for d in done]
            entry = folder / name
            if entry.exists():  # remember the post-quarantine state so it isn't rescanned as "new"
                sig = self._signature(entry)
                rep["sig"] = [sig["size"], sig["mtime"], sig["count"]]
        self._save()
        return done

    @staticmethod
    def _inside(folder: Path, rel: str) -> Path | None:
        """Resolve rel under folder, refusing anything that escapes it."""
        try:
            p = (folder / rel).resolve()
            p.relative_to(folder)
            return p
        except (ValueError, OSError):
            return None

    def forget(self, name: str) -> None:
        with self._lock:
            self.reports.pop(name, None)
        self._save()

    # --------------------------------------------------------------- status
    def status(self) -> dict:
        engine = None
        if self.engine:
            engine = {"name": self.engine.name, **self.engine.status()}
        with self._lock:
            items = sorted((r for r in self.reports.values() if r.get("folder") == str(self.folder)),
                           key=lambda r: r.get("scanned_at") or r.get("seen") or 0, reverse=True)
            items = [{k: v for k, v in r.items() if k != "sig"} for r in items]
        counts = {"dangerous": 0, "suspicious": 0}
        for r in items:
            if r["state"] == "done" and r["verdict"] in counts and not _all_quarantined(r):
                counts[r["verdict"]] += 1
        return {
            "enabled": self.enabled, "folder": str(self.folder) if self.folder else None,
            "auto_quarantine": self.auto_quarantine, "virustotal": bool(self.vt), "engine": engine,
            "settle_seconds": SETTLE, "items": items, "counts": counts, "suggestions": suggest_folders(),
            "quarantine_dir": str(self.quarantine_dir),
        }


def _all_quarantined(r: dict) -> bool:
    flagged = {t["file"] for t in (r.get("engine") or {}).get("threats", [])} | \
              {f["file"] for f in r.get("findings", []) if f["level"] == "danger"}
    fixed = {q["file"] for q in r.get("quarantined", []) if q.get("ok")}
    return bool(flagged) and flagged <= fixed

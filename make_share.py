"""Builds a clean copy of BuzzTorrent to give to friends:  python make_share.py

Writes dist/BuzzTorrent-<date>.zip with the app, the TV app (APK) and the instructions, and
none of your own things: no config.json (your Jackett key, TV PIN, private-section PIN), no
data folder (history, Continue watching, logs, download list), no quarantine, no downloads,
and not the key that signs your TV app. Afterwards it scans the zip for anything private
and refuses to finish if it finds some.

Each friend gets their own BuzzTorrent on their own PC, with their own TV pairing and
their own choice of Jackett sites."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist"

INCLUDE = ["app.py", "launcher.pyw", "Start BuzzTorrent.bat", "Stop BuzzTorrent.bat", "Start BuzzTorrent.command",
           "Stop BuzzTorrent.command", "Setup BuzzTorrent (Mac).command", "README.md", "config.example.json",
           ".gitignore"]
EXECUTABLE = {"Start BuzzTorrent.command", "Stop BuzzTorrent.command", "Setup BuzzTorrent (Mac).command"}      # double-clickable on a Mac
INCLUDE_DIRS = ["buzz", "static", "tests", "android/src", "android/res", "android/assets"]
INCLUDE_ANDROID = ["android/AndroidManifest.xml", "android/build_apk.py", "android/make_icons.py",
                   "android/version.json", "android/build/BuzzTorrentTV.apk"]
SKIP_PARTS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIX = {".pyc", ".pyo", ".tmp", ".log", ".keystore", ".jks"}

FRIENDS_TXT = """BuzzTorrent: how to start
==========================

WINDOWS
1. Unzip this folder somewhere permanent (for example Documents\\BuzzTorrent).
2. Double-click "Start BuzzTorrent" (the .bat). If Windows says "Windows protected your PC":
   More info > Run anyway.
   - No Python yet? It offers to install it (say Y), then run it again.
   - It offers to install aria2, the download engine (say Y): BuzzTorrent then downloads torrents itself.
3. BuzzTorrent opens in its own window and puts a shortcut on your Desktop.

MAC
1. Unzip, and move the BuzzTorrent folder somewhere permanent (for example your Documents folder).
2. Right-click "Setup BuzzTorrent (Mac).command" > Open > Open (only the first time: macOS asks
   before running downloaded scripts). It installs, asking first each time: Homebrew, Python, aria2
   (downloads), ClamAV (virus scan), and optionally VLC and Jackett, then starts BuzzTorrent.
3. BuzzTorrent opens (as its own window in Chrome/Edge if you have one, else in Safari) and adds a
   BuzzTorrent app to your Applications folder: use that from then on.

More sources (optional): install Jackett (https://github.com/Jackett/Jackett), add the sites you choose,
then BuzzTorrent > Settings > Connect Jackett / Prowlarr. Which sites you add, and what you download,
is up to you: downloading copyrighted films can be illegal where you live.

Google TV (optional): BuzzTorrent > Settings > Living-room TV > turn on TV access, then install
android\\build\\BuzzTorrentTV.apk on the TV (steps are shown in that settings section) and pair it
with the PIN shown on your PC.

Every download is virus-scanned once the Download scanner is on (the first download turns it on).
"""


def private_values() -> list[str]:
    """Things from this PC that must never end up in a shared copy."""
    vals = set()
    try:
        cfg = json.loads((ROOT / "config.json").read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        cfg = {}
    for src in cfg.get("torznab", []):
        if len(src.get("apikey", "")) >= 8:
            vals.add(src["apikey"])
    tvc, adult = cfg.get("tv", {}), cfg.get("adult", {})
    for v in (tvc.get("pin"), tvc.get("secret"), adult.get("pin_hash"), adult.get("pin_salt"),
              (cfg.get("scanner") or {}).get("virustotal_key")):
        if v and len(str(v)) >= 6:
            vals.add(str(v))
    vals.update(t for t in tvc.get("tokens", []) if t)
    user = os.environ.get("USERNAME", "")
    if len(user) >= 3:
        vals.add(f"\\Users\\{user}")
        vals.add(f"/Users/{user}")
    return sorted(vals)


def files() -> list[Path]:
    out = [ROOT / f for f in INCLUDE + INCLUDE_ANDROID if (ROOT / f).is_file()]
    for d in INCLUDE_DIRS:
        for p in sorted((ROOT / d).rglob("*")):
            if p.is_file() and not (set(p.relative_to(ROOT).parts) & SKIP_PARTS) and p.suffix.lower() not in SKIP_SUFFIX:
                out.append(p)
    return out


def main() -> int:
    if not (ROOT / "android/build/BuzzTorrentTV.apk").is_file():
        print("Note: the TV app isn't built (python android/build_apk.py); the copy won't include it.")
    DIST.mkdir(exist_ok=True)
    zpath = DIST / f"BuzzTorrent-{time.strftime('%Y-%m-%d')}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in files():
            arc = "BuzzTorrent/" + p.relative_to(ROOT).as_posix()
            if p.name in EXECUTABLE:
                info = zipfile.ZipInfo(arc, time.localtime(p.stat().st_mtime)[:6])
                info.external_attr = 0o100755 << 16           # regular file, rwxr-xr-x (kept by macOS unzip)
                info.create_system = 3                        # "made on Unix", so the mode is honoured
                info.compress_type = zipfile.ZIP_DEFLATED
                z.writestr(info, p.read_bytes())
            else:
                z.write(p, arc)
        z.writestr("BuzzTorrent/START HERE.txt", FRIENDS_TXT)

    # Check the result: no private files, no private values inside any file
    secrets_ = private_values()
    problems = []
    with zipfile.ZipFile(zpath) as z:
        names = z.namelist()
        for n in names:
            low = n.lower()
            if low.endswith(("config.json", ".keystore")) or "/data/" in low or "/quarantine/" in low:
                problems.append(f"private file included: {n}")
            data = z.read(n)
            for s in secrets_:
                if s.encode() in data:
                    problems.append(f"{n} contains private information ({s[:3]}…)")
    if problems:
        zpath.unlink()
        print("NOT created. Private information found:\n  " + "\n  ".join(problems))
        return 1
    size = zpath.stat().st_size / 1024 / 1024
    print(f"OK: {zpath}  ({len(names)} files, {size:.1f} MB). Checked: no config, history, keys or PINs inside.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

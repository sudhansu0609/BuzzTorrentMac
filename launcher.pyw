"""BuzzTorrent desktop launcher.

Runs with pythonw on Windows / python3 on macOS (no console window): starts the server in
the background, opens BuzzTorrent in its own app window, and quits when that window is
closed. Started by the BuzzTorrent shortcut / app, "Start BuzzTorrent.bat" or
"Start BuzzTorrent.command".
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
os.chdir(ROOT)
sys.path.insert(0, str(ROOT))

try:
    import app
    sys.exit(app.main(["--app", *sys.argv[1:]]))   # e.g. --no-window for the TV server
except SystemExit:
    raise
except BaseException as e:  # no console to print to: show the problem instead of failing silently
    import traceback
    (ROOT / "data").mkdir(exist_ok=True)
    (ROOT / "data" / "launcher-error.log").write_text(traceback.format_exc(), encoding="utf-8")
    try:
        from buzz.osutil import message_box
        message_box(f"BuzzTorrent couldn't start:\n\n{e}\n\nDetails: data/launcher-error.log")
    except Exception:
        pass
    sys.exit(1)

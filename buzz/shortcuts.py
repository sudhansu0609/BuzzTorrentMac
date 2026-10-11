"""Shortcuts that start BuzzTorrent with no console window: Desktop / Start Menu
shortcuts on Windows, a BuzzTorrent app in ~/Applications on macOS. Plus "start with the
computer" (Startup folder on Windows, a login item on macOS) for the TV server."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .icon import ensure


def pythonw() -> str:
    exe = Path(sys.executable)
    cand = exe.with_name("pythonw.exe")
    return str(cand if cand.exists() else exe)


def _ps_quote(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


MAC_APP = Path.home() / "Applications" / "BuzzTorrent.app"
MAC_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.buzzcaf.buzztorrent.tv.plist"


def _plist(entries: dict) -> str:
    """A tiny property-list writer (stdlib plistlib would do too; this keeps the output readable)."""
    import plistlib
    return plistlib.dumps(entries).decode()


def install_mac(root: Path) -> list[str]:
    """~/Applications/BuzzTorrent.app: double-click it (or find it in Spotlight / Launchpad)."""
    contents = MAC_APP / "Contents"
    (contents / "MacOS").mkdir(parents=True, exist_ok=True)
    (contents / "Resources").mkdir(parents=True, exist_ok=True)
    (contents / "Info.plist").write_text(_plist({
        "CFBundleName": "BuzzTorrent", "CFBundleDisplayName": "BuzzTorrent",
        "CFBundleIdentifier": "com.buzzcaf.buzztorrent", "CFBundleVersion": "1.0",
        "CFBundleShortVersionString": "1.0", "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "BuzzTorrent", "CFBundleIconFile": "buzztorrent",
        "LSMinimumSystemVersion": "11.0", "NSHighResolutionCapable": True,
    }), encoding="utf-8")
    exe = contents / "MacOS" / "BuzzTorrent"
    exe.write_text("#!/bin/bash\n"
                   "# Starts BuzzTorrent (made by BuzzTorrent's installer; safe to delete with the app)\n"
                   f'cd "{root}" || exit 1\n'
                   f'exec "{sys.executable}" "{root / "launcher.pyw"}" "$@"\n', encoding="utf-8")
    exe.chmod(0o755)
    icns = root / "static" / "buzztorrent.icns"
    if icns.is_file():
        (contents / "Resources" / "buzztorrent.icns").write_bytes(icns.read_bytes())
    return [str(MAC_APP)]


def install(root: Path) -> list[str]:
    if sys.platform == "darwin":
        return install_mac(root)
    if os.name != "nt":
        raise SystemExit("Shortcuts are created on Windows and macOS only")
    icon = ensure(root / "static")
    launcher = root / "launcher.pyw"
    targets = [
        Path(os.environ["USERPROFILE"]) / "Desktop" / "BuzzTorrent.lnk",
        Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "BuzzTorrent.lnk",
    ]
    # The Desktop may be redirected (e.g. OneDrive): ask the shell where it really is.
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        "$d = [Environment]::GetFolderPath('Desktop');"
        f"$targets = @((Join-Path $d 'BuzzTorrent.lnk'), {_ps_quote(str(targets[1]))});"
        "foreach ($t in $targets) {"
        " $l = $s.CreateShortcut($t);"
        f" $l.TargetPath = {_ps_quote(pythonw())};"
        f" $l.Arguments = {_ps_quote(chr(34) + str(launcher) + chr(34))};"
        f" $l.WorkingDirectory = {_ps_quote(str(root))};"
        f" $l.IconLocation = {_ps_quote(str(icon) + ',0')};"
        " $l.Description = 'BuzzTorrent - safe torrent search';"
        " $l.Save(); Write-Output $t }"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        raise SystemExit(f"Could not create shortcuts: {r.stderr.strip()}")
    return [line for line in r.stdout.splitlines() if line.strip()]


def set_autostart(root: Path, on: bool) -> None:
    """A Startup-folder shortcut that runs the server (no window) at sign-in, so the TV
    can reach BuzzTorrent whenever this PC is on."""
    if sys.platform == "darwin":
        _autostart_mac(root, on)
        return
    if os.name != "nt":
        return
    link = Path(os.environ["APPDATA"]) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / \
        "BuzzTorrent TV server.lnk"
    if not on:
        link.unlink(missing_ok=True)
        return
    icon = ensure(root / "static")
    script = (
        "$s = New-Object -ComObject WScript.Shell;"
        f"$l = $s.CreateShortcut({_ps_quote(str(link))});"
        f"$l.TargetPath = {_ps_quote(pythonw())};"
        f"$l.Arguments = {_ps_quote(chr(34) + str(root / 'launcher.pyw') + chr(34) + ' --no-window')};"
        f"$l.WorkingDirectory = {_ps_quote(str(root))};"
        f"$l.IconLocation = {_ps_quote(str(icon) + ',0')};"
        "$l.Description = 'BuzzTorrent for the living-room TV (runs in the background)';"
        "$l.Save()"
    )
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                       capture_output=True, text=True, timeout=60,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode:
        raise ValueError(f"Could not create the start-up shortcut: {r.stderr.strip()[:200]}")


def _autostart_mac(root: Path, on: bool) -> None:
    """A login item (LaunchAgent) that runs the server with no window when you log in."""
    if not on:
        MAC_AGENT.unlink(missing_ok=True)
        return
    MAC_AGENT.parent.mkdir(parents=True, exist_ok=True)
    MAC_AGENT.write_text(_plist({
        "Label": "com.buzzcaf.buzztorrent.tv",
        "ProgramArguments": [sys.executable, str(root / "launcher.pyw"), "--no-window"],
        "WorkingDirectory": str(root), "RunAtLoad": True, "ProcessType": "Background",
    }), encoding="utf-8")

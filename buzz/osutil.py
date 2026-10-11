"""The few things that differ between Windows and macOS (and Linux), in one place:
opening files and folders, the folder picker, message boxes, app-style browser windows,
finding helper programs, and starting a background copy of BuzzTorrent."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

IS_WIN = os.name == "nt"
IS_MAC = sys.platform == "darwin"
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# Apps started from Finder don't see Homebrew's folders on PATH
EXTRA_BIN = ["/opt/homebrew/bin", "/usr/local/bin", "/opt/local/bin"] if not IS_WIN else []


def which(name: str) -> str | None:
    found = shutil.which(name)
    if found:
        return found
    for d in EXTRA_BIN:
        p = Path(d) / name
        if p.is_file() and os.access(p, os.X_OK):
            return str(p)
    return None


def open_path(target: str) -> None:
    """Opens a file, folder or link with the app the system uses for it."""
    if IS_WIN:
        os.startfile(target)  # noqa: S606 - callers pass vetted paths/links only
    elif IS_MAC:
        subprocess.Popen(["open", target])
    else:
        subprocess.Popen(["xdg-open", target])


def reveal(path: Path) -> None:
    """Shows a file selected in Explorer / Finder."""
    if IS_WIN:
        subprocess.Popen(["explorer", "/select,", str(path)] if path.is_file() else ["explorer", str(path)])
    elif IS_MAC:
        subprocess.Popen(["open", "-R", str(path)] if path.is_file() else ["open", str(path)])
    else:
        subprocess.Popen(["xdg-open", str(path if path.is_dir() else path.parent)])


def choose_folder(prompt: str) -> str | None:
    """The system's own folder picker (on this computer, where the app window is)."""
    try:
        if IS_WIN:
            script = (
                "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
                "Add-Type -AssemblyName System.Windows.Forms;"
                "$o=New-Object System.Windows.Forms.Form -Property @{TopMost=$true;ShowInTaskbar=$false};"
                "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
                f"$d.Description='{prompt.replace(chr(39), '')}';"
                "$d.ShowNewFolderButton=$false;"
                "if($d.ShowDialog($o) -eq 'OK'){$d.SelectedPath}")
            r = subprocess.run(["powershell", "-NoProfile", "-STA", "-Command", script], capture_output=True,
                               text=True, encoding="utf-8", errors="replace", timeout=600, creationflags=NO_WINDOW)
        elif IS_MAC:
            safe = prompt.replace('"', "'")
            r = subprocess.run(["osascript", "-e", f'POSIX path of (choose folder with prompt "{safe}")'],
                               capture_output=True, text=True, timeout=600)
        else:
            raise ValueError("Type the folder path instead")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout.strip() or None


def message_box(text: str, title: str = "BuzzTorrent") -> None:
    if IS_WIN:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, 0x10)
    elif IS_MAC:
        safe = text.replace("\\", "\\\\").replace('"', "'")
        subprocess.run(["osascript", "-e", f'display alert "{title}" message "{safe}" as critical'], timeout=600)
    else:
        print(text, file=sys.stderr)


def app_browser() -> str | None:
    """A Chromium browser that can show a page as its own app window (Edge, Chrome, Brave)."""
    env = os.environ
    if IS_WIN:
        cands = [
            Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft/Edge/Application/msedge.exe",
            Path(env.get("ProgramFiles", r"C:\Program Files")) / "Microsoft/Edge/Application/msedge.exe",
            Path(env.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(env.get("ProgramFiles", r"C:\Program Files")) / "Google/Chrome/Application/chrome.exe",
            Path(env.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Google/Chrome/Application/chrome.exe",
            Path(env.get("LOCALAPPDATA", "")) / "Google/Chrome/Application/chrome.exe",
        ]
    elif IS_MAC:
        apps = [("Google Chrome", "Google Chrome"), ("Microsoft Edge", "Microsoft Edge"),
                ("Brave Browser", "Brave Browser"), ("Chromium", "Chromium")]
        cands = [Path(base) / f"{app}.app/Contents/MacOS/{exe}"
                 for base in ("/Applications", str(Path.home() / "Applications")) for app, exe in apps]
    else:
        cands = [Path(p) for p in (which("google-chrome"), which("chromium"), which("microsoft-edge")) if p]
    return next((str(c) for c in cands if c.is_file()), None)


def vlc() -> str | None:
    if IS_WIN:
        for base in (os.environ.get("ProgramFiles"), os.environ.get("ProgramFiles(x86)"), os.environ.get("LOCALAPPDATA")):
            if base and (Path(base) / "VideoLAN/VLC/vlc.exe").is_file():
                return str(Path(base) / "VideoLAN/VLC/vlc.exe")
    elif IS_MAC:
        for base in ("/Applications", str(Path.home() / "Applications")):
            p = Path(base) / "VLC.app/Contents/MacOS/VLC"
            if p.is_file():
                return str(p)
    return which("vlc")


def background_python() -> str:
    """The Python to run BuzzTorrent with no console window (pythonw on Windows)."""
    exe = Path(sys.executable)
    if IS_WIN and exe.with_name("pythonw.exe").exists():
        return str(exe.with_name("pythonw.exe"))
    return str(exe)


def detached() -> dict:
    """Popen options for a process that outlives this one."""
    if IS_WIN:
        return {"creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | NO_WINDOW,
                "close_fds": True}
    return {"start_new_session": True, "close_fds": True,
            "stdin": subprocess.DEVNULL, "stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}

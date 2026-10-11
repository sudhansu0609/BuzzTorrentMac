"""Real antivirus scanning of downloaded files.

* Microsoft Defender (built into Windows) via MpCmdRun.exe: a full signature +
  heuristic engine scan of the downloaded files, archives included.
* ClamAV (clamscan / clamdscan) if installed, used when Defender isn't available.
* VirusTotal, optional and off by default: only SHA-256 *hashes* of risky files
  (programs, scripts, archives, documents) are looked up. Files are never uploaded.
* Authenticode signature checks for programs (who published it, was it tampered).
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

from . import osutil

from .safehttp import FetchError, FetchPolicy, fetch

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SCAN_TIMEOUT = 60 * 60


def _run(args: list[str], timeout: int) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=timeout, creationflags=NO_WINDOW)


class Defender:
    name = "Microsoft Defender"

    def __init__(self):
        self.exe = self.find()
        self._status: tuple[float, dict] | None = None

    @staticmethod
    def find() -> str | None:
        if os.name != "nt":
            return None
        platform = sorted(glob.glob(r"C:\ProgramData\Microsoft\Windows Defender\Platform\*\MpCmdRun.exe"),
                          key=os.path.getmtime, reverse=True)
        legacy = os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"), "Windows Defender", "MpCmdRun.exe")
        for p in platform + [legacy]:
            if os.path.isfile(p):
                return p
        return None

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def status(self) -> dict:
        """Engine health (cached 10 min): enabled, real-time protection, signature age."""
        if self._status and time.time() - self._status[0] < 600:
            return self._status[1]
        info = {"available": self.available}
        if self.available:
            try:
                r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command",
                          "Get-MpComputerStatus | Select-Object AntivirusEnabled,RealTimeProtectionEnabled,"
                          "AntivirusSignatureLastUpdated | ConvertTo-Json -Compress"], 30)
                d = json.loads(r.stdout or "{}")
                sig = str(d.get("AntivirusSignatureLastUpdated") or "")
                if sig.startswith("/Date("):
                    sig = time.strftime("%Y-%m-%d", time.localtime(int(sig[6:-2].split("+")[0].split("-")[0]) / 1000))
                info.update(enabled=bool(d.get("AntivirusEnabled")), realtime=bool(d.get("RealTimeProtectionEnabled")),
                            signatures=sig or None)
            except (OSError, ValueError, subprocess.SubprocessError):
                info.update(enabled=None)
        self._status = (time.time(), info)
        return info

    def scan(self, path: Path, remediate: bool = False) -> dict:
        """Scan a file or folder. With remediate=False Defender only reports."""
        args = [self.exe, "-Scan", "-ScanType", "3", "-File", str(path)]
        if not remediate:
            args.append("-DisableRemediation")
        try:
            r = _run(args, SCAN_TIMEOUT)
        except subprocess.TimeoutExpired:
            return {"engine": self.name, "ok": False, "threats": [], "error": "scan timed out"}
        except OSError as e:
            return {"engine": self.name, "ok": False, "threats": [], "error": str(e)}
        if r.returncode == 0:
            return {"engine": self.name, "ok": True, "threats": [], "error": None}
        if r.returncode == 2:
            return {"engine": self.name, "ok": True, "threats": self.parse_threats(r.stdout), "error": None}
        tail = (r.stdout + r.stderr).strip().splitlines()[-2:]
        return {"engine": self.name, "ok": False, "threats": [],
                "error": f"Defender error {r.returncode:#x}: {' '.join(tail)}"[:300]}

    @staticmethod
    def parse_threats(text: str) -> list[dict]:
        threats, current = [], None
        for line in text.splitlines():
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "threat":
                current = value
            elif current and key in ("file", "containerfile"):
                threats.append({"name": current, "file": value.split("->")[0]})
        if current and not threats:
            threats.append({"name": current, "file": ""})
        return threats


class ClamAV:
    name = "ClamAV"

    def __init__(self):
        self.exe = osutil.which("clamdscan") or osutil.which("clamscan")     # incl. Homebrew on a Mac

    @property
    def available(self) -> bool:
        return bool(self.exe)

    def status(self) -> dict:
        return {"available": self.available, "enabled": self.available}

    def scan(self, path: Path, remediate: bool = False) -> dict:
        try:
            r = _run([self.exe, "--no-summary", "--infected", "-r", str(path)], SCAN_TIMEOUT)
        except (OSError, subprocess.SubprocessError) as e:
            return {"engine": self.name, "ok": False, "threats": [], "error": str(e)}
        if r.returncode not in (0, 1):
            return {"engine": self.name, "ok": False, "threats": [], "error": (r.stderr or r.stdout).strip()[:300]}
        threats = []
        for line in r.stdout.splitlines():
            if line.endswith(" FOUND"):
                file, _, name = line[:-6].rpartition(": ")
                threats.append({"name": name, "file": file})
        return {"engine": self.name, "ok": True, "threats": threats, "error": None}


def _open_error(path: Path) -> int:
    """Win32 error code from opening the file for reading (0 = opened fine).
    Python's open() hides the code behind a generic errno, so call CreateFileW."""
    if os.name != "nt":
        return 0
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateFileW.restype = wintypes.HANDLE
    k32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    h = k32.CreateFileW(str(path), 0x80000000, 0x7, None, 3, 0x80, None)  # GENERIC_READ, share all, OPEN_EXISTING
    if h in (None, wintypes.HANDLE(-1).value):
        return ctypes.get_last_error()
    k32.CloseHandle(wintypes.HANDLE(h))
    return 0


def neutralised(path: Path, wait: float = 15.0) -> str | None:
    """After a Defender remediation: 'removed' if the file is gone, 'blocked' if
    Windows now refuses all access to it (Defender deletes it shortly after)."""
    end = time.monotonic() + wait
    while True:
        if not path.exists():
            return "removed"
        if _open_error(path) == 225:  # ERROR_VIRUS_INFECTED: Windows refuses all access
            return "blocked"
        if time.monotonic() >= end:
            return None
        time.sleep(0.5)


def best_engine():
    for engine in (Defender(), ClamAV()):
        if engine.available:
            return engine
    return None


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class VirusTotal:
    """Hash-only reputation lookup. Free API keys allow 4 lookups per minute."""
    POLICY = FetchPolicy(("www.virustotal.com",), content_types=("json",), timeout=20)
    MIN_INTERVAL = 15.5
    MAX_FILE = 650 * 1024 * 1024

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._last = 0.0
        self._lock = threading.Lock()
        self.cache: dict[str, dict] = {}

    def lookup(self, digest: str) -> dict:
        if digest in self.cache:
            return self.cache[digest]
        with self._lock:
            wait = self.MIN_INTERVAL - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
        try:
            body = fetch(f"https://www.virustotal.com/api/v3/files/{digest}", self.POLICY,
                         accept="application/json", headers={"x-apikey": self.api_key})
            stats = json.loads(body)["data"]["attributes"]["last_analysis_stats"]
            res = {"found": True, "malicious": int(stats.get("malicious", 0)),
                   "suspicious": int(stats.get("suspicious", 0)),
                   "total": sum(int(v) for v in stats.values() if isinstance(v, int))}
        except FetchError as e:
            if e.status == 404:
                res = {"found": False}
            elif e.status in (401, 403):
                raise PermissionError("VirusTotal rejected the API key") from e
            else:
                return {"error": str(e)}
        except (ValueError, KeyError, TypeError):
            return {"error": "unexpected VirusTotal response"}
        self.cache[digest] = res
        return res


def signatures(paths: list[Path]) -> dict[str, dict]:
    """Authenticode status for Windows programs: Valid / NotSigned / HashMismatch / NotTrusted…"""
    if os.name != "nt" or not paths:
        return {}
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="utf-8") as f:
        f.write("\n".join(str(p) for p in paths))
        listing = f.name
    script = (
        "$ErrorActionPreference='SilentlyContinue';"
        f"Get-Content -LiteralPath '{listing}' -Encoding UTF8 | ForEach-Object {{"
        " $s = Get-AuthenticodeSignature -LiteralPath $_;"
        " [pscustomobject]@{p=[string]$_; s=[string]$s.Status;"
        "  n=$(if ($s.SignerCertificate) { $s.SignerCertificate.GetNameInfo('SimpleName', $false) } else { '' })}"
        "} | ConvertTo-Json -Compress"
    )
    try:
        r = _run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script], 120)
        data = json.loads(r.stdout or "[]")
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    finally:
        os.unlink(listing)
    if isinstance(data, dict):
        data = [data]
    return {d["p"]: {"status": d.get("s") or "Unknown", "signer": d.get("n") or ""} for d in data if d.get("p")}

"""Builds BuzzTorrentTV.apk with the plain Android SDK tools (no Gradle, no Android Studio).

Needs: Android SDK build-tools + a platform android.jar (ANDROID_HOME, or the default
%LOCALAPPDATA%\\Android\\Sdk), and a JDK 11+ (JAVA_HOME or javac on PATH).
Output: android/build/BuzzTorrentTV.apk, which BuzzTorrent also serves to the TV at
http://<pc>:8765/BuzzTorrentTV.apk.

Every build gets the next version number (android/version.json), so Android accepts it as
an update over the installed app. The app is signed with android/buzztv.keystore (made on
first build). Keep that file: an update only installs over the old app if it is signed with
the same key.

  python android/build_apk.py          build
  python android/build_apk.py --usb    build, then copy the APK to every plugged-in pen drive"""
from __future__ import annotations

import os
import re
import json
import shutil
import string
import subprocess
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
KEYSTORE = HERE / "buzztv.keystore"
KS_PASS = "buzztorrent-tv"            # protects a self-signed sideload key, not a store key
VERSION_FILE = HERE / "version.json"
MIN_SDK, TARGET_SDK = 21, 29


def sdk_dir() -> Path:
    for cand in (os.environ.get("ANDROID_HOME"), os.environ.get("ANDROID_SDK_ROOT"),
                 Path(os.environ.get("LOCALAPPDATA", "")) / "Android" / "Sdk"):
        if cand and Path(cand, "build-tools").is_dir():
            return Path(cand)
    sys.exit("Android SDK not found: set ANDROID_HOME")


def newest(parent: Path, pattern: str = "*") -> Path:
    def key(p: Path):
        return [int(x) for x in re.findall(r"\d+", p.name)]
    items = sorted((p for p in parent.glob(pattern) if p.is_dir()), key=key)
    if not items:
        sys.exit(f"nothing in {parent}")
    return items[-1]


def jdk_tool(name: str) -> str:
    exe = name + (".exe" if os.name == "nt" else "")
    home = os.environ.get("JAVA_HOME")
    if home and Path(home, "bin", exe).is_file():
        return str(Path(home, "bin", exe))
    found = shutil.which(name)
    if not found:
        sys.exit(f"{name} not found: install a JDK 11+ and set JAVA_HOME")
    return found


def run(*cmd) -> None:
    cmd = [str(c) for c in cmd]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"FAILED: {' '.join(cmd)}\n{r.stdout}\n{r.stderr}")


def next_version() -> tuple[int, str]:
    try:
        code = int(json.loads(VERSION_FILE.read_text(encoding="utf-8"))["code"]) + 1
    except (OSError, ValueError, KeyError):
        code = 2                        # 1 was the first build, before versions were tracked
    return code, f"1.{code}"


def usb_drives() -> list[Path]:
    """Removable drives (pen drives, SD cards) on Windows."""
    if os.name != "nt":
        return []
    import ctypes
    out = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if ctypes.windll.kernel32.GetDriveTypeW(root) == 2 and Path(root).exists():   # DRIVE_REMOVABLE
            out.append(Path(root))
    return out


def copy_to_usb(apk: Path, name: str) -> None:
    drives = usb_drives()
    if not drives:
        print("No pen drive found. Plug one in and run:  python android/build_apk.py --usb")
        return
    for d in drives:
        for old in d.glob("BuzzTorrentTV*.apk"):
            old.unlink()
        dest = d / f"BuzzTorrentTV-{name}.apk"
        shutil.copyfile(apk, dest)
        print(f"Copied to {dest}")


def main() -> None:
    sdk = sdk_dir()
    version_code, version_name = next_version()
    bt = newest(sdk / "build-tools")
    platform = newest(sdk / "platforms", "android-*")
    android_jar = platform / "android.jar"
    aapt2 = bt / ("aapt2" + (".exe" if os.name == "nt" else ""))
    zipalign = bt / ("zipalign" + (".exe" if os.name == "nt" else ""))
    # Run the tools' jars directly: some build-tools' .bat wrappers pass options newer JDKs reject.
    java = jdk_tool("java")
    d8 = [java, "-cp", bt / "lib" / "d8.jar", "com.android.tools.r8.D8"]
    apksigner = [java, "-jar", bt / "lib" / "apksigner.jar"]
    print(f"build-tools {bt.name}, {platform.name}")

    if not (HERE / "res" / "drawable" / "icon.png").is_file():
        run(sys.executable, HERE / "make_icons.py")
    if BUILD.exists():
        shutil.rmtree(BUILD)
    classes, gen, dex = BUILD / "classes", BUILD / "gen", BUILD / "dex"
    for d in (classes, gen, dex):
        d.mkdir(parents=True)

    res_zip, unsigned, aligned = BUILD / "res.zip", BUILD / "unsigned.apk", BUILD / "aligned.apk"
    run(aapt2, "compile", "--dir", HERE / "res", "-o", res_zip)
    run(aapt2, "link", "-o", unsigned, "-I", android_jar, "--manifest", HERE / "AndroidManifest.xml",
        "-A", HERE / "assets", "--java", gen, "--min-sdk-version", MIN_SDK, "--target-sdk-version", TARGET_SDK,
        "--version-code", version_code, "--version-name", version_name, res_zip)

    sources = [str(p) for p in (HERE / "src").rglob("*.java")] + [str(p) for p in gen.rglob("*.java")]
    run(jdk_tool("javac"), "--release", "8", "-classpath", android_jar,
        "-encoding", "UTF-8", "-d", classes, *sources)
    run(*d8, "--release", "--min-api", MIN_SDK, "--lib", android_jar, "--output", dex,
        *[str(p) for p in classes.rglob("*.class")])

    with zipfile.ZipFile(unsigned, "a", zipfile.ZIP_DEFLATED) as z:
        z.write(dex / "classes.dex", "classes.dex")
    run(zipalign, "-p", "-f", "4", unsigned, aligned)

    if not KEYSTORE.is_file():
        run(jdk_tool("keytool"), "-genkeypair", "-keystore", KEYSTORE, "-storepass", KS_PASS, "-keypass", KS_PASS,
            "-alias", "buzztv", "-keyalg", "RSA", "-keysize", "2048", "-validity", "10000",
            "-dname", "CN=BuzzTorrent TV, O=Buzzcaf Media")
    apk = BUILD / "BuzzTorrentTV.apk"
    run(*apksigner, "sign", "--ks", KEYSTORE, "--ks-pass", f"pass:{KS_PASS}", "--key-pass", f"pass:{KS_PASS}",
        "--out", apk, aligned)
    run(*apksigner, "verify", apk)
    for p in (unsigned, aligned, res_zip):
        p.unlink(missing_ok=True)
    info = json.dumps({"code": version_code, "name": version_name})
    VERSION_FILE.write_text(info, encoding="utf-8")            # only after a successful build
    (BUILD / "version.json").write_text(info, encoding="utf-8")  # read by the server
    print(f"OK: {apk} version {version_name} ({apk.stat().st_size // 1024} KB)")
    if "--usb" in sys.argv[1:]:
        copy_to_usb(apk, version_name)


if __name__ == "__main__":
    main()

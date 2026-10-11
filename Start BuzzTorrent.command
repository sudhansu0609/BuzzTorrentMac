#!/bin/bash
# BuzzTorrent for macOS: double-click to start.
# (First time: right-click > Open, because macOS asks before running downloaded scripts.)
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

# ---- 1. Python 3.10 or newer ------------------------------------------------
PY=""
for c in python3 python3.13 python3.12 python3.11 python3.10 \
         /Library/Frameworks/Python.framework/Versions/Current/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; then
    PY="$(command -v "$c")"
    break
  fi
done
if [ -z "$PY" ]; then
  echo
  echo "   BuzzTorrent needs Python 3.10 or newer."
  if command -v brew >/dev/null 2>&1; then
    read -r -p "   Install it now with Homebrew? [Y/n] " a
    if [ "$a" != "n" ] && [ "$a" != "N" ]; then brew install python && exec "$0"; fi
  fi
  echo "   Get it from https://www.python.org/downloads/macos/ , then double-click Start BuzzTorrent again."
  open "https://www.python.org/downloads/macos/"
  read -r -p "   Press Return to close. "
  exit 1
fi

# ---- 2. First run: download engine + virus scanner (offered once) -----------
mkdir -p data
if [ ! -f data/mac-setup-asked ]; then
  if command -v brew >/dev/null 2>&1; then
    if ! command -v aria2c >/dev/null 2>&1; then
      echo "   BuzzTorrent downloads torrents itself with aria2, a free open-source download engine."
      read -r -p "   Install aria2 with Homebrew? [Y/n] " a
      if [ "$a" != "n" ] && [ "$a" != "N" ]; then brew install aria2; fi
    fi
    if ! command -v clamscan >/dev/null 2>&1; then
      echo "   Every finished download gets a virus scan with ClamAV, a free open-source antivirus."
      read -r -p "   Install ClamAV with Homebrew (takes a few minutes)? [Y/n] " a
      if [ "$a" != "n" ] && [ "$a" != "N" ]; then
        brew install clamav
        CONF="$(brew --prefix)/etc/clamav"
        if [ ! -f "$CONF/freshclam.conf" ] && [ -f "$CONF/freshclam.conf.sample" ]; then
          sed 's/^Example/#Example/' "$CONF/freshclam.conf.sample" > "$CONF/freshclam.conf"
        fi
        echo "   Downloading the virus database…"
        freshclam || echo "   (freshclam failed; run 'freshclam' later to get the virus database)"
      fi
    fi
  else
    echo "   Tip: install Homebrew (https://brew.sh), then 'brew install aria2 clamav',"
    echo "   so BuzzTorrent can download torrents itself and virus-scan every download."
  fi
  date > data/mac-setup-asked
fi

# ---- 3. First run: the BuzzTorrent app in ~/Applications -------------------
if [ ! -d "$HOME/Applications/BuzzTorrent.app" ]; then
  "$PY" app.py --install-shortcuts >/dev/null 2>&1 && \
    echo "   Added BuzzTorrent to your Applications folder (find it in Launchpad or Spotlight next time)."
fi

# ---- 4. Launch (opens the app window; quits when you close it) -------------
nohup "$PY" launcher.pyw >/dev/null 2>&1 &
echo "   BuzzTorrent is starting. You can close this window."
sleep 2
exit 0

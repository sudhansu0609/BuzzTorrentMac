#!/bin/bash
# Stops the BuzzTorrent started from this folder (uses the PID it recorded).
# Normally you don't need this: closing the BuzzTorrent window stops it.
cd "$(dirname "$0")" || exit 1
if [ ! -f .buzztorrent.pid ]; then
  echo "   BuzzTorrent is not running."
  sleep 2
  exit 0
fi
PID="$(cat .buzztorrent.pid)"
if ps -p "$PID" -o command= 2>/dev/null | grep -qE "launcher\.pyw|app\.py"; then
  kill "$PID" && echo "   BuzzTorrent stopped."
else
  echo "   BuzzTorrent is not running."
fi
rm -f .buzztorrent.pid
sleep 2

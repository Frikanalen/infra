#!/bin/sh
# Keep files/decklink-audio-probe.bt attached to the running CasparCG, and
# re-attach after every CasparCG restart (bpftrace -p exits with the process).
# The uprobe offsets are only valid for one build, so it refuses any other.
BIN=/usr/bin/casparcg-server-2.5
BUILD_ID="$1"
SCRIPT=/usr/local/lib/caspar-sdi-audio-watchdog/decklink-audio-probe.bt
while :; do
  if ! file -L "$BIN" | grep -q "BuildID\[sha1\]=$BUILD_ID"; then
    echo "casparcg build differs from $BUILD_ID; probe offsets invalid, not attaching"
    sleep 600
    continue
  fi
  P=$(pgrep -x -f "$BIN /etc/casparcg.config") || { sleep 5; continue; }
  echo "attaching to casparcg pid $P"
  bpftrace --unsafe -p "$P" "$SCRIPT"
  echo "bpftrace exited ($?)"
  sleep 5
done

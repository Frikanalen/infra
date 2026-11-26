#!/usr/bin/env bash
# Reproduce air-like load on a CasparCG test machine via AMCP. See --help.
set -euo pipefail
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$BENCH_DIR/lib.sh"

CLIP_CHANNEL="${CLIP_CHANNEL:-1}"             # the channel that feeds the DeckLink under test
CLIP_LAYER="${CLIP_LAYER:-10}"
SYNC_CLIP="${SYNC_CLIP:-BENCH_SYNC}"
LOAD_CHANNEL="${LOAD_CHANNEL:-$CLIP_CHANNEL}" # HTML / stream / decode load can go to another channel
HTML_LAYER="${HTML_LAYER:-20}"
HTML_URL="${HTML_URL:-file://$BENCH_DIR/busy.html}"
HTML_FILL="${HTML_FILL:-0.70 0.70 0.28 0.28}" # keep it out of the flash-detection quadrant; "" = full frame
STREAM_ENABLE="${STREAM_ENABLE:-1}"
TEST_MCAST="${TEST_MCAST:-udp://239.255.0.99:5004}"
STREAM_URL="${STREAM_URL:-${TEST_MCAST}?pkt_size=1316&ttl=1}"
STREAM_ARGS="${STREAM_ARGS:--format mpegts -codec:v libx264 -preset:v veryfast -tune:v zerolatency -b:v 4M -maxrate:v 4M -bufsize:v 4M -g:v 50 -codec:a aac -b:a 192k}"
STREAM_PORT="${STREAM_PORT:-$((100000 + LOAD_CHANNEL))}"   # ffmpeg consumer index = 100000 + channel
DECODE_CLIP="${DECODE_CLIP:-}"                # e.g. a 1080p50 H.264/ProRes clip in the media dir
DECODE_LAYER="${DECODE_LAYER:-30}"
DECODE_FILL="${DECODE_FILL:-0.70 0.02 0.28 0.28}"
STRESS_CPU="${STRESS_CPU:-0}"                 # stress-ng --cpu N (0 = off)
STRESS_ARGS="${STRESS_ARGS:---cpu-method matrixprod}"
STATE_DIR="${STATE_DIR:-${XDG_RUNTIME_DIR:-/tmp}/decklink-bench}"

usage() {
cat <<EOF
Usage: $(basename "$0") start|stop|status [--help]

start   PLAY $CLIP_CHANNEL-$CLIP_LAYER \$SYNC_CLIP LOOP          (the clip capture-analyse.sh measures)
        PLAY $LOAD_CHANNEL-$HTML_LAYER [HTML] \$HTML_URL, MIXER FILL \$HTML_FILL
        ADD $LOAD_CHANNEL STREAM \$STREAM_URL \$STREAM_ARGS          (if STREAM_ENABLE=1)
        PLAY $LOAD_CHANNEL-$DECODE_LAYER \$DECODE_CLIP LOOP, MIXER FILL   (if DECODE_CLIP is set)
        stress-ng --cpu \$STRESS_CPU in the background           (if > 0 and installed)
stop    undo all of the above (REMOVE the stream consumer, CLEAR the layers, kill stress-ng)
status  show layers/consumers on the channels and whether stress-ng runs

Env (defaults):
  CASPAR_HOST=$CASPAR_HOST CASPAR_PORT=$CASPAR_PORT
  CLIP_CHANNEL=$CLIP_CHANNEL CLIP_LAYER=$CLIP_LAYER SYNC_CLIP=$SYNC_CLIP
  LOAD_CHANNEL=$LOAD_CHANNEL HTML_LAYER=$HTML_LAYER HTML_FILL="$HTML_FILL"
  HTML_URL=$HTML_URL
  STREAM_ENABLE=$STREAM_ENABLE TEST_MCAST=$TEST_MCAST
  STREAM_URL=$STREAM_URL
  STREAM_ARGS="$STREAM_ARGS"
  STREAM_PORT=$STREAM_PORT
  DECODE_CLIP=$DECODE_CLIP DECODE_LAYER=$DECODE_LAYER DECODE_FILL="$DECODE_FILL"
  STRESS_CPU=$STRESS_CPU STRESS_ARGS="$STRESS_ARGS" STATE_DIR=$STATE_DIR
  BENCH_ACK_NOT_ON_AIR=1 is required for start/stop.

STREAM_ARGS follow the CasparCG 2.5 ffmpeg consumer syntax: -name value pairs,
no spaces inside values; options ending in :v / :a go to that encoder, the rest
to the muxer/protocol. Keep the multicast on a test VLAN (ttl=1 by default).
EOF
}

cmd="${1:-}"
case "$cmd" in
    -h|--help) usage; exit 0 ;;
    start|stop|status) ;;
    *) usage >&2; exit 2 ;;
esac

mkdir -p "$STATE_DIR"
STRESS_PIDFILE="$STATE_DIR/stress-ng.pid"

# Run AMCP commands, but do not abort on an individual 4xx (e.g. CLEAR of an empty layer).
amcp_soft() { amcp "$@" || log "warning: AMCP returned an error for: $*"; }

stress_running() { [[ -s "$STRESS_PIDFILE" ]] && kill -0 "$(cat "$STRESS_PIDFILE")" 2>/dev/null; }

do_start() {
    bench_guard
    log "load start"
    amcp "PLAY $CLIP_CHANNEL-$CLIP_LAYER \"$SYNC_CLIP\" LOOP"
    amcp "PLAY $LOAD_CHANNEL-$HTML_LAYER [HTML] \"$HTML_URL\""
    if [[ -n "$HTML_FILL" ]]; then amcp_soft "MIXER $LOAD_CHANNEL-$HTML_LAYER FILL $HTML_FILL"; fi
    if [[ "$STREAM_ENABLE" == 1 ]]; then
        if port_present "$LOAD_CHANNEL" "$STREAM_PORT"; then
            log "stream consumer already on $LOAD_CHANNEL-$STREAM_PORT, leaving it"
        else
            amcp "ADD $LOAD_CHANNEL STREAM \"$STREAM_URL\" $STREAM_ARGS"
        fi
    fi
    if [[ -n "$DECODE_CLIP" ]]; then
        amcp "PLAY $LOAD_CHANNEL-$DECODE_LAYER \"$DECODE_CLIP\" LOOP"
        if [[ -n "$DECODE_FILL" ]]; then amcp_soft "MIXER $LOAD_CHANNEL-$DECODE_LAYER FILL $DECODE_FILL"; fi
    fi
    if (( STRESS_CPU > 0 )); then
        if ! have stress-ng; then
            log "stress-ng not installed; skipping CPU stress"
        elif stress_running; then
            log "stress-ng already running (pid $(cat "$STRESS_PIDFILE"))"
        else
            # shellcheck disable=SC2086
            nohup stress-ng --cpu "$STRESS_CPU" $STRESS_ARGS --timeout 0 >"$STATE_DIR/stress-ng.log" 2>&1 &
            echo $! >"$STRESS_PIDFILE"
            log "stress-ng --cpu $STRESS_CPU started (pid $!)"
        fi
    fi
    {
        echo "started=$(utc)"
        echo "clip=$CLIP_CHANNEL-$CLIP_LAYER $SYNC_CLIP"
        echo "html=$LOAD_CHANNEL-$HTML_LAYER $HTML_URL fill=$HTML_FILL"
        echo "stream=$STREAM_ENABLE $STREAM_URL $STREAM_ARGS"
        echo "decode=${DECODE_CLIP:-none}"
        echo "stress_cpu=$STRESS_CPU"
    } >"$STATE_DIR/load.env"
    log "load running; '$0 stop' to undo"
}

do_stop() {
    bench_guard
    log "load stop"
    if port_present "$LOAD_CHANNEL" "$STREAM_PORT"; then
        amcp_soft "REMOVE $LOAD_CHANNEL-$STREAM_PORT"
    fi
    if [[ -n "$DECODE_CLIP" ]]; then amcp_soft "CLEAR $LOAD_CHANNEL-$DECODE_LAYER"; amcp_soft "MIXER $LOAD_CHANNEL-$DECODE_LAYER CLEAR"; fi
    amcp_soft "CLEAR $LOAD_CHANNEL-$HTML_LAYER"
    amcp_soft "MIXER $LOAD_CHANNEL-$HTML_LAYER CLEAR"
    amcp_soft "CLEAR $CLIP_CHANNEL-$CLIP_LAYER"
    if stress_running; then
        kill "$(cat "$STRESS_PIDFILE")" 2>/dev/null || true
        log "stress-ng stopped"
    fi
    rm -f "$STRESS_PIDFILE" "$STATE_DIR/load.env"
}

do_status() {
    local ch
    for ch in $(printf '%s\n' "$CLIP_CHANNEL" "$LOAD_CHANNEL" | sort -u); do
        echo "== channel $ch"
        amcp_q "INFO $ch" | grep -E '<(layer_[0-9]+|port_[0-9]+)>|<consumer>|<name>|<path>|<file>' || echo "(nothing matched / INFO failed)"
    done
    if stress_running; then echo "stress-ng: running (pid $(cat "$STRESS_PIDFILE"))"; else echo "stress-ng: not running"; fi
    [[ -s "$STATE_DIR/load.env" ]] && { echo "== $STATE_DIR/load.env"; cat "$STATE_DIR/load.env"; }
    return 0
}

"do_$cmd"

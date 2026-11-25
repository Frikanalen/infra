#!/usr/bin/env bash
# Cycle the DeckLink consumer through experiment modes and record each run. See --help.
set -euo pipefail
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$BENCH_DIR/lib.sh"

MODES="${MODES:-base ahead ahead,pool=heap ahead,pool=sdk nokeeper}"
RUN_MINUTES="${RUN_MINUTES:-45}"
RUN_SECONDS="${RUN_SECONDS:-$((RUN_MINUTES * 60))}"        # overrides RUN_MINUTES (for dry runs)
REPEAT="${REPEAT:-2}"
WARMUP_MINUTES="${WARMUP_MINUTES:-10}"
WARMUP_MODE="${WARMUP_MODE:-base}"
WARMUP_SECONDS="${WARMUP_SECONDS:-$((WARMUP_MINUTES * 60))}"
EXP_FILE="${EXP_FILE:-/run/casparcg/decklink-exp}"
DL_CHANNEL="${DL_CHANNEL:-1}"
DL_PORT="${DL_PORT:-301}"
ADD_PARAMS="${ADD_PARAMS:-DECKLINK 1 EMBEDDED_AUDIO INTERNAL_KEY}"
REMOVE_TIMEOUT_S="${REMOVE_TIMEOUT_S:-30}"
ADD_TIMEOUT_S="${ADD_TIMEOUT_S:-20}"
SETTLE_S="${SETTLE_S:-5}"                     # pause between REMOVE completing and ADD
POST_ADD_SETTLE_S="${POST_ADD_SETTLE_S:-30}"  # preroll/settle before the run clock starts
OUT_DIR="${OUT_DIR:-$PWD/bench-out-$(date -u +%Y%m%dT%H%M%SZ)}"
CAPTURE_URL="${CAPTURE_URL:-}"                # srt://… udp://… http://… or decklink:<device name>; empty = no capture
CAPTURE_SECONDS="${CAPTURE_SECONDS:-60}"
CAPTURE_EVERY_S="${CAPTURE_EVERY_S:-600}"
CAPTURE_FIRST_S="${CAPTURE_FIRST_S:-60}"      # first capture this long after the run starts
CAPTURE_FORMAT_CODE="${CAPTURE_FORMAT_CODE:-hp50}"   # ffmpeg -f decklink -format_code (720p50)
CAPTURE_INPUT_ARGS="${CAPTURE_INPUT_ARGS:-}"  # extra ffmpeg input options for URL captures
CAPTURE_CODEC_ARGS="${CAPTURE_CODEC_ARGS:-}"  # override output options; default depends on source
CAPTURE_EXT="${CAPTURE_EXT:-}"
DIAG_LOG="${DIAG_LOG:-/var/log/casparcg/decklink-diag.log}"
DIAG_CSV="${DIAG_CSV:-$DIAG_LOG.csv}"
COPY_DIAG="${COPY_DIAG:-1}"
COPY_DIAG_CSV="${COPY_DIAG_CSV:-1}"
PIDSTAT_ARGS="${PIDSTAT_ARGS:--h -t -u -w}"
PIDSTAT_INTERVAL="${PIDSTAT_INTERVAL:-1}"
RESTORE_ON_EXIT="${RESTORE_ON_EXIT:-1}"       # on exit: remove EXP_FILE and re-create the consumer (= env/base mode)
HEALTH_EVERY_S="${HEALTH_EVERY_S:-30}"

usage() {
cat <<EOF
Usage: $(basename "$0") [--help]

Runs an optional warm-up, then REPEAT rounds over MODES (odd rounds in the
given order, even rounds reversed). For each run:

  1. writes the mode string as the first line of \$EXP_FILE (the patched
     DeckLink consumer reads it at creation, overriding CASPAR_DECKLINK_EXP)
  2. REMOVE $DL_CHANNEL-$DL_PORT, polls INFO $DL_CHANNEL until <port_$DL_PORT> is gone,
     waits \$SETTLE_S, ADD $DL_CHANNEL \$ADD_PARAMS, waits until it is back and
     for the diag START line, then \$POST_ADD_SETTLE_S more
  3. records diag log / CSV byte offsets, starts pidstat on the casparcg pid
     and captures \$CAPTURE_SECONDS s of the SDI return every \$CAPTURE_EVERY_S s
  4. after \$RUN_MINUTES min stops the helpers, appends a row to runs.csv and
     copies the diag log (and CSV) slice into runs/<id>/

Outputs: \$OUT_DIR/runs.csv, \$OUT_DIR/runs/<id>/{run.env,diag.log,diag.csv,
pidstat.txt,captures.csv,cap-*.{ts,mkv},consumer-start.txt,info-*.xml}.
Afterwards: capture-analyse.sh \$OUT_DIR/runs/*/cap-*.* ; summarize.py \$OUT_DIR

Env (defaults):
  MODES="$MODES"
  RUN_MINUTES=$RUN_MINUTES (or RUN_SECONDS) REPEAT=$REPEAT WARMUP_MINUTES=$WARMUP_MINUTES (or WARMUP_SECONDS) WARMUP_MODE=$WARMUP_MODE
  EXP_FILE=$EXP_FILE
  DL_CHANNEL=$DL_CHANNEL DL_PORT=$DL_PORT ADD_PARAMS="$ADD_PARAMS"
  REMOVE_TIMEOUT_S=$REMOVE_TIMEOUT_S ADD_TIMEOUT_S=$ADD_TIMEOUT_S SETTLE_S=$SETTLE_S POST_ADD_SETTLE_S=$POST_ADD_SETTLE_S
  OUT_DIR=\$PWD/bench-out-<UTC timestamp>
  CAPTURE_URL=${CAPTURE_URL:-<none>}  (e.g. srt://host:9000, udp://@239.1.1.1:5000, "decklink:DeckLink SDI 4K (2)")
  CAPTURE_SECONDS=$CAPTURE_SECONDS CAPTURE_EVERY_S=$CAPTURE_EVERY_S CAPTURE_FIRST_S=$CAPTURE_FIRST_S
  CAPTURE_FORMAT_CODE=$CAPTURE_FORMAT_CODE CAPTURE_INPUT_ARGS CAPTURE_CODEC_ARGS CAPTURE_EXT
  DIAG_LOG=$DIAG_LOG DIAG_CSV=\$DIAG_LOG.csv COPY_DIAG=$COPY_DIAG COPY_DIAG_CSV=$COPY_DIAG_CSV
  PIDSTAT_ARGS="$PIDSTAT_ARGS" PIDSTAT_INTERVAL=$PIDSTAT_INTERVAL
  CASPAR_PID (default: pgrep casparcg) CASPAR_HOST=$CASPAR_HOST CASPAR_PORT=$CASPAR_PORT
  RESTORE_ON_EXIT=$RESTORE_ON_EXIT HEALTH_EVERY_S=$HEALTH_EVERY_S
  BENCH_ACK_NOT_ON_AIR=1 is required.

Start the load first (load.sh start). Every mode change re-creates the
DeckLink consumer, which drops the SDI output for a few seconds.
EOF
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }
[[ $# -eq 0 ]] || { usage >&2; exit 2; }

bench_guard
have python3 || die "python3 not found"
amcp_q "VERSION" >/dev/null || die "no AMCP at $CASPAR_HOST:$CASPAR_PORT"
port_present "$DL_CHANNEL" "$DL_PORT" || log "note: no consumer on $DL_CHANNEL-$DL_PORT yet; it will be added"
[[ -n "$CAPTURE_URL" ]] && ! have ffmpeg && die "CAPTURE_URL set but ffmpeg not found"
[[ -n "$CAPTURE_URL" ]] || log "note: CAPTURE_URL empty, no SDI return captures"
have pidstat || log "note: pidstat not installed (sysstat), no per-thread CPU log"
[[ -e "$DIAG_LOG" ]] || log "note: $DIAG_LOG does not exist (yet); diag offsets will be -1"

mkdir -p "$OUT_DIR/runs" "$(dirname "$EXP_FILE")" || die "cannot create $OUT_DIR or $(dirname "$EXP_FILE")"
touch "$EXP_FILE.probe" 2>/dev/null && rm -f "$EXP_FILE.probe" || die "cannot write to $(dirname "$EXP_FILE") (fix permissions or set EXP_FILE)"
RUNS_CSV="$OUT_DIR/runs.csv"
[[ -s "$RUNS_CSV" ]] || echo "run_id,mode,round,kind,start_utc,end_utc,status,caspar_pid,diag_off_start,diag_off_end,csv_off_start,csv_off_end,captures" >"$RUNS_CSV"
{
    echo "# bench matrix $(utc) on $(hostname)"
    for v in MODES RUN_SECONDS REPEAT WARMUP_SECONDS WARMUP_MODE EXP_FILE DL_CHANNEL DL_PORT ADD_PARAMS \
             CAPTURE_URL CAPTURE_SECONDS CAPTURE_EVERY_S DIAG_LOG PIDSTAT_ARGS; do
        printf '%s=%q\n' "$v" "${!v}"
    done
    echo "caspar_version=$(amcp_q VERSION 2>/dev/null | tail -n +2 | head -1)"
    uname -a
} >>"$OUT_DIR/matrix.env"

# ---------------------------------------------------------------------------
BG_PIDS=()
CUR_ID="" CUR_MODE="" CUR_ROUND="" CUR_KIND="" CUR_START="" CUR_PID=""
CUR_DOFF=-1 CUR_COFF=-1

kill_tree() {
    local p="$1" c
    for c in $(pgrep -P "$p" 2>/dev/null || true); do kill_tree "$c"; done
    kill -TERM "$p" 2>/dev/null || true
}

stop_helpers() {
    local p
    for p in "${BG_PIDS[@]}"; do kill_tree "$p"; done
    for p in "${BG_PIDS[@]}"; do { wait "$p"; } 2>/dev/null || true; done
    BG_PIDS=()
}

write_exp() {
    local tmp="$EXP_FILE.tmp.$$"
    printf '%s\n' "$1" >"$tmp"
    mv -f "$tmp" "$EXP_FILE"
}

copy_slice() {  # src from to dst
    local src="$1" from="$2" to="$3" dst="$4"
    [[ -e "$src" ]] || return 0
    if (( from < 0 )); then from=0; fi
    if (( to < from )); then
        log "warning: $src shrank during the run (rotated/truncated?); copying it whole"
        cp -f "$src" "$dst"; return 0
    fi
    tail -c +$((from + 1)) "$src" | head -c $((to - from)) >"$dst" || true
}

wait_port() {  # want(present|absent) timeout
    local want="$1" t="$2" i
    for ((i = 0; i < t * 2; i++)); do
        if port_present "$DL_CHANNEL" "$DL_PORT"; then
            [[ "$want" == present ]] && return 0
        else
            [[ "$want" == absent ]] && return 0
        fi
        sleep 0.5
    done
    return 1
}

recreate_consumer() {  # mode rundir
    local mode="$1" dir="$2" doff i
    write_exp "$mode"
    log "exp file $EXP_FILE <- '$mode'"
    if port_present "$DL_CHANNEL" "$DL_PORT"; then
        amcp "REMOVE $DL_CHANNEL-$DL_PORT" >>"$dir/amcp.log" || die "REMOVE $DL_CHANNEL-$DL_PORT failed"
        wait_port absent "$REMOVE_TIMEOUT_S" || die "consumer $DL_CHANNEL-$DL_PORT still present after ${REMOVE_TIMEOUT_S}s"
    fi
    sleep "$SETTLE_S"
    doff="$(file_size "$DIAG_LOG")"
    amcp "ADD $DL_CHANNEL $ADD_PARAMS" >>"$dir/amcp.log" || die "ADD $DL_CHANNEL $ADD_PARAMS failed"
    wait_port present "$ADD_TIMEOUT_S" || die "consumer $DL_CHANNEL-$DL_PORT did not appear in ${ADD_TIMEOUT_S}s"
    # Wait for the diag logger to write the new START line (it carries the config; the exp
    # build is expected to log the active experiment flags there too).
    if [[ -e "$DIAG_LOG" ]]; then
        for ((i = 0; i < 20; i++)); do
            if copy_slice "$DIAG_LOG" "$doff" "$(file_size "$DIAG_LOG")" "$dir/consumer-start.txt" \
               && grep -q ' I START' "$dir/consumer-start.txt"; then break; fi
            sleep 0.5
        done
        grep -E ' I (START|EXP)' "$dir/consumer-start.txt" 2>/dev/null | tail -3 | sed 's/^/    /' >&2 \
            || log "warning: no diag START line seen after ADD"
    fi
    amcp_q "INFO $DL_CHANNEL" >"$dir/info-after-add.xml" 2>&1 || true
}

capture_once() {  # dir n
    local dir="$1" n="$2" out ext t0 rc=0
    local -a in_args codec
    if [[ "$CAPTURE_URL" == decklink:* ]]; then
        ext="${CAPTURE_EXT:-mkv}"
        in_args=(-f decklink -format_code "$CAPTURE_FORMAT_CODE" -i "${CAPTURE_URL#decklink:}")
        # Small video (flash detection only) keeps the encoder cheap; audio untouched.
        if [[ -n "$CAPTURE_CODEC_ARGS" ]]; then read -r -a codec <<<"$CAPTURE_CODEC_ARGS"
        else codec=(-vf scale=320:180 -c:v libx264 -preset ultrafast -crf 23 -c:a pcm_s16le); fi
    else
        ext="${CAPTURE_EXT:-ts}"
        read -r -a in_args <<<"$CAPTURE_INPUT_ARGS"
        in_args+=(-i "$CAPTURE_URL")
        if [[ -n "$CAPTURE_CODEC_ARGS" ]]; then read -r -a codec <<<"$CAPTURE_CODEC_ARGS"
        else codec=(-map 0:v:0 -map 0:a:0 -c copy); fi
    fi
    out="$dir/cap-$(printf '%02d' "$n")-$(date -u +%H%M%S).$ext"
    t0="$(utc)"
    timeout -k 5 $((CAPTURE_SECONDS + 30)) \
        ffmpeg -nostdin -hide_banner -loglevel warning -y "${in_args[@]}" -t "$CAPTURE_SECONDS" \
        "${codec[@]}" "$out" 2>"$out.log" || rc=$?
    echo "$(basename "$out"),$t0,$(utc),$rc" >>"$dir/captures.csv"
}

capture_loop() {  # dir
    local dir="$1" n=0 next
    trap 'exit 0' TERM
    echo "file,start_utc,end_utc,ffmpeg_rc" >"$dir/captures.csv"
    sleep "$CAPTURE_FIRST_S"
    while true; do
        next=$(( $(date +%s) + CAPTURE_EVERY_S ))
        n=$((n + 1))
        capture_once "$dir" "$n"
        while (( $(date +%s) < next )); do sleep 1; done
    done
}

finish_run() {  # status
    local status="$1" dend cend ncap=0
    [[ -n "$CUR_ID" ]] || return 0
    stop_helpers
    dend="$(file_size "$DIAG_LOG")"; cend="$(file_size "$DIAG_CSV")"
    local dir="$OUT_DIR/runs/$CUR_ID"
    [[ -s "$dir/captures.csv" ]] && ncap=$(( $(wc -l <"$dir/captures.csv") - 1 ))
    echo "$CUR_ID,\"$CUR_MODE\",$CUR_ROUND,$CUR_KIND,$CUR_START,$(utc),$status,$CUR_PID,$CUR_DOFF,$dend,$CUR_COFF,$cend,$ncap" >>"$RUNS_CSV"
    if [[ "$COPY_DIAG" == 1 ]]; then copy_slice "$DIAG_LOG" "$CUR_DOFF" "$dend" "$dir/diag.log"; fi
    if [[ "$COPY_DIAG_CSV" == 1 && -e "$DIAG_CSV" ]]; then copy_slice "$DIAG_CSV" "$CUR_COFF" "$cend" "$dir/diag.csv"; fi
    log "run $CUR_ID ($CUR_MODE) finished: $status"
    CUR_ID=""
}

on_exit() {
    local rc=$?
    trap - EXIT INT TERM
    finish_run "aborted(rc=$rc)" || true
    if [[ "$RESTORE_ON_EXIT" == 1 ]]; then
        log "restoring: removing $EXP_FILE and re-creating $DL_CHANNEL-$DL_PORT"
        rm -f "$EXP_FILE"
        ( recreate_consumer_restore ) || log "warning: restore failed; check $DL_CHANNEL-$DL_PORT by hand"
    fi
    exit "$rc"
}
recreate_consumer_restore() {
    if port_present "$DL_CHANNEL" "$DL_PORT"; then
        amcp "REMOVE $DL_CHANNEL-$DL_PORT" >/dev/null || true
        wait_port absent "$REMOVE_TIMEOUT_S" || return 1
    fi
    sleep "$SETTLE_S"
    amcp "ADD $DL_CHANNEL $ADD_PARAMS" >/dev/null && wait_port present "$ADD_TIMEOUT_S"
}
trap on_exit EXIT
trap 'exit 130' INT
trap 'exit 143' TERM

do_run() {  # id mode round kind seconds
    local id="$1" mode="$2" round="$3" kind="$4" seconds="$5"
    local dir="$OUT_DIR/runs/$id" end pid now next_health=0
    mkdir -p "$dir"
    log "=== run $id: mode '$mode' (${kind}, round $round, ${seconds} s)"
    recreate_consumer "$mode" "$dir"
    sleep "$POST_ADD_SETTLE_S"

    pid="$(caspar_pid)"
    CUR_ID="$id" CUR_MODE="$mode" CUR_ROUND="$round" CUR_KIND="$kind" CUR_PID="${pid:-}"
    CUR_DOFF="$(file_size "$DIAG_LOG")" CUR_COFF="$(file_size "$DIAG_CSV")"
    CUR_START="$(utc)"
    {
        printf 'run_id=%q\nmode=%q\nround=%s\nkind=%s\nstart_utc=%s\ncaspar_pid=%s\n' \
            "$id" "$mode" "$round" "$kind" "$CUR_START" "${pid:-}"
        printf 'exp_file=%q\nexp_file_content=%q\n' "$EXP_FILE" "$(head -1 "$EXP_FILE")"
        printf 'diag_log=%q\ndiag_off_start=%s\ndiag_csv=%q\ncsv_off_start=%s\n' \
            "$DIAG_LOG" "$CUR_DOFF" "$DIAG_CSV" "$CUR_COFF"
    } >"$dir/run.env"

    if [[ -n "$pid" ]] && have pidstat; then
        # shellcheck disable=SC2086
        S_TIME_FORMAT=ISO LC_ALL=C pidstat $PIDSTAT_ARGS -p "$pid" "$PIDSTAT_INTERVAL" >"$dir/pidstat.txt" 2>&1 &
        BG_PIDS+=($!)
    fi
    if [[ -n "$CAPTURE_URL" ]]; then
        capture_loop "$dir" &
        BG_PIDS+=($!)
    fi

    end=$(( $(date +%s) + seconds ))
    while (( (now = $(date +%s)) < end )); do
        if (( now >= next_health )); then
            next_health=$(( now + HEALTH_EVERY_S ))
            if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
                finish_run "casparcg_exited"; die "casparcg (pid $pid) exited during run $id"
            fi
            if ! port_present "$DL_CHANNEL" "$DL_PORT"; then
                echo "$(utc) consumer missing" >>"$dir/health.log"
                log "warning: $DL_CHANNEL-$DL_PORT missing during run $id"
            fi
        fi
        sleep 5
    done
    amcp_q "INFO $DL_CHANNEL" >"$dir/info-end.xml" 2>&1 || true
    if [[ -s "$dir/health.log" ]]; then finish_run "consumer_missing"; else finish_run "ok"; fi
}

read -r -a mode_list <<<"$MODES"
(( ${#mode_list[@]} > 0 )) || die "MODES is empty"
idx=0
safe() { tr -c 'A-Za-z0-9=._\n-' '_' <<<"$1" | tr '=' '-'; }

if (( WARMUP_SECONDS > 0 )); then
    do_run "00-warmup-$(safe "$WARMUP_MODE")" "$WARMUP_MODE" 0 warmup "$WARMUP_SECONDS"
fi
for ((round = 1; round <= REPEAT; round++)); do
    order=("${mode_list[@]}")
    if (( round % 2 == 0 )); then
        order=()
        for ((k = ${#mode_list[@]} - 1; k >= 0; k--)); do order+=("${mode_list[$k]}"); done
    fi
    for mode in "${order[@]}"; do
        idx=$((idx + 1))
        do_run "$(printf '%02d' "$idx")-r${round}-$(safe "$mode")" "$mode" "$round" measure "$RUN_SECONDS"
    done
done
log "matrix complete: $RUNS_CSV"

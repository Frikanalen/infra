# shellcheck shell=bash
# Shared helpers for the DeckLink bench kit. Source it; do not run it.

BENCH_DIR="${BENCH_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)}"

CASPAR_HOST="${CASPAR_HOST:-127.0.0.1}"
CASPAR_PORT="${CASPAR_PORT:-5250}"
export CASPAR_HOST CASPAR_PORT

# Hostnames that must never run this kit (space separated, exact match).
BENCH_FORBIDDEN_HOSTS="${BENCH_FORBIDDEN_HOSTS:-caspar1 caspar2}"
# A file whose presence marks a machine as on-air.
BENCH_ONAIR_MARKER="${BENCH_ONAIR_MARKER:-/etc/casparcg-onair}"

log()  { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*" >&2; }
die()  { log "ERROR: $*"; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
utc()  { date -u +%Y-%m-%dT%H:%M:%SZ; }

# amcp CMD... : send AMCP commands, print responses. Non-zero on 4xx/5xx.
amcp() { python3 "$BENCH_DIR/amcp.py" -H "$CASPAR_HOST" -p "$CASPAR_PORT" "$@"; }
amcp_q() { python3 "$BENCH_DIR/amcp.py" -q -H "$CASPAR_HOST" -p "$CASPAR_PORT" "$@"; }

# Refuse to change anything on a machine that could be on air.
bench_guard() {
    local h
    h="$(hostname -s 2>/dev/null || hostname)"
    for f in $BENCH_FORBIDDEN_HOSTS; do
        [[ "$h" == "$f" ]] && die "host '$h' is in BENCH_FORBIDDEN_HOSTS; this kit must never run on an on-air machine"
    done
    [[ -e "$BENCH_ONAIR_MARKER" ]] && die "$BENCH_ONAIR_MARKER exists: this machine is marked on-air"
    case "$CASPAR_HOST" in
        127.0.0.1|localhost|::1) ;;
        *) [[ "${BENCH_ALLOW_REMOTE:-0}" == 1 ]] || die "CASPAR_HOST=$CASPAR_HOST is not local; set BENCH_ALLOW_REMOTE=1 if that host is a test machine" ;;
    esac
    [[ "${BENCH_ACK_NOT_ON_AIR:-0}" == 1 ]] || die "set BENCH_ACK_NOT_ON_AIR=1 to confirm this is a test machine whose outputs feed nothing on air"
}

caspar_pid() {
    if [[ -n "${CASPAR_PID:-}" ]]; then echo "$CASPAR_PID"; return; fi
    pgrep -o -x casparcg 2>/dev/null || pgrep -o -f 'bin/casparcg' 2>/dev/null || true
}

file_size() { if [[ -e "$1" ]]; then stat -c %s "$1"; else echo -1; fi; }

# True if INFO <ch> lists a consumer on the given port (XML node port_<n>).
port_present() {
    local ch="$1" port="$2" out
    out="$(amcp_q "INFO $ch" 2>/dev/null)" || return 2
    grep -q "<port_${port}>" <<<"$out"
}

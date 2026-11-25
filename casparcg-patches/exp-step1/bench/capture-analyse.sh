#!/usr/bin/env bash
# Analyse an SDI return capture of the sync clip: audio gaps and A/V offset. See --help.
set -euo pipefail
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$BENCH_DIR/lib.sh"

GAP_NOISE="${GAP_NOISE:--80dB}"
GAP_MIN_S="${GAP_MIN_S:-0.0002}"
GAP_SKIP_S="${GAP_SKIP_S:-1.0}"            # ignore gaps that start in the first N s (capture start-up)
GAP_MONO="${GAP_MONO:-0}"                  # 1 = detect gaps per channel (silencedetect mono=1)
BEEP_THRESH="${BEEP_THRESH:--40dB}"
BEEP_FILTER="${BEEP_FILTER:-pan=mono|c0=c0,bandreject=f=1000:width_type=q:w=0.7,bandreject=f=1000:width_type=q:w=0.7,highpass=f=1600,highpass=f=1600,highpass=f=1600,bandpass=f=2000:width_type=q:w=1.5}"
FLASH_CROP="${FLASH_CROP:-iw/2:ih/2:0:0}"  # top-left quadrant, clear of burned-in text and load layers
FLASH_YAVG="${FLASH_YAVG:-128}"            # 8-bit luma mean threshold for "white"
NOMINAL_FPS="${NOMINAL_FPS:-50}"
BEEP_MIN_MS="${BEEP_MIN_MS:-8}"           # shorter non-silent spans after the filter are clicks, not beeps
KEEP_TMP="${KEEP_TMP:-0}"

usage() {
cat <<EOF
Usage: $(basename "$0") [--help] CAPTURE_FILE [CAPTURE_FILE...]

For each capture of the sync clip (from gen-sync-clip.sh) played through the
DeckLink output, prints and writes <capture>.analysis.json with:

  * audio gaps: ffmpeg silencedetect noise=\$GAP_NOISE d=\$GAP_MIN_S on the
    first audio stream; count per minute of capture and total silent time.
    The clip never contains silence, so every hit is a gap/dropout.
  * beep onsets: first channel, 1 kHz tone removed (\$BEEP_FILTER), then
    silencedetect at \$BEEP_THRESH; silence_end = onset of each 2 kHz burst.
    Beep-to-beep intervals that are not ~1000 ms show audio slips (n x 20 ms
    = whole frames of audio dropped or repeated).
  * flash onsets: signalstats YAVG of crop=\$FLASH_CROP above \$FLASH_YAVG.
    Flash intervals other than 1000 ms show dropped/repeated video frames.
  * A/V offset: beep onset minus flash onset for each pair within +-500 ms;
    median, p5, p95, min, max in ms. Positive = audio late.
    Resolution is one frame (20 ms) on the video side.

Env (defaults): GAP_NOISE=$GAP_NOISE GAP_MIN_S=$GAP_MIN_S GAP_SKIP_S=$GAP_SKIP_S GAP_MONO=$GAP_MONO
  BEEP_THRESH=$BEEP_THRESH BEEP_MIN_MS=$BEEP_MIN_MS FLASH_CROP=$FLASH_CROP FLASH_YAVG=$FLASH_YAVG
  NOMINAL_FPS=$NOMINAL_FPS KEEP_TMP=$KEEP_TMP (1 keeps the raw ffmpeg outputs)
  BEEP_FILTER=<ffmpeg audio filter chain before silencedetect>

This is CPU heavy: run it after the matrix, or on another machine, never
while a measured run is in progress.
EOF
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -eq 0 ]] && { usage; [[ $# -eq 0 ]] && exit 2; exit 0; }
have ffmpeg || die "ffmpeg not found"

analyse_one() {
    local f="$1" tmp gapopt
    [[ -s "$f" ]] || { log "skip $f: missing or empty"; return 1; }
    tmp="$(mktemp -d "${TMPDIR:-/tmp}/capan.XXXXXX")"
    TMPDIRS+=("$tmp")

    gapopt="noise=${GAP_NOISE}:d=${GAP_MIN_S}"
    [[ "$GAP_MONO" == 1 ]] && gapopt+=":mono=1"

    log "analysing $f"
    # 1) gaps on the untouched audio
    ffmpeg -nostdin -hide_banner -nostats -i "$f" -map 0:a:0 -vn \
        -af "silencedetect=${gapopt}" -f null - 2>"$tmp/gaps.txt" || { log "gap pass failed for $f"; return 1; }
    # 2) beep onsets
    ffmpeg -nostdin -hide_banner -nostats -i "$f" -map 0:a:0 -vn \
        -af "${BEEP_FILTER},silencedetect=noise=${BEEP_THRESH}:d=0.3" -f null - 2>"$tmp/beeps.txt" \
        || { log "beep pass failed for $f"; return 1; }
    # 3) flash luma per frame
    ffmpeg -nostdin -hide_banner -nostats -i "$f" -map 0:v:0 -an \
        -vf "crop=${FLASH_CROP},signalstats,metadata=mode=print:key=lavfi.signalstats.YAVG:file=$tmp/luma.txt" \
        -f null - 2>"$tmp/video.txt" || { log "video pass failed for $f"; return 1; }

    python3 "$BENCH_DIR/analyse_capture.py" \
        --gaps "$tmp/gaps.txt" --beeps "$tmp/beeps.txt" --luma "$tmp/luma.txt" \
        --flash-yavg "$FLASH_YAVG" --beep-min-ms "$BEEP_MIN_MS" --gap-skip-s "$GAP_SKIP_S" --fps "$NOMINAL_FPS" --source "$f" \
        --json "$f.analysis.json"
    if [[ "$KEEP_TMP" == 1 ]]; then log "raw outputs kept in $tmp"; fi
    return 0
}

TMPDIRS=()
cleanup() { [[ "$KEEP_TMP" == 1 ]] || { for d in "${TMPDIRS[@]}"; do rm -rf "$d"; done; }; }
trap cleanup EXIT
trap 'exit 130' INT TERM

rc=0
for f in "$@"; do
    analyse_one "$f" || rc=1
done
exit $rc

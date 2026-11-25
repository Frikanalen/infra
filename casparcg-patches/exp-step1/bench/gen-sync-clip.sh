#!/usr/bin/env bash
# Generate the 720p50 A/V sync test clip. See --help.
set -euo pipefail
BENCH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=lib.sh
source "$BENCH_DIR/lib.sh"

MEDIA_DIR="${MEDIA_DIR:-/var/lib/casparcg/media}"
CLIP_NAME="${CLIP_NAME:-BENCH_SYNC}"
CLIP_FORMAT="${CLIP_FORMAT:-mov}"          # mov (H.264 + PCM s16le) | mp4 (H.264 + AAC)
DURATION_S="${DURATION_S:-600}"            # must be a whole number of seconds to stay loopable
TONE_DBFS="${TONE_DBFS:--20}"
BEEP_DBFS="${BEEP_DBFS:--20}"
BEEP_MS="${BEEP_MS:-20}"
FLASH_FRAMES="${FLASH_FRAMES:-2}"
FONT="${FONT:-}"
CRF="${CRF:-18}"
X264_PRESET="${X264_PRESET:-medium}"
OVERWRITE="${OVERWRITE:-0}"

usage() {
cat <<EOF
Usage: $(basename "$0") [--help]

Writes \$MEDIA_DIR/\$CLIP_NAME.<mov|mp4>: 1280x720 at 50 fps, \$DURATION_S seconds,
loopable (every period is a whole number of seconds).

  Video: black, with a full-frame white flash on the first \$FLASH_FRAMES frames of
         every second; frame number, clip time and timecode burned in near the
         bottom edge (flash detection crops the top-left quadrant, away from text).
  Audio: stereo 48 kHz. 1 kHz sine at \$TONE_DBFS dBFS peak on both channels, plus
         a \$BEEP_MS ms 2 kHz sine burst at \$BEEP_DBFS dBFS added at the start of
         every second, starting exactly with the first flash frame.

Env (defaults):
  MEDIA_DIR=$MEDIA_DIR     CasparCG media folder
  CLIP_NAME=$CLIP_NAME  CLIP_FORMAT=$CLIP_FORMAT (mov = H.264+PCM, mp4 = H.264+AAC)
  DURATION_S=$DURATION_S  TONE_DBFS=$TONE_DBFS  BEEP_DBFS=$BEEP_DBFS  BEEP_MS=$BEEP_MS
  FLASH_FRAMES=$FLASH_FRAMES  CRF=$CRF  X264_PRESET=$X264_PRESET
  FONT=<path to .ttf> (default: fontconfig picks one)
  OVERWRITE=0 (1 replaces an existing clip)

Load it in CasparCG as: PLAY 1-10 $CLIP_NAME LOOP
EOF
}
[[ "${1:-}" == "-h" || "${1:-}" == "--help" ]] && { usage; exit 0; }
[[ $# -eq 0 ]] || { usage >&2; exit 2; }
have ffmpeg || die "ffmpeg not found"

[[ "$DURATION_S" =~ ^[0-9]+$ ]] || die "DURATION_S must be a whole number of seconds"
mkdir -p "$MEDIA_DIR"
out="$MEDIA_DIR/$CLIP_NAME.$CLIP_FORMAT"
[[ -e "$out" && "$OVERWRITE" != 1 ]] && die "$out exists (OVERWRITE=1 to replace)"

amp() { python3 -c "print(f'{10**($1/20):.6f}')"; }
A_TONE="$(amp "$TONE_DBFS")"
A_BEEP="$(amp "$BEEP_DBFS")"
BEEP_S="$(python3 -c "print($BEEP_MS/1000)")"

case "$CLIP_FORMAT" in
    mov) acodec=(-c:a pcm_s16le) ;;
    mp4) acodec=(-c:a aac -b:a 256k) ;;
    *) die "CLIP_FORMAT must be mov or mp4" ;;
esac

fontopt=""
[[ -n "$FONT" ]] && fontopt="fontfile='$FONT':"

# Audio: 1 kHz tone + 2 kHz burst in [k, k+BEEP_S) for every whole second k.
aexpr="${A_TONE}*sin(2*PI*1000*t)+if(lt(mod(t\\,1)\\,${BEEP_S})\\,${A_BEEP}*sin(2*PI*2000*t)\\,0)"

vf="drawbox=x=0:y=0:w=iw:h=ih:color=white:t=fill:enable='lt(mod(n\\,50)\\,${FLASH_FRAMES})'"
vf+=",drawbox=x=0:y=600:w=iw:h=120:color=black@1:t=fill"
vf+=",drawtext=${fontopt}text='frame %{frame_num}   t=%{pts\\:hms}':x=40:y=615:fontsize=40:fontcolor=white"
vf+=",drawtext=${fontopt}timecode='00\\:00\\:00\\:00':rate=50:x=40:y=665:fontsize=40:fontcolor=yellow"
vf+=",format=yuv420p"

tmp="$out.tmp.$$.$CLIP_FORMAT"
trap 'rm -f "$tmp"' EXIT

log "generating $out (${DURATION_S}s)"
ffmpeg -nostdin -hide_banner -loglevel warning -y \
    -f lavfi -i "color=c=black:s=1280x720:r=50:d=${DURATION_S}" \
    -f lavfi -i "aevalsrc=exprs='${aexpr}|${aexpr}':s=48000:c=stereo:d=${DURATION_S}" \
    -vf "$vf" \
    -c:v libx264 -preset "$X264_PRESET" -crf "$CRF" -g 50 -keyint_min 50 -sc_threshold 0 \
    -bf 0 -pix_fmt yuv420p -color_range tv -colorspace bt709 -color_primaries bt709 -color_trc bt709 \
    "${acodec[@]}" -ar 48000 -ac 2 \
    -t "$DURATION_S" -movflags +faststart \
    "$tmp"
mv -f "$tmp" "$out"
trap - EXIT
log "done: $out"

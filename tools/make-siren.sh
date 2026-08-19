#!/bin/sh
# Generates the alarm siren locally with ffmpeg. Nothing is downloaded: the sound is synthesised,
# so the repository carries a generator instead of an audio binary.
#
#   yelp  fast alternation of 700/1100 Hz  — harder to ignore, the default
#   wail  smooth 600<->1200 Hz sweep       — the classic two-tone
#
# Usage: tools/make-siren.sh [--mode yelp|wail] [--sec N] [--out PATH]
set -eu

MODE=yelp
SEC=6
OUT="${XDG_DATA_HOME:-$HOME/.local/share}/camtrap/sounds/siren.ogg"

while [ $# -gt 0 ]; do
    case "$1" in
        --mode) MODE="$2"; shift 2 ;;
        --sec)  SEC="$2";  shift 2 ;;
        --out)  OUT="$2";  shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

case "$MODE" in
    # square-ish two-tone: switches between 700 and 1100 Hz every 250 ms
    yelp) EXPR="0.7*sin(2*PI*t*(700+400*gt(mod(t,0.5),0.25)))" ;;
    # FM sweep: 900 Hz carrier, 0.5 Hz modulator, +-300 Hz deviation
    wail) EXPR="0.7*sin(2*PI*(900*t+95.5*sin(2*PI*0.5*t)))" ;;
    *) echo "unknown mode: $MODE (expected yelp or wail)" >&2; exit 2 ;;
esac

mkdir -p "$(dirname "$OUT")"
ffmpeg -y -loglevel error \
    -f lavfi -i "aevalsrc='$EXPR':s=48000:d=$SEC" \
    -ac 2 -c:a libvorbis "$OUT"

echo "$OUT"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"

#!/bin/sh
# Generates a camera-shutter click with ffmpeg. Nothing is downloaded.
#
# Two transients ~75 ms apart — mirror up, mirror down — each a burst of noise with a fast decay.
# That pattern is what makes a shutter recognisable as a shutter in any country, which is the
# whole point: it tells the person in the room they have been photographed without needing a
# single word.
#
# Usage: tools/make-shutter.sh [--out PATH] [--gap 0.075] [--decay 120]
set -eu

OUT="${XDG_DATA_HOME:-$HOME/.local/share}/camtrap/sounds/shutter.ogg"
GAP=0.075
DECAY=120

while [ $# -gt 0 ]; do
    case "$1" in
        --out)   OUT="$2";   shift 2 ;;
        --gap)   GAP="$2";   shift 2 ;;
        --decay) DECAY="$2"; shift 2 ;;
        -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

# First click: full level. Second click: slightly quieter, as on a real mirror box.
EXPR="0.95*random(0)*exp(-$DECAY*t)*lt(t,0.05)"
EXPR="$EXPR + 0.75*random(0)*exp(-$DECAY*(t-$GAP))*between(t,$GAP,$GAP+0.05)"

mkdir -p "$(dirname "$OUT")"
ffmpeg -y -loglevel error \
    -f lavfi -i "aevalsrc='$EXPR':s=48000:d=0.3" \
    -af "highpass=f=900,acompressor=threshold=0.3:ratio=4,volume=2.0,aresample=48000" \
    -ar 48000 -ac 2 -c:a libvorbis "$OUT"

echo "$OUT"
ffprobe -v error -show_entries format=duration,size -of default=noprint_wrappers=1 "$OUT"

#!/bin/sh
# Renders the stage 1 spoken warning from assets/voice/warn-<lang>.txt to an .ogg the player uses.
# espeak-ng + ffmpeg, both already present. Nothing is downloaded.
#
# Two engines. espeak-ng is a formant synthesiser: fine for English, useless for a tonal language
# — a speech recogniser trained on real Vietnamese heard its output as "Trời ơi! Mày tính là..."
# (29% word recall). The same sentence through piper's neural voice was recognised word for word.
#
# So: if a piper voice exists for the language, it is used. espeak is the fallback, and the script
# says out loud when it falls back for a tonal language.
#
# Voices live in $VOICE_DIR as <lang>.onnx + <lang>.onnx.json. Verify with tools/check-warning.py.
#
# Usage: tools/make-warning.sh --lang vi [--voice vi-vn-x-central] [--speed 150] [--out PATH]
#        tools/make-warning.sh --all            # every warn-*.txt in assets/voice/
#        tools/make-warning.sh --list-voices vi # what espeak-ng offers for a language
set -eu

REPO_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
TEXT_DIR="$REPO_DIR/assets/voice"
OUT_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/camtrap/sounds"
LANG_CODE=
VOICE=
SPEED=150
VOICE_DIR="${CAMTRAP_VOICE_DIR:-${XDG_DATA_HOME:-$HOME/.local/share}/camtrap/voices}"
PIPER_PY="${CAMTRAP_PIPER_PY:-}"
# Languages where a formant synthesiser is not good enough to be understood.
TONAL_LANGS="vi th zh yue lo my"
OUT=
ALL=0

while [ $# -gt 0 ]; do
    case "$1" in
        --lang)  LANG_CODE="$2"; shift 2 ;;
        --voice) VOICE="$2";     shift 2 ;;
        --speed) SPEED="$2";     shift 2 ;;
        --out)   OUT="$2";       shift 2 ;;
        --all)   ALL=1;          shift ;;
        --list-voices) espeak-ng --voices 2>/dev/null | awk -v l="$2" '$2 ~ "^" l {print $2, $4}'; exit 0 ;;
        -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
        *) echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

piper_available() {
    lang=$1
    [ -f "$VOICE_DIR/$lang.onnx" ] || return 1
    [ -n "$PIPER_PY" ] || PIPER_PY=$(command -v piper 2>/dev/null || true)
    [ -n "$PIPER_PY" ] || {
        for candidate in "$REPO_DIR/.venv-voice/bin/python" "$REPO_DIR/.venv/bin/python"; do
            [ -x "$candidate" ] && "$candidate" -c "import piper" 2>/dev/null && {
                PIPER_PY="$candidate -m piper"; break
            }
        done
    }
    [ -n "$PIPER_PY" ]
}

is_tonal() {
    for tonal in $TONAL_LANGS; do
        [ "$1" = "$tonal" ] && return 0
    done
    return 1
}

render() {
    lang=$1
    src="$TEXT_DIR/warn-$lang.txt"
    [ -f "$src" ] || { echo "no text for '$lang': $src" >&2; return 1; }

    # A language code is not always a voice name: 'vi' and 'th' exist, 'en' does not (it is
    # en-us, en-gb, ...). Take an exact match if there is one, otherwise the first voice whose
    # name starts with the code.
    voice=${VOICE:-}
    if [ -z "$voice" ]; then
        names=$(espeak-ng --voices 2>/dev/null | awk 'NR>1 {print $2}')
        # exact code, then the common regional defaults, then any variant of that language
        for cand in "$lang" "$lang-us" "$lang-gb"; do
            if printf '%s\n' "$names" | grep -qx "$cand"; then voice=$cand; break; fi
        done
        [ -n "$voice" ] || voice=$(printf '%s\n' "$names" \
            | awk -v l="$lang" 'index($1, l "-") == 1 {print; exit}')
    fi
    [ -n "$voice" ] || { echo "espeak-ng has no voice for '$lang'; try --list-voices $lang" >&2; return 1; }
    espeak-ng --voices 2>/dev/null | awk 'NR>1 {print $2}' | grep -qx "$voice" || {
        echo "espeak-ng has no voice '$voice'; try --list-voices $lang" >&2
        return 1
    }

    dst=${OUT:-"$OUT_DIR/warn-$lang.ogg"}
    mkdir -p "$(dirname "$dst")"
    tmp=$(mktemp -t "camtrap-warn-$lang.XXXXXX.wav")
    # shellcheck disable=SC2064
    trap "rm -f '$tmp'" EXIT INT TERM

    if piper_available "$lang"; then
        # Neural voice: the only way a tonal language comes out intelligible.
        tr -d "\n" < "$src" | $PIPER_PY -m "$VOICE_DIR/$lang.onnx" -f "$tmp" 2>/dev/null
        engine="piper"
    else
        # -s speed, quiet stdout; the text file holds exactly one sentence
        espeak-ng -v "$voice" -s "$SPEED" -w "$tmp" -f "$src"
        engine="espeak-ng"
        if is_tonal "$lang"; then
            echo "WARNING: '$lang' is tonal and no piper voice is installed in $VOICE_DIR." >&2
            echo "         espeak output for tonal languages is not understood by native" >&2
            echo "         speakers — verify with tools/check-warning.py before relying on it." >&2
        fi
    fi
    # adelay: 250 ms of silence up front, because PipeWire can clip the first syllable on a cold
    # sink. loudnorm: even level across languages. aresample + -ar: loudnorm internally runs at
    # 192 kHz and would leave the file there, four times the size for no gain on a 48 kHz sink.
    ffmpeg -y -loglevel error -i "$tmp" \
        -af "adelay=250|250,loudnorm=I=-16:TP=-1.5:LRA=11,aresample=48000" \
        -ar 48000 -ac 2 -c:a libvorbis "$dst"
    rm -f "$tmp"
    trap - EXIT INT TERM

    printf '%s  engine=%s  voice=%s  ' "$dst" "${engine:-espeak-ng}" "$voice"
    ffprobe -v error -show_entries format=duration -of csv=p=0 "$dst"
}

if [ "$ALL" -eq 1 ]; then
    found=0
    for f in "$TEXT_DIR"/warn-*.txt; do
        [ -f "$f" ] || continue
        found=1
        base=${f##*/warn-}
        render "${base%.txt}" || echo "skipped ${base%.txt}" >&2
    done
    [ "$found" -eq 1 ] || { echo "no warn-*.txt in $TEXT_DIR" >&2; exit 1; }
else
    [ -n "$LANG_CODE" ] || { echo "--lang is required (or --all)" >&2; exit 2; }
    render "$LANG_CODE"
fi

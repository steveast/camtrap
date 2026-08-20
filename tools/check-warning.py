#!/usr/bin/env python3
"""Measure whether a synthesised warning is intelligible, without a native speaker.

The idea: run the generated audio through a speech recogniser trained on real speech in that
language. If the recogniser hears the sentence we meant, a person plausibly will too. If it hears
gibberish, the file is decoration — which is the failure mode this project must not ship.

Not a substitute for a speaker's ear, but it is evidence rather than hope, and it catches the case
that matters: a tonal language mangled by a formant synthesiser.

Usage:
  tools/check-warning.py --lang vi --text "Chú ý! ..." --audio a.wav [b.wav ...] [--model small]
"""

from __future__ import annotations

import argparse
import re
import sys
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path


def normalise(text: str) -> str:
    """Lowercase, strip punctuation, keep Vietnamese/Thai diacritics as they carry meaning."""
    text = unicodedata.normalize("NFC", text.lower())
    # Dashes and quotes carry no meaning here; diacritics do, so they stay.
    text = re.sub(r"[!?.,;:\u2014\u2013\-\"'()]+", " ", text)
    return " ".join(text.split())


def similarity(expected: str, heard: str) -> float:
    return SequenceMatcher(None, normalise(expected), normalise(heard)).ratio()


def word_recall(expected: str, heard: str) -> tuple[float, list[str]]:
    """Share of expected words the recogniser produced, plus the ones it missed.

    Recall matters more than a character ratio: a listener needs the key words — camera, alarm —
    not a perfect transcript.
    """
    want = normalise(expected).split()
    got = set(normalise(heard).split())
    missed = [word for word in want if word not in got]
    hit = len(want) - len(missed)
    return (hit / len(want) if want else 0.0), missed


def transcribe(path: Path, lang: str, model_name: str) -> str:
    from faster_whisper import WhisperModel

    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, _info = model.transcribe(str(path), language=lang, beam_size=5)
    return " ".join(segment.text.strip() for segment in segments)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--lang", required=True)
    parser.add_argument("--text", required=True)
    parser.add_argument("--audio", nargs="+", required=True)
    parser.add_argument("--model", default="small")
    parser.add_argument("--pass-recall", type=float, default=0.75)
    args = parser.parse_args()

    print(f"expected: {args.text}")
    print(f"model:    faster-whisper {args.model}, language={args.lang}")
    print()

    rows = []
    for name in args.audio:
        path = Path(name)
        if not path.exists():
            print(f"{path}: missing")
            continue
        heard = transcribe(path, args.lang, args.model)
        recall, missed = word_recall(args.text, heard)
        ratio = similarity(args.text, heard)
        rows.append((path.name, recall, ratio, heard, missed))

    rows.sort(key=lambda row: row[1], reverse=True)
    for name, recall, ratio, heard, missed in rows:
        verdict = "PASS" if recall >= args.pass_recall else "fail"
        print(f"[{verdict}] {name}")
        print(f"    heard:  {heard or '(nothing)'}")
        print(f"    recall: {recall:.0%}   char-similarity: {ratio:.0%}")
        if missed:
            print(f"    missed: {', '.join(missed)}")
        print()

    best = rows[0] if rows else None
    if best and best[1] >= args.pass_recall:
        print(f"best: {best[0]} — a recogniser trained on real {args.lang} speech understood it")
        return 0
    print("none of the files cleared the bar: the warning would be decoration")
    return 1


if __name__ == "__main__":
    sys.exit(main())

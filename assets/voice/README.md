# Spoken warning texts

One file per language, `warn-<code>.txt`, rendered to `warn-<code>.ogg` by
[`../../tools/make-warning.sh`](../../tools/make-warning.sh). Generated audio is not in the
repository (`*.ogg` is ignored) — only the text is.

The warning is stage 1 of the audible response: it plays when motion is detected, before anything
touches the laptop. Stage 2, the siren, is wordless. See [SPEC.md §3.4](../../SPEC.md).

## Rules for the text

- **Facts only.** No "the police have been called", no "security is on the way", no threats, and
  nothing about frames having already been uploaded.
- **Never claim to be an authority.** A travel alarm announcing itself is ordinary; a device
  impersonating police is not.
- **Short.** Five to eight seconds spoken.
- **Local language first, English second.** English alone is a coin flip with hotel housekeeping.

## Intelligibility is measured, not assumed

No native speaker was available, so the check is machine-run: synthesise the file, then feed it to
a speech recogniser trained on real speech in that language
([`tools/check-warning.py`](../../tools/check-warning.py), faster-whisper). If the recogniser hears
the sentence we meant, a person plausibly will. If it hears gibberish, the file is decoration.

Measured 2026-08-20, faster-whisper `small`, word recall against the intended sentence:

| Language | Engine | Recall | Verdict |
|---|---|---|---|
| `vi` | **piper** neural voice (`vi_VN-vais1000-medium`) | **100 %** | shipped |
| `vi` | espeak-ng | 29 % | rejected — heard as *"Trời ơi! Mày tính là ủy sát vệ thân…"* |
| `en` | espeak-ng | 100 % | shipped |
| `th` | espeak-ng | **0 %** | rejected — heard as *"โอปราบาท ฮาราส ฮาราส…"* |

Two conclusions worth keeping:

1. **A formant synthesiser cannot speak a tonal language.** espeak-ng renders Vietnamese as
   something a recogniser reads as a different sentence entirely, and Thai as nothing at all. For
   `vi` this is solved by a piper voice; `make-warning.sh` uses it automatically when the model is
   present in `~/.local/share/camtrap/voices/` and warns loudly when it has to fall back.
2. **Thai is currently unshippable.** piper has no Thai voice, so there is no way to produce an
   intelligible Thai warning offline. It is left out of the generated set rather than shipped as
   decoration. For a trip to Thailand, either record a human saying the line or render it once
   through an online neural TTS — the player only reads a local `.ogg` and does not care how it was
   made.

## Re-checking after any change

```sh
tools/make-warning.sh --lang vi
tools/check-warning.py --lang vi --text "$(cat assets/voice/warn-vi.txt)" \
    --audio ~/.local/share/camtrap/sounds/warn-vi.ogg
```

The check runs against the final `.ogg`, after `adelay`/`loudnorm`/resampling, because the point is
to verify what actually plays in the room.

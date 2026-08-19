# Spoken warning texts

One file per language, `warn-<code>.txt`, rendered to `warn-<code>.ogg` by
[`../../tools/make-warning.sh`](../../tools/make-warning.sh). The generated audio is not in the
repository (`*.ogg` is ignored) — only the text is.

The warning is stage 1 of the audible response: it plays when motion is detected, before anything
touches the laptop. Stage 2, the siren, is wordless. See [SPEC.md §3.4](../../SPEC.md).

## Rules for the text

- **Facts only.** No "the police have been called", no "security is on the way", no threats, and
  nothing about frames having already been uploaded. State that the laptop is alarmed and watched;
  let the listener draw the conclusion.
- **Never claim to be an authority.** A travel alarm announcing itself is ordinary; a device
  impersonating police is not.
- **Short.** Five to eight seconds spoken. The current renders are 4.3 s (`vi`) and ~7 s (`th`).
- **Local language first, English second.** English alone is a coin flip with hotel housekeeping.

## Intelligibility must be verified, not assumed

`espeak-ng` is a formant synthesiser and both Vietnamese and Thai are tonal. The phoneme output
shows tone marks being applied, but nobody on this project can judge by ear whether a native
speaker actually understands the result. **Before the trip, each file must either be checked by a
speaker of that language or replaced with a better recording** — a neural TTS (`piper` with a
`vi`/`th` voice) or a one-off render from an online service.

Replacing the audio costs nothing architecturally: the player only reads a local `.ogg`, so the
offline guarantee holds regardless of how the file was produced. A warning nobody understands is
decoration.

Regional note: for southern and central Vietnam, `vi-vn-x-central` or `vi-vn-x-south` are closer
than the default northern voice. `tools/make-warning.sh --voice` selects one.

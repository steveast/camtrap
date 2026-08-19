# camtrap

A camera trap running on a laptop, for a hotel room you have to leave your things in.

**Sound is the point, and it escalates in two stages.**

- **Someone is in the room** → a spoken warning, in the local language and then English:
  *"Attention. This laptop is protected by an alarm and a camera."* Most visits end there, without
  an incident.
- **The laptop is picked up** — cable pulled, lid closed, case lifted → a police siren at full
  volume, which cannot be silenced from the keyboard. Carrying a screaming laptop out of a hotel
  room is not something people go through with.

The camera side documents what happened; sound is what changes what happens. Keeping the siren for
the case that deserves it is what keeps the siren believable.

Alongside that it works as a recorder: motion detection through the built-in webcam, a snapshot
every ~5 s while motion lasts, immediate off-box upload, and a Telegram alert with the photo and
an exact timestamp — the kind of evidence that turns "I think something is missing" into a
concrete conversation with hotel management and a usable police report.

It is not a lock. It does not record video, never records audio, recognises no faces, and keeps
no database of people.

**Status: specification, no code yet.** Everything substantive is in [SPEC.md](SPEC.md); the
hotel-theft data the requirements grew from is in
[docs/threat-model.md](docs/threat-model.md).

## How it fits together

```
laptop ──ssh forced-cmd──▶ VPS ◀──cron──── Pi at home ──▶ Telegram
   │     frames, heartbeat  inbox, state   token lives only here
   ├──cp──▶ ~/MEGA/camtrap  (backup receiver, no alerts)
   ├──motion in frame ────────────▶ 🔊 spoken warning, local language then English
   └──power / lid / frame shift ──▶ 🔊 police siren (local, no network involved)
```

Four decisions everything else follows from:

- **Sound is decided locally.** No inbound channel to the laptop, no waiting on a poll: a
  reaction two minutes late is worthless. This is the only part that still works when
  connectivity is completely dead.
- **Evidence first, noise second.** The first frame of an event jumps the upload queue, and sound
  starts once delivery is acknowledged — or after 3 seconds, whichever comes first.
- **The laptop's disk is assumed to be in the thief's hands.** Hence a write-only forced-command
  key to the VPS, and no Telegram token on the laptop at all: it lives only on a Raspberry Pi at
  home, which polls the VPS and does the sending.
- **A false siren is worse than a missed one.** Warm-up, an ignore mask, lighting-change
  suppression, a grace window after a session unlock: an alarm that goes off on its own teaches
  you to ignore it, and then the trap is worthless. Note the asymmetry — a warning during
  housekeeping is expected behaviour, a *siren* at a cleaner is a bug.

## Warning languages

`WARN_LANGS` is an ordered list — `["vi", "en"]` in Vietnam, `["th", "en"]` in Thailand — and the
files play local language first, because English alone is a coin flip with hotel housekeeping.
Texts live in [assets/voice/](assets/voice/) and render through
[tools/make-warning.sh](tools/make-warning.sh) (`espeak-ng` + `ffmpeg`, both offline).

One caveat worth repeating: `espeak-ng` is a formant synthesiser and Vietnamese and Thai are tonal,
so **every file must be checked by a native speaker or replaced with a better recording before it
is relied on**. The player reads a local `.ogg` and does not care how it was made, so swapping in a
neural-TTS or human recording costs nothing. A warning nobody understands is decoration.

## Keeping the sound on

A siren a mute key can silence is not a siren. On the target machine mute, volume and power keys
all come from the same built-in keyboard as the letters, so grabbing "just the media keys" is
impossible — and grabbing the whole keyboard would lock the owner out of typing their own unlock
password. So instead: the audio path is re-asserted every 250 ms while the siren plays (unmute,
volume, `Speaker` profile, auto-mute off), the session is locked so the keyboard cannot reach the
process, and inhibitors keep a power-key press or a closed lid from stopping the machine.
Details, including what stays possible in hardware, are in [SPEC.md §3.4](SPEC.md).

## What detects "picked up"

The target machine has no accelerometer, so the signal is composite: power cable
(`/sys/class/power_supply/*/online`), lid (`/proc/acpi/button/lid/*/state`), global scene shift
between frames (`cv2.phaseCorrelate`), and camera disappearance. The ambient light sensor acts as
an arbiter between "the light was switched on" and "the case was lifted" — both change nearly the
whole frame. External USB accelerometers are discussed in [SPEC.md §3.3](SPEC.md).

## Requirements

Linux with systemd (a `--user` unit), Python 3.12, `opencv-python-headless`, PipeWire for audio,
a V4L2 camera, and ssh access to a receiver. System tools: `pw-play`, `pactl`, `loginctl`,
`systemd-inhibit`, plus `ffmpeg` to generate the siren.

```sh
tools/make-siren.sh --mode yelp    # writes ~/.local/share/camtrap/sounds/siren.ogg
tools/make-warning.sh --all        # writes warn-vi.ogg, warn-th.ogg, warn-en.ogg
tools/make-warning.sh --lang vi --voice vi-vn-x-south   # southern Vietnamese instead of northern
```

## Repository discipline

The repository is the source of truth; everything reaches the boxes through `deploy/` only, and
nothing is edited by hand on a box. Deployment specifics — box addresses, key names, dates — are
deliberately not in here: the spec is written impersonally (`user@vps`, `Pi at home`).

## Legal frame

Filming your own paid hotel room to protect your belongings is generally permissible, but frames
with people in them are material for the police and hotel management, not for publication — many
jurisdictions grant a right to one's own image. An alarm sound is the same category as a car
alarm; it must never impersonate an authority, which is why the siren carries no voice claiming
to be the police.

## License

[MIT](LICENSE).

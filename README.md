# camtrap

A camera trap running on a laptop, for a hotel room you have to leave your things in.

**Sound is the point, and it is spent narrowly.**

- **The cable is pulled or the lid is closed** → a camera-shutter click, and then a police siren
  at full volume which cannot be silenced from the keyboard. The click needs no language: it says
  a picture was just taken. Carrying a screaming laptop out of a hotel room is not something
  people go through with.
- **Anything else** — someone in the room, the case lifted, the power button pressed, the camera
  unplugged → photographed, uploaded and alerted, with **no alarm**. What it does make is a
  camera-shutter click, once per frame taken, which at the shipped cadence is one click every
  30 s for as long as someone is in frame. It says "you are being photographed" in no language
  and is over in a third of a second.

That split is deliberate and was narrowed after the first night of real use: the person most
likely to set a camera trap off is its owner, walking back in and picking their own laptop up. A
trap that shouts at you at the door every evening is a trap you stop arming, and an unarmed trap
protects nothing. Frames cost nothing and are always taken; noise is spent only where the signal
cannot plausibly be you. The click is what remains of the spoken warning, and deliberately so: a
voice argues with the person in the room, a shutter only reports a fact. All of it is one config
line away — the warning is built, tested and shipped, just switched off (`sound.warn_on_motion`,
`sound.shutter_on_capture`, `tamper.siren_signals`).

Alongside that it works as a recorder: motion detection through the built-in webcam, a snapshot
at once and then every 30 s for as long as the visit lasts, immediate off-box upload, and a
Telegram alert with the photo and
an exact timestamp — the kind of evidence that turns "I think something is missing" into a
concrete conversation with hotel management and a usable police report.

It is not a lock. It does not record video, never records audio, recognises no faces, and keeps
no database of people.

**Status: phase 1 implemented.** The agent, the receiver scripts and the poller are in place
with 185 tests; what remains is physical — a siren heard in a real room, a native speaker
confirming the warning, and a 24-hour run in an empty one. See [tasks/todo.md](tasks/todo.md).

The design and its reasoning are in [SPEC.md](SPEC.md), the hotel-theft data the requirements grew
from is in [docs/threat-model.md](docs/threat-model.md), and what to do on arrival — and when it
fires — is in [docs/runbook.md](docs/runbook.md).

```sh
deploy/install-laptop.sh     # venv, sounds, systemd --user unit
deploy/install-guard.sh      # the `guard` launcher, onto PATH
guard check                  # camera, sounds, a real quiet burst through the speakers
guard test                   # the siren and the warning, at full volume
```

## Leaving the room

```sh
guard            # preflight, then arm once the room has been still for 30 s
guard off        # back for good: stop, and mark the offline as expected
```

`guard` is started by hand, on the way out. It refuses to arm if the camera, the sound files or
the speakers fail their check — walking away believing in a trap that cannot shout is worse than
knowing it is broken. Then it waits for the room to go quiet rather than counting down a fixed
delay: take what you need, leave, and it arms behind you. Coming back, unlock the screen — that
disarms it and opens a grace window, so picking the laptop up does not set off a siren.

**Where everything lives** — which machine runs what, every path, and the daily cycle:
[docs/deployment.md](docs/deployment.md).

## How it fits together

```
laptop ──ssh forced-cmd──▶ VPS ◀──cron──── Pi at home ──▶ Telegram
   │     frames, heartbeat  inbox, state   token lives only here
   ├──cp──▶ ~/MEGA/camtrap  (backup warehouse, recompressed, no alerts)
   ├──power pulled / lid closed ──▶ 🔊 police siren (local, no network involved)
   └──motion / lift / power key ──▶ 📸 shutter click per frame, an alert, no alarm
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
  you to ignore it, and then the trap is worthless. Taken to its conclusion, this is why the
  siren is now left to the cable and the lid alone: a *siren* at a cleaner is a bug, and so is one
  at the owner coming home.

## Warning languages

`WARN_LANGS` is an ordered list — `["vi", "en"]` in Vietnam, `["th", "en"]` in Thailand — and the
files play local language first, because English alone is a coin flip with hotel housekeeping.
Texts live in [assets/voice/](assets/voice/) and render through
[tools/make-warning.sh](tools/make-warning.sh) (`espeak-ng` + `ffmpeg`, both offline).

Intelligibility is measured rather than hoped for: each file is fed to a speech recogniser trained
on real speech in that language ([tools/check-warning.py](tools/check-warning.py)). Vietnamese
through a piper neural voice comes back recognised word for word; the same sentence through
`espeak-ng` comes back as a *different sentence* (29 % word recall), and Thai comes back as nothing
at all. So a formant synthesiser cannot speak a tonal language — `vi` ships with a neural voice,
`en` with espeak, and Thai ships not at all until there is a voice that can say it. Details and
numbers: [assets/voice/README.md](assets/voice/README.md).

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

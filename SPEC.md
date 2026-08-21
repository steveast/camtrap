# camtrap — specification

A camera trap running on a laptop: motion detection through the built-in webcam, a snapshot
every ~5 s while motion lasts, immediate off-box upload, and a Telegram alert with the photo.
Sound escalates in two stages — a spoken warning in the local language when someone is in the
room, a police siren when the laptop itself is picked up.

Status: phase 1 implemented (see `tasks/todo.md` for what is left, all of it physical).
Specification date: 2026-08-19.

---

## 1. Purpose

### The task

The laptop is left unattended in a hotel room for several nights. Two things need to be known:
**that someone entered while the owner was away**, with a **timestamped photo of them** solid
enough for a conversation with hotel management and for a police report; and **that someone
picked the laptop up**, which is a different situation entirely — once the case is in someone's
hands, a minute later there is neither laptop nor connectivity.

### Why this way

By the collected evidence (see `docs/threat-model.md`) 60–70 % of hotel thefts are committed by
staff, who already hold a master key and access to the in-room safe. Hotels pull camera footage
only against a filed report, and in many jurisdictions a report without evidence never becomes
a case file. Your own timestamped frame is what turns "I think something of mine is missing"
into a concrete conversation.

### What this project does NOT do

- It does not prevent theft. This is a recorder, not a lock. What it does actively is make noise:
  a spoken warning when someone is in the room, a siren when the laptop is picked up. Neither
  stops anyone from walking out with it; they only make walking out loud.
- It does not record video and it does not record audio. It plays sound; it never captures it.
- It does not recognise faces and keeps no database of people.
- It is not a "find my laptop" service — geolocation is handled by separately installed
  software.

### Sound is the primary feature

This is the owner's ranking, and the build order follows it: a loud siren is what makes carrying
the laptop out of the room — let alone out of the hotel — a thing nobody wants to attempt. Frames
document what happened; sound is the only part that changes what happens. It therefore gets
the strictest requirements in this document: it must be audible, it must not be silenceable from
the keyboard, and it must never fire when the owner is the one holding the laptop.

Sound escalates rather than firing at one level for everything. Someone who has just walked in
gets a spoken notice in the local language — the room is watched, that is all. Someone who has
picked the laptop up gets the siren. The order matters in both directions: the voice ends most
visits without an incident, and keeping the siren for the case that deserves it is what keeps
the siren believable.

What it does not do, stated plainly so that nobody is surprised later: a siren does not make
removal physically impossible. A laptop can be muffled under a pillow — built-in speakers are
weak — carried out while still sounding until the burst limits run out, or shut down by holding
the power button for several seconds, which is a hardware path the kernel never sees. The layers
in 3.4 close every software route to silencing it; the physical ones stay open. That is still a
good trade: the entire value is in making a quiet, unremarkable exit impossible, and a thief who
needs both hands on a screaming laptop in a hotel corridor is a thief with a problem.

### Success criteria

1. The lid is closed or the power cable is pulled → the siren sounds within 3 s, at full volume,
   and cannot be silenced from the keyboard; a separate 🚨 "the laptop is being handled" alert
   goes out, not mixed in with ordinary motion.
2. Someone is in the room → the spoken warning plays in the local language, then in English,
   within 3 s of the motion being confirmed.
3. A person enters the room → a photo arrives in Telegram within 6 minutes.
4. The laptop is taken away or shut down → a "the agent went silent" alert arrives with an
   exact cut-off time.
5. No frame of an event is lost to a Wi-Fi outage: it is delivered as soon as the link is back.
6. Over a full day in an **empty** room: zero alerts, zero warnings, zero sirens.

Criterion 6 carries the same weight as the rest: nobody keeps trusting alarms that clear up on
their own, and at that point the whole thing is pointless. Note what it does and does not say —
an empty room must stay silent, but a room with a cleaner in it is *supposed* to produce a spoken
warning. That is the design, not a defect. What must never happen is the siren firing at a
cleaner: the voice is a notice, the siren is an accusation, and mixing them up costs the trust
the whole system runs on.

---

## 2. Architecture

The governing constraint: **the laptop's disk is not encrypted**, so anything stored on it is
assumed to be in the thief's hands. The entire key layout follows from that.

```
  hotel room                          receiver (VPS, abroad)          home
┌────────────────────┐              ┌──────────────────────┐      ┌──────────────────┐
│ laptop             │  ssh         │  user@vps            │ ssh  │ Pi at home       │
│  /dev/video0       │  forced-cmd  │                      │◀─────│ cron */2 min     │
│  detector          │─────────────▶│ camtrap-recv.sh      │ pull │ camtrap-poll.sh  │
│  tamper: AC / lid  │  put-frame   │  ~/camtrap/inbox/    │      │                  │
│  player ──▶ siren  │  heartbeat   │  ~/camtrap/state/    │      │ TELEGRAM_TOKEN   │
│  spool             │              │                      │      │ (lives only here)│
│  uploader ─┐       │              │ camtrap-tg.sh        │◀─────│ send-photo       │
└────────────┼───────┘              └──────────┬───────────┘      └──────────────────┘
             │ cp                              │ curl api.telegram.org (sendPhoto)
             ▼                                 ▼
    ~/MEGA/camtrap/                    bot → owner's private chat
    sync client → cloud
    (backup receiver)
```

### Why this and not something simpler

**Why not straight from the laptop to Telegram.** A bot token on the unencrypted disk of a
stolen laptop hands someone else the alerting channel. On top of that `api.telegram.org` is
blocked on the owner's home network, so testing and debugging at home would be impossible.

**Why the Pi polls the receiver instead of the receiver pushing.** The token is stored only on
the Pi. That is the standing principle of the owner's existing external prober for another
project, and breaking it for the sake of one more project is the wrong trade. The receiver gets
the token from the Pi at send time and never stores it.

**Why the photo does not travel through the Pi.** The frame is already physically on the
receiver. The Pi only says "send file `<id>` with caption `<text>`", and the receiver curls
`sendPhoto` as multipart from its local file. The picture crosses the network once, not three
times.

**Why sound is played by the laptop itself and not commanded from Telegram.** There is no
inbound channel to the laptop and there will not be one: the key is write-only, the laptop
accepts no incoming connections, and the Pi learns about an event a minute or two later.
A reaction two minutes late is worthless — by then the case is in a bag. So the decision to
play is made locally, from local signals, with no network involved. This is the only part of
the system that still works when connectivity is completely dead.

**Why evidence first, siren second.** A siren tells whoever is in the room that something is
watching, which is a reason to leave fast — but also a reason to take the laptop along. So the
first frame of a tamper event goes to the head of the queue, and the siren starts once delivery
is acknowledged, or after `SOUND_DELAY_MAX_SEC` at the latest, because waiting longer buys
nothing. Evidence safe first, noise second.

**Why there are two receivers.** The VPS is primary and the only one whose delivery can be
confirmed. The cloud folder is a fallback for exactly one scenario: the VPS is unreachable
while frames are already being taken. A copy in `~/MEGA/camtrap/` leaves for the cloud on its
own, the sync client is already running, and no new infrastructure is needed. No alerts are
sent over it — it is a warehouse, not a channel.

**The cloud fallback has a weakness that has to be named.** The account is logged in on the
laptop itself and the sync is bidirectional: whoever gets the session can delete the cloud
copy, and deletions on disk propagate upward by design. That is why it can never be the primary
receiver or the source of truth — the VPS stays both, and the laptop can only write to it.
The cloud provider's trash bin covers part of this, but a specification cannot lean on it.

**The price of this design is latency.** The alert arrives within the Pi's polling interval
(2 minutes), not instantly. For "record the entry" that is immaterial: there is nothing to
respond with anyway, and the value is in the record. For "the laptop is being handled" the
siren is the immediate response, and it needs no network at all.

### The prerequisite without which nothing works

`HandleLidSwitch` on the target system defaults to `suspend`, and the decision is in practice
taken by the desktop's power manager, which holds a block inhibitor on `handle-lid-switch`.
A closed lid then means sleep: dead camera, dead upload, dead sound — and the Pi reports it as
"went silent" five minutes later, when it is all over. So the laptop is left in the room **with
the lid open**, and `camtrap run` holds
`systemd-inhibit --what=sleep:idle:handle-lid-switch` for as long as it runs. A screen that
blanks is irrelevant to the trap, and so is a locked session: PipeWire in the user session
plays either way.

It follows that closing the lid is not a routine event but a tamper signal — the owner did not
close it.

### Known blind spot

If the receiver is dead altogether, neither frames nor alerts are delivered — the same
limitation the owner's existing prober has, for the same reason (the relay goes through it).
A deliberate trade-off. Frames are not lost in that case: they accumulate in the local spool
and leave once the link is back, the cloud copy goes up independently of the receiver's state,
and the siren does not depend on the network at all.

---

## 3. Components

| Where | Component | What it does |
|---|---|---|
| laptop | `camtrap run` | detection, frame slicing, spool, heartbeat, upload |
| laptop | tamper monitor | power, lid, case movement, ALS — "the laptop is being handled" |
| laptop | `player` | plays the siren into the built-in speakers, cooldown and limits |
| laptop | sink `mega` | copies frames into `~/MEGA/camtrap/` for the sync client to pick up |
| VPS | `camtrap-recv.sh` | forced-command receiver: puts the frame in the inbox, updates heartbeat |
| VPS | `camtrap-tg.sh` | forced command for the Pi: `sendPhoto`/`sendMessage`, token from stdin |
| Pi | `camtrap-poll.sh` | cron: collects new events, sends them to Telegram, watches for silence |

### 3.1 Detector (laptop)

- Capture from `/dev/video0` through OpenCV V4L2, **1920×1080** at JPEG quality 95, 5 fps.
  Measured on the target camera: 1080p runs at the same rate as 720p (8.3 fps either way — the
  limit is exposure in room light, not resolution), and the frame may end up in front of an
  investigator looking at a face. 283 KB per frame.
- MOG2 background subtraction on greyscale with Gaussian blur; motion means the share of
  changed pixels stays above `MIN_AREA_PCT` for `MIN_MOTION_FRAMES` consecutive frames.
- **Warm-up** `WARMUP_SEC = 20`: the background adapts and no events are produced. The same
  idea as a warm-up flag in a monitoring prober — without it every start flaps.
- **Ignore mask**: polygons from the config (window, curtain, mirror) are excluded from
  analysis.
- **Lighting-change suppression**: if more than `GLOBAL_CHANGE_PCT` (70 %) of the frame changed,
  someone switched the light on or off rather than moved. This produces a `light` event, exactly
  one frame, no series. The event is valuable — someone came in — but it must not flood the
  channel. Light is not the only reason the whole frame changes; the other one is the laptop
  being lifted. Telling those apart is described in 3.3, and without it either cause would be
  reported as the other.

### 3.2 Event

An event is a series of frames from one intrusion. Its type is `motion` (movement in frame),
`light` (lighting change) or `tamper` (the laptop is being handled, see 3.3). The type decides
queue priority, the shape of the alert, and which sound plays — `motion` triggers the spoken
warning, `tamper` the siren, `light` nothing by default (3.4). Everything else is shared.

- First frame immediately, then one every `SNAPSHOT_INTERVAL = 5 s` while motion holds.
- **Pre-buffer**: a ring buffer of `PREBUFFER_FRAMES = 5` frames (1/s) kept from before the
  trigger. A detector by definition wakes up after motion has begun, and without the buffer the
  first frame is a back in the doorway.
- The event closes after `EVENT_GAP = 30 s` without motion.
- `MAX_FRAMES_PER_EVENT = 60`. On truncation: a log line and `truncated: true` in the manifest.
  Silent truncation is unacceptable — it reads as "everything was captured" when it was not.
- Artefacts: `evt_<utc_ts>_<nnn>.jpg` plus a manifest `evt_<utc_ts>.json` (start, end, frame
  count, type, agent version, `truncated`).

### 3.3 Tamper: the laptop is being handled (laptop)

**The target machine has no accelerometer** — verified: both `iio:device*` entries are ALS
(ambient light sensors), and there is no `hdaps` and no `/dev/freefall`. "Picked up" cannot be
caught from a single sensor, so the signal is composite: several cheap independent sources, each
of which already means interference on its own.

| Signal | Source | What it means | Latency |
|---|---|---|---|
| power cable pulled | `/sys/class/power_supply/<mains>/online` 1→0 | the prelude to walking out | ≤ `TAMPER_POLL_SEC` |
| lid closed | `/proc/acpi/button/lid/<id>/state` | being prepared for transport | ≤ `TAMPER_POLL_SEC` |
| case moved | global scene shift between frames | the laptop was lifted or turned | 1 frame |
| camera gone | `/dev/video0` gone or yields no frames | the device or its cable was touched | 1 frame |
| illuminance | `in_illuminance_raw` of both ALS | not a trigger but an arbiter (below) | ≤ `TAMPER_POLL_SEC` |

The first two are the ones the owner named explicitly, and they are also the least ambiguous:
in an empty room a cable does not unplug itself and a lid does not close itself.

USB-C charging is exposed separately from the barrel jack (`ucsi-source-psy-*/online`), and on
the target machine there are two such ports — both need watching, or a cable pulled from the
second one goes unnoticed.

Sysfs is polled every `TAMPER_POLL_SEC = 1 s` on the same thread as the heartbeat: the files
are cheap and pull in no dependencies. All paths come from the config — tests point them at a
directory of plain files, and a machine with a different sysfs layout is handled by config
rather than code.

**External USB accelerometer — a workable option, but not for phase 1.** Such devices exist and
are cheap: ready-made USB IMUs appear as `/dev/ttyUSB0` through a CH340 bridge and report
acceleration over a documented protocol; cheaper still is a microcontroller (ESP32 or RP2040)
with an MPU-6050, where the threshold is computed on the device and the host reads a plain
"moving / not moving" from `/dev/ttyACM0`, so the logic does not depend on Python on the laptop.
A separate class is game controllers: `hid-playstation` and `hid-nintendo` expose DualSense and
Joy-Con accelerometers out of the box, so a suitable device may already be lying around at
home. Bluetooth variants are unsuitable: latency, battery drain, dropped links.

What it adds on top of the composite: a lift is caught before the cable is pulled, and in total
darkness where the camera is useless. What it costs: a physical module next to the laptop, one
more failure point (`ttyUSB` drops), and a purchase that does not fit the phase 1 schedule if
the device is not already on hand. Tamper is therefore specified so that signal sources plug in
as a list: an external sensor becomes one more source instead of a rewrite. On the owner's
machine there is currently nothing of the kind — `lsusb` shows only a mouse, keyboard, webcam,
USB audio, touchpad and Bluetooth, and `/dev/ttyUSB*` and `/dev/ttyACM*` are empty.

`/dev/input/*` is out of scope for phase 1, even though access exists (the owner is in the
`input` group): among this machine's input devices is a virtual one created by the owner's own
automation, which means the owner's own tooling generates input events, and separating those
from someone else's hands is work that does not belong in phase 1. Moved to phase 2.

**Case movement versus the light being switched on.** Both change nearly the whole frame. They
are separated like this: a lighting change preserves scene geometry — the `cv2.phaseCorrelate`
peak stays at zero and only brightness moves; a case shift produces a vector longer than
`MOVE_SHIFT_PX`. If the laptop was lifted abruptly the frame is smeared and the correlation
degenerates: a low-confidence peak combined with a fully changed frame also counts as a shift,
because the scene became unrecognisable and light does not do that. The arbiter is the ALS:
a synchronous jump in `in_illuminance_raw` confirms light, a steady ALS with a changed frame
confirms case movement.

Tamper produces an event of type `tamper`: a series of frames like any other event, but with
higher priority in the upload queue, its own alert, and — the only case in the system — an
audible response. The list of signals that fired goes into the manifest: "cable pulled" and
"case lifted" are different stories, and they will have to be reconstructed from the manifest
rather than from memory.

### 3.4 Audible response: warning, then siren (laptop)

Sound comes in **two stages**, and the difference between them is the whole design:

| Stage | Trigger | Sound | Volume | Extra measures |
|---|---|---|---|---|
| 1 — warning | `motion` (someone is in the room) | spoken notice, in the local language then English | `WARN_VOLUME_PCT = 85` | none |
| 2 — alarm | `tamper` (the laptop is being handled) | two-tone police siren | `SOUND_VOLUME_PCT = 100` | session lock, input hold |

**Why two stages rather than one.** They address different moments. Someone who has just walked
in has not touched anything yet, and the cheapest way to end the visit is to tell them the room
is watched — a voice does that, in words they understand, without turning the corridor into an
incident. Someone who has already picked the laptop up is past persuasion, and there the siren
earns its keep. Escalating in that order also means the loud option stays credible: a siren that
fires at every cleaner is a siren nobody believes by day three.

**Stage 1 — the spoken warning.**

- Text: *"Attention. This laptop is protected by an alarm and a camera."* Nothing more — no
  threats, no claims about the police, no mention of frames having already been sent. It states a
  fact and lets the person draw their own conclusion.
- **Language follows the country**, set as an ordered list in the config —
  `WARN_LANGS = ["vi", "en"]` for Vietnam, `["th", "en"]` for Thailand. Files are played in
  order, local language first, English second, because English alone is a coin flip with hotel
  housekeeping. `camtrap warn-make --lang <code>` builds each file; `camtrap setup` suggests a
  default from the system time zone (`Asia/Ho_Chi_Minh` → `vi`, `Asia/Bangkok` → `th`) but never
  decides on its own — a wrong guess here means a warning nobody in the room understands.
- Generated by `tools/make-warning.sh` from `assets/voice/warn-<lang>.txt` through `espeak-ng`
  plus `ffmpeg`. Verified available on the target machine: `vi` (Northern), `vi-vn-x-central`,
  `vi-vn-x-south` and `th`; sample renders are 4.3 s for Vietnamese and 8.4 s for Thai.
- **Intelligibility is measured, not assumed.** No native speaker was available, so the check is
  machine-run: synthesise the file, then feed it to a speech recogniser trained on real speech in
  that language (`tools/check-warning.py`, faster-whisper). If the recogniser hears the sentence
  we meant, a person plausibly will; if it hears gibberish, the file is decoration. Measured
  2026-08-20 by word recall against the intended sentence: **`vi` through a piper neural voice
  100 %, `vi` through espeak-ng 29 %** (heard as an entirely different sentence), **`en` through
  espeak-ng 100 %**, **`th` through espeak-ng 0 %**. So a formant synthesiser cannot speak a tonal
  language, and `make-warning.sh` uses a piper voice whenever one is installed, warning loudly
  when it has to fall back. Thai is left out of the shipped set: piper has no Thai voice, and
  shipping an unintelligible file is worse than shipping none. None of this weakens the offline
  guarantee — the file is prepared once at home, and in the room only a local `.ogg` is played.
- Limits: one warning per event, `WARN_COOLDOWN_SEC = 120`, `WARN_MAX_PER_HOUR = 10`.
- **Stage 1 takes no hostile measures**: no session lock, no input grab, no forced power
  inhibition beyond what `camtrap run` already holds. It is a notice, not a confrontation.
- **Expect it to fire during housekeeping, every single day.** That is correct behaviour, not a
  false positive: someone is in the room. What it means in practice is that hotel staff will hear
  a voice from the laptop while cleaning, and the front desk may ask about it — an ordinary
  conversation about a travel alarm, but one to be ready for rather than surprised by. A "do not
  disturb" sign for the days the room is left armed removes most of it.

**Stage 2 — the siren.** Fires on `tamper` only, never on `motion` or `light`.

- **The sound is a two-tone police siren**, generated locally by `tools/make-siren.sh` through
  `ffmpeg` — nothing is downloaded. Two modes: `yelp`, a fast alternation of 700 and 1100 Hz
  (default, harder to ignore), and `wail`, a smooth 600↔1200 Hz sweep. Verified on the target
  machine: 6 s, ~20 KB, Vorbis 48 kHz. The file lives at
  `~/.local/share/camtrap/sounds/siren.ogg`, path in the config, and any other recording can be
  dropped in instead — the player does not care where the audio came from.
- If a `tamper` signal arrives while a stage 1 warning is still playing, the warning is cut off
  mid-sentence and the siren takes over. The queue never delays the alarm.
- **The sink is set explicitly** — `SOUND_SINK` in the config, by default the built-in speakers
  of the internal audio card (`Speaker` port of its `HiFi` profile). "Play to the default sink"
  is a bug: on the target machine the default is currently a USB audio dongle and the card's
  active profile is `Headphones`, so the siren would go to a device that will not be in the
  hotel room, or into headphones lying on the desk. Before playing, the agent switches to a
  profile that has a `Speaker` port, clears mute, and sets `SOUND_VOLUME_PCT` (100). Previous
  values go to the log.
- **The player** is an external `pw-play` with a timeout; no audio bindings are added to the
  dependencies. Everything the agent does with sound can be reproduced by hand from a shell —
  that is what makes it fixable in a hotel room.
- **Siren limits**: `SIREN_SEC = 6` per burst, `SOUND_COOLDOWN_SEC = 60`,
  `SOUND_MAX_PER_EVENT = 3`, `SOUND_MAX_PER_HOUR = 10`.

**Rules that apply to both stages.**

- `SOUND_DELAY_MAX_SEC = 3` — how long to wait for the event's first frame to be acknowledged
  before playing anyway. Evidence first, sound second, for the voice as much as for the siren:
  both tell the person in the room that something is watching.
- Neither plays during warm-up, in `paused` mode, or inside the window after a session unlock
  (3.8). Arming (3.8) gates both.
- **Audio readiness is checked up front and continuously**: `camtrap siren-test` and
  `camtrap warn-test` play the real files on the real speakers, and the heartbeat carries
  `sound_ok` — sink present, mute cleared, siren file in place, **and one file present for every
  language in `WARN_LANGS`**. A missing `warn-th.ogg` must fail loudly at home, not silently in
  the room.
- What played, when, and whether it played before the frame was acknowledged goes into the
  manifest (`sound_played`, `sound_stage`, `sound_langs`, latency) and into the Telegram caption.

**Holding the siren on: the input problem.** A siren that a mute key silences is not a siren.
The mute, volume-down and power keys all arrive from the same built-in keyboard device as the
letters (verified on the target machine: `AT Translated Set 2 keyboard` reports `KEY_MUTE`,
`KEY_VOLUMEDOWN` and `KEY_POWER`), so grabbing "just the media keys" is not possible — an
exclusive grab of that device would also take away the only way the owner can type the unlock
password. The defence is therefore layered, strongest and least intrusive first:

1. **Volume watchdog — the primary mechanism.** While any sound plays — warning or siren — every
   `SOUND_HOLD_POLL_MS = 250` the agent re-asserts the whole audio path: unmute,
   `SOUND_VOLUME_PCT`, the card profile with a `Speaker` port, and `Auto-Mute Mode` disabled so
   that a plugged headphone jack cannot silence the speakers (on the target machine that control
   is already `Disabled`).
   Pressing mute buys silence for a quarter of a second. This works no matter which device sent
   the keypress, and it takes nothing away from the owner.
2. **Lock the session.** On `tamper` the agent calls `loginctl lock-session`. Without the password
   there is no terminal, no process to kill, no shutdown menu — the keyboard stops being a way to
   reach the agent at all, while still being a way for the owner to identify themselves.
3. **Inhibitors, and why they are not enough on their own.** `camtrap run` holds
   `handle-power-key` alongside `sleep`, `idle` and `handle-lid-switch`. That stops *logind* from
   acting — but on this desktop KDE's PowerDevil holds the same locks in `block` mode and handles
   the press itself, and its configured action is suspend, which kills the trap outright. So while
   armed the agent also takes an **exclusive evdev grab of the power-button devices**, and then
   neither logind nor the desktop ever sees the event. Verified on the target machine: both
   `Power Button` devices grab and release cleanly. Holding the button for several seconds still
   cuts power in hardware; nothing in software can prevent that.
4. **The screen locks when the trap arms.** The owner has left the room, so an unlocked session is
   both a way into the machine and a way to stop the agent. Unlocking disarms and hands the power
   buttons back — the same gesture that already opens the grace window.
5. **Optional `EVIOCGRAB` on external input devices** (default off). Devices other than the
   built-in keyboard — a wireless keyboard, a USB audio dongle with media buttons — can be
   grabbed exclusively for the duration of the burst without locking the owner out;
   `camtrap input-scan` lists which devices report mute and volume keys. The grab is released
   when the file descriptor closes, so killing the agent cannot leave input captured — a required
   fail-safe.
6. **Magic SysRq is already closed** on the target machine: `kernel.sysrq = 16` allows only
   `sync`, so `Alt+SysRq+B` (reboot) and `Alt+SysRq+F` (OOM kill) are unavailable. This must stay
   that way — raising it to `1` or `438` for convenience would reopen a one-keystroke kill.

Why this can work at all: by `docs/threat-model.md` the primary actor is staff with a master
key, and what they need is specifically an unremarkable exit. Noise is the cheapest way to deny
them that from inside a locked room. The opposite risk is real too — a siren can prompt someone
to grab the laptop and leave faster — and what works against it is the "evidence first, noise
second" ordering, not the sound itself.

### 3.5 Spool and upload (laptop)

A separate thread, fully asynchronous from detection. A sagging Wi-Fi link must never slow the
camera down.

- Directory `~/.local/share/camtrap/spool/`.
- There are two receivers, independent and configured as a list:
  - `prod` — ssh forced command, delivery acknowledged by the receiver;
  - `mega` — a recompressed copy into `~/MEGA/camtrap/`, best effort. 1280x720 at quality 75
    instead of the captured 1920x1080 at 95: this copy is a warehouse for the case where the
    receiver is unreachable, and it syncs over hotel wifi. Measured on a real frame: 65 KB against
    288 KB, so a 60-frame event costs 3.8 MB in the cloud against 16.9 MB on the receiver. The
    evidence-grade original never leaves the receiver and the spool.
- **A frame leaves the spool only after `prod` acknowledges it.** Otherwise the very frame the
  whole thing exists for disappears together with the laptop. The cloud copy does not count as
  an acknowledgement: a successful `cp` only means the file landed in a sync folder, not that it
  reached the cloud — the sync client does not expose its upload state.
- An exception for a receiver that stays down: when `SPOOL_MAX_MB` is hit, frames already
  copied to the cloud folder are dropped first — they at least have some chance of surviving.
- Retries with exponential backoff, capped at `UPLOAD_RETRY_MAX_SEC = 300`.
- **Send priority**: the first frame of an event and the manifest jump the queue, and the first
  frame of a `tamper` event jumps ahead of everything else: when the siren fires depends on its
  acknowledgement, and at that moment the laptop may already be on its way out.
- `SPOOL_MAX_MB = 512`. On overflow the oldest mid-event frames are dropped; the first frame of
  an event is never dropped. Every drop is logged.
- One receiver failing must not stop the other: an unreachable VPS does not cancel the cloud
  copy, and a missing cloud folder does not cancel the upload.

### 3.6 Heartbeat (laptop → VPS)

Every `HEARTBEAT_SEC = 60` a status line goes out: timestamp, version, uptime, camera state,
spool depth, event counter for the session, mode (`armed` / `paused`), power state
(`ac_online`), lid state, `sound_ok`, and the time the siren last played.

**This is a more valuable signal than the frames.** It arrives even when the camera caught
nothing and it gives the exact cut-off time when the laptop was taken away or shut down. Power
state and `sound_ok` are in there so that unreadiness is visible before an event rather than
after: a trap running on battery with mute still set looks like it works and does not.

### 3.7 Poller (Pi)

Cron every 2 minutes, in the style and discipline of the owner's external prober:

- Pulls the list of new events from the VPS and sends the first frame to Telegram with a caption
  (local time, event type, frame count); the remaining frames follow as an album.
- A `tamper` event → 🚨 as its own message ahead of the queue: first frame, the list of signals
  that fired, local time, and whether the siren played. Mixing it with ordinary motion is not
  acceptable: the owner reacts differently to "someone came in" and to "the laptop is in
  someone's hands".
- Watches the heartbeat: older than `HB_STALE_SEC = 300` while `armed` → 🔴 "the agent went
  silent".
- A 🚨 `tamper` followed within `HB_STALE_SEC` by heartbeat silence is merged into one message,
  "handled → went silent", with both cut-off times: two separate alarms about one event read
  worse than a single story.
- `sound_ok = false` while `armed` → 🔴 "the siren will not fire": that is a failure of the
  trap, not a detail, and it needs to be known before the event, not after.
- **State mutations only after a successful Telegram send.** A failed send means a retry on the
  next tick, not a lost event.
- 🔴 on a new failure → repeat every `REPEAT_SEC` → 🟢 recovery.

### 3.8 Paused mode

Without it every ordinary return of the owner produces two false signals at once: 🚨 "handled"
with a siren going off because the cable was pulled and the lid closed, and then 🔴 "went
silent" on shutdown. There is no sleep on lid close any more (the inhibitor, see section 2), so
the lid no longer kills the agent — it became a tamper signal instead, which makes an explicit
pause all the more necessary.

`camtrap pause` marks an expected offline period on the VPS, `camtrap resume` clears it. While
`paused`, heartbeat silence is not a failure, tamper events are not produced, and the siren does
not play. The pause is set automatically when the unit stops cleanly (`ExecStop`) and cleared
on start.

The second automatic reason to hold back is a **session unlock**: the owner's session moves
`LockedHint` to `no` (verified — `loginctl` reports it on Wayland). Only the owner knows the
password, so an unlock is the most reliable "this is me, not someone else's hands" available on
this machine. For `GRACE_AFTER_UNLOCK_SEC` after it, tamper does not fire and the siren does not
play: otherwise the owner, back in the room to collect the laptop, would get a siren out of the
case every time they picked it up. Capture continues throughout — only an explicit
`camtrap pause` stops frames.

---

## 4. Commands

```
guard                       the way it is actually started: preflight, arm when still, run
camtrap guard [--still N]   same thing without the launcher
camtrap preflight           would this trap survive being left alone? refuses on a failure
camtrap run                 main mode (used by the systemd unit)
camtrap selftest            camera, detector, key to the VPS, inbox reachability, audio
camtrap calibrate [--sec N] N seconds of noise statistics, prints a recommended MIN_AREA_PCT
camtrap mask                take a frame and write the ignore mask into the config
camtrap siren-test          play the siren into the configured sink at real volume
camtrap siren-make          generate siren.ogg (ffmpeg; --mode yelp|wail)
camtrap warn-test           play the spoken warning in every configured language, in order
camtrap warn-make --lang X  generate warn-<lang>.ogg (espeak-ng + ffmpeg)
camtrap setup               suggest WARN_LANGS from the system time zone; writes nothing itself
camtrap input-scan          list input devices reporting mute/volume keys (for the grab list)
camtrap pause | resume      expected offline
camtrap status              local state: mode, spool, last heartbeat, sound files, languages
camtrap install             installs and enables the systemd --user unit
```

**`preflight` refuses rather than warns.** Camera, sound files and a real burst through the
speakers are blocking; a missing receiver is not, because frames still accumulate locally and the
siren needs no network. The audio probe plays the siren for `audio_probe_sec` at
`audio_probe_volume_pct` — checking that a file exists proves nothing about whether the sink still
routes to the speakers, and 0.4 s at 20 % does not announce itself to the corridor.

`selftest` and `calibrate` are mandatory at home before departure. Tuning the detector in a
hotel room on the first evening is a reliable way to travel with a trap that does not work.

`siren-test` and `warn-test` are run twice: at home, to confirm the files exist and are audible at
all, and in the hotel room before the first arming, to confirm they play through the speakers at
the volume intended. The second run is not optional, because the sink changes with a single
plugged cable: a jack in the 3.5 mm socket moves the card to its `Headphones` profile and the
built-in speakers go quiet.

`warn-test` has a second purpose: it is the moment to judge whether the synthesised speech is
actually intelligible in the target language. If it is not, the file gets replaced before
departure — see 3.4.

---

## 5. Repository layout

Discipline borrowed from a neighbouring project: **the repository is the source of truth,
everything reaches the boxes through `deploy/` only, and nothing is edited by hand on a box.**

```
camtrap/
├── SPEC.md
├── README.md
├── pyproject.toml
├── requirements.txt
├── src/camtrap/
│   ├── cli.py            command parsing
│   ├── config.py         defaults plus ~/.config/camtrap/config.toml
│   ├── camera.py         V4L2 capture, retry on USB drop
│   ├── detector.py       MOG2, mask, lighting-change suppression
│   ├── tamper.py         power, lid, case movement, ALS arbiter
│   ├── player.py         sink selection, volume, cooldown, limits
│   ├── event.py          event slicing, pre-buffer, throttling, manifest
│   ├── spool.py          queue, priorities, cap, retention
│   ├── uploader.py       ssh transport plus a fake sink for tests
│   ├── heartbeat.py
│   └── selftest.py
├── assets/voice/         warn-<lang>.txt — the warning text per language, checked by a speaker
├── tools/make-siren.sh   ffmpeg → siren.ogg (yelp | wail)
├── tools/make-warning.sh espeak-ng + ffmpeg → warn-<lang>.ogg
├── deploy/
│   ├── prod/camtrap-recv.sh
│   ├── prod/camtrap-tg.sh
│   ├── pi/camtrap-poll.sh
│   ├── pi/camtrap-poll.cron
│   ├── systemd/camtrap.service
│   └── install-*.sh
├── tests/
│   ├── fixtures/          synthetic frames and short sequences
│   └── test_*.py
└── docs/
    ├── threat-model.md    hotel theft data the requirements grew from
    └── runbook.md         what to do on arrival and what to do if it fires
```

Deployment specifics — box addresses, key names, dates — are deliberately absent: the
specification is written impersonally (`user@vps`, `Pi at home`).

---

## 6. Stack and style

- **Python 3.12**, `venv` plus `requirements.txt`.
- **OpenCV** `opencv-python-headless` (no GUI needed; the detector reads V4L2 directly and is
  indifferent to the windowing system).
- Formatting with `ruff format`, linting with `ruff check`. Lines up to 100.
- Type annotations on modules' public functions. No architecture "for future growth": one
  process, two threads (detection and upload), a queue between them.
- Sound and sink handling through external processes (`pw-play`, `pactl`/`wpctl`), with no audio
  bindings in the dependencies: in a hotel room this will be fixed by hand from a shell, not
  with a debugger.
- Hardware access is read-only sysfs and `/proc` at paths taken from the config. No `root` and
  no new groups: the owner already has `video` and `input`.
- Logs go to stdout in a structured form, `journalctl -t camtrap`. Detector ticks are not
  logged; events, drops, retries and mode changes are.
- Config is TOML at `~/.config/camtrap/config.toml`; every threshold from section 3 lives there.
  Defaults are in code, the file only overrides.
- The shell parts (`deploy/`) are POSIX sh, `set -eu`, in the style of the external prober: one
  tick line per run in the journal with the key fields.

---

## 7. Testing

No camera is available in CI, so everything that can be is tested against synthetic input.

- **Detector** — generated numpy frames: static background, moving rectangle, abrupt brightness
  change, noise. Checks: it triggers, it does not trigger on noise, a lighting change is
  classified as `light`, the mask works.
- **Throttling** — 60 seconds of continuous motion yield exactly 12 frames plus the pre-buffer.
- **Pre-buffer** — when the trigger lands on frame N, frames N-5…N are in the event.
- **Tamper** — sysfs paths come from the config and are pointed at `tmp_path` in tests: an
  `online` 1→0 transition produces `tamper`, the reverse transition does not; closing the lid
  produces one; a 1→0→1 bounce inside one tick produces exactly one event, not two.
- **Case movement versus light** — synthetic: the same frame shifted by N pixels gives `tamper`;
  the same frame with changed brightness gives `light`; a smeared unrecognisable frame gives
  `tamper`. The ALS arbiter is checked on both outcomes, including when it contradicts the
  correlation.
- **Player** — against a fake player that records calls: it does not play during warm-up, in
  `paused`, or inside the unlock window; it honours the cooldown and the per-event and per-hour
  limits; it plays after the first frame is acknowledged, and when the receiver is unreachable
  it plays after `SOUND_DELAY_MAX_SEC` and marks that in the manifest.
- **Two stages** — a `motion` event plays the warning and never the siren; a `tamper` event plays
  the siren and never the warning; `light` plays nothing by default. A `tamper` arriving mid-warning
  cuts the warning off and starts the siren without waiting for it to finish. The session lock is
  requested on `tamper` only, never on `motion`.
- **Languages** — files play in `WARN_LANGS` order, local language before English; a language with
  no generated file is reported at startup and in `sound_ok`, not skipped silently at event time.
- **Audio readiness** — a missing siren file, a missing file for any configured language, a muted
  sink and an absent sink all produce `sound_ok = false` in the heartbeat instead of a silent
  failure at event time.
- **Siren hold** — against fakes for the mixer and the session: a mute applied mid-burst is
  reverted within one `SOUND_HOLD_POLL_MS` tick, a volume drop is restored, a card profile
  switched away from `Speaker` is switched back, and the session lock is requested exactly once
  per tamper event. The optional device grab is released on process exit even when the agent is
  killed mid-burst.
- **Spool** — overflow drops the middle and keeps the first frame of an event; after a
  simulated outage everything accumulated is delivered; deletion only after acknowledgement.
- **Uploader** — against a fake sink (a local directory instead of ssh), including failure
  halfway through a batch.
- **Two receivers** — a `prod` failure does not prevent the `mega` write and vice versa; a frame
  does not leave the spool until `prod` confirms; on spool overflow the frames already copied to
  the cloud go first.
- **Retention** — files older than `RETENTION_DAYS` are removed, fresh ones are not.
- **Warm-up** — no events are produced during the first `WARMUP_SEC`.
- Live check — `camtrap selftest` and `camtrap siren-test` on the real camera and speakers, by
  hand, before the trip.

Frame fixtures are generated by code rather than stored as binaries, except for one short real
sequence kept for regression.

---

## 8. Boundaries

### Always

- A frame is deleted locally only after the receiver acknowledges it.
- The laptop's key to the VPS is **write-only**, forced command, `no-pty`,
  `no-port-forwarding`. It must not be able to read the inbox, delete anything, or run anything
  else.
- The siren plays only on `tamper`, and only after the event's first frame has been sent or
  `SOUND_DELAY_MAX_SEC` has expired. The "evidence first, noise second" ordering is not
  negotiable.
- `camtrap run` holds inhibitors for `sleep`, `idle`, `handle-lid-switch` and `handle-power-key`
  for as long as it runs, and the laptop is left with the lid open.
- While the siren plays, the audio path is re-asserted every `SOUND_HOLD_POLL_MS`: unmute,
  volume, `Speaker` profile, `Auto-Mute Mode` off. A siren that one keypress can silence does not
  count as implemented.
- Whether and when the siren played goes into the manifest and the alert: "it fired" and "it
  should have fired" are different things, and they need to be told apart from the record rather
  than from memory.
- Deployment order for changes: receiver on the VPS first, then the sender on the laptop, then
  the poller on the Pi. The reverse order produces a false 🔴 on the tick between edits.
- Any limit on coverage (frame truncation, spool drops) is written to the log and the manifest.

### Ask first

- **Any change on the VPS or the Pi.** Another project's production monitoring lives there, with
  its own cron files, send scripts and pollers. New scripts must sit alongside, never on top,
  and must not touch paths that belong to someone else.
- Creating new ssh keys and editing `authorized_keys` on the VPS.
- Anything that increases storage consumption on the VPS.
- System power settings (desktop power manager, `logind.conf`): that is the owner's machine, and
  the agent does not rewrite them — it holds its own inhibitor and verifies the result.
- The final siren mode and volume.
- The wording of the spoken warning and the language list, before the files are generated.
- Enabling the optional exclusive grab of external input devices, and any change to
  `kernel.sysrq` (currently `16`, which is the safe value here).

### Never

- Do not store the Telegram token on the laptop or on the VPS. The Pi only.
- Do not reuse the existing ssh keys of another project's monitoring: camtrap creates its own.
- Do not record audio. Playing is fine, capturing is not, and that does not change.
- Do not play the siren on ordinary motion or on a lighting change, while `paused`, during
  warm-up, or more often than `SOUND_COOLDOWN_SEC`. Motion gets the voice; only tampering gets
  the siren, and confusing the two is the fastest way to lose the room's goodwill.
- Do not add a voice claiming to be the police or any other authority, and do not put anything in
  the warning that is not a fact: no "the police have been called", no "security is on the way",
  no threats. An alarm sound is an alarm sound; impersonating officials is a different act with
  different consequences.
- Do not ship a warning nobody in the room can understand. A file that has not been checked for
  intelligibility in its language is not ready, however cleanly it was generated.
- Do not take hostile measures on the warning stage — no session lock, no input grab. Stage 1 is
  a notice; escalation is what stage 2 is for.
- Do not grab the built-in keyboard exclusively. It is the only way the owner can type the
  unlock password, and a trap that locks out its owner in a foreign hotel room is worse than the
  theft it guards against. External devices only, and only for the burst.
- Do not log the codes of pressed keys if phase 2 brings in evdev — only that an event
  occurred. The disk is unencrypted, and a keystroke log on it is someone else's passwords in a
  thief's hands.
- Do not capture outside an explicitly armed mode. The trap is switched on by a command; it does
  not live permanently.
- Do not keep frames longer than `RETENTION_DAYS` (14 by default) locally, on the VPS, or in the
  cloud folder. Cleaning the cloud folder is the agent's job, not something done by hand.
- Do not treat the cloud copy as proof of delivery and do not drop anything from the spool
  because of it.
- Do not put anything except frames and manifests into `~/MEGA/camtrap/`: that folder leaves for
  the cloud, and the rule about credentials and passwords applies there too.
- Do not publish the frames. The material is for hotel management and the police.

### Legal frame

Filming your own paid hotel room to protect your belongings is generally permissible, but frames
with people in them are material for the police and hotel management, not for publication —
many jurisdictions grant a right to one's own image. The ignore mask exists partly to keep out
of frame what does not need to be in it.

An alarm sound is the same category as a car alarm and stays on the right side of that line as
long as it does not pretend to be an authority — hence the rule against a voice claiming to be
the police. Volume is its own subtlety: at night a siren in a room is heard in the corridor and
will bring hotel staff. For this task that is closer to a benefit than a cost, but the effect
should be deliberate rather than accidental.

The spoken warning helps on this axis rather than hurting. Announcing that a space is recorded is
what signage does in shops and lobbies, and saying it out loud in the local language means the
person who walks in is informed rather than filmed unawares — which is exactly the distinction the
right to one's own image turns on. It only holds while the sentence stays a statement of fact.

---

## 9. Phases

Departure is day `T`. The at-home shakedown occupies `T-3 … T-1`, so phase 1 code has to be
ready by `T-3`: configuring a detector in a hotel room on the first evening is not a plan but a
lottery.

### Phase 1 — by `T-3`. Required for the trip

Detector with warm-up, mask and lighting suppression · events with pre-buffer and throttling ·
tamper on power and lid · separation of case movement from lighting change with the ALS arbiter ·
`player` with both stages, per-language warning files, limits, the siren generator and the hold
layers (volume watchdog, session lock, inhibitors) · spool with priorities and a cap
· uploader with retries · heartbeat with `sound_ok` · `camtrap-recv.sh` · `camtrap-tg.sh` with
`sendPhoto` · `camtrap-poll.sh` on the Pi with the 🚨 tamper alert · pause/resume · systemd unit
· `selftest`, `calibrate`, `siren-test`, `warn-test` · the tests from section 7 ·
`docs/runbook.md`.

The audible part is small in volume (sink selection, an external player, limit counters, a text
file per language), but it cannot be deferred: it is the only part that works when connectivity is
dead, and it has to be exercised during the same two shakedown days as everything else. The one
item on it with a real lead time is intelligibility: getting a native speaker to listen to the
Vietnamese and Thai files needs to start early, because the fix — a better recording — is not
something code can produce.

### Phase 2 — after the trip

A web gallery of events on the VPS (static files behind basic auth, index generated on frame
receipt), event statistics, and "mine / not mine" labelling to filter out the owner's own
returns to the room. Input monitoring through evdev as one more tamper signal — deferred from
phase 1 because the owner's own automation generates input events, and without separating those
the signal produces false positives, which is precisely what this project must not do.

### Phase 3 — undecided

Configuration through a UI. I consider it unnecessary and am not pulling it into phase 1: there
are about a dozen thresholds, they are adjusted once per trip, and `calibrate` covers the only
non-obvious one. The owner's call.

---

## 10. Open questions

1. **When does the alarm arm?** This is the open question the owner raised, and it is the one
   that decides how often a siren goes off in an empty room for no reason. The options:
   (a) armed for as long as `camtrap run` is running, with the unlock window from 3.8 as the
   only exemption — simplest, but forgetting `camtrap pause` before picking the laptop up costs
   a siren; (b) an explicit `camtrap arm` with an exit delay, like a car alarm: the siren becomes
   live 60 s after the command, so the owner can leave; (c) armed on session lock and disarmed
   on unlock — the machine decides from a signal only the owner can produce, at the cost of the
   trap being disarmed whenever the owner is at the laptop; (d) time windows for known absences.
   My recommendation is (c) plus (b): lock arms the alarm, an exit delay covers the walk to the
   door, and unlock disarms it. That keeps capture always on and makes only the noise
   conditional — the alert channel keeps working even while the siren is held back.
2. Retention on the VPS — 14 days by default, to be confirmed.
3. The Pi polling interval — 2 minutes against the external prober's 5. It needs its own cron
   file rather than being mixed into the existing one.
4. Whether the first trigger warrants an alert separate from later ones in the same event
   (currently assumed: one event, one message with an album).
5. Behaviour when the camera drops off the USB bus: treat it as a failure and alert, or
   reconnect quietly. I suggest alerting and classifying it as `tamper` — a camera vanishing in
   a locked room is suspicious in itself — but **without the siren**: bus glitches are more
   plausible than a hand on the cable of a built-in camera.
6. Retention of the cloud copy — the same 14 days or shorter: that account also holds personal
   documents, and there is no reason to keep an archive of room footage there any longer than
   necessary.
7. Whether to disable "downward" sync of the cloud folder for the trip (upload only), so that a
   deletion in the cloud is not propagated back. The sync client does not expose that setting,
   so this is closer to "accept the risk" than to "fix it".
8. Siren mode and volume: `yelp` at `SOUND_VOLUME_PCT = 100` is audible in the corridor and at
   night will bring hotel staff. Is that the desired effect (I think it is), or should it be
   `wail` at 70?
9. Whether the **warning** should also fire on `light` — someone came in and switched the light on
   without producing confirmed motion yet, which happens when they enter from a dark corridor. The
   siren certainly should not. I lean towards yes for the voice, since a light going on in an empty
   room means a person is in it, but it is one more thing housekeeping will hear.
10. Delivery interval for `tamper`: the shared two-minute tick, or a separate cheap tick every
    minute that only checks the tamper flag on the VPS. The latter costs one extra ssh per
    minute and halves the delay on the one alert where delay is actually felt.
11. `GRACE_AFTER_UNLOCK_SEC` — how long to hold the window after an unlock. I suggest 300 s:
    enough to collect the laptop, short enough that the window does not outlive the owner
    leaving the room.
12. Whether to buy an external USB accelerometer (see 3.3). The only thing it adds in substance
    is a reaction to a lift in the dark and before the cable is pulled. I suggest travelling
    without one first and checking the journal for whether the composite of power, lid and frame
    shift ever fired; buying hardware for a hypothesis before there is data is premature.
13. Residual risk of the siren, to be reviewed after the trip: it does not tell the intruder
    that frames have already left, which is why it stays wordless — but it also does not remove
    impunity as directly. If it turns out that a siren makes people grab the laptop and run, the
    answer is not a different sound but a faster acknowledgement of the first frame.
14. Escalation from stage 1 to stage 2 without tampering: if motion continues for
    `ESCALATE_AFTER_SEC` after the warning and nobody has left, should the siren follow? Currently
    no — the siren is reserved for the laptop being handled. Worth revisiting after the first trip
    with real journals, because "warned, ignored, still in the room" is a different situation from
    "walked past the desk".
15. Whether the warning should say more than it does now. "This laptop is protected by an alarm and
    a camera" is neutral; adding "and the owner has been notified" would increase pressure but also
    tell the intruder that evidence has already left the machine — the exact trade rejected for the
    siren. Keeping both sounds free of that claim is deliberate.

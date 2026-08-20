# Runbook

What to do before the trip, on arrival, when it fires, and when it fires for nothing.
Thresholds and rationale are in [SPEC.md](../SPEC.md); this file is the sequence to follow.

## Before leaving home

```sh
deploy/install-laptop.sh              # venv, sounds, systemd --user unit
camtrap selftest                      # every check must be green or a known warning
camtrap siren-test                    # loud, from the built-in speakers
camtrap warn-test                     # intelligible in the local language
```

Four things that are easy to skip and expensive to skip:

1. **Have a native speaker listen to the warning.** `espeak-ng` is a formant synthesiser and both
   Vietnamese and Thai are tonal; nobody on this side of the screen can judge whether the result
   is understood. If it is not, replace the `.ogg` — the player does not care how it was made.
2. **Set the lid action to "do nothing"** in the desktop power manager. The agent holds its own
   inhibitor, but a desktop that insists on suspending still wins, and a suspended laptop is a
   dead trap.
3. **Leave `kernel.sysrq` at 16** (sync only). At `1` or `438`, `Alt+SysRq+B` reboots the machine
   and ends the siren with one keystroke.
4. **Decide the languages** — `sound.warn_langs = ["vi", "en"]` for Vietnam, `["th", "en"]` for
   Thailand. Local language first; English alone is a coin flip with housekeeping.

## On arrival, before the first arming

```sh
camtrap mask                          # capture a reference frame
# add ignore polygons for: window, curtain, mirror, gap under the door, indicator lights, TV
camtrap calibrate --sec 60            # in this room, at the light level it will have at night
camtrap siren-test && camtrap warn-test   # the sink changes with one plugged cable
systemctl --user enable --now camtrap
camtrap status                        # sound_ok must be true
```

Then leave the room, lock the screen, and wait out `ARM_EXIT_DELAY_SEC` (60 s by default).
The alarm is live once the screen is locked and the delay has passed — `camtrap status` shows
`armed` with a reason when it is not.

**Put a "do not disturb" sign on the door for the days the room is armed.** Housekeeping will
otherwise hear the spoken warning every time they come in. That is correct behaviour, not a fault,
but it is a conversation with the front desk you can simply avoid.

## Taking the laptop yourself

Unlock the screen. That disarms the alarm and opens a grace window
(`GRACE_AFTER_UNLOCK_SEC`, 300 s) during which nothing sounds. Capture continues.

If you are shutting the machine down or packing it away, run `camtrap pause` first — otherwise the
poller reports "the agent went silent" a few minutes later. `camtrap resume` when you set it up
again; the systemd unit does both automatically on stop and start.

## When a 🚨 arrives

1. **Do not rush back into the room.** The alert already carries the frame; the evidence is off
   the machine.
2. Read the caption: the signal list separates "power cable pulled" from "case lifted" from "lid
   closed", and says whether the siren actually played.
3. If the message is the linked kind — *handled at HH:MM, then went silent at HH:MM* — the laptop
   was taken or shut down. The second timestamp is the cut-off you will quote to the hotel and to
   the police.
4. Ask the front desk for the door log and the corridor footage for those two timestamps, in
   writing. Hotels pull footage against a report, so file one even if the laptop is still there.
5. Keep the frames. They are material for management and the police, not for publication.

## When it fires for nothing

| Symptom | Likely cause | Fix |
|---|---|---|
| Warning during cleaning | working as designed — someone is in the room | "do not disturb" sign, or accept it |
| Warning with the room empty | curtain, mirror, TV, indicator light, or `min_area_pct` too low | `camtrap mask`, then `camtrap calibrate --sec 60` |
| Siren with nobody there | a USB-C port flapped `online`, or the lid switch bounced | raise `tamper.debounce_sec`; check the journal for `tamper signal=` |
| Siren when you picked it up | you forgot to unlock first, or the grace window expired | unlock before touching it; raise `GRACE_AFTER_UNLOCK_SEC` |
| 🔴 "the siren will not fire" | a sound file is missing, or the sink moved to headphones | `camtrap status`, unplug the jack, regenerate the sounds |
| 🔴 "agent went silent" while you are holding it | you skipped `camtrap pause` | `camtrap resume`, then pause properly next time |

Every one of these leaves a line in the journal:

```sh
journalctl --user -t camtrap -n 200          # on the laptop
journalctl -t camtrap-poll -n 100            # on the Pi
journalctl -t camtrap-recv -n 100            # on the receiver
```

## If the siren is going off right now and should not be

```sh
# unlock the screen — this is the intended way, and it opens the grace window
camtrap pause          # stops sound and suppresses the silence alert
systemctl --user stop camtrap
```

Holding the power button for several seconds always works and always will: that is a hardware
path the kernel never sees. It is listed here because in a hotel corridor at 3am it is the thing
you will actually reach for.

## What this does not do

It does not stop anyone from walking out with the laptop. Muffling the case, carrying it out
mid-burst, or a long power-button press all remain open. The value is that a quiet, unremarkable
exit becomes impossible — see the limits stated in [SPEC.md §1](../SPEC.md).

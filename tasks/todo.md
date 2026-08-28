# camtrap phase 1 — task list

Plan: [plan.md](plan.md) · Spec: [../SPEC.md](../SPEC.md)

Deadline: **23 August 2026** for code · shakedown 23–25 · departure the 26th. 34.5 h total.
Slice order is the mitigation: every checkpoint leaves the trap worth travelling with.

Decisions already taken: local fake receiver first (boxes touched only after review), nothing cut
from phase 1 scope, arming is `on_lock` plus a manual `camtrap arm`.

---

## S0 · Skeleton — `camtrap status` prints real state (1.5 h, no deps)

- [x] **S0.1** `pyproject.toml` + `requirements.txt`, venv, install `opencv-python-headless`,
      `pytest`, `ruff` — none are present yet, `cv2` import currently fails and `ruff` is absent
      system-wide. *Done when* `.venv/bin/python -c "import cv2"` and `.venv/bin/ruff --version` both
      succeed.
- [x] **S0.2** `src/camtrap/config.py` — in-code defaults overridden by
      `~/.config/camtrap/config.toml`; every threshold from spec §3; **every sysfs and command path
      injectable** so tests can point them at `tmp_path`. *Done when* a test overrides a path and no
      real sysfs is touched.
- [x] **S0.3** `src/camtrap/cli.py` + structured stdout logging (`journalctl -t camtrap` style, one
      line per tick with key fields). *Done when* `camtrap status` prints mode, arming state, spool
      depth, thresholds and resolved paths.
- [x] **S0.4** Gate: `ruff check src tests`, `ruff format --check`, `pytest -q`.

---

## S1 · Siren on cable pull and lid close (6 h, deps: S0) ← the feature

- [x] **S1.1** `tamper.py` — poll `ADP1/online`, both `ucsi-source-psy-USBC000:00{1,2}/online`, and
      `LID0/state` every `TAMPER_POLL_SEC`; debounce so a 1→0→1 bounce is one event. *Done when* fake
      sysfs tests pass: 1→0 fires, 0→1 does not, bounce fires exactly once.
- [x] **S1.2** `player.py` — `pw-play` with timeout; explicit `SOUND_SINK`; switch card to a profile
      with a `Speaker` port; unmute; `SOUND_VOLUME_PCT`; `Auto-Mute Mode` off; `SIREN_SEC`. *Done
      when* `camtrap siren-test` plays through the built-in speakers **while the default sink is the
      USB dongle**.
- [x] **S1.3** Hold layer — re-assert the audio path every `SOUND_HOLD_POLL_MS = 250`
      (unmute + volume + profile + auto-mute). *Done when* mute and volume-down pressed mid-burst are
      reverted within one tick, and a headphone jack plugged mid-burst does not silence the speakers.
- [x] **S1.4** Hold layer — `loginctl lock-session` on tamper (config flag to disable while
      iterating, default on) and `systemd-inhibit --what=sleep:idle:handle-lid-switch:handle-power-key`
      held for the lifetime of `camtrap run`. *Done when* a short power press does not stop the
      machine and a closed lid neither sleeps it nor stops the siren.
- [x] **S1.5** `arming.py` — `ARM_MODE` (`on_lock` default | `always` | `manual`), `LockedHint`
      polling, `ARM_EXIT_DELAY_SEC = 60`, `GRACE_AFTER_UNLOCK_SEC = 300`, `camtrap arm` / `disarm`.
      *Done when* an unlocked session stays silent on a cable pull, and unlocking mid-burst stops the
      siren and suppresses tamper for the grace window.
- [x] **S1.6** Limits — cooldown, `SOUND_MAX_PER_EVENT`, `SOUND_MAX_PER_HOUR`, no play during warm-up
      or `paused`. *Done when* limit tests pass against a fake player.
- [x] **S1.9** Stage 1 plumbing in `player.py` — two stages sharing one audio path: `WARN_LANGS`
      ordered playback, `WARN_VOLUME_PCT = 85`, `WARN_COOLDOWN_SEC = 120`, `WARN_MAX_PER_HOUR`, and a
      `tamper` mid-warning cuts the warning off and starts the siren. **No session lock and no input
      grab on stage 1.** *Done when* tests prove `motion` → warning only, `tamper` → siren only,
      lock requested on tamper alone, and languages played in configured order.
- [x] **S1.10** `warn-test` + `sound_ok` covering every language — a missing `warn-<lang>.ogg` for a
      configured language fails at startup and sets `sound_ok = false`, instead of going quiet at
      event time. *Done when* deleting `warn-th.ogg` with `th` in `WARN_LANGS` turns the check red.
- [x] **S1.11** Intelligibility — **closed 2026-08-20 by measurement instead of by a speaker.** No
      native speaker was reachable, so the file is judged by a recogniser trained on real speech:
      `vi` via piper scores 100 % word recall, espeak-ng scored 29 % (a different sentence), `th`
      scored 0 % and was dropped from the shipped set. Re-run with
      `tools/check-warning.py` after any text or engine change. — get a native speaker to listen to `warn-vi.ogg` and `warn-th.ogg`,
      or replace them with better recordings. **Start this first, it has the longest lead time and
      code cannot fix it.** *Done when* each configured language is either confirmed by a speaker or
      replaced.
- [x] **S1.7** `camtrap input-scan` — list input devices reporting mute/volume keys; optional
      `EVIOCGRAB` on **external devices only**, released on fd close. *Done when* the grab survives a
      `kill -9` without leaving input captured.
- [x] **S1.8** Physical pass — done 2026-08-20 via `guard test 30` and `guard drill`; the owner
      reports the cable, mute, lid and power behaviours all correct. **→ CHECKPOINT 1 PASSED.**
      From here the trap already does the thing ranked first, even if nothing else lands.

**→ CHECKPOINT 1.** Nothing proceeds until the physical checks pass. After this the trap already
does the thing ranked first.

---

## S2 · Motion → frames and manifest on disk (7 h, deps: S0)

- [x] **S2.1** `camera.py` — MJPG 1280×720; the driver offers **only 30 fps for MJPG** (10 fps for
      YUYV), so 5 fps is decimation in code; retry on USB drop. *Done when* a 60 s capture yields
      ~300 decoded frames and survives an unplug/replug.
- [x] **S2.2** `detector.py` — MOG2 on blurred greyscale, `MIN_AREA_PCT`, `MIN_MOTION_FRAMES`,
      `WARMUP_SEC`, ignore mask, `GLOBAL_CHANGE_PCT` → `light`. *Done when* synthetic tests pass:
      triggers on a moving rectangle, silent on noise, brightness step classified `light`, mask
      respected, nothing during warm-up.
- [x] **S2.3** `event.py` — `PREBUFFER_FRAMES` ring at 1/s, `SNAPSHOT_INTERVAL`, `EVENT_GAP`,
      `MAX_FRAMES_PER_EVENT` + `truncated`, JSON manifest. *Done when* 60 s of continuous motion
      yields exactly 12 frames plus pre-buffer, and a trigger on frame N includes N-5…N.
- [x] **S2.4** `spool.py` — priorities (first frame and manifest first, `tamper` ahead of all),
      `SPOOL_MAX_MB`, drop from the middle, never the first frame, every drop logged,
      `RETENTION_DAYS`. *Done when* forced overflow keeps first frames and logs each drop.
- [x] **S2.5** `camtrap calibrate` and `camtrap mask`. *Done when* calibrate prints a recommended
      `MIN_AREA_PCT` from real noise and mask writes polygons into the config.
- [x] **S2.6** Wire `motion` → stage 1 warning (needs S1.9). *Done when* walking into the room plays
      the warning in `WARN_LANGS` order within 3 s of confirmed motion, and the same walk-through
      never produces a siren.
- [ ] **S2.7** 30-minute empty-room run.

**→ CHECKPOINT 2.** Zero false events **and zero warnings** over the empty-room run. A warning with
nobody in the room means `MIN_AREA_PCT` or the mask is wrong — fix before proceeding, because from
here on every detector mistake is audible in the corridor.

---

## S3 · Frame reaches the receiver and is acknowledged (5 h, deps: S2)

- [x] **S3.1** `deploy/prod/camtrap-recv.sh` — POSIX sh, `set -eu`, forced command, frame → inbox,
      state/heartbeat update, one tick line. Defines the wire contract, so it comes first. *Done when*
      `sh -n` passes and a local run against a temp inbox stores a frame.
- [x] **S3.2** `uploader.py` — ssh transport plus local-directory sink for tests, backoff capped at
      `UPLOAD_RETRY_MAX_SEC`, **spool deletion only on `prod` ack**. *Done when* an unreachable sink
      leaves the spool untrimmed and restoring it drains in priority order.
- [x] **S3.3** `mega` sink — copy into `~/MEGA/camtrap/`, own retention, never counts as an ack.
      *Done when* tests prove `prod` failure does not stop the copy, a missing `~/MEGA` does not stop
      the upload, and a `mega`-only copy never frees a frame.
- [x] **S3.4** Mid-batch failure resumes without duplicates.

---

## S4 · Alert with photo in Telegram (6 h, deps: S3)

- [x] **S4.1** `heartbeat.py` — every `HEARTBEAT_SEC`: timestamp, version, uptime, camera, spool
      depth, event count, mode, `ac_online`, lid, `sound_ok`, last siren time.
- [x] **S4.2** `deploy/prod/camtrap-tg.sh` — `sendPhoto`/`sendMessage`, token from stdin, never
      stored. *Done when* `sh -n` passes and a run against a local HTTP stub posts multipart.
- [x] **S4.3** `deploy/pi/camtrap-poll.sh` + `.cron` — events → Telegram, 🚨 `tamper` as its own
      message ahead of the queue, `sound_ok = false` → 🔴, `HB_STALE_SEC`, `REPEAT_SEC`, state mutated
      only after a successful send. *Done when* a failed send leaves state unmutated and retries next
      tick.
- [x] **S4.4** Merge "handled → went silent" into one message with two cut-off times; `paused`
      suppresses the silence alert.

**→ CHECKPOINT 3.** Owner reads `recv.sh`, `tg.sh`, `poll.sh`; then I ask permission for the boxes —
new ssh keys, install, cron (spec §8). Deployment order: receiver → sender → poller.

---

## S5 · Tamper from the camera: lifted case vs switched light (4 h, deps: S1 + S2)

- [x] **S5.1** `cv2.phaseCorrelate` on normalised greyscale, `MOVE_SHIFT_PX`; degenerate correlation
      with a fully changed frame → `tamper`. *Done when* synthetic shift → `tamper`, brightness step
      → `light`, smeared frame → `tamper`.
- [x] **S5.2** ALS arbiter on `in_illuminance_raw` of both sensors, including the case where it
      contradicts the correlation.
- [x] **S5.3** Live: lifting the laptop without touching the cable sounds the siren; switching the
      room light gives `light` and **no** siren.

---

## S6 · Trip harness (5 h, deps: S1–S5)

- [x] **S6.1** `deploy/systemd/camtrap.service` (`--user`, inhibitors, `ExecStop` → pause) +
      `deploy/install-*.sh` + `camtrap install`. *Done when* a reboot brings the unit back armed.
- [x] **S6.2** `selftest.py` — camera, detector, receiver key, inbox, audio path, inhibitors, arming.
      *Done when* every check reports green on the real machine.
- [x] **S6.3** `docs/runbook.md` — arrival, what to do when it fires, pausing before picking the
      laptop up, what to do if the siren goes off for nothing, and the `kernel.sysrq` warning.
- [ ] **S6.4** 24-hour empty-room run; read the journal afterwards.

**→ CHECKPOINT 4.** Zero false alerts and zero sirens over 24 h (spec criterion 5). This gate decides
whether the trap travels.

---

## Added after the plan: manual start

The owner starts the trap by hand on the way out, so `guard` is the entry point rather than the
systemd unit, and arming keys on the room going quiet instead of on a screen lock.

- [x] **G.1** `deploy/guard` launcher, installed into `~/MEGA/os/apps` (on PATH) and `~/.local/bin`
      as a fallback. A launcher, not a compiled bundle: that folder holds one-kilobyte scripts and
      syncs to the cloud, while a PyInstaller build with OpenCV is ~200 MB.
- [x] **G.2** `camtrap preflight` — camera, sound files and a real quiet burst through the speakers
      are blocking; a missing receiver is only a warning.
- [x] **G.3** `arming.mode = "on_still"` — arms once the frame is quiet for 30 s, movement restarts
      the clock, and a room that never quiets arms at the 300 s deadline anyway.
- [x] **G.4** Poller announces arming transitions: 🛡 when it takes hold, 🔓 when it stops.

## Independent verification, 2026-08-20

Ran by the agent rather than the owner, which is how these three were found — all in code paths
the unit tests had not been asked about:

1. **Delivery could kill the whole trap.** A plain file where the inbox directory belonged raised
   FileExistsError out of the sink, through the capture path, and the process died. Frames stopped,
   siren stopped. Now: sinks report instead of raising, the uploader treats a sink as untrusted
   code, and housekeeping can never end the loop — detection and sound outrank delivery.
2. **A tamper event could arrive as plain motion.** The manifest was only written on close, so an
   agent killed mid-event (battery out, laptop carried off) delivered frames with no type and no
   signals. Now the manifest is written at begin, refreshed on escalation and on sound, and marked
   `closed: false` — which the poller now reports as "event was still running".
3. **`guard watch` did not exist.** The command was wired in the CLI but the function was never
   added, so the 30-minute empty-room test would have failed with ImportError at the moment it was
   needed. Written, and verified by running it.

Also fixed: the poller announced "no longer armed" on its very first tick (noise), and test modes
wrote frames into the cloud sync folder.

## Checkpoint status

- **Checkpoint 1 — PASSED (2026-08-20).** Siren audible from the built-in speakers, silencing
  defeated, machine stays awake on a closed lid and a power press.
- Checkpoint 2 — 30-minute empty-room run, not yet done.
- **Checkpoint 3 — PASSED (2026-08-20).** Receiver on the VPS, poller on the Pi, two restricted
  keys, cron every 2 minutes. Verified: the laptop key can put frames but `list` and arbitrary
  commands are refused; the Pi key can read state but `put-frame` is refused; a real frame reached
  Telegram as a photo; pause reaches the receiver so the poller stays quiet.
- Checkpoint 4 — 24-hour empty-room run, the gate that decides whether the trap travels.

## What is left, and why code cannot close it

- **S1.8** physical pass — pull the cable, close the lid, press mute mid-burst, press power. Needs
  hands on the machine and a room where a siren is acceptable.
- **S1.11** intelligibility — a native speaker has to listen to `warn-vi.ogg` and `warn-th.ogg`.
  Longest lead time in the whole plan.
- **S2.7** 30-minute empty-room run — checkpoint 2 (zero events, zero warnings).
- ~~S3/S4 deployment~~ — done 2026-08-20. Details in `TRIP.local.md` (not in git).
- **S6.4** 24-hour empty-room run — checkpoint 4, the gate that decides whether the trap travels.

## Open questions that block work

- **None block S1–S4.** Arming was decided (`on_lock` + manual); sound is two-stage (warning on
  motion, siren on tamper).
- `SIREN_SEC`, mode (`yelp`/`wail`) and volume need a final call during the S1 physical pass — spec
  §10 item 8.
- Warning wording and the language list want confirming before the files are treated as final —
  spec §10 items 9, 14, 15. Current default: `WARN_LANGS = ["vi", "en"]`.
- Boxes stay untouched until checkpoint 3 approval.

## Revision note

The two-stage sound model was added after the first version of this list: originally sound fired
only on `tamper`. Slice sizes grew by roughly 2 h in S1 (stage 1 plumbing, per-language files) and
0.5 h in S2 (wiring motion to the warning), which the schedule below does not yet reflect —
call it ~37 h rather than 34.5 h.

## Review follow-up, 2026-08-21

Full-project review for reliability; eleven findings, all closed. Order was the review's own: the
critical one first, because the rest are degradations and that one was a missing function.

- [x] **R1 (critical)** A dead camera silenced the whole trap. Tamper polling lived inside the
      camera's generator, which retried forever and never yielded — measured 446 reopen attempts,
      zero frames, no ticks. Split into `Camera.next_frame()` (one attempt, one answer) and
      `Runner.pump()` (a frame is optional, a tick is not). Verified on the real machine with
      `camera.device = /dev/video99`: the loop ran its full 30 s, reopened every 2.01 s, declared
      the camera gone and raised one `camera_gone` tamper event with zero frames.
- [x] **R2** `enforce_cap` was quadratic — 1644 ms → 9.7 ms on 1137 drops, same outcome.
- [x] **R3** Draining ran every tick; now on a 1 s timer, with tamper draining immediately.
- [x] **R4** Frames, manifests, state files and the MEGA copy are written atomically.
- [x] **R5** Vanished files no longer raise out of housekeeping.
- [x] **R6** Dead `Runner.run()` deleted.
- [x] **R7** `runner.py` (784) split into `runner.py` (395) + `lifecycle.py` + `modes.py`.
- [x] **R8** Stale "every 2 minutes" comments; the schedule has been `* * * * *` since the first
      live run. Sanitisation leak found alongside: the Pi's real username was in four files.
- [x] **R9** Declined with a measurement: `read_mode` costs 0.017 ms and runs once per sound
      decision. The expensive poll was `loginctl` at 1.88 ms **per tick** — now 1 s.
- [x] **R10** `guard report` streams the log: peak RSS 100 MB → 16.6 MB on a 33 MB file.
- [x] **R11** `_power_grab` annotated.

Found while fixing, not in the review:

- [x] "Evidence first, noise second" (spec 3.5) had never run: `set_ack_waiter` was called only by
      a test, so every manifest in the field says `sound_evidence_confirmed: false`. Wired, with
      two tests — one for the ordering, one for the cap on the wait.
- [x] `sound_latency_ms` was a manifest field nobody filled.
- [x] A dead camera logged one line per retry: 28 MB in two seconds on a stub.
- [x] The unified loop slept 250 ms on frames that had already arrived — 7.3 fps became 3.4 fps.
      Caught by measuring the live rate, not by the suite. Now 6.9 fps.

## Reaction time, 2026-08-21 (after the 30-minute run)

Reported: "it barely reacts to motion, and the messages arrive late." Both real, neither where I
would have guessed. Measured before changing anything.

- [x] **Detection is not slow — the threshold is a cliff.** Above `min_area_pct` a verdict takes
      two frames (0.29 s at the measured 6.8 fps); below it, never, however long you stand there.
      Live triggers that afternoon: 3.05, 3.11, 5.5, 5.5, 6.92, 7.78, 10.76, 11.17, 11.37,
      22.07 % against a threshold of 3.0 — two of them within 0.05 of the cutoff, so everything
      weaker was dropped silently.
- [x] Confirmation counted over a window (2 of 5) instead of consecutive frames, and one frame at
      `instant_area_pct` (7 %) is motion outright. Replayed over 29 real captures: same 7 person
      events caught, one frame sooner in the median, no new false event among the 22 empty-room
      ones.
- [x] `--trace` logs every analysed frame's changed_pct. The `log_ticks` knob had existed since S0
      with nothing reading it, so the number for a frame that did NOT fire was unobservable.
- [ ] **`min_area_pct` stays at 3.0 until the curtain is masked.** Same replay: 2.0 % took false
      events from 1 of 22 to 4 of 22. Next physical step, needs wind in the room:
      `guard suggest-mask --sec 120`, then `guard mask`, then drop the threshold to 2.0.
- [x] **Telegram was 37-60 s late** (event id against the Pi's journal: 37, 46, 55, 60 s). Deployed
      to the Pi: four passes per cron minute (`POLL_PASSES=4`, `POLL_INTERVAL_SEC=15`) and ssh
      connection reuse. Measured after: a pass costs ~2 s instead of 8-10 s, gaps 16-17 s instead
      of 60, 24 passes over six minutes with no overlap and no cut passes.
- [x] A run now spans most of its minute, so two guards: a non-blocking lock (two runs would both
      send the same event — a duplicate alert is what teaches you to stop reading them) and a
      `POLL_BUDGET_SEC` that hands the minute back. Both exercised on a stand.
- [x] **Alerts were still leading with an empty room** — two of four sends used `_000.jpg`, the
      oldest pre-buffer frame. The manifest sorts ahead of the frames it names, so it arrives
      naming one that is still uploading and the fallback took the lowest number. Fallback is now
      the newest frame present, and the album follows the key frame with the newest frames.

## First hotel run, 2026-08-26 to 28

Two nights of real use. Everything here came from watching what the trap actually did, not from
the plan; all of it is closed except the one line that needs the Pi.

- [x] **Both sirens fired at the owner** — coming back in is motion, picking your own laptop up is
      `scene_shift`. Stage 1 off (`warn_on_motion = false`), siren narrowed to `ac_offline` and
      `lid_closed`, everything else quiet. Stated cost: a power-button press is now silent.
- [x] **The alert led with a photograph of darkness.** A light coming on changes ~99 % of the
      pixels, so the transition frame won on raw change and it is the one frame where the sensor
      is still black. `key_frame` is now scored on what a frame is worth as a photograph.
- [x] **A six-photo album was six views of one second.** Cadence is a frame at once, then one
      every 10 s; the boost that jumped the throttle is disabled.
- [x] **The shutter clicks on capture**, taking over from the voice: one click per frame written,
      never interrupting, on the siren's arming gate.
- [ ] **`SEND_ORIGINAL=0` has to land on the receiver instead of the Pi.** There is no way into
      the home network from abroad — established by reading prod's `authorized_keys`, not by
      guessing: the Pi's six keys are all forced commands with `no-port-forwarding`, msx's reverse
      tunnel is `permitlisten="127.0.0.1:8899"` and nothing else, and the one full tunnel on
      `127.0.0.1:2222` is this laptop's own `revtunnel.service`, which is in the hotel. Prod is
      the only box both ends can always reach, so the relay learned to decline `send-doc`. To
      apply it (the code is committed; the switch is not set):

          ssh mt 'mkdir -p ~/.config && printf "SEND_DOC=0\n" > ~/.config/camtrap-tg.env'
          scp deploy/prod/camtrap-tg.sh mt:/tmp/ && ssh mt 'cp ~/camtrap-tg.sh ~/camtrap-tg.sh.bak.pre-senddoc && install -m 700 /tmp/camtrap-tg.sh ~/camtrap-tg.sh'

      Order matters: the switch first, then the script — the reverse leaves a tick where the new
      script is live with the switch unset, which is simply the old behaviour, so it is safe
      either way, but this order is never wrong.
- [ ] **When home, put the decision back where it belongs** — deploy the poller and remove the
      workaround, in that order, or the two disagree in a way that reads as a bug:

          scp deploy/pi/camtrap-poll.sh pi:/tmp/
          ssh pi 'sudo install -m 755 -o root -g root /tmp/camtrap-poll.sh /usr/local/bin/camtrap-poll.sh'
          ssh mt 'rm ~/.config/camtrap-tg.env'

      Until then, setting `SEND_ORIGINAL=1` on the Pi would do nothing, because prod is declining.
- [ ] **Give the Pi a way in, so this cannot happen again.** msx already has the shape: a key on
      prod with `restrict,port-forwarding,permitlisten="127.0.0.1:<port>"` and an `autossh` unit
      at the Pi end. One evening at home; it turns "wait until we are back" into `ssh -p <port>`.

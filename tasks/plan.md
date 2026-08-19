# camtrap phase 1 — implementation plan

## Context

`SPEC.md` is complete and the repository has no code: only the spec, README, `tools/make-siren.sh`
and the licence. Phase 1 has to be finished by **23 August** (3 days from 2026-08-19), because the
at-home shakedown occupies 23–25 August and departure is the 26th.

The owner's ranking decides the build order: **the siren is the primary feature**. Frames document
what happened, the siren changes what happens. So the first vertical slice is "cable pulled →
siren", not "camera → frames", and every slice after it leaves the system in a state that is
already worth travelling with.

Decisions taken with the owner before planning:

- **Local first.** Slices 1–4 are built against a local fake receiver (a directory). `camtrap-recv.sh`,
  `camtrap-tg.sh` and `camtrap-poll.sh` are written and tested locally; installing them on the VPS and
  the Pi is a separate step after the owner reads them (spec §8 "ask first").
- **Nothing is cut.** Full phase 1 scope. Risk is stated in "Schedule reality" below.
- **Sound is two-stage.** A spoken warning in the local language fires on `motion`; the siren is
  reserved for `tamper`. Stage 1 takes no hostile measures (no session lock, no input grab), stage 2
  does. `WARN_LANGS` is an ordered list (`["vi","en"]` for Vietnam, `["th","en"]` for Thailand).
- **Arming: `on_lock` plus manual.** The siren goes live when the screen is locked (after an exit
  delay) and can also be armed by hand with `camtrap arm`. Unlock disarms it. Capture is always on;
  only the noise is conditional. `ARM_MODE` stays a config switch (`on_lock` | `always` | `manual`).

## Dependency graph

```
config.py ─┬─▶ tamper.py ──────┬─▶ player.py ─▶ hold: watchdog / lock-session / inhibit
           │      ▲            │       ▲
           │      │ frames     │       └── arming.py (LockedHint, manual arm, exit delay, grace)
           ├─▶ camera.py ─▶ detector.py ─▶ event.py ─▶ spool.py ─▶ uploader.py ─▶ recv.sh (VPS)
           │                                              │                          │
           └─▶ heartbeat.py ◀── spool / player / tamper ──┘                          ▼
                                                                    poll.sh (Pi) ─▶ tg.sh ─▶ Telegram
cli.py ─▶ everything;  selftest.py ─▶ camera + player + uploader + inhibitors
```

Two independent roots: `tamper → player` (slice 1) and `camera → detector → event → spool`
(slice 2). They meet only in slice 5, where a scene shift becomes a tamper source. `recv.sh` defines
the wire contract, so it is written before `uploader.py` consumes it.

## Slices

Each slice is one complete path, not a horizontal layer. Tests from spec §7 live inside the slice
that introduces the behaviour — there is no "write the tests" task at the end.

### S0 · Skeleton — `camtrap status` prints real state (1.5 h)

`pyproject.toml`, `requirements.txt` (`opencv-python-headless`, `pytest`, `ruff` — none of the three
are installed yet; `ruff` is absent system-wide), venv, `src/camtrap/config.py` (TOML overrides over
in-code defaults, every threshold from spec §3, every sysfs path injectable for tests),
`src/camtrap/cli.py`, structured stdout logging in the `journalctl -t camtrap` style.

- **Accept**: `camtrap status` prints mode, arming state, spool depth, thresholds and resolved
  paths; `ruff check` and `ruff format --check` pass; `pytest` runs (zero tests is fine).
- **Verify**: `python -m camtrap status`, `ruff check src tests`, `pytest -q`.

### S1 · Sound: warning on motion, siren on cable pull and lid close (8 h) ← the feature

`tamper.py` (poll `ADP1/online` + both `ucsi-source-psy-USBC000:00{1,2}/online` + `LID0/state` every
`TAMPER_POLL_SEC`, debounce so a 1→0→1 bounce is one event), `player.py` (`pw-play`, explicit
`SOUND_SINK`, switch the card to a profile with a `Speaker` port, unmute, `SOUND_VOLUME_PCT`,
`Auto-Mute Mode` off, `SIREN_SEC`, cooldown, per-event and per-hour limits), the hold layers
(re-assert the audio path every `SOUND_HOLD_POLL_MS = 250`, `loginctl lock-session` on tamper,
`systemd-inhibit --what=sleep:idle:handle-lid-switch:handle-power-key`), `arming.py`
(`LockedHint` polling, `ARM_EXIT_DELAY_SEC = 60`, `GRACE_AFTER_UNLOCK_SEC = 300`, manual
`arm`/`disarm`), CLI: `run` (tamper+player only at this stage), `siren-test`, `arm`, `disarm`,
`input-scan`.

- **Accept**:
  1. Lock the screen, wait out the exit delay, pull the cable → siren within 3 s from the built-in
     speakers, at `SOUND_VOLUME_PCT`.
  2. Press mute, then volume-down, mid-burst → sound is back within one 250 ms tick; unplug/replug a
     headphone jack mid-burst → speakers keep sounding.
  3. Short power-button press → machine stays up; close the lid → machine stays awake and the siren
     keeps playing.
  4. Unlock with the password → siren stops, and no tamper fires for `GRACE_AFTER_UNLOCK_SEC`.
  5. Session unlocked (not armed) → pulling the cable produces a log line and **no sound**.
  6. `siren-test` plays through the speakers even while the default sink is the USB dongle.
  7. Unit tests: fake sysfs in `tmp_path` (1→0 fires, 0→1 does not, bounce fires once); fake mixer
     (mute reverted, volume restored, profile restored, `Auto-Mute` re-disabled); fake session
     (lock requested once per event); limits and cooldown honoured; no play during warm-up, in
     `paused`, or inside the unlock window; the optional device grab is released on process exit.
- **Verify**: `pytest -q tests/test_tamper.py tests/test_player.py tests/test_arming.py`, then the
  six physical checks above by hand. Keep `SOUND_VOLUME_PCT` low and `SIREN_SEC = 2` while iterating
  at home; do the final pass at the real values.

**Checkpoint 1 — nothing proceeds until the physical checks pass.** From here on, even if every
later slice is abandoned, the trap already does the thing the owner ranked first.

### S2 · Motion → frames, manifest, and the spoken warning (7.5 h)

`camera.py` (MJPG 1280×720; **the driver only offers 30 fps for MJPG and 10 fps for YUYV at that
size**, so the 5 fps in the spec is decimation in code, not a driver setting — retry on USB drop),
`detector.py` (MOG2 on blurred greyscale, `MIN_AREA_PCT`, `MIN_MOTION_FRAMES`, `WARMUP_SEC`, ignore
mask, `GLOBAL_CHANGE_PCT` → `light`), `event.py` (`PREBUFFER_FRAMES` ring at 1/s,
`SNAPSHOT_INTERVAL`, `EVENT_GAP`, `MAX_FRAMES_PER_EVENT` + `truncated`, JSON manifest),
`spool.py` (priorities: first frame and manifest first, `tamper` ahead of everything;
`SPOOL_MAX_MB`, drop from the middle, never the first frame, every drop logged; `RETENTION_DAYS`),
CLI: `calibrate`, `mask`.

- **Accept**: waving a hand produces `evt_<ts>_<nnn>.jpg` plus a manifest; 30 minutes of an empty
  room produces zero events; switching the light produces exactly one `light` frame; 60 s of
  continuous motion produces exactly 12 frames plus the pre-buffer; a trigger on frame N includes
  N-5…N; forcing spool overflow drops middles, keeps first frames, and logs every drop;
  `truncated: true` appears when the cap is hit.
- **Verify**: `pytest -q tests/test_detector.py tests/test_event.py tests/test_spool.py` (synthetic
  numpy frames, no camera), then `camtrap run` in the room for 30 minutes with nobody in it, then a
  deliberate walk-through.

**Checkpoint 2 — zero false events over a 30-minute empty-room run before moving on.**

### S3 · Frame reaches the receiver and is acknowledged (5 h)

`uploader.py` (ssh transport plus a local-directory sink for tests, exponential backoff capped at
`UPLOAD_RETRY_MAX_SEC`, **delete from spool only on `prod` ack**, receivers independent, `mega` sink
= copy into `~/MEGA/camtrap/` with its own retention), `deploy/prod/camtrap-recv.sh` (POSIX sh,
`set -eu`, forced command, frame → inbox, state/heartbeat update, one tick line to the journal).

- **Accept**: with the fake sink unreachable, frames accumulate and the spool is never trimmed;
  restoring it drains everything in priority order; a `mega` copy alone never removes a frame from
  the spool; `prod` failing does not stop the `mega` copy and a missing `~/MEGA` does not stop the
  upload; failure halfway through a batch resumes without duplicates.
- **Verify**: `pytest -q tests/test_uploader.py`, plus `sh -n deploy/prod/camtrap-recv.sh` and a
  local run of `camtrap-recv.sh` against a temporary inbox directory (no VPS involved yet).

### S4 · Alert with photo in Telegram (6 h)

`heartbeat.py` (every `HEARTBEAT_SEC`: timestamp, version, uptime, camera state, spool depth, event
count, mode, `ac_online`, lid, `sound_ok`, last siren time), `deploy/prod/camtrap-tg.sh`
(`sendPhoto`/`sendMessage`, token from stdin, never stored), `deploy/pi/camtrap-poll.sh` +
`camtrap-poll.cron` (events → Telegram, 🚨 `tamper` as its own message ahead of the queue, the
"handled → went silent" merge, `sound_ok = false` → 🔴, `HB_STALE_SEC`, `REPEAT_SEC`, state mutated
only after a successful send).

- **Accept**: against a stub inbox and a local HTTP stub standing in for the Bot API — a `tamper`
  event yields a separate 🚨 message with the signal list and whether the siren played; heartbeat
  silence yields 🔴; tamper followed by silence yields **one** merged message with two cut-off times;
  a failed send leaves state unmutated and retries on the next tick; `paused` suppresses the silence
  alert.
- **Verify**: `pytest -q tests/test_heartbeat.py`, `sh -n` on both shell scripts, and a scripted
  poll run against the stub. Nothing is installed on the VPS or the Pi in this slice.

**Checkpoint 3 — the owner reads `recv.sh`, `tg.sh` and `poll.sh`, then I ask for permission to
install them, create the new ssh keys, and add the cron file (spec §8).** Deployment order is
receiver → sender → poller.

### S5 · Tamper from the camera: lifted case vs switched light (4 h)

`cv2.phaseCorrelate` on normalised greyscale, `MOVE_SHIFT_PX`, degenerate correlation with a fully
changed frame → `tamper`; ALS arbiter reading `in_illuminance_raw` from both sensors.

- **Accept**: synthetic — a frame shifted by N px → `tamper`; the same frame brightened → `light`; a
  smeared unrecognisable frame → `tamper`; the arbiter is exercised on both outcomes including when
  it contradicts the correlation. Live — lifting the laptop without touching the cable sounds the
  siren; switching the room light produces `light` and **no** siren.
- **Verify**: `pytest -q tests/test_tamper_scene.py`, then both live checks.

### S6 · Trip harness (5 h)

`deploy/systemd/camtrap.service` (`--user`, inhibitors, `ExecStop` → pause), `deploy/install-*.sh`,
`camtrap install`, `selftest.py` (camera, detector, key, inbox, audio path, inhibitors, arming),
`docs/runbook.md` (what to do on arrival, what to do when it fires, how to pause before picking the
laptop up, what to do if the siren goes off for nothing).

- **Accept**: `systemctl --user restart camtrap` → warm-up → armed once the screen locks; `camtrap
  selftest` reports every check green on the real machine; a reboot brings the unit back;
  `camtrap pause` before collecting the laptop produces neither siren nor 🔴.
- **Verify**: `camtrap selftest`, a reboot, and the runbook walked through end to end by hand.

**Checkpoint 4 — a full 24-hour run in an empty room: zero false alerts and, above all, zero
sirens (spec criterion 5). This gate decides whether the trap travels.**

## Critical files

New: `src/camtrap/{config,cli,camera,detector,event,tamper,player,arming,spool,uploader,heartbeat,selftest}.py`,
`tests/test_*.py`, `deploy/prod/camtrap-{recv,tg}.sh`, `deploy/pi/camtrap-poll.{sh,cron}`,
`deploy/systemd/camtrap.service`, `deploy/install-*.sh`, `docs/runbook.md`, `pyproject.toml`,
`requirements.txt`.
Existing and reused as-is: `tools/make-siren.sh` (already generates `siren.ogg` in `yelp` and `wail`
modes — verified: 6 s / 28 KB stereo), `SPEC.md` as the source of every threshold name.

## Schedule reality

~37 h of work across 20–22 August is over 12 h/day (the two-stage sound model added ~2.5 h after
the first estimate). The owner chose to cut nothing, so this is
stated rather than negotiated: the slice order is the mitigation. After checkpoint 1 the siren
works; after checkpoint 2 frames are recorded locally; after checkpoint 4 the whole path is live. If
a day is lost, what travels is whatever cleared its last checkpoint — never a half-finished slice.

## Risks

- `opencv-python-headless` is a ~90 MB download and `cv2` is not installed yet — do it in S0, not on
  the last evening.
- The two USB-C PD ports may flap `online` on their own; the debounce in S1 must be measured against
  real hardware, not assumed.
- `loginctl lock-session` on every tamper will interrupt debugging; keep a config flag to disable it
  while iterating, defaulting to on.
- Testing at home means real sirens in a flat. Iterate at low volume and `SIREN_SEC = 2`; do the
  final pass at real values, deliberately.
- The Vietnamese and Thai warning files are synthesised by `espeak-ng`, a formant synthesiser, and
  both languages are tonal. Intelligibility cannot be judged from this side of the screen, so a
  native speaker has to listen before departure, or the files get replaced by a neural-TTS or human
  recording. This has the longest lead time of anything in phase 1 and no code can fix it — start
  it on day one.
- Housekeeping will hear the warning every day the room is serviced. That is correct behaviour, but
  the front desk may ask about it; the runbook needs a sentence on what to say.
- `kernel.sysrq = 16` today (only `sync`). If it ever rises to `1`/`438`, `Alt+SysRq+B` becomes a
  one-keystroke kill of the siren; the runbook must say so.

## Verification, end to end

1. `python -m venv .venv && .venv/bin/pip install -r requirements.txt`
2. `.venv/bin/ruff check src tests && .venv/bin/pytest -q` — the whole synthetic suite.
3. `tools/make-siren.sh --mode yelp` → `~/.local/share/camtrap/sounds/siren.ogg`.
4. `camtrap selftest` on the real machine — camera, audio path, inhibitors, arming, receiver key.
5. Physical checks per slice: pull the cable, close the lid, press mute, press power, lift the
   laptop, switch the light — each with the expected outcome listed in its slice.
6. 24-hour empty-room run, then read the journal: zero events, zero sirens.

## First action after approval

Write this plan to `tasks/plan.md` and the task list to `tasks/todo.md` (the owner asked for both;
plan mode allows no other file writes), then start S0.

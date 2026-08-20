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
- [ ] **S1.11** Intelligibility — get a native speaker to listen to `warn-vi.ogg` and `warn-th.ogg`,
      or replace them with better recordings. **Start this first, it has the longest lead time and
      code cannot fix it.** *Done when* each configured language is either confirmed by a speaker or
      replaced.
- [x] **S1.7** `camtrap input-scan` — list input devices reporting mute/volume keys; optional
      `EVIOCGRAB` on **external devices only**, released on fd close. *Done when* the grab survives a
      `kill -9` without leaving input captured.
- [ ] **S1.8** Physical pass (checks 1–6 in plan.md S1). Iterate at low volume with `SIREN_SEC = 2`,
      final pass at real values.

**→ CHECKPOINT 1.** Nothing proceeds until the physical checks pass. After this the trap already
does the thing ranked first.

---

## S2 · Motion → frames and manifest on disk (7 h, deps: S0)

- [ ] **S2.1** `camera.py` — MJPG 1280×720; the driver offers **only 30 fps for MJPG** (10 fps for
      YUYV), so 5 fps is decimation in code; retry on USB drop. *Done when* a 60 s capture yields
      ~300 decoded frames and survives an unplug/replug.
- [x] **S2.2** `detector.py` — MOG2 on blurred greyscale, `MIN_AREA_PCT`, `MIN_MOTION_FRAMES`,
      `WARMUP_SEC`, ignore mask, `GLOBAL_CHANGE_PCT` → `light`. *Done when* synthetic tests pass:
      triggers on a moving rectangle, silent on noise, brightness step classified `light`, mask
      respected, nothing during warm-up.
- [ ] **S2.3** `event.py` — `PREBUFFER_FRAMES` ring at 1/s, `SNAPSHOT_INTERVAL`, `EVENT_GAP`,
      `MAX_FRAMES_PER_EVENT` + `truncated`, JSON manifest. *Done when* 60 s of continuous motion
      yields exactly 12 frames plus pre-buffer, and a trigger on frame N includes N-5…N.
- [ ] **S2.4** `spool.py` — priorities (first frame and manifest first, `tamper` ahead of all),
      `SPOOL_MAX_MB`, drop from the middle, never the first frame, every drop logged,
      `RETENTION_DAYS`. *Done when* forced overflow keeps first frames and logs each drop.
- [ ] **S2.5** `camtrap calibrate` and `camtrap mask`. *Done when* calibrate prints a recommended
      `MIN_AREA_PCT` from real noise and mask writes polygons into the config.
- [ ] **S2.6** Wire `motion` → stage 1 warning (needs S1.9). *Done when* walking into the room plays
      the warning in `WARN_LANGS` order within 3 s of confirmed motion, and the same walk-through
      never produces a siren.
- [ ] **S2.7** 30-minute empty-room run.

**→ CHECKPOINT 2.** Zero false events **and zero warnings** over the empty-room run. A warning with
nobody in the room means `MIN_AREA_PCT` or the mask is wrong — fix before proceeding, because from
here on every detector mistake is audible in the corridor.

---

## S3 · Frame reaches the receiver and is acknowledged (5 h, deps: S2)

- [ ] **S3.1** `deploy/prod/camtrap-recv.sh` — POSIX sh, `set -eu`, forced command, frame → inbox,
      state/heartbeat update, one tick line. Defines the wire contract, so it comes first. *Done when*
      `sh -n` passes and a local run against a temp inbox stores a frame.
- [ ] **S3.2** `uploader.py` — ssh transport plus local-directory sink for tests, backoff capped at
      `UPLOAD_RETRY_MAX_SEC`, **spool deletion only on `prod` ack**. *Done when* an unreachable sink
      leaves the spool untrimmed and restoring it drains in priority order.
- [ ] **S3.3** `mega` sink — copy into `~/MEGA/camtrap/`, own retention, never counts as an ack.
      *Done when* tests prove `prod` failure does not stop the copy, a missing `~/MEGA` does not stop
      the upload, and a `mega`-only copy never frees a frame.
- [ ] **S3.4** Mid-batch failure resumes without duplicates.

---

## S4 · Alert with photo in Telegram (6 h, deps: S3)

- [ ] **S4.1** `heartbeat.py` — every `HEARTBEAT_SEC`: timestamp, version, uptime, camera, spool
      depth, event count, mode, `ac_online`, lid, `sound_ok`, last siren time.
- [ ] **S4.2** `deploy/prod/camtrap-tg.sh` — `sendPhoto`/`sendMessage`, token from stdin, never
      stored. *Done when* `sh -n` passes and a run against a local HTTP stub posts multipart.
- [ ] **S4.3** `deploy/pi/camtrap-poll.sh` + `.cron` — events → Telegram, 🚨 `tamper` as its own
      message ahead of the queue, `sound_ok = false` → 🔴, `HB_STALE_SEC`, `REPEAT_SEC`, state mutated
      only after a successful send. *Done when* a failed send leaves state unmutated and retries next
      tick.
- [ ] **S4.4** Merge "handled → went silent" into one message with two cut-off times; `paused`
      suppresses the silence alert.

**→ CHECKPOINT 3.** Owner reads `recv.sh`, `tg.sh`, `poll.sh`; then I ask permission for the boxes —
new ssh keys, install, cron (spec §8). Deployment order: receiver → sender → poller.

---

## S5 · Tamper from the camera: lifted case vs switched light (4 h, deps: S1 + S2)

- [x] **S5.1** `cv2.phaseCorrelate` on normalised greyscale, `MOVE_SHIFT_PX`; degenerate correlation
      with a fully changed frame → `tamper`. *Done when* synthetic shift → `tamper`, brightness step
      → `light`, smeared frame → `tamper`.
- [ ] **S5.2** ALS arbiter on `in_illuminance_raw` of both sensors, including the case where it
      contradicts the correlation.
- [ ] **S5.3** Live: lifting the laptop without touching the cable sounds the siren; switching the
      room light gives `light` and **no** siren.

---

## S6 · Trip harness (5 h, deps: S1–S5)

- [ ] **S6.1** `deploy/systemd/camtrap.service` (`--user`, inhibitors, `ExecStop` → pause) +
      `deploy/install-*.sh` + `camtrap install`. *Done when* a reboot brings the unit back armed.
- [ ] **S6.2** `selftest.py` — camera, detector, receiver key, inbox, audio path, inhibitors, arming.
      *Done when* every check reports green on the real machine.
- [ ] **S6.3** `docs/runbook.md` — arrival, what to do when it fires, pausing before picking the
      laptop up, what to do if the siren goes off for nothing, and the `kernel.sysrq` warning.
- [ ] **S6.4** 24-hour empty-room run; read the journal afterwards.

**→ CHECKPOINT 4.** Zero false alerts and zero sirens over 24 h (spec criterion 5). This gate decides
whether the trap travels.

---

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

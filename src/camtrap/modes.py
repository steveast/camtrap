"""The runs a person starts on purpose: preflight, watch, drill, sound check.

None of these is the trap doing its job — they are the ways of finding out whether it would. They
share the loop in `runner.py` and differ only in what they arm, what they publish and what they
print, which is why each one states its own compromises in place.
"""

from __future__ import annotations

import signal
import subprocess
import time

from . import log, sounds
from . import tamper as tamper_mod
from .camera import Camera
from .config import Config
from .heartbeat import publish as publish_heartbeat
from .inhibit import Inhibitor
from .player import SoundResponder, Stage
from .runner import Runner
from .state import MODE_ARMED, MODE_PAUSED, read_mode, write_mode


def audio_probe(cfg: Config, *, restore: bool = False) -> tuple[bool, str]:
    """Play a short, quiet burst to prove the path to the speakers actually carries audio.

    Checking that a file exists proves nothing: the sink can be gone, the profile wrong, the
    output muted at the ALSA level. This is the cheapest way to find that out before walking away,
    and at 20 % for 0.4 s it does not announce itself to the corridor.
    """
    from .player import AudioPath

    path = AudioPath(cfg)
    sink = path.prepare(volume_pct=cfg.arming.audio_probe_volume_pct)
    if not sink:
        if restore:
            path.restore_profile()
        return False, "no sink"
    target = cfg.siren_path if cfg.siren_path.exists() else None
    if target is None:
        if restore:
            path.restore_profile()
        return False, "no siren file"
    argv = [*cfg.sound.player_cmd, "--target", sink, str(target)]
    try:
        proc = subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    except OSError as exc:
        return False, str(exc)
    time.sleep(cfg.arming.audio_probe_sec)
    # A check puts the card back where it found it; arming deliberately leaves it on the speakers.
    if restore:
        path.restore_profile()
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
        return True, f"{sink} (probe cut short as intended)"
    stderr = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
    if proc.returncode not in (0, -15, 143):
        return False, stderr[:120] or f"player exited {proc.returncode}"
    return True, sink


def preflight(
    cfg: Config, *, probe: bool = True, restore_audio: bool = False
) -> tuple[bool, list[tuple[str, bool, str]]]:
    """Everything that must hold before the owner walks out of the room.

    Returns (ready, rows). A False here is the whole point of the command: leaving with a trap
    that cannot see, cannot shout or cannot deliver is worse than knowing it is broken.
    """
    from . import selftest

    rows: list[tuple[str, bool, str]] = []

    camera_check = selftest.check_camera(cfg)
    rows.append(("camera", camera_check.verdict != selftest.FAIL, camera_check.detail))

    missing = sounds.missing_sounds(cfg)
    rows.append(("sound files", not missing, ",".join(missing) if missing else "siren + warnings"))

    # What will actually make a noise, spelled out before the owner walks out. Both halves of
    # this policy were narrowed by hand, and a trap that is quieter than its owner remembers is
    # worse than one that is louder. A misspelled signal name raises here rather than being
    # silently unmatched at the moment the siren was supposed to sound.
    try:
        allowed = sorted(tamper_mod.siren_signals(cfg))
        policy_detail = f"siren: {', '.join(allowed) or 'nothing'}"
        policy_detail += "; motion: " + ("warning" if cfg.sound.warn_on_motion else "silent")
        rows.append(("sound policy", bool(allowed), policy_detail))
    except ValueError as exc:
        rows.append(("sound policy", False, str(exc)))

    if probe:
        ok, detail = audio_probe(cfg, restore=restore_audio)
        rows.append(("speakers", ok, detail))
    else:
        rows.append(("speakers", True, "probe skipped"))

    receiver = selftest.check_receiver(cfg)
    # A missing receiver is a warning, not a blocker: frames still accumulate locally and the
    # siren does not need the network at all.
    rows.append(("receiver", receiver.verdict != selftest.FAIL, receiver.detail))

    inhibit = selftest.check_inhibit()
    rows.append(("no-sleep", inhibit.verdict != selftest.FAIL, inhibit.detail))

    blocking = {"camera", "sound files", "speakers", "sound policy"}
    ready = all(ok for name, ok, _ in rows if name in blocking)
    return ready, rows


def watch(cfg: Config, *, minutes: float = 30.0, still: float = 10.0) -> int:
    """An empty-room run: real detection, real events, no sound (spec criterion 6).

    Checkpoints 2 and 4 ask one question — does anything fire when nobody is there. Answering it
    with the siren live would mean a police siren in a flat every time a curtain moved, so sound
    is decided and logged exactly as in a live run, then suppressed at the last step.
    """
    cfg.sound.dry_run = True
    cfg.arming.mode = "on_still"
    cfg.arming.arm_when_still_sec = still
    # A rehearsal is not evidence: keep test frames out of the cloud sync folder.
    cfg.upload.sinks = [name for name in cfg.upload.sinks if name != "mega"]

    # An observation run in `paused` measures nothing: the gate refuses every stage with
    # reason=paused before the detector's verdict is ever consulted, so the report comes back
    # CLEAN without having asked a single question. Arm for the duration, restore on every exit
    # path — leaving the trap armed after a rehearsal is how a siren goes off at nobody.
    previous_paused = read_mode(cfg.root).paused
    if previous_paused:
        write_mode(cfg.root, MODE_ARMED)
        print("(temporarily armed for this observation; paused is restored when it ends)")
    try:
        return _watch_body(cfg, minutes=minutes, still=still)
    finally:
        if previous_paused:
            write_mode(cfg.root, MODE_PAUSED)
        # Tell the receiver how this ended. Without it the poller keeps the last heartbeat —
        # `armed`, from mid-run — sees the agent go quiet, and reports the laptop as taken.
        publish_heartbeat(cfg)


def _watch_body(cfg: Config, *, minutes: float, still: float) -> int:
    from .arming import Arming

    missing = sounds.missing_sounds(cfg)
    if missing:
        print(f"missing sounds: {', '.join(missing)} — run `guard sounds` first")
        return 1

    seconds = minutes * 60.0
    print(f"OBSERVATION — {minutes:.0f} min, real detection, sound suppressed and logged.")
    print(f"Arms {still:.0f} s after the room goes quiet. Leave the room; do not come back in.")
    print()
    print("What this measures: whether an empty room produces events. One `light` event when the")
    print("screen blanks is expected — that is the room getting darker, not a fault.")
    print()
    print("Ends by itself. Afterwards: guard report")
    print()

    with Inhibitor() as inhibitor:
        camera = Camera(cfg)
        runner = Runner(cfg, inhibitor=inhibitor, camera=camera, arming=Arming(cfg))
        runner.started = runner.clock()
        runner.arming.start(now=runner.started)
        deadline = runner.started + seconds
        signal.signal(signal.SIGINT, runner.request_stop)
        signal.signal(signal.SIGTERM, runner.request_stop)
        if not camera.open():
            print("camera unavailable — observing the sysfs signals only")
        log.emit("start", mode="watch", arming=cfg.arming.mode, minutes=minutes, dry_run=True)
        try:
            runner.pump(camera, deadline=deadline)
        finally:
            runner.finish()
            camera.release()
            log.emit(
                "stop",
                mode="watch",
                frames=runner.stats.frames,
                events=runner.stats.motion_events + runner.stats.light_events,
                tamper=runner.stats.tamper_events,
                would_sirens=runner.stats.sirens,
                would_warnings=runner.stats.warnings,
            )

    audible = runner.stats.sirens + runner.stats.warnings
    print()
    print(
        f"{'CLEAN' if audible == 0 else 'NOT CLEAN'}: {runner.stats.frames} frames, "
        f"{runner.stats.motion_events} motion, {runner.stats.light_events} light, "
        f"{runner.stats.tamper_events} tamper — would have sounded {audible} time(s)"
    )
    if audible:
        print("Next: guard report, then guard mask (cover what moves) and guard calibrate.")
    return 0


def drill(
    cfg: Config, *, volume_pct: int = 40, siren_sec: float = 2.0, seconds: float = 180.0
) -> int:
    """A rehearsal for checkpoint 1: armed immediately, short quiet siren, no session lock.

    The six physical checks in the plan are otherwise six separate experiments with a locked
    screen between them. Here they are one run: pull the cable, press mute, close the lid, and
    watch what the agent reports.
    """
    from .arming import Arming

    cfg.sound.volume_pct = volume_pct
    cfg.sound.warn_volume_pct = volume_pct
    cfg.sound.siren_sec = siren_sec
    # A drill must not lock the screen on every trigger: the point is to keep testing.
    cfg.sound.lock_session_on_tamper = False
    cfg.sound.cooldown_sec = min(cfg.sound.cooldown_sec, 8.0)
    cfg.sound.max_per_event = 99
    cfg.arming.mode = "always"
    cfg.arming.exit_delay_sec = 0.0
    cfg.arming.grace_after_unlock_sec = 0.0
    cfg.detector.warmup_sec = min(cfg.detector.warmup_sec, 3.0)
    # Same rule as watch(): a drill does not publish frames to the cloud.
    cfg.upload.sinks = [name for name in cfg.upload.sinks if name != "mega"]

    missing = sounds.missing_sounds(cfg)
    if missing:
        print(f"missing sounds: {', '.join(missing)} — run `guard sounds` first")
        return 1

    pct = f"{volume_pct}%"
    print(f"DRILL — armed immediately, siren {siren_sec:.0f}s at {pct}, screen will NOT lock.")
    print()
    print("  1. pull the power cable      -> siren within ~2 s")
    print("  2. press mute mid-siren      -> sound returns within 250 ms, logged as sound_hold")
    print("  3. turn the volume down      -> restored, also logged")
    print("  4. close the lid             -> siren, and the machine stays awake")
    print("  5. press the power button    -> machine stays up")
    print("  6. plug the cable back in    -> nothing (replugging is not a signal)")
    print()
    print(f"Runs for {seconds:.0f}s, Ctrl+C to stop early.")
    print()

    with Inhibitor() as inhibitor:
        camera = Camera(cfg)
        runner = Runner(cfg, inhibitor=inhibitor, camera=camera, arming=Arming(cfg))
        runner.started = runner.clock()
        runner.arming.start(now=runner.started)
        deadline = runner.started + seconds
        signal.signal(signal.SIGINT, runner.request_stop)
        signal.signal(signal.SIGTERM, runner.request_stop)
        camera_ok = camera.open()
        if not camera_ok:
            print("camera unavailable — the cable and lid checks still work")
        log.emit(
            "start",
            mode="drill",
            arming=cfg.arming.mode,
            siren_sec=siren_sec,
            volume=volume_pct,
            camera=camera_ok,
        )
        try:
            runner.pump(camera, deadline=deadline)
        finally:
            runner.finish()
            camera.release()
            publish_heartbeat(cfg)
            log.emit(
                "stop",
                mode="drill",
                frames=runner.stats.frames,
                tamper=runner.stats.tamper_events,
                sirens=runner.stats.sirens,
                warnings=runner.stats.warnings,
            )
    print()
    print(
        f"drill over: {runner.stats.tamper_events} tamper signal(s), "
        f"{runner.stats.sirens} siren(s), {runner.stats.warnings} warning(s), "
        f"signals: {', '.join(runner.stats.signals) or 'none'}"
    )
    return 0


def sound_selftest(cfg: Config, stage: Stage, *, volume_pct: int | None = None) -> int:
    """Play one stage on the real speakers, forcing the audio path as an event would.

    volume_pct overrides the configured level for a rehearsal: the first test in a flat does not
    need to be the full 100 %, and a quiet run still proves the whole path works.
    """
    missing = sounds.missing_sounds(cfg)
    wanted = "siren" if stage is Stage.SIREN else "warn-"
    blocking = [name for name in missing if name.startswith(wanted)]
    if blocking:
        # A machine-readable reason is not an answer for the person standing there.
        print(f"nothing to play: {', '.join(blocking)} not generated yet")
        print(f"  expected in {cfg.sounds_dir}")
        print("  fix:  guard sounds        (or tools/make-siren.sh --mode yelp)")
        log.emit("selftest", stage=stage.value, ok=False, reason="missing_file")
        return 1

    if volume_pct is not None:
        cfg.sound.volume_pct = volume_pct
        cfg.sound.warn_volume_pct = volume_pct
    responder = SoundResponder(cfg)
    responder.set_gate(lambda _stage, _now: (True, ""))
    now = time.monotonic()
    result = (
        responder.on_tamper(["selftest"], now=now)
        if stage is Stage.SIREN
        else responder.on_motion(now=now)
    )
    if not result.played:
        log.emit("selftest", stage=stage.value, ok=False, reason=result.reason)
        return 1
    duration = cfg.sound.siren_sec if stage is Stage.SIREN else cfg.sound.warn_timeout_sec
    deadline = now + duration
    while time.monotonic() < deadline and responder.playing is not None:
        responder.hold_tick(now=time.monotonic())
        time.sleep(cfg.sound.hold_poll_ms / 1000.0)
    log.emit(
        "selftest",
        stage=stage.value,
        ok=True,
        sink=responder.audio.sink or "-",
        volume=cfg.sound.volume_pct if stage is Stage.SIREN else cfg.sound.warn_volume_pct,
    )
    return 0

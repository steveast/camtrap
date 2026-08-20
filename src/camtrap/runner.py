"""The run loop: capture, detect, slice into events, respond with sound, hold the sound on.

The camera drives the cadence and sysfs is polled between frames, so a cable pull is noticed even
while the detector is busy. Two independent paths converge here: motion in frame produces a spoken
warning, tampering produces the siren.
"""

from __future__ import annotations

import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import log, sounds
from . import tamper as tamper_mod
from .arming import Arming
from .camera import Camera
from .config import Config
from .detector import Detector, EventKind
from .event import EventWriter
from .heartbeat import HeartbeatSender
from .heartbeat import build as build_heartbeat
from .inhibit import Inhibitor
from .player import SoundResponder, Stage
from .spool import Spool
from .state import read_mode
from .uploader import Uploader


@dataclass
class LoopStats:
    ticks: int = 0
    frames: int = 0
    tamper_events: int = 0
    motion_events: int = 0
    light_events: int = 0
    warnings: int = 0
    sirens: int = 0
    signals: list[str] = field(default_factory=list)


class Runner:
    """Owns the loop so tests can step it deterministically instead of sleeping."""

    def __init__(
        self,
        cfg: Config,
        *,
        monitor: tamper_mod.TamperMonitor | None = None,
        responder: SoundResponder | None = None,
        arming: Arming | None = None,
        inhibitor: Inhibitor | None = None,
        detector: Detector | None = None,
        events: EventWriter | None = None,
        spool: Spool | None = None,
        camera: Camera | None = None,
        uploader: Uploader | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.monitor = monitor if monitor is not None else tamper_mod.TamperMonitor(cfg)
        self.responder = responder if responder is not None else SoundResponder(cfg)
        self.arming = arming if arming is not None else Arming(cfg)
        self.detector = detector if detector is not None else Detector(cfg)
        self.events = events if events is not None else EventWriter(cfg)
        self.spool = spool if spool is not None else Spool(cfg)
        self.uploader = uploader if uploader is not None else Uploader(cfg, self.spool)
        self.heartbeat = HeartbeatSender(cfg, self.uploader)
        self.camera = camera
        self.started = 0.0
        self.inhibitor = inhibitor
        self.clock = clock
        self.stats = LoopStats()
        self._stop = False
        self._last_housekeeping = float("-inf")
        self._warned_event: str | None = None
        self.responder.set_gate(self.arming.gate)

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def finish(self) -> None:
        """Close an event that is still open, so its manifest is complete on disk."""
        if self.events.active is not None:
            closed = self.events.close(now=self.clock())
            log.emit("event_flush", id=closed.event_id, frames=closed.frames_written)
        # One last drain attempt: frames already acknowledged are gone, the rest stay spooled.
        self.uploader.drain(now=self.clock(), limit=32)

    def step(self, now: float) -> list[tamper_mod.Signal]:
        """One iteration: arming state, tamper poll, sound, hold, then network work."""
        self.arming.poll(now=now)
        self.responder.hold_tick(now=now)
        signals = self.monitor.poll(now=now)
        self.stats.ticks += 1
        if signals:
            self._tamper(signals, now=now)
        self._housekeeping(now)
        return signals

    def motion(self, now: float) -> None:
        """Stage 1: someone is in the room."""
        result = self.responder.on_motion(now=now)
        if result.played:
            self.stats.warnings += 1
            self.events.mark_sound(
                stage=Stage.WARNING.value, evidence_confirmed=result.evidence_confirmed
            )

    # --- camera path ---------------------------------------------------------

    def on_frame(self, frame: np.ndarray, now: float) -> EventKind:
        """One decimated frame: detect, slice into an event, respond with sound."""
        self.stats.frames += 1
        self.events.observe(frame, now=now)
        detection = self.detector.submit(frame, now=now)

        if detection.kind is EventKind.NONE:
            self.arming.note_quiet(now=now)
        else:
            self.arming.note_activity(now=now)

        if detection.kind is EventKind.MOTION:
            self.stats.motion_events += 1
            event = self.events.begin(EventKind.MOTION, now=now, frame=frame)
            self.events.note_motion(now=now)
            # One warning per event. Asking per frame would re-enter the responder five times a
            # second and bury the journal in sound_skip lines.
            if self._warned_event != event.event_id:
                self._warned_event = event.event_id
                self.motion(now)
        elif detection.kind is EventKind.LIGHT:
            self.stats.light_events += 1
            self.events.begin(EventKind.LIGHT, now=now, frame=frame)
            if self.cfg.sound.warn_on_light:
                self.motion(now)
        elif detection.kind is EventKind.TAMPER:
            signal = self.monitor.report_external(
                tamper_mod.SCENE_SHIFT, detail=detection.detail, now=now
            )
            self._tamper([signal], now=now, frame=frame)
        else:
            self.events.feed(frame, now=now)

        closed = self.events.maybe_close(now=now)
        if closed is not None:
            self.responder.end_event()
        self._housekeeping(now)
        return detection.kind

    def _tamper(self, signals: list[tamper_mod.Signal], *, now: float, frame=None) -> None:
        self.stats.tamper_events += 1
        self.stats.signals.extend(s.name for s in signals)
        names = [s.name for s in signals]
        event = self.events.begin(EventKind.TAMPER, now=now, frame=frame, signals=names)
        self.spool.mark_tamper(event.event_id)
        self.events.note_motion(now=now)
        if not tamper_mod.plays_siren(signals):
            return
        result = self.responder.on_tamper(names, now=now)
        if result.played:
            self.stats.sirens += 1
            self.events.mark_sound(
                stage=Stage.SIREN.value, evidence_confirmed=result.evidence_confirmed
            )

    def _housekeeping(self, now: float) -> None:
        # Uploading and heartbeats run from here so the capture path never blocks on the network:
        # a sagging link must not slow the camera down (spec 3.5).
        #
        # And it must never STOP the camera either. Delivery touches the filesystem, ssh and the
        # network; detection and the siren do not need any of them. An exception here used to take
        # the whole trap down, which is the one failure that must not be possible.
        try:
            self._housekeeping_inner(now)
        except Exception as exc:
            log.emit("housekeeping_error", why=f"{type(exc).__name__}: {exc}")

    def _housekeeping_inner(self, now: float) -> None:
        self.uploader.drain(now=now, limit=8)
        if self.heartbeat.due(now=now):
            self.heartbeat.maybe_send(
                build_heartbeat(
                    self.cfg,
                    started=self.started,
                    now=now,
                    stats=self.stats,
                    monitor=self.monitor,
                    arming=self.arming,
                    spool=self.spool,
                    camera=self.camera,
                ),
                now=now,
            )
        if now - self._last_housekeeping < 60.0:
            return
        self._last_housekeeping = now
        self.spool.enforce_cap()
        self.spool.enforce_retention()

    def run(self, *, max_ticks: int | None = None) -> LoopStats:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        started = self.clock()
        self.started = started
        self.arming.start(now=started)
        log.emit(
            "start",
            mode=read_mode(self.cfg.root).name,
            arming=self.cfg.arming.mode,
            langs=",".join(self.cfg.sound.warn_langs) or "-",
            inhibit=bool(self.inhibitor and self.inhibitor.active),
        )
        interval = max(0.05, self.cfg.tamper.poll_sec)
        hold = max(0.05, self.cfg.sound.hold_poll_ms / 1000.0)
        ticks = 0
        next_poll = started
        while not self._stop:
            now = self.clock()
            if now >= next_poll:
                self.step(now)
                next_poll = now + interval
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
            else:
                self.responder.hold_tick(now=now)
            time.sleep(hold)
        log.emit(
            "stop",
            ticks=self.stats.ticks,
            tamper=self.stats.tamper_events,
            sirens=self.stats.sirens,
            warnings=self.stats.warnings,
        )
        return self.stats


def run_forever(cfg: Config) -> int:
    """Capture loop plus tamper polling. The camera drives the cadence; sysfs is polled between
    frames, so a cable pull is noticed even while the detector is busy."""
    with Inhibitor() as inhibitor:
        if not inhibitor.active:
            log.emit("warn", reason="no sleep inhibitor: a closed lid may suspend the machine")
        camera = Camera(cfg)
        runner = Runner(cfg, inhibitor=inhibitor, camera=camera)
        runner.started = runner.clock()
        runner.arming.start(now=runner.started)
        log.emit(
            "start",
            mode=read_mode(cfg.root).name,
            arming=cfg.arming.mode,
            langs=",".join(cfg.sound.warn_langs) or "-",
            inhibit=inhibitor.active,
        )
        signal.signal(signal.SIGTERM, runner.request_stop)
        signal.signal(signal.SIGINT, runner.request_stop)
        if not camera.open():
            log.emit("warn", reason="camera unavailable at start; tamper monitoring continues")
        try:
            for frame in camera.frames():
                now = runner.clock()
                runner.on_frame(frame, now)
                runner.step(now)
                if runner._stop:
                    break
        finally:
            runner.finish()
            camera.release()
            if camera.status.gone and cfg.tamper.camera_gone_is_tamper:
                now = runner.clock()
                signal_ = runner.monitor.report_external(
                    tamper_mod.CAMERA_GONE, detail="capture device disappeared", now=now
                )
                runner._tamper([signal_], now=now)
            log.emit(
                "stop",
                frames=runner.stats.frames,
                tamper=runner.stats.tamper_events,
                sirens=runner.stats.sirens,
                warnings=runner.stats.warnings,
            )
    return 0


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

    blocking = {"camera", "sound files", "speakers"}
    ready = all(ok for name, ok, _ in rows if name in blocking)
    return ready, rows


def watch(cfg: Config, *, minutes: float = 30.0, still: float = 10.0) -> int:
    """An empty-room run: real detection, real events, no sound (spec criterion 6).

    Checkpoints 2 and 4 ask one question — does anything fire when nobody is there. Answering it
    with the siren live would mean a police siren in a flat every time a curtain moved, so sound
    is decided and logged exactly as in a live run, then suppressed at the last step.
    """
    from .arming import Arming

    cfg.sound.dry_run = True
    cfg.arming.mode = "on_still"
    cfg.arming.arm_when_still_sec = still
    # A rehearsal is not evidence: keep test frames out of the cloud sync folder.
    cfg.upload.sinks = [name for name in cfg.upload.sinks if name != "mega"]

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
            print("camera unavailable — nothing to observe")
            return 1
        log.emit("start", mode="watch", arming=cfg.arming.mode, minutes=minutes, dry_run=True)
        try:
            for frame in camera.frames():
                now = runner.clock()
                runner.on_frame(frame, now)
                runner.step(now)
                if runner._stop or now >= deadline:
                    break
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
            while not runner._stop and runner.clock() < deadline:
                now = runner.clock()
                if camera_ok:
                    frame = camera.read()
                    if frame is not None and runner.stats.frames % 6 == 0:
                        runner.on_frame(frame, now)
                    elif frame is not None:
                        runner.stats.frames += 1
                runner.step(now)
                time.sleep(cfg.sound.hold_poll_ms / 1000.0)
        finally:
            runner.finish()
            camera.release()
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

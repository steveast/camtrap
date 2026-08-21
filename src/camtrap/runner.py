"""The run loop: capture, detect, slice into events, respond with sound, hold the sound on.

The camera drives the cadence and sysfs is polled between frames, so a cable pull is noticed even
while the detector is busy. Two independent paths converge here: motion in frame produces a spoken
warning, tampering produces the siren.
"""

from __future__ import annotations

import atexit
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
from .heartbeat import publish as publish_heartbeat
from .inhibit import Inhibitor
from .player import SoundResponder, Stage
from .spool import Spool
from .state import MODE_ARMED, MODE_PAUSED, read_mode, write_mode
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
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self.sleep = sleep
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
        self._last_drain = float("-inf")
        self._warned_event: str | None = None
        self._armed_actions_done = False
        #: The most recent decoded frame, so a tamper signal that arrives between frames (power,
        #: lid, the power button) still gets a picture of the room as it is, not just the
        #: pre-buffer from seconds earlier.
        self._last_frame: np.ndarray | None = None
        self._burst_until = float("-inf")
        self._camera_gone_reported = False
        self.responder.set_ack_waiter(self._await_evidence)
        self._power_grab: object | None = None
        self.responder.set_gate(self.arming.gate)

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def finish(self) -> None:
        """Close an event that is still open, so its manifest is complete on disk."""
        self.on_disarmed()
        if self.events.active is not None:
            closed = self.events.close(now=self.clock())
            log.emit("event_flush", id=closed.event_id, frames=closed.frames_written)
        # One last drain attempt: frames already acknowledged are gone, the rest stay spooled.
        self.uploader.drain(now=self.clock(), limit=32)

    def on_armed(self) -> None:
        """Once, when the trap goes live: lock the screen and take the power buttons.

        The owner has left the room. An unlocked session is a way into the machine and a way to
        stop the agent; an unguarded power button is one press away from suspending the machine and
        ending the trap, because KDE acts on that press before logind ever sees it.
        """
        if self._armed_actions_done:
            return
        self._armed_actions_done = True

        if self.cfg.sound.lock_on_arm:
            try:
                self.responder._run([*self.cfg.sound.loginctl_cmd, "lock-session"])
                log.emit("armed_action", action="screen_locked")
            except Exception as exc:
                log.emit("armed_action_failed", action="lock", why=str(exc))

        if self.cfg.sound.grab_power_button:
            try:
                from . import inputdev

                buttons = inputdev.power_buttons(inputdev.scan())
                if buttons:
                    grab = inputdev.Grab(buttons)
                    held = grab.acquire()
                    if held:
                        self._power_grab = grab
                        log.emit("armed_action", action="power_button_grabbed", devices=held)
                    else:
                        log.emit("armed_action_failed", action="power_grab", why="no device opened")
            except Exception as exc:
                log.emit("armed_action_failed", action="power_grab", why=str(exc))

    def _power_button_presses(self, now: float) -> list[tamper_mod.Signal]:
        """A press on the power button we are holding is interference, not a shutdown request."""
        if self._power_grab is None:
            return []
        try:
            from . import inputdev

            codes = self._power_grab.read_key_presses()
        except Exception as exc:
            log.emit("input_read_failed", why=str(exc))
            return []
        if inputdev.KEY_POWER not in codes:
            return []
        return [
            self.monitor.report_external(
                tamper_mod.POWER_BUTTON, detail="power button pressed while armed", now=now
            )
        ]

    def on_disarmed(self) -> None:
        """Hand the power buttons back. The owner is here; they may want to switch it off."""
        if not self._armed_actions_done:
            return
        self._armed_actions_done = False
        if self._power_grab is not None:
            self._power_grab.release()
            self._power_grab = None
            log.emit("disarmed_action", action="power_button_released")

    def _sync_armed_actions(self, now: float) -> None:
        allowed, _reason = self.arming.gate(Stage.SIREN, now)
        if allowed:
            self.on_armed()
        else:
            self.on_disarmed()

    def step(self, now: float) -> list[tamper_mod.Signal]:
        """One iteration: arming state, tamper poll, sound, hold, then network work."""
        self.arming.poll(now=now)
        self._sync_armed_actions(now)
        self.responder.hold_tick(now=now)
        signals = self.monitor.poll(now=now)
        signals.extend(self._power_button_presses(now))
        self.stats.ticks += 1
        if signals:
            self._tamper(signals, now=now)
        self._housekeeping(now)
        return signals

    def motion(self, now: float) -> None:
        """Stage 1: someone is in the room."""
        started = self.clock()
        result = self.responder.on_motion(now=now)
        if result.played:
            self.stats.warnings += 1
            self.events.mark_sound(
                stage=Stage.WARNING.value,
                latency_ms=int((self.clock() - started) * 1000),
                evidence_confirmed=result.evidence_confirmed,
            )

    # --- camera path ---------------------------------------------------------

    def on_frame(self, frame: np.ndarray, now: float) -> EventKind:
        """One decimated frame: detect, slice into an event, respond with sound."""
        self.stats.frames += 1
        self._last_frame = frame
        self.events.observe(frame, now=now)

        # A tamper burst outranks the detector: keep taking frames whatever it thinks.
        if now < self._burst_until and self.events.active is not None:
            self.events.feed(frame, now=now, changed_pct=0.0, force=True)
        detection = self.detector.submit(frame, now=now)

        if detection.kind is EventKind.NONE:
            self.arming.note_quiet(now=now)
        else:
            self.arming.note_activity(now=now)

        if detection.kind is EventKind.MOTION:
            self.stats.motion_events += 1
            event = self.events.begin(
                EventKind.MOTION, now=now, frame=frame, changed_pct=detection.changed_pct
            )
            self.events.note_motion(now=now)
            # Motion inside an open event still deserves a frame when it is big enough.
            self.events.feed(frame, now=now, changed_pct=detection.changed_pct)
            # One warning per event. Asking per frame would re-enter the responder five times a
            # second and bury the journal in sound_skip lines.
            if self._warned_event != event.event_id:
                self._warned_event = event.event_id
                self.motion(now)
        elif detection.kind is EventKind.LIGHT:
            self.stats.light_events += 1
            self.events.begin(
                EventKind.LIGHT, now=now, frame=frame, changed_pct=detection.changed_pct
            )
            if self.cfg.sound.warn_on_light:
                self.motion(now)
        elif detection.kind is EventKind.TAMPER:
            signal = self.monitor.report_external(
                tamper_mod.SCENE_SHIFT, detail=detection.detail, now=now
            )
            self._tamper([signal], now=now, frame=frame)
        else:
            self.events.feed(frame, now=now, changed_pct=detection.changed_pct)

        closed = self.events.maybe_close(now=now)
        if closed is not None:
            self.responder.end_event()
        self._housekeeping(now)
        return detection.kind

    def _await_evidence(self, timeout: float) -> bool:
        """Get the picture off the box before the room gets loud — but never wait long.

        `mark_tamper` has already put this event at the head of the queue, so one drain sends the
        manifest and the first frame ahead of everything else. The wait is capped: a siren that
        waits on a dead uplink is a siren that never sounds, and the point of the sound is that it
        works with no network at all.

        This is spec 3.5 "evidence first, noise second", which until now existed only as a setter
        nothing called — every manifest in the field reads `sound_evidence_confirmed: false`.
        """
        deadline = self.clock() + timeout
        while True:
            # Delivery must never be able to stop the siren. This wait sits between a tamper
            # signal and the sound, so an exception escaping here would silence the alarm — the
            # one failure mode the whole design refuses to allow.
            try:
                report = self.uploader.drain(now=self.clock(), limit=2)
            except Exception as exc:
                log.emit("evidence_wait_error", why=f"{type(exc).__name__}: {exc}")
                return False
            if any(not name.endswith(".json") for name in report.acknowledged):
                return True
            if not report.failed:
                return False  # nothing waiting, or nothing that can be sent
            if self.clock() >= deadline:
                log.emit("evidence_wait_expired", timeout=round(timeout, 2))
                return False
            self.sleep(min(0.25, self.cfg.spool.drain_interval_sec))

    def pump(
        self,
        camera: Camera,
        *,
        deadline: float | None = None,
        max_iterations: int | None = None,
    ) -> None:
        """The one capture loop, used by run, watch and drill alike.

        Its shape is the fix for the worst bug this project had: a frame is optional, `step()` is
        not. Detection needs the camera; tamper polling, the siren and the heartbeat do not, and
        coupling them meant a dead camera produced a silent trap.
        """
        iterations = 0
        while not self._stop:
            now = self.clock()
            if deadline is not None and now >= deadline:
                return
            frame = camera.next_frame()
            if frame is not None:
                self.on_frame(frame, self.clock())
            self.step(self.clock())
            self._note_camera_state(camera, self.clock())
            iterations += 1
            if max_iterations is not None and iterations >= max_iterations:
                return
            # Idle: pace by the hold tick when frames are flowing, by the reopen delay when not.
            self.sleep(
                self.cfg.sound.hold_poll_ms / 1000.0
                if frame is not None
                else self.cfg.camera.reopen_delay_sec
            )

    def _note_camera_state(self, camera: Camera, now: float) -> None:
        """Raise a tamper signal the first time the camera is declared gone.

        A camera vanishing in a locked room is suspicious in itself, so it alerts — but without the
        siren by default, because a bus glitch is more plausible than a hand on the cable of a
        built-in camera (spec section 10, item 5).
        """
        if not camera.status.gone or self._camera_gone_reported:
            if not camera.status.gone:
                self._camera_gone_reported = False
            return
        self._camera_gone_reported = True
        if not self.cfg.tamper.camera_gone_is_tamper:
            return
        signal_ = self.monitor.report_external(
            tamper_mod.CAMERA_GONE, detail="capture device stopped delivering frames", now=now
        )
        self._tamper([signal_], now=now)

    def _tamper(self, signals: list[tamper_mod.Signal], *, now: float, frame=None) -> None:
        self.stats.tamper_events += 1
        self.stats.signals.extend(s.name for s in signals)
        names = [s.name for s in signals]
        # Power, lid and power-button signals arrive between frames, so use the newest frame we
        # have rather than opening the event with nothing but stale pre-buffer.
        if frame is None:
            frame = self._last_frame
        self._burst_until = now + self.cfg.event.tamper_burst_sec
        event = self.events.begin(EventKind.TAMPER, now=now, frame=frame, signals=names)
        self.spool.mark_tamper(event.event_id)
        self.events.note_motion(now=now)
        if not tamper_mod.plays_siren(signals):
            return
        started = self.clock()
        result = self.responder.on_tamper(names, now=now)
        if result.played:
            self.stats.sirens += 1
            self.events.mark_sound(
                stage=Stage.SIREN.value,
                latency_ms=int((self.clock() - started) * 1000),
                evidence_confirmed=result.evidence_confirmed,
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
        if now - self._last_drain >= self.cfg.spool.drain_interval_sec:
            self._last_drain = now
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


def system_is_stopping() -> bool:
    """True while systemd is shutting the machine down.

    The owner powers the laptop off every day; that is not a theft. A thief cannot reach a clean
    shutdown either — the session is locked on tamper, and pulling the power or holding the button
    kills the process without a signal, so there is no shutdown to observe.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() in ("stopping", "offline")


def _final_word(cfg: Config) -> None:
    """Tell the receiver how this ended, and whether it ended on purpose."""
    from .state import MODE_PAUSED, read_mode, write_mode

    if system_is_stopping() and not read_mode(cfg.root).paused:
        # A deliberate shutdown is an expected offline. Without this the last heartbeat says
        # `armed`, the agent goes quiet, and the poller reports a stolen laptop every single
        # evening — which is how alerting gets muted.
        write_mode(cfg.root, MODE_PAUSED)
        log.emit("shutdown", detail="system stopping; marking the offline expected")
    publish_heartbeat(cfg)


def harden_process(cfg: Config) -> None:
    """Make the agent independent of the terminal that started it.

    A run was killed at 22:31 when its terminal closed, and because the process died on SIGHUP the
    receiver never learned it had stopped: the last heartbeat still said `armed`, which reads as a
    stolen laptop, and the poller repeated that alert for eight hours. A camera trap must outlive
    the shell that launched it.
    """
    for signal_name in ("SIGHUP", "SIGPIPE"):
        handler = getattr(signal, signal_name, None)
        if handler is not None:
            signal.signal(handler, signal.SIG_IGN)
    log.set_file(cfg.log_file or None)
    # Whatever happens next, tell the receiver how this ended.
    atexit.register(lambda: _final_word(cfg))


def run_forever(cfg: Config) -> int:
    """Capture loop plus tamper polling. The camera drives the cadence; sysfs is polled between
    frames, so a cable pull is noticed even while the detector is busy."""
    harden_process(cfg)
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
            # Not a warning to shrug at: with no camera there are no frames and no scene-shift
            # signal. The cable, the lid and the power button are still watched, and a camera that
            # stays away is itself reported as tamper by pump().
            log.emit("warn", reason="camera did not open; sysfs signals only until it comes back")
        try:
            runner.pump(camera)
        finally:
            runner.finish()
            camera.release()
            publish_heartbeat(cfg)
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

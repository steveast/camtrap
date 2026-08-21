"""The run loop: capture, detect, slice into events, respond with sound, hold the sound on.

A frame is optional; a tick is not. Motion in frame produces a spoken warning, tampering produces
the siren, and the tamper path does not depend on the camera working — losing the eyes must not
cost the ears.

Around this file: `lifecycle.py` starts and ends a real run (signals, hardening, the final word to
the receiver), and `modes.py` holds the operator-facing runs — preflight, watch, drill, selftest.
Keeping them apart keeps this file about the loop.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np

from . import log
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
            if frame is None:
                # No frame to wait on, so pace by the hold tick: the audio path is re-asserted
                # and sysfs is polled at that rate, and a camera with nothing to give must not
                # turn into a spin. When frames ARE flowing the blocking read is the pacing, and
                # sleeping on top of it would halve the frame rate — measured: 7.3 fps became
                # 3.4 fps, on the one thing the owner asked to make faster.
                self.sleep(self.cfg.sound.hold_poll_ms / 1000.0)

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

"""The run loop: capture, detect, slice into events, respond with sound, hold the sound on.

The camera drives the cadence and sysfs is polled between frames, so a cable pull is noticed even
while the detector is busy. Two independent paths converge here: motion in frame produces a spoken
warning, tampering produces the siren.
"""

from __future__ import annotations

import signal
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
        self.responder.set_gate(self.arming.gate)

    def request_stop(self, *_: object) -> None:
        self._stop = True

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

        if detection.kind is EventKind.MOTION:
            self.stats.motion_events += 1
            event = self.events.begin(EventKind.MOTION, now=now, frame=frame)
            self.events.note_motion(now=now)
            if event.frames_written <= self.cfg.event.prebuffer_frames + 1:
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


def sound_selftest(cfg: Config, stage: Stage) -> int:
    """Play one stage on the real speakers, forcing the audio path as an event would."""
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
    log.emit("selftest", stage=stage.value, ok=True, sink=responder.audio.sink or "-")
    return 0

"""The run loop: poll tamper signals, respond with sound, hold the sound on.

Detection through the camera joins this loop in S2; at this stage the loop already delivers the
feature ranked first — cable pulled or lid closed produces a siren that a keypress cannot silence.
"""

from __future__ import annotations

import signal
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from . import log
from . import tamper as tamper_mod
from .arming import Arming
from .config import Config
from .inhibit import Inhibitor
from .player import SoundResponder, Stage
from .state import read_mode


@dataclass
class LoopStats:
    ticks: int = 0
    tamper_events: int = 0
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
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.monitor = monitor if monitor is not None else tamper_mod.TamperMonitor(cfg)
        self.responder = responder if responder is not None else SoundResponder(cfg)
        self.arming = arming if arming is not None else Arming(cfg)
        self.inhibitor = inhibitor
        self.clock = clock
        self.stats = LoopStats()
        self._stop = False
        self.responder.set_gate(self.arming.gate)

    def request_stop(self, *_: object) -> None:
        self._stop = True

    def step(self, now: float) -> list[tamper_mod.Signal]:
        """One iteration: arming state, tamper poll, sound, hold."""
        self.arming.poll(now=now)
        self.responder.hold_tick(now=now)
        signals = self.monitor.poll(now=now)
        self.stats.ticks += 1
        if signals:
            self.stats.tamper_events += 1
            self.stats.signals.extend(s.name for s in signals)
            if tamper_mod.plays_siren(signals):
                result = self.responder.on_tamper([s.name for s in signals], now=now)
                if result.played:
                    self.stats.sirens += 1
        return signals

    def motion(self, now: float) -> None:
        """Called by the detector (S2) when motion is confirmed."""
        result = self.responder.on_motion(now=now)
        if result.played:
            self.stats.warnings += 1

    def run(self, *, max_ticks: int | None = None) -> LoopStats:
        signal.signal(signal.SIGTERM, self.request_stop)
        signal.signal(signal.SIGINT, self.request_stop)
        started = self.clock()
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
    with Inhibitor() as inhibitor:
        if not inhibitor.active:
            log.emit("warn", reason="no sleep inhibitor: a closed lid may suspend the machine")
        runner = Runner(cfg, inhibitor=inhibitor)
        runner.run()
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

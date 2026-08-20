"""Slicing frames into events: pre-buffer, throttle, manifest (spec 3.2).

The pre-buffer exists because a detector by definition wakes up after motion has begun; without it
the first frame of every event is a back in a doorway. Truncation is always recorded — silent
truncation reads as "everything was captured" when it was not.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

from . import __version__, log
from .config import Config
from .detector import EventKind


@dataclass
class Event:
    event_id: str
    kind: EventKind
    started: float
    started_wall: float
    signals: list[str] = field(default_factory=list)
    frames_written: int = 0
    truncated: bool = False
    ended: float | None = None
    last_motion: float = 0.0
    last_frame_at: float = float("-inf")
    sound_played: bool = False
    sound_stage: str = ""
    sound_latency_ms: int | None = None
    sound_evidence_confirmed: bool = False

    @property
    def single_frame(self) -> bool:
        """A light event is one frame, never a series (spec 3.1)."""
        return self.kind is EventKind.LIGHT


class EventWriter:
    """Owns the ring buffer, the active event, and the on-disk artefacts."""

    def __init__(self, cfg: Config, *, wall_clock=None) -> None:
        self.cfg = cfg
        self._ring: deque[tuple[float, np.ndarray]] = deque(maxlen=cfg.event.prebuffer_frames)
        self._ring_last = float("-inf")
        self.active: Event | None = None
        self._wall = wall_clock or __import__("time").time

    # --- observation ---------------------------------------------------------

    def observe(self, frame: np.ndarray, *, now: float) -> None:
        """Feed every frame here; the ring keeps one per prebuffer_interval_sec."""
        if now - self._ring_last >= self.cfg.event.prebuffer_interval_sec:
            self._ring.append((now, frame.copy()))
            self._ring_last = now

    # --- lifecycle -----------------------------------------------------------

    def begin(
        self,
        kind: EventKind,
        *,
        now: float,
        frame: np.ndarray | None = None,
        signals: list[str] | None = None,
    ) -> Event:
        if self.active is not None:
            # An event already runs: escalate its type if this is a stronger signal.
            if kind is EventKind.TAMPER and self.active.kind is not EventKind.TAMPER:
                self.active.kind = EventKind.TAMPER
                self.active.signals.extend(signals or [])
            self.active.last_motion = now
            return self.active

        stamp = self._wall()
        event = Event(
            event_id=f"evt_{log.utc_stamp(stamp)}",
            kind=kind,
            started=now,
            started_wall=stamp,
            signals=list(signals or []),
            last_motion=now,
        )
        self.active = event
        self.cfg.spool_dir.mkdir(parents=True, exist_ok=True)

        # A light event is a single frame: before the switch there was nothing to see, so the
        # pre-buffer would only add dark frames (spec 3.1).
        prebuffer = [] if event.single_frame else list(self._ring)
        for when, buffered in prebuffer:
            self._write_frame(event, buffered, now=when, throttled=False)

        trigger = frame if frame is not None else (self._ring[-1][1] if self._ring else None)
        if trigger is not None:
            self._write_frame(event, trigger, now=now, throttled=True)

        log.emit(
            "event_begin",
            id=event.event_id,
            type=kind.value,
            signals=",".join(event.signals) or "-",
            prebuffer=len(prebuffer),
        )
        return event

    def note_motion(self, *, now: float) -> None:
        if self.active is not None:
            self.active.last_motion = now

    def feed(self, frame: np.ndarray, *, now: float) -> bool:
        """Add a frame to the active event if the throttle and the cap allow it."""
        event = self.active
        if event is None or event.single_frame:
            return False
        if now - event.last_frame_at < self.cfg.event.snapshot_interval_sec:
            return False
        if event.frames_written >= self.cfg.event.max_frames_per_event:
            if not event.truncated:
                event.truncated = True
                log.emit("event_truncated", id=event.event_id, cap=event.frames_written)
            return False
        self._write_frame(event, frame, now=now, throttled=True)
        return True

    def mark_sound(
        self, *, stage: str, latency_ms: int | None = None, evidence_confirmed: bool = False
    ) -> None:
        if self.active is None:
            return
        self.active.sound_played = True
        self.active.sound_stage = stage
        self.active.sound_latency_ms = latency_ms
        self.active.sound_evidence_confirmed = evidence_confirmed

    def maybe_close(self, *, now: float) -> Event | None:
        event = self.active
        if event is None:
            return None
        if event.single_frame or now - event.last_motion >= self.cfg.event.event_gap_sec:
            return self.close(now=now)
        return None

    def close(self, *, now: float) -> Event:
        event = self.active
        assert event is not None
        event.ended = now
        self.active = None
        self._write_manifest(event)
        log.emit(
            "event_end",
            id=event.event_id,
            type=event.kind.value,
            frames=event.frames_written,
            truncated=event.truncated,
            sound=event.sound_stage or "-",
        )
        return event

    # --- artefacts -----------------------------------------------------------

    def _write_frame(
        self, event: Event, frame: np.ndarray | None, *, now: float, throttled: bool
    ) -> None:
        if frame is None:
            return
        index = event.frames_written
        path = self.cfg.spool_dir / f"{event.event_id}_{index:03d}.jpg"
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.event.jpeg_quality]
        if not cv2.imwrite(str(path), frame, params):
            log.emit("frame_error", id=event.event_id, index=index)
            return
        event.frames_written = index + 1
        if throttled:
            event.last_frame_at = now

    def _write_manifest(self, event: Event) -> Path:
        path = self.cfg.spool_dir / f"{event.event_id}.json"
        payload = {
            "id": event.event_id,
            "type": event.kind.value,
            "signals": event.signals,
            "started": event.started_wall,
            "ended": event.started_wall + max(0.0, (event.ended or event.started) - event.started),
            "frames": event.frames_written,
            "truncated": event.truncated,
            "agent_version": __version__,
            "sound_played": event.sound_played,
            "sound_stage": event.sound_stage,
            "sound_latency_ms": event.sound_latency_ms,
            "sound_evidence_confirmed": event.sound_evidence_confirmed,
        }
        path.write_text(json.dumps(payload, indent=2, sort_keys=True))
        return path

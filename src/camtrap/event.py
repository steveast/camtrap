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
from .atomic import write_atomic
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
    boosted_frames: int = 0
    #: The frame worth looking at, and what it scored. NOT simply the most changed one: the
    #: first frame by number is the OLDEST pre-buffer frame — an empty room five seconds before
    #: anything happened — and the MOST CHANGED one is often the moment a light came on, which is
    #: a photograph of darkness. See `_key_score`.
    key_index: int = 0
    key_score: float = 0.0
    key_changed_pct: float = 0.0

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
        #: Every frame this writer has ever put on disk, across all events. The run loop watches
        #: it to know a photograph was really taken: the shutter clicks on the fact, not on the
        #: intention, so a frame the throttle or the cap refused makes no sound.
        self.frames_total = 0
        self._wall = wall_clock or __import__("time").time
        self._used_ids: set[str] = set()

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
        changed_pct: float | None = None,
    ) -> Event:
        if self.active is not None:
            # An event already runs: escalate its type if this is a stronger signal.
            if kind is EventKind.TAMPER and self.active.kind is not EventKind.TAMPER:
                self.active.kind = EventKind.TAMPER
                self.active.signals.extend(signals or [])
                self._write_manifest(self.active)  # escalation must survive a sudden death
            self.active.last_motion = now
            return self.active

        # The id is second-resolution, so two events starting inside the same second would
        # share it and their frames would overwrite each other. Nudge the stamp forward until the
        # id is free: the manifest still carries the real start time.
        stamp = self._wall()
        event_id = f"evt_{log.utc_stamp(stamp)}"
        while event_id in self._used_ids or (self.cfg.spool_dir / f"{event_id}.json").exists():
            stamp += 1.0
            event_id = f"evt_{log.utc_stamp(stamp)}"
        self._used_ids.add(event_id)

        event = Event(
            event_id=event_id,
            kind=kind,
            started=now,
            started_wall=self._wall(),
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
            self._write_frame(
                event, trigger, now=now, throttled=True, changed_pct=changed_pct or 0.0
            )
            if event.key_score <= 0.0:
                # A cable pull carries no changed_pct at all, so nothing scores and the key would
                # stay at index 0 — the oldest pre-buffer frame, an empty room. Two tamper alerts
                # in August led with `_000.jpg` for exactly this reason. The trigger frame is the
                # room as it was when the signal arrived, and that is the floor.
                event.key_index = event.frames_written - 1

        # Write the manifest immediately, marked open. If the agent dies mid-event — battery
        # pulled, machine shut down, laptop carried off — the frames would otherwise arrive with
        # no type and no signals, and a tamper event would read as ordinary motion.
        self._write_manifest(event)

        log.emit(
            "event_begin",
            id=event.event_id,
            type=kind.value,
            signals=",".join(event.signals) or "-",
            prebuffer=len(prebuffer),
            changed_pct=round(changed_pct, 2) if changed_pct is not None else "-",
        )
        return event

    def note_motion(self, *, now: float) -> None:
        if self.active is not None:
            self.active.last_motion = now

    def feed(
        self, frame: np.ndarray, *, now: float, changed_pct: float = 0.0, force: bool = False
    ) -> bool:
        """Add a frame to the active event if the throttle and the cap allow it.

        `changed_pct` lets a big change jump the throttle. Without that, an event opened by a
        twitching curtain snapshots the curtain every 5 s and a person crossing the room in two
        seconds lands between frames — the event exists, the person is not in it.
        """
        event = self.active
        if event is None or event.single_frame:
            return False
        since = now - event.last_frame_at
        boost_pct = self.cfg.event.boost_area_pct
        # `> 0`, not `>= 0`: with the threshold at zero every frame clears it, so treating 0 as
        # "no threshold" would turn the boost permanently ON and give a frame a second — the
        # exact opposite of what disabling it means.
        boosted = (
            boost_pct > 0
            and changed_pct >= boost_pct
            and since >= self.cfg.event.boost_min_interval_sec
        )
        if force and since >= self.cfg.event.tamper_burst_interval_sec:
            # A tamper burst ignores the throttle: the person is in the room right now.
            boosted = True
        elif since < self.cfg.event.snapshot_interval_sec and not boosted:
            return False
        if event.frames_written >= self.cfg.event.max_frames_per_event:
            if not event.truncated:
                event.truncated = True
                log.emit("event_truncated", id=event.event_id, cap=event.frames_written)
            return False
        self._write_frame(event, frame, now=now, throttled=True, changed_pct=changed_pct)
        if boosted:
            event.boosted_frames += 1
        # Keep the on-disk count roughly current without rewriting JSON for every frame.
        if event.frames_written % 4 == 0:
            self._write_manifest(event)
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
        self._write_manifest(self.active)

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
            boosted=event.boosted_frames,
            truncated=event.truncated,
            sound=event.sound_stage or "-",
        )
        return event

    # --- artefacts -----------------------------------------------------------

    def _key_score(
        self, event: Event, frame: np.ndarray, *, now: float, changed_pct: float
    ) -> float:
        """What this frame is worth AS THE PHOTO — which is not how much of it changed.

        Two corrections to raw `changed_pct`, each from a frame that was actually delivered:

        A light coming on in a dark room changes ~99 % of the pixels, so the transition frame won
        every time — and it is the one frame in the event where the sensor has not caught up and
        the room is still black. Measured: mean luma 14 against 119 two frames later.

        And the frames right after the trigger are the worst ones of a real intrusion: a detector
        wakes up after motion has begun, so it opens the event on a back in a doorway. Waiting
        `key_settle_sec` before a frame may lead cost nothing — the frames are written either way,
        and the poller is a cron tick behind regardless.

        Neither is a veto. An event that is dark from end to end still has to name a frame, and a
        weight beats a filter for that: the least bad frame wins instead of index 0.
        """
        cfg = self.cfg.event
        # Every 8th pixel: a numpy stride, not a resize, and a mean over 1/64th of the frame is
        # the same number to well within the margin that separates 14 from 119.
        luma = float(frame[::8, ::8].mean())
        weight = 1.0
        if luma < cfg.key_min_luma or luma > cfg.key_max_luma:
            weight *= 0.05
        if now - event.started < cfg.key_settle_sec:
            weight *= 0.25
        return changed_pct * weight

    def _write_frame(
        self,
        event: Event,
        frame: np.ndarray | None,
        *,
        now: float,
        throttled: bool,
        changed_pct: float = 0.0,
    ) -> None:
        if frame is None:
            return
        index = event.frames_written
        path = self.cfg.spool_dir / f"{event.event_id}_{index:03d}.jpg"
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.event.jpeg_quality]
        # Encode first, then write in one step: imwrite streams into the very file the uploader is
        # about to list, so a frame could be sent while it was still being written.
        ok, buffer = cv2.imencode(".jpg", frame, params)
        if not ok or not write_atomic(path, buffer.tobytes()):
            log.emit("frame_error", id=event.event_id, index=index)
            return
        event.frames_written = index + 1
        self.frames_total += 1
        score = self._key_score(event, frame, now=now, changed_pct=changed_pct)
        if score > event.key_score:
            event.key_score = score
            event.key_changed_pct = changed_pct
            event.key_index = index
        if throttled:
            event.last_frame_at = now

    def _write_manifest(self, event: Event) -> Path:
        path = self.cfg.spool_dir / f"{event.event_id}.json"
        payload = {
            "id": event.event_id,
            "closed": event.ended is not None,
            "type": event.kind.value,
            "signals": event.signals,
            "started": event.started_wall,
            "ended": event.started_wall + max(0.0, (event.ended or event.started) - event.started),
            "frames": event.frames_written,
            "boosted_frames": event.boosted_frames,
            # The frame worth sending: most changed, not oldest.
            "key_frame": f"{event.event_id}_{event.key_index:03d}.jpg",
            "key_changed_pct": round(event.key_changed_pct, 2),
            "truncated": event.truncated,
            "agent_version": __version__,
            "sound_played": event.sound_played,
            "sound_stage": event.sound_stage,
            "sound_latency_ms": event.sound_latency_ms,
            "sound_evidence_confirmed": event.sound_evidence_confirmed,
        }
        # The manifest says whether this event was tamper or motion. A torn one is read as a
        # missing type, and the alert loses the only thing that distinguishes a thief from a
        # cleaner — so it is written whole or not at all.
        write_atomic(path, json.dumps(payload, indent=2, sort_keys=True))
        return path

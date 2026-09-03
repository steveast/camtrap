"""The heartbeat: the signal that arrives even when the camera caught nothing.

More valuable than the frames, because it gives the exact cut-off time when the laptop was taken
away or shut down. Power state and sound readiness ride along so that unreadiness is visible
*before* an event: a trap on battery with mute still set looks like it works and does not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import __version__, log, sounds
from .config import Config
from .state import read_mode


@dataclass
class Heartbeat:
    fields: dict[str, object]

    def render(self) -> str:
        parts = []
        for key, value in self.fields.items():
            if isinstance(value, bool):
                value = 1 if value else 0
            if value is None:
                value = "-"
            parts.append(f"{key}={value}")
        return " ".join(parts) + "\n"


def build(
    cfg: Config,
    *,
    started: float,
    now: float,
    stats=None,
    monitor=None,
    arming=None,
    spool=None,
    camera=None,
    clips=None,
    uploader=None,
    wall: float | None = None,
) -> Heartbeat:
    missing = sounds.missing_sounds(cfg)
    power = monitor.power_present() if monitor is not None else None
    lid_closed = monitor.lid_closed() if monitor is not None else None
    fields: dict[str, object] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(wall if wall else time.time())),
        "version": __version__,
        "uptime": int(max(0.0, now - started)),
        "mode": read_mode(cfg.root).name,
        "arming": cfg.arming.mode,
        "armed": None,
        "ac_online": power,
        "lid": ("closed" if lid_closed else "open") if lid_closed is not None else None,
        "camera": ("ok" if camera.status.opened else "gone") if camera is not None else None,
        "frames": getattr(stats, "frames", None),
        "events": (
            getattr(stats, "tamper_events", 0) + getattr(stats, "motion_events", 0)
            if stats is not None
            else None
        ),
        "sirens": getattr(stats, "sirens", None),
        "warnings": getattr(stats, "warnings", None),
        "spool": spool.depth() if spool is not None else None,
        "spool_mb": round(spool.total_bytes() / 1048576, 2) if spool is not None else None,
        "sound_ok": not missing,
        "missing": ",".join(missing) if missing else None,
        "langs": ",".join(cfg.sound.warn_langs) or None,
    }
    if clips is not None:
        # Same reason `sound_ok` is here: a trap that silently cannot encode looks like it works.
        # `clip_drops` is the number that says the machine could not keep up with the encoder, and
        # a clip with holes in it is worth knowing about before the event rather than after.
        ok, why = clips.available()
        fields["video_ok"] = ok
        fields["video_why"] = None if ok else why
        fields["clip_segments"] = clips.status.segments_ready
        fields["clip_mb"] = round(clips.status.bytes_ready / 1048576, 2)
        fields["clip_drops"] = clips.status.frames_dropped
    if uploader is not None:
        # Any sink that can say whether it is ready, says so here. The Telegram sink can: a
        # missing or group-readable token file means clips never reach the chat, and without this
        # the only trace would be a line in the agent's own log — on the machine that is assumed
        # to be walking out of the room.
        for sink in getattr(uploader, "sinks", []):
            checker = getattr(sink, "available", None)
            if not callable(checker):
                continue
            ok, why = checker()
            fields[f"{sink.name}_ok"] = ok
            if not ok:
                fields[f"{sink.name}_why"] = why
    if arming is not None:
        described = arming.describe(now=now)
        fields["armed"] = bool(described["armed"])
        fields["arm_reason"] = described["reason"] or None
    return Heartbeat({key: value for key, value in fields.items()})


def publish(cfg: Config) -> bool:
    """Send one heartbeat immediately, so the receiver learns the current mode.

    Needed whenever the agent changes mode and then stops: the poller reads the last heartbeat it
    was given, and a stale `armed` plus a dead agent reads as "the laptop was taken". Every run
    that ends deliberately owes the receiver a final word.
    """
    from .spool import Spool
    from .uploader import Uploader

    uploader = Uploader(cfg, Spool(cfg))
    line = build(cfg, started=0.0, now=0.0).render()
    ok = uploader.heartbeat(line)
    log.emit("mode_published", ok=ok, mode=read_mode(cfg.root).name)
    return ok


class HeartbeatSender:
    """Sends on an interval; a failure waits out the interval rather than retrying in a loop."""

    def __init__(self, cfg: Config, uploader) -> None:
        self.cfg = cfg
        self.uploader = uploader
        self._last = float("-inf")
        self._failing = False
        self._warned_unconfigured = False

    def due(self, *, now: float) -> bool:
        return now - self._last >= self.cfg.upload.heartbeat_sec

    @property
    def configured(self) -> bool:
        """False when there is no receiver at all — nothing to send to, nothing to complain about
        every tick. Frames still queue locally and the siren never needed the network."""
        return any(
            getattr(sink, "name", "") == "prod" for sink in getattr(self.uploader, "sinks", [])
        )

    def maybe_send(self, heartbeat: Heartbeat, *, now: float) -> bool:
        if not self.due(now=now):
            return False
        if not self.configured:
            if not self._warned_unconfigured:
                self._warned_unconfigured = True
                log.emit("heartbeat_skip", reason="no receiver configured")
            self._last = now
            return False

        line = heartbeat.render()
        ok = self.uploader.heartbeat(line)
        # Either way the timer advances: the next attempt is the next due tick, not the next loop
        # iteration 250 ms later.
        self._last = now
        if ok:
            if self._failing:
                self._failing = False
                log.emit("heartbeat_recovered")
        elif not self._failing:
            self._failing = True
            log.emit("heartbeat_failed", reason="no sink acknowledged")
        return ok

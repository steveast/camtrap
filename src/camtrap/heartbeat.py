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
    if arming is not None:
        described = arming.describe(now=now)
        fields["armed"] = bool(described["armed"])
        fields["arm_reason"] = described["reason"] or None
    return Heartbeat({key: value for key, value in fields.items()})


class HeartbeatSender:
    """Sends on an interval; a failure is logged and retried on the next due tick."""

    def __init__(self, cfg: Config, uploader) -> None:
        self.cfg = cfg
        self.uploader = uploader
        self._last = float("-inf")

    def due(self, *, now: float) -> bool:
        return now - self._last >= self.cfg.upload.heartbeat_sec

    def maybe_send(self, heartbeat: Heartbeat, *, now: float) -> bool:
        if not self.due(now=now):
            return False
        line = heartbeat.render()
        ok = self.uploader.heartbeat(line)
        if ok:
            self._last = now
        else:
            log.emit("heartbeat_failed", reason="no sink acknowledged")
        return ok

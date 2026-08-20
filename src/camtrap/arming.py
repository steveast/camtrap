"""When is the alarm live?

Capture is always on; only the noise is conditional. The alarm arms when the screen is locked —
a signal only the owner can produce, since only the owner knows the password — after an exit delay
that covers the walk to the door. Unlocking disarms it and opens a grace window, so the owner
coming back for the laptop does not get a siren out of the case.

`mode` selects the policy: on_lock (default), always, manual.
"""

from __future__ import annotations

import subprocess
from typing import Protocol

from . import log, state
from .config import Config
from .player import Stage


class Session(Protocol):
    def locked_hint(self) -> bool | None: ...


class LogindSession:
    """Reads LockedHint from logind.

    "self" only resolves for a caller that belongs to a session, which a systemd --user service or
    a shell spawned outside the seat does not. So resolve the session id explicitly: XDG_SESSION_ID
    first, then the user's display session.
    """

    def __init__(self, cfg: Config) -> None:
        self._cfg = cfg
        self._session: str | None = None

    def _run(self, args: list[str]) -> str | None:
        try:
            result = subprocess.run(
                ["loginctl", *args], capture_output=True, text=True, timeout=5, check=False
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        return result.stdout.strip()

    def _session_id(self) -> str | None:
        if self._session:
            return self._session
        from os import environ

        candidates = [environ.get("XDG_SESSION_ID")]
        user = environ.get("USER") or environ.get("LOGNAME")
        if user:
            candidates.append(self._run(["show-user", user, "-p", "Display", "--value"]))
        for candidate in candidates:
            if candidate and self._run(["show-session", candidate, "-p", "Id", "--value"]):
                self._session = candidate
                return candidate
        return None

    def locked_hint(self) -> bool | None:
        session = self._session_id()
        if session is None:
            return None
        answer = self._run(["show-session", session, "-p", "LockedHint", "--value"])
        if answer is None:
            return None
        answer = answer.lower()
        if answer in ("yes", "true", "1"):
            return True
        if answer in ("no", "false", "0"):
            return False
        return None


class Arming:
    """Tracks lock state and answers the gate question the player asks per stage."""

    def __init__(self, cfg: Config, *, session: Session | None = None) -> None:
        self.cfg = cfg
        self._session = session if session is not None else LogindSession(cfg)
        self._locked: bool | None = None
        self._locked_since: float | None = None
        self._unlocked_at: float | None = None
        self._started: float | None = None
        self._last_activity: float | None = None
        self._still_since: float | None = None
        manual = state.read_manual_arm(cfg.root)
        self._manual_since: float | None = manual

    # --- lifecycle -----------------------------------------------------------

    def start(self, *, now: float) -> None:
        """Mark the start of a run; the warm-up window is measured from here."""
        self._started = now

    def arm_manually(self, *, now: float) -> None:
        self._manual_since = state.write_manual_arm(self.cfg.root, now=now)

    def disarm_manually(self) -> None:
        state.clear_manual_arm(self.cfg.root)
        self._manual_since = None

    def note_activity(self, *, now: float) -> None:
        """The detector saw movement: the owner is (probably) still in the room."""
        self._last_activity = now
        self._still_since = None

    def note_quiet(self, *, now: float) -> None:
        """A frame with nothing happening in it; starts the stillness clock."""
        if self._still_since is None:
            self._still_since = now

    def poll(self, *, now: float) -> None:
        locked = self._session.locked_hint()
        if locked is None:
            return
        if self._locked is None:
            self._locked = locked
            self._locked_since = now if locked else None
            return
        if locked and not self._locked:
            self._locked_since = now
            self._unlocked_at = None
            log.emit("arming", event="locked")
        elif not locked and self._locked:
            self._unlocked_at = now
            self._locked_since = None
            log.emit("arming", event="unlocked", grace=self.cfg.arming.grace_after_unlock_sec)
        self._locked = locked

    # --- the gate ------------------------------------------------------------

    def armed_since(self, now: float | None = None) -> float | None:
        """Timestamp from which the alarm counts as armed, or None.

        `now` is needed for on_still, which arms a fixed interval after the room went quiet.
        """
        mode = self.cfg.arming.mode
        candidates: list[float] = []
        if self._manual_since is not None:
            candidates.append(self._manual_since)
        if mode == "on_still" and now is not None:
            still_for = self.cfg.arming.arm_when_still_sec
            if self._still_since is not None and now - self._still_since >= still_for:
                candidates.append(self._still_since + still_for)
            elif (
                self._started is not None
                and now - self._started >= self.cfg.arming.arm_deadline_sec
            ):
                # The room never went quiet — a curtain, a fan, a street window. Arm anyway
                # rather than leaving the trap disarmed for the whole trip.
                candidates.append(self._started + self.cfg.arming.arm_deadline_sec)
        if mode == "always":
            # Armed for the lifetime of the run; the exit delay is measured from start() so a
            # unit restart while the owner is in the room does not sound immediately.
            candidates.append(self._started if self._started is not None else float("-inf"))
        elif mode == "on_lock" and self._locked and self._locked_since is not None:
            candidates.append(self._locked_since)
        return min(candidates) if candidates else None

    def gate(self, stage: Stage, now: float) -> tuple[bool, str]:
        if state.read_mode(self.cfg.root).paused:
            return False, "paused"

        if self._started is not None and now - self._started < self.cfg.detector.warmup_sec:
            return False, "warmup"

        if stage is Stage.WARNING and not self.cfg.sound.warn_langs:
            return False, "no_warn_langs"

        unlocked_at = self._unlocked_at
        if unlocked_at is not None and now - unlocked_at < self.cfg.arming.grace_after_unlock_sec:
            return False, "unlock_grace"

        since = self.armed_since(now)
        if since is None:
            return False, "waiting_for_quiet" if self.cfg.arming.mode == "on_still" else "not_armed"
        # on_still already waited for the room to empty; no second delay on top of it.
        delay = 0.0 if self.cfg.arming.mode == "on_still" else self.cfg.arming.exit_delay_sec
        if now - since < delay:
            return False, "exit_delay"
        return True, ""

    # --- introspection -------------------------------------------------------

    def describe(self, *, now: float) -> dict[str, object]:
        since = self.armed_since(now)
        allowed, reason = self.gate(Stage.SIREN, now)
        return {
            "mode": self.cfg.arming.mode,
            "locked": self._locked,
            "armed": allowed,
            "reason": reason,
            "armed_since": None if since is None or since == float("-inf") else since,
            "manual": self._manual_since is not None,
        }

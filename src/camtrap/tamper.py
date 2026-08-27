"""Tamper detection: is someone handling the laptop?

The target machine has no accelerometer (both iio devices are ambient light sensors, no hdaps,
no /dev/freefall), so the signal is composite: power, lid, scene shift, camera disappearance.

Everything here is judged on being *held* rather than on the instant it is seen. USB-C PD ports
flap their `online` attribute on their own, and an alarm that fires on a flap is an alarm nobody
believes by the third day.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import log
from .config import Config

AC_OFFLINE = "ac_offline"
LID_CLOSED = "lid_closed"
CAMERA_GONE = "camera_gone"
SCENE_SHIFT = "scene_shift"
POWER_BUTTON = "power_button_pressed"

#: Every signal this module can raise, for validating `tamper.siren_signals`. Which of them
#: actually make noise is config, not code — see `TamperConfig.siren_signals`.
ALL_SIGNALS = frozenset({AC_OFFLINE, LID_CLOSED, CAMERA_GONE, SCENE_SHIFT, POWER_BUTTON})


@dataclass(frozen=True)
class Signal:
    name: str
    detail: str = ""


def _read_text(path: str | Path) -> str | None:
    try:
        return Path(path).read_text().strip()
    except OSError:
        return None


def _read_int(path: str | Path) -> int | None:
    raw = _read_text(path)
    if raw is None:
        return None
    try:
        return int(raw.split()[0])
    except (ValueError, IndexError):
        return None


class _Latch:
    """Fires once when a condition has been true continuously for `hold`."""

    def __init__(self, hold: float) -> None:
        self.hold = hold
        self._since: float | None = None
        self._fired = False

    def update(self, active: bool, now: float) -> bool:
        if not active:
            self._since = None
            self._fired = False
            return False
        if self._since is None:
            self._since = now
        if self._fired:
            return False
        if now - self._since >= self.hold:
            self._fired = True
            return True
        return False


class TamperMonitor:
    """Polls sysfs for the two least ambiguous signals: power pulled and lid closed."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        hold = max(0.0, cfg.tamper.debounce_sec)
        self._ac = _Latch(hold)
        self._lid = _Latch(hold)
        self._primed = False

    # --- inputs --------------------------------------------------------------

    def power_present(self) -> bool | None:
        """True if any source reports online. None if no source could be read at all."""
        values = [_read_int(path) for path in self.cfg.tamper.ac_online_paths]
        known = [value for value in values if value is not None]
        if not known:
            return None
        return any(value == 1 for value in known)

    def lid_closed(self) -> bool | None:
        raw = _read_text(self.cfg.tamper.lid_state_path)
        if raw is None:
            return None
        return "closed" in raw.lower()

    def read_als(self) -> float | None:
        """Mean illuminance across sensors, used to arbitrate light versus movement (3.3)."""
        values = [_read_int(path) for path in self.cfg.tamper.als_paths]
        known = [float(value) for value in values if value is not None]
        if not known:
            return None
        return sum(known) / len(known)

    # --- polling -------------------------------------------------------------

    def poll(self, now: float) -> list[Signal]:
        signals: list[Signal] = []

        power = self.power_present()
        if power is not None and self._ac.update(power is False, now):
            signals.append(Signal(AC_OFFLINE, "power source went offline"))

        lid = self.lid_closed()
        if lid is not None and self._lid.update(lid is True, now):
            signals.append(Signal(LID_CLOSED, "lid closed"))

        self._primed = True
        for signal in signals:
            log.emit("tamper", signal=signal.name, detail=signal.detail)
        return signals

    def report_external(self, name: str, *, detail: str = "", now: float = 0.0) -> Signal:
        """Record a signal discovered elsewhere (camera loop, scene arbitration).

        No debounce: the caller already established the condition is real.
        """
        signal = Signal(name, detail)
        log.emit("tamper", signal=name, detail=detail)
        return signal


def siren_signals(cfg: Config) -> frozenset[str]:
    """The configured set, with unknown names refused loudly rather than silently ignored.

    A typo here is silence at the moment the trap is meant to scream, and it would never show up
    in a test of anything else. `preflight` calls this so the mistake surfaces before the trip
    rather than during it.

    **Raises**, which is why the run loop calls `plays_siren` instead: this is a check, and a
    check may refuse. Nothing on the path between a tamper signal and the sound may throw.
    """
    configured = frozenset(cfg.tamper.siren_signals)
    unknown = configured - ALL_SIGNALS
    if unknown:
        raise ValueError(
            "unknown tamper.siren_signals: "
            + ", ".join(sorted(unknown))
            + " (known: "
            + ", ".join(sorted(ALL_SIGNALS))
            + ")"
        )
    return configured


def plays_siren(cfg: Config, signals: list[Signal]) -> bool:
    """Whether this batch of signals makes a noise. Deliberately cannot fail.

    A misspelled name here simply never matches — one signal loses its siren. Validating instead
    would turn that into an exception raised from inside the run loop, on the tamper path, which
    is how "one signal went quiet" becomes "the trap stopped". `preflight` is where the typo is
    caught, and it blocks arming.
    """
    allowed = frozenset(cfg.tamper.siren_signals)
    return any(signal.name in allowed for signal in signals)

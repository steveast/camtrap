"""Local run state: capture mode (armed/paused) and the manual arming override.

Both live as files under the state directory so that `camtrap pause` from a shell and the running
agent agree without any IPC, and so the systemd unit's ExecStop can flip the mode on its way out.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path

from . import log
from .atomic import write_atomic

MODE_ARMED = "armed"
MODE_PAUSED = "paused"


@dataclass
class Mode:
    name: str
    since: float

    @property
    def paused(self) -> bool:
        return self.name == MODE_PAUSED


def _read(path: Path) -> str:
    try:
        return path.read_text().strip()
    except OSError:
        return ""


def read_mode(state_dir: Path) -> Mode:
    path = state_dir / "mode"
    raw = _read(path)
    if not raw:
        return Mode(MODE_ARMED, 0.0)
    name, _, stamp = raw.partition(" ")
    if name not in (MODE_ARMED, MODE_PAUSED):
        return Mode(MODE_ARMED, 0.0)
    try:
        since = float(stamp)
    except ValueError:
        since = 0.0
    return Mode(name, since)


def write_mode(state_dir: Path, name: str, *, now: float | None = None) -> Mode:
    if name not in (MODE_ARMED, MODE_PAUSED):
        raise ValueError(f"unknown mode: {name}")
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    write_atomic(state_dir / "mode", f"{name} {stamp:.0f}\n", durable=False)
    log.emit("mode", name=name)
    return Mode(name, stamp)


def read_manual_arm(state_dir: Path) -> float | None:
    """Timestamp of an explicit `camtrap arm`, or None if the operator has not armed by hand."""
    raw = _read(state_dir / "armed")
    if not raw:
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def write_manual_arm(state_dir: Path, *, now: float | None = None) -> float:
    state_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.time() if now is None else now
    write_atomic(state_dir / "armed", f"{stamp:.0f}\n", durable=False)
    log.emit("arm", source="manual")
    return stamp


def clear_manual_arm(state_dir: Path) -> None:
    (state_dir / "armed").unlink(missing_ok=True)
    log.emit("disarm", source="manual")

"""Sleep and power-key inhibition.

Without this the whole trap dies on a closed lid: HandleLidSwitch defaults to suspend and the
desktop power manager acts on it. A short power-button press would shut the machine down mid-siren
too. Holding the button for several seconds still cuts power in hardware — nothing in software can
prevent that, and the spec says so out loud.
"""

from __future__ import annotations

import subprocess

WHAT = "sleep:idle:handle-lid-switch:handle-power-key"


class Inhibitor:
    """Holds a systemd-inhibit lock for the lifetime of the run."""

    def __init__(self, *, what: str = WHAT, who: str = "camtrap", why: str = "camera trap armed"):
        self._argv = [
            "systemd-inhibit",
            f"--what={what}",
            f"--who={who}",
            f"--why={why}",
            "--mode=block",
            "sleep",
            "infinity",
        ]
        self._proc: subprocess.Popen[bytes] | None = None

    def __enter__(self) -> Inhibitor:
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()

    def start(self) -> bool:
        if self._proc is not None:
            return True
        try:
            self._proc = subprocess.Popen(
                self._argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        except OSError:
            self._proc = None
            return False
        return True

    def stop(self) -> None:
        if self._proc is None:
            return
        self._proc.terminate()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None

    @property
    def active(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

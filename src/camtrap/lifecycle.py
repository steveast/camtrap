"""Starting and ending a real run: signals, hardening, and the last word to the receiver.

The trap is expected to die badly — power pulled, laptop carried off, terminal closed. What this
file exists for is the difference between those deaths and an ordinary shutdown, because the
receiver reads silence as theft and the owner powers the machine off every day.
"""

from __future__ import annotations

import atexit
import signal
import subprocess

from . import log
from .camera import Camera
from .config import Config
from .heartbeat import publish as publish_heartbeat
from .inhibit import Inhibitor
from .runner import Runner
from .state import read_mode


def system_is_stopping() -> bool:
    """True while systemd is shutting the machine down.

    The owner powers the laptop off every day; that is not a theft. A thief cannot reach a clean
    shutdown either — the session is locked on tamper, and pulling the power or holding the button
    kills the process without a signal, so there is no shutdown to observe.
    """
    try:
        result = subprocess.run(
            ["systemctl", "is-system-running"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return result.stdout.strip() in ("stopping", "offline")


def _final_word(cfg: Config) -> None:
    """Tell the receiver how this ended, and whether it ended on purpose."""
    from .state import MODE_PAUSED, read_mode, write_mode

    if system_is_stopping() and not read_mode(cfg.root).paused:
        # A deliberate shutdown is an expected offline. Without this the last heartbeat says
        # `armed`, the agent goes quiet, and the poller reports a stolen laptop every single
        # evening — which is how alerting gets muted.
        write_mode(cfg.root, MODE_PAUSED)
        log.emit("shutdown", detail="system stopping; marking the offline expected")
    publish_heartbeat(cfg)


def harden_process(cfg: Config) -> None:
    """Make the agent independent of the terminal that started it.

    A run was killed at 22:31 when its terminal closed, and because the process died on SIGHUP the
    receiver never learned it had stopped: the last heartbeat still said `armed`, which reads as a
    stolen laptop, and the poller repeated that alert for eight hours. A camera trap must outlive
    the shell that launched it.
    """
    for signal_name in ("SIGHUP", "SIGPIPE"):
        handler = getattr(signal, signal_name, None)
        if handler is not None:
            signal.signal(handler, signal.SIG_IGN)
    log.set_file(cfg.log_file or None)
    # Whatever happens next, tell the receiver how this ended.
    atexit.register(lambda: _final_word(cfg))


def run_forever(cfg: Config) -> int:
    """Capture loop plus tamper polling. The camera drives the cadence; sysfs is polled between
    frames, so a cable pull is noticed even while the detector is busy."""
    harden_process(cfg)
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
            # Not a warning to shrug at: with no camera there are no frames and no scene-shift
            # signal. The cable, the lid and the power button are still watched, and a camera that
            # stays away is itself reported as tamper by pump().
            log.emit("warn", reason="camera did not open; sysfs signals only until it comes back")
        try:
            runner.pump(camera)
        finally:
            runner.finish()
            camera.release()
            publish_heartbeat(cfg)
            log.emit(
                "stop",
                frames=runner.stats.frames,
                tamper=runner.stats.tamper_events,
                sirens=runner.stats.sirens,
                warnings=runner.stats.warnings,
            )
    return 0

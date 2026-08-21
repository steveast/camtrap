"""Critical: a dead camera must not silence the alarm.

The capture loop used to be `for frame in camera.frames(): step()`, with tamper polling living
inside step(). When the camera stopped delivering, frames() spun forever on reopen attempts and
never yielded — so step() never ran, and pulling the cable produced no siren at all. Measured on a
stub device: 446 reopen attempts, zero frames, no tamper poll.

Detection and tamper are independent paths; the loop must not couple them.
"""

import numpy as np
import pytest

from camtrap.arming import Arming
from camtrap.camera import Camera
from camtrap.player import SoundResponder
from camtrap.runner import Runner
from camtrap.tamper import TamperMonitor
from tests.fakes import FakeProcess, FakeRunner


class DeadCapture:
    """Opens fine, never delivers a frame: a USB camera that dropped off the bus."""

    def isOpened(self):  # noqa: N802
        return True

    def set(self, *args):
        return True

    def grab(self):
        return False

    def read(self):
        return False, None

    def release(self):
        self.released = True


class Spawned(list):
    def __call__(self, argv, duration):
        proc = FakeProcess(argv=argv, duration=duration, started=0.0)
        self.append(proc)
        return proc

    def played(self, needle):
        return any(needle in " ".join(p.argv) for p in self)


@pytest.fixture
def wired_dead(cfg, sysfs):
    cfg.tamper.ac_online_paths = [str(sysfs.ac)]
    cfg.tamper.lid_state_path = str(sysfs.lid)
    cfg.tamper.als_paths = [str(sysfs.als0)]
    cfg.tamper.debounce_sec = 1.0
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"warn")
    cfg.arming.mode = "always"
    cfg.arming.exit_delay_sec = 0.0
    cfg.detector.warmup_sec = 0.0

    class Clock:
        def __init__(self):
            self.now = 0.0

        def __call__(self):
            return self.now

    clock = Clock()
    spawned = Spawned()
    cmds = FakeRunner()
    camera = Camera(cfg, opener=lambda device: DeadCapture(), clock=clock, sleep=lambda s: None)
    runner = Runner(
        cfg,
        monitor=TamperMonitor(cfg),
        responder=SoundResponder(cfg, runner=cmds, spawn=spawned),
        arming=Arming(cfg, session=type("S", (), {"locked_hint": lambda self: True})()),
        camera=camera,
        clock=clock,
    )
    runner.spawned = spawned
    return runner, camera, clock, sysfs, spawned


def test_next_frame_returns_none_instead_of_blocking(cfg):
    """One attempt, one answer. No internal loop that never comes back."""
    camera = Camera(cfg, opener=lambda device: DeadCapture(), sleep=lambda s: None)
    assert camera.next_frame() is None
    assert camera.next_frame() is None  # and it keeps being answerable


def test_a_dead_camera_is_eventually_declared_gone(cfg):
    cfg.camera.max_reopen_attempts = 3
    camera = Camera(cfg, opener=lambda device: DeadCapture(), sleep=lambda s: None)
    for _ in range(5):
        camera.next_frame()
    assert camera.status.gone, "after the configured attempts it must admit the camera is gone"


def test_reopening_continues_even_after_gone(cfg):
    """Declaring it gone must not stop trying: a USB glitch should self-heal."""
    cfg.camera.max_reopen_attempts = 2
    frames = [np.full((72, 128, 3), 40, dtype=np.uint8)]
    state = {"alive": False}

    class Flaky(DeadCapture):
        def read(self):
            if state["alive"]:
                return True, frames[0]
            return False, None

        def grab(self):
            return state["alive"]

    camera = Camera(cfg, opener=lambda device: Flaky(), sleep=lambda s: None)
    for _ in range(4):
        camera.next_frame()
    assert camera.status.gone
    state["alive"] = True
    assert camera.next_frame() is not None, "it must recover when the device comes back"
    assert not camera.status.gone, "and stop claiming to be gone"


def test_the_siren_still_fires_with_a_dead_camera(wired_dead):
    """The whole point: the trap must keep its ears when it loses its eyes."""
    runner, camera, clock, sysfs, spawned = wired_dead

    runner.pump(camera, max_iterations=3)  # camera dead throughout
    assert runner.stats.ticks >= 3, "step() must run on every iteration, frame or no frame"

    sysfs.set_ac(0)  # the cable is pulled while the camera is dead
    clock.now = 10.0
    runner.pump(camera, max_iterations=1)
    clock.now = 12.0
    runner.pump(camera, max_iterations=1)

    assert "ac_offline" in runner.stats.signals
    assert runner.stats.sirens == 1, "a dead camera must not silence the siren"
    assert spawned.played("shutter.ogg") or spawned.played("siren.ogg")


def test_camera_gone_is_reported_as_tamper_once(wired_dead):
    runner, camera, clock, _sysfs, _spawned = wired_dead
    runner.cfg.camera.max_reopen_attempts = 2
    for tick in range(6):
        clock.now = float(tick)
        runner.pump(camera, max_iterations=1)
    assert runner.stats.signals.count("camera_gone") == 1, "reported once, not every tick"

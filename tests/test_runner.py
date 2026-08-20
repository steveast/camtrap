"""S1: the loop that turns a tamper signal into sound (plan S1, checkpoint 1 in code form)."""

import pytest
from tests.fakes import FakeProcess, FakeRunner

from camtrap.arming import Arming
from camtrap.player import SoundResponder, Stage
from camtrap.runner import Runner
from camtrap.tamper import TamperMonitor


class Spawned(list):
    """Collects the player processes so tests can assert on what was played."""

    def __call__(self, argv, duration):
        proc = FakeProcess(argv=argv, duration=duration, started=0.0)
        self.append(proc)
        return proc

    def played(self, needle):
        return any(needle in " ".join(p.argv) for p in self)


@pytest.fixture
def wired(cfg, sysfs):
    cfg.tamper.ac_online_paths = [str(sysfs.ac)]
    cfg.tamper.lid_state_path = str(sysfs.lid)
    cfg.tamper.als_paths = [str(sysfs.als0)]
    cfg.tamper.debounce_sec = 1.0
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"warn")

    class Session:
        def __init__(self):
            self.locked = True

        def locked_hint(self):
            return self.locked

    session = Session()
    runner_cmds = FakeRunner()
    spawned = Spawned()
    responder = SoundResponder(cfg, runner=runner_cmds, spawn=spawned)
    arming = Arming(cfg, session=session)
    runner = Runner(
        cfg,
        monitor=TamperMonitor(cfg),
        responder=responder,
        arming=arming,
        clock=lambda: 0.0,
    )
    runner.spawned = spawned
    return runner, sysfs, session, runner_cmds


def test_armed_cable_pull_sounds_the_siren(wired):
    runner, sysfs, _session, cmds = wired
    runner.step(0.0)  # arming sees a locked session
    runner.step(1.0)
    sysfs.set_ac(0)
    runner.step(2.0)
    assert runner.stats.sirens == 0  # exit delay still holds
    runner.step(70.0)
    sysfs.set_ac(1)
    runner.step(71.0)
    sysfs.set_ac(0)
    runner.step(72.0)
    runner.step(73.5)
    assert runner.stats.sirens == 1
    assert runner.spawned.played("siren.ogg")
    assert cmds.ran("lock-session")


def test_unlocked_session_stays_silent_on_a_cable_pull(wired):
    runner, sysfs, session, _cmds = wired
    session.locked = False
    runner.step(0.0)
    sysfs.set_ac(0)
    runner.step(1.0)
    runner.step(2.5)
    assert runner.stats.tamper_events == 1  # the signal is still recorded
    assert runner.stats.sirens == 0  # but nothing is played
    assert not runner.spawned.played("siren.ogg")


def test_motion_plays_the_warning_not_the_siren(wired):
    runner, _sysfs, _session, cmds = wired
    runner.step(0.0)
    runner.step(70.0)
    runner.motion(now=70.0)
    assert runner.stats.warnings == 1
    assert runner.spawned.played("warn-vi.ogg")
    assert runner.spawned.played("warn-en.ogg")
    assert not runner.spawned.played("siren.ogg")
    assert not cmds.ran("lock-session")


def test_hold_tick_runs_every_step_while_playing(wired):
    runner, sysfs, _session, cmds = wired
    runner.step(0.0)
    runner.step(70.0)
    sysfs.set_ac(0)
    runner.step(71.0)
    runner.step(72.5)
    assert runner.stats.sirens == 1
    before = cmds.count("set-sink-mute")
    runner.step(73.0)
    assert cmds.count("set-sink-mute") > before, "the audio path must be re-asserted while playing"


def test_lid_close_is_a_tamper_signal(wired):
    runner, sysfs, _session, _cmds = wired
    runner.step(0.0)
    runner.step(70.0)
    sysfs.set_lid("closed")
    runner.step(71.0)
    runner.step(72.5)
    assert "lid_closed" in runner.stats.signals
    assert runner.stats.sirens == 1


def test_stats_and_gate_reflect_pause(wired):
    from camtrap.state import MODE_PAUSED, write_mode

    runner, sysfs, _session, _cmds = wired
    write_mode(runner.cfg.root, MODE_PAUSED, now=0.0)
    runner.step(0.0)
    runner.step(70.0)
    sysfs.set_ac(0)
    runner.step(71.0)
    runner.step(72.5)
    assert runner.stats.sirens == 0
    assert not runner.spawned.played("siren.ogg")


def test_gate_is_wired_from_arming_to_responder(wired):
    runner, _sysfs, _session, _cmds = wired
    allowed, reason = runner.responder._gate(Stage.SIREN, 0.0)
    assert not allowed and reason in ("exit_delay", "not_armed")


# --- camera path (S2.6): motion gets the voice, a lifted case gets the siren -------------------


@pytest.fixture
def framed(wired):
    """The wired runner plus a settled background, ready to receive frames."""
    import numpy as np

    runner, _sysfs, _session, cmds = wired
    runner.cfg.detector.warmup_sec = 0.0
    runner.cfg.detector.min_motion_frames = 2
    runner.cfg.event.prebuffer_interval_sec = 0.2

    def blank(value=40):
        return np.full((360, 640, 3), value, dtype=np.uint8)

    runner.step(0.0)  # arming sees the locked session at t=0, so the exit delay ends at t=60
    for index in range(30):
        runner.on_frame(blank(), now=index * 0.2)
    runner.step(70.0)
    runner.stats = type(runner.stats)()  # count only what the test itself provokes
    runner.spawned.clear()
    return runner, blank, cmds


def test_motion_in_frame_plays_the_warning_only(framed):
    runner, blank, cmds = framed
    for index in range(6):
        frame = blank()
        frame[120:260, 100 + index * 25 : 230 + index * 25] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    assert runner.stats.warnings >= 1
    assert runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played("siren.ogg")
    assert not cmds.ran("lock-session")


def test_motion_writes_frames_and_a_manifest(framed):
    runner, blank, _cmds = framed
    for index in range(6):
        frame = blank()
        frame[120:260, 100 + index * 25 : 230 + index * 25] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    runner.on_frame(blank(), now=200.0)  # past the gap: closes the event
    manifests = list(runner.cfg.spool_dir.glob("evt_*.json"))
    frames = list(runner.cfg.spool_dir.glob("evt_*.jpg"))
    assert manifests and frames
    assert runner.stats.motion_events >= 1


def test_a_lifted_case_sounds_the_siren_from_the_camera_path(framed):
    import numpy as np

    runner, _blank, cmds = framed
    rng = np.random.default_rng(5)
    texture = np.dstack([rng.integers(0, 255, size=(360, 640), dtype=np.uint8)] * 3)
    for index in range(30):
        runner.on_frame(texture, now=100.0 + index * 0.2)
    runner.on_frame(np.roll(texture, 60, axis=1), now=110.0)
    assert runner.spawned.played("siren.ogg")
    assert "scene_shift" in runner.stats.signals
    assert cmds.ran("lock-session")


def test_a_light_switch_makes_no_sound_by_default(framed):
    runner, blank, _cmds = framed
    runner.on_frame(blank(215), now=100.0)
    assert runner.stats.light_events == 1
    assert not runner.spawned.played("siren.ogg")
    assert not runner.spawned.played("warn-vi.ogg")


def test_light_can_be_configured_to_warn(framed):
    runner, blank, _cmds = framed
    runner.cfg.sound.warn_on_light = True
    runner.on_frame(blank(215), now=100.0)
    assert runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played("siren.ogg")

"""S1: the loop that turns a tamper signal into sound (plan S1, checkpoint 1 in code form)."""

from pathlib import Path
from typing import ClassVar

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

    def played_siren(self):
        """The siren stage started. Its first file is the shutter click, by design."""
        return self.played("shutter.ogg") or self.played("siren.ogg")


@pytest.fixture
def wired(cfg, sysfs):
    cfg.tamper.ac_online_paths = [str(sysfs.ac)]
    cfg.tamper.lid_state_path = str(sysfs.lid)
    cfg.tamper.als_paths = [str(sysfs.als0)]
    cfg.tamper.debounce_sec = 1.0
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")
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
    assert runner.spawned.played_siren()
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
    assert not runner.spawned.played_siren()


def test_motion_plays_the_warning_not_the_siren(wired):
    runner, _sysfs, _session, cmds = wired
    runner.step(0.0)
    runner.step(70.0)
    runner.motion(now=70.0)
    assert runner.stats.warnings == 1
    assert runner.spawned.played("warn-vi.ogg")
    # English is queued behind Vietnamese, not passed in the same invocation: pw-play plays one
    # file at a time. The hand-off is covered in test_player.
    assert [c.lang for c in runner.responder._queue] == ["en"]
    assert not runner.spawned.played_siren()
    assert cmds.count("lock-session") <= 1  # from arming, not from the warning


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
    assert not runner.spawned.played_siren()


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
    assert not runner.spawned.played_siren()
    # The session is locked once, by arming. A warning must not lock anything by itself:
    # stage 1 is a notice, not a confrontation.
    assert cmds.count("lock-session") <= 1


def test_motion_writes_frames_and_a_manifest(framed):
    runner, blank, _cmds = framed
    for index in range(6):
        frame = blank()
        frame[120:260, 100 + index * 25 : 230 + index * 25] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    runner.on_frame(blank(), now=200.0)  # past the gap: closes the event
    # Artefacts may already have been drained to the receiver, so look in both places: what
    # matters is that they exist somewhere, not that they are still queued.
    inbox = Path(runner.cfg.upload.local_inbox)
    manifests = list(runner.cfg.spool_dir.glob("evt_*.json")) + list(inbox.glob("evt_*.json"))
    frames = list(runner.cfg.spool_dir.glob("evt_*.jpg")) + list(inbox.glob("evt_*.jpg"))
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
    assert runner.spawned.played_siren()
    assert "scene_shift" in runner.stats.signals
    assert cmds.ran("lock-session")


def test_a_light_switch_makes_no_sound_by_default(framed):
    runner, blank, _cmds = framed
    runner.on_frame(blank(215), now=100.0)
    assert runner.stats.light_events == 1
    assert not runner.spawned.played_siren()
    assert not runner.spawned.played("warn-vi.ogg")


def test_light_can_be_configured_to_warn(framed):
    runner, blank, _cmds = framed
    runner.cfg.sound.warn_on_light = True
    runner.on_frame(blank(215), now=100.0)
    assert runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played_siren()


def test_warning_is_requested_once_per_event_not_once_per_frame(framed):
    """Asking per frame re-enters the responder five times a second and buries the journal."""
    runner, blank, _cmds = framed
    for index in range(12):
        frame = blank()
        frame[120:260, 100 + index * 15 : 230 + index * 15] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    warnings = [p for p in runner.spawned if any("warn-" in a for a in p.argv)]
    assert len(warnings) == 1, f"expected one warning burst, got {len(warnings)}"


def test_a_new_event_warns_again(framed):
    runner, blank, _cmds = framed
    for index in range(4):
        frame = blank()
        frame[120:260, 100 + index * 20 : 230 + index * 20] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    # let the event close, then move again well past the warning cooldown
    runner.on_frame(blank(), now=200.0)
    for index in range(4):
        frame = blank()
        frame[120:260, 100 + index * 20 : 230 + index * 20] = 210
        runner.on_frame(frame, now=400.0 + index * 0.2)
    warnings = [p for p in runner.spawned if any("warn-" in a for a in p.argv)]
    assert len(warnings) == 2


def test_finish_closes_an_open_event(framed):
    """However the loop stops, the manifest on disk must be complete."""
    import json

    runner, blank, _cmds = framed
    for index in range(4):
        frame = blank()
        frame[120:260, 100 + index * 20 : 230 + index * 20] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    assert runner.events.active is not None
    runner.finish()
    assert runner.events.active is None
    inbox = Path(runner.cfg.upload.local_inbox)
    manifests = list(runner.cfg.spool_dir.glob("evt_*.json")) + list(inbox.glob("evt_*.json"))
    assert manifests
    payload = json.loads(manifests[0].read_text())
    assert payload["closed"] is True


def test_a_failing_uploader_never_stops_the_loop(framed, capsys):
    """Detection and the siren outrank delivery: an exception in housekeeping must not end the run.

    This killed the agent for real: a plain file where the inbox directory belonged raised
    FileExistsError out of the sink, up through the capture path, and the trap was dead — no more
    frames, no more siren.
    """
    runner, blank, _cmds = framed

    class Exploding:
        def drain(self, **kwargs):
            raise RuntimeError("delivery exploded")

        def heartbeat(self, line):
            raise RuntimeError("delivery exploded")

        sinks: ClassVar[list] = []

    runner.uploader = Exploding()
    runner.heartbeat.uploader = Exploding()

    for index in range(6):
        frame = blank()
        frame[120:260, 100 + index * 20 : 230 + index * 20] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)

    assert runner.stats.frames == 6, "the camera path kept running"
    assert runner.stats.motion_events >= 1
    assert "housekeeping_error" in capsys.readouterr().out


# --- what happens the moment the trap goes live -------------------------------------------------


def test_arming_locks_the_screen_once(wired):
    """The owner has left: an unlocked session is a way in, and a way to stop the agent."""
    runner, _sysfs, _session, cmds = wired
    runner.cfg.sound.lock_on_arm = True
    runner.cfg.sound.grab_power_button = False
    runner.step(0.0)
    assert not cmds.ran("lock-session"), "not armed yet: the exit delay is still running"
    runner.step(70.0)
    assert cmds.ran("lock-session")
    before = cmds.count("lock-session")
    runner.step(80.0)
    assert cmds.count("lock-session") == before, "lock once, not on every tick"


def test_locking_on_arm_can_be_disabled(wired):
    runner, _sysfs, _session, cmds = wired
    runner.cfg.sound.lock_on_arm = False
    runner.cfg.sound.grab_power_button = False
    runner.step(0.0)
    runner.step(70.0)
    assert not cmds.ran("lock-session")


def test_the_power_buttons_are_grabbed_while_armed(wired, monkeypatch):
    """systemd-inhibit is not enough: KDE acts on the press before logind sees it."""
    from camtrap import inputdev

    released = []

    class FakeGrab:
        def __init__(self, devices):
            self.devices = devices

        def acquire(self):
            return len(self.devices)

        def release(self):
            released.append(True)

    runner, _sysfs, session, _cmds = wired
    runner.cfg.sound.grab_power_button = True
    monkeypatch.setattr(
        inputdev,
        "scan",
        lambda *a, **k: [
            inputdev.InputDevice("event1", "Power Button", frozenset({inputdev.KEY_POWER}))
        ],
    )
    monkeypatch.setattr(inputdev, "Grab", FakeGrab)

    runner.step(0.0)
    runner.step(70.0)
    assert isinstance(runner._power_grab, FakeGrab), "armed: the buttons must be held"

    # unlocking hands them back — the owner is here and may want to switch the machine off
    session.locked = False
    runner.step(71.0)
    assert released, "disarming must release the power buttons"
    assert runner._power_grab is None


def test_finish_releases_the_power_buttons(wired, monkeypatch):
    from camtrap import inputdev

    released = []

    class FakeGrab:
        def __init__(self, devices):
            pass

        def acquire(self):
            return 1

        def release(self):
            released.append(True)

    runner, _sysfs, _session, _cmds = wired
    runner.cfg.sound.grab_power_button = True
    monkeypatch.setattr(
        inputdev,
        "scan",
        lambda *a, **k: [
            inputdev.InputDevice("event1", "Power Button", frozenset({inputdev.KEY_POWER}))
        ],
    )
    monkeypatch.setattr(inputdev, "Grab", FakeGrab)
    runner.step(0.0)
    runner.step(70.0)
    runner.finish()
    assert released, "however the run ends, the buttons go back"


def test_a_power_button_press_sounds_the_siren(wired, monkeypatch):
    """The owner's idea: if the press cannot switch the machine off, let it set off the alarm.

    Holding the button for several seconds still cuts power in hardware — but the siren now starts
    on the first press, so those seconds are loud ones.
    """
    from camtrap import inputdev

    class FakeGrab:
        def __init__(self, devices):
            self.queue = [inputdev.KEY_POWER]

        def acquire(self):
            return 1

        def release(self):
            pass

        def read_key_presses(self):
            codes, self.queue = self.queue, []
            return codes

    runner, _sysfs, _session, cmds = wired
    runner.cfg.sound.lock_on_arm = True
    runner.cfg.sound.grab_power_button = True
    monkeypatch.setattr(
        inputdev,
        "scan",
        lambda *a, **k: [
            inputdev.InputDevice("event1", "Power Button", frozenset({inputdev.KEY_POWER}))
        ],
    )
    monkeypatch.setattr(inputdev, "Grab", FakeGrab)

    runner.step(0.0)
    runner.step(70.0)  # armed: buttons grabbed, queue holds one press
    runner.step(71.0)

    assert "power_button_pressed" in runner.stats.signals
    assert runner.spawned.played_siren(), "a press while armed must be audible"
    assert cmds.ran("lock-session")


def test_reading_input_never_ends_the_run(wired, monkeypatch, capsys):
    from camtrap import inputdev

    class ExplodingGrab:
        def __init__(self, devices):
            pass

        def acquire(self):
            return 1

        def release(self):
            pass

        def read_key_presses(self):
            raise OSError("device vanished")

    runner, _sysfs, _session, _cmds = wired
    runner.cfg.sound.grab_power_button = True
    monkeypatch.setattr(
        inputdev,
        "scan",
        lambda *a, **k: [
            inputdev.InputDevice("event1", "Power Button", frozenset({inputdev.KEY_POWER}))
        ],
    )
    monkeypatch.setattr(inputdev, "Grab", ExplodingGrab)
    runner.step(0.0)
    runner.step(70.0)
    runner.step(71.0)  # must not raise
    assert "input_read_failed" in capsys.readouterr().out


# --- a tamper signal must arrive with a picture of the room as it is right now -------------------


def test_a_power_button_press_captures_a_fresh_frame(framed, monkeypatch):
    """A siren without a face is half the job: the press has to produce a photograph."""
    import json

    from camtrap import inputdev

    class FakeGrab:
        def __init__(self, devices):
            self.queue = [inputdev.KEY_POWER]

        def acquire(self):
            return 1

        def release(self):
            pass

        def read_key_presses(self):
            codes, self.queue = self.queue, []
            return codes

    runner, blank, _cmds = framed
    runner.cfg.sound.grab_power_button = True
    runner._armed_actions_done = False  # let the fixture's arming redo itself with the fake
    monkeypatch.setattr(
        inputdev,
        "scan",
        lambda *a, **k: [
            inputdev.InputDevice("event1", "Power Button", frozenset({inputdev.KEY_POWER}))
        ],
    )
    monkeypatch.setattr(inputdev, "Grab", FakeGrab)

    # a distinctive frame is the newest thing the loop has seen
    intruder = blank()
    intruder[100:300, 200:500] = 230
    runner.on_frame(intruder, now=100.0)

    runner.step(100.5)  # arms and grabs
    runner.step(101.0)  # reads the press -> tamper

    assert "power_button_pressed" in runner.stats.signals
    event = runner.events.active
    assert event is not None and event.kind.value == "tamper"
    assert event.frames_written > 0, "the event must carry frames, not just a signal"

    # The manifest may already have been drained to the receiver, so look in both places.
    inbox = Path(runner.cfg.upload.local_inbox)
    candidates = [
        runner.cfg.spool_dir / f"{event.event_id}.json",
        inbox / f"{event.event_id}.json",
    ]
    manifest_path = next(p for p in candidates if p.exists())
    manifest = json.loads(manifest_path.read_text())
    assert manifest["type"] == "tamper"
    assert "power_button_pressed" in manifest["signals"]


def test_a_tamper_burst_ignores_the_throttle(framed):
    """One frame every 5 s photographs the door closing. A burst photographs the person."""
    runner, blank, _cmds = framed
    runner.cfg.event.tamper_burst_sec = 10.0
    runner.cfg.event.tamper_burst_interval_sec = 1.0

    runner.on_frame(blank(), now=200.0)
    runner._tamper([runner.monitor.report_external("ac_offline", now=200.5)], now=200.5)
    event = runner.events.active
    assert event is not None
    before = event.frames_written

    # frames arriving 1 s apart during the burst are all kept, throttle notwithstanding
    for tick in range(1, 6):
        frame = blank()
        frame[50:100, 50:100] = 90  # small change: normally throttled away
        runner.on_frame(frame, now=200.5 + tick)

    assert event.frames_written >= before + 4, (
        f"burst should keep ~1 frame/s, got {event.frames_written - before}"
    )


def test_the_burst_ends_and_the_throttle_returns(framed):
    runner, blank, _cmds = framed
    runner.cfg.event.tamper_burst_sec = 3.0
    runner.on_frame(blank(), now=300.0)
    runner._tamper([runner.monitor.report_external("lid_closed", now=300.0)], now=300.0)
    event = runner.events.active
    for tick in range(1, 4):
        runner.on_frame(blank(), now=300.0 + tick)
    after_burst = event.frames_written
    for tick in range(5, 9):  # past tamper_burst_sec
        small = blank()
        small[10:40, 10:40] = 80
        runner.on_frame(small, now=300.0 + tick)
    assert event.frames_written <= after_burst + 1, "the throttle must come back"

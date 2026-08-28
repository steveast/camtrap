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

    #: Set by the fixtures, so `played_siren` can ask what actually fired.
    runner = None

    def played(self, needle):
        return any(needle in " ".join(p.argv) for p in self)

    def played_shutter(self):
        return self.played("shutter.ogg")

    def shutters(self):
        return [p for p in self if "shutter.ogg" in " ".join(p.argv)]

    def advance(self, now):
        """Move every spawned process's clock. Real time ends a real `pw-play`; this ends these,
        and without it the first sound of a test blocks every later one as `busy`."""
        for proc in self:
            proc.advance(now)

    def played_siren(self):
        """Whether the SIREN STAGE fired — not a file check any more.

        `shutter.ogg` used to imply the alarm, because the only thing that played it was the
        siren leading with a click. It now also stands alone as the click on every captured
        frame, so the file no longer identifies the stage and the runner's own count does.
        """
        return self.runner is not None and self.runner.stats.sirens > 0


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
    spawned.runner = runner
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


def test_motion_says_nothing_at_all_by_default(wired):
    """Stage 1 is off since the first hotel run. The room stays quiet; the frames still happen.

    Deliberately asserted on the default config rather than on a fixture that switches it off:
    if the default ever drifts back to speaking, this is the test that says so.
    """
    runner, _sysfs, _session, cmds = wired
    runner.step(0.0)
    runner.step(70.0)
    runner.motion(now=70.0)
    assert runner.stats.warnings == 0
    assert not runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played_siren()
    assert cmds.count("lock-session") <= 1  # from arming, and nothing else


def test_motion_plays_the_warning_when_it_is_switched_back_on(wired):
    runner, _sysfs, _session, cmds = wired
    runner.cfg.sound.warn_on_motion = True
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


def test_motion_in_frame_is_photographed_with_a_click_and_nothing_else(framed):
    """The whole camera path under the shipped sound policy: a shutter, no voice, no alarm."""
    runner, blank, cmds = framed
    for index in range(6):
        frame = blank()
        frame[120:260, 100 + index * 25 : 230 + index * 25] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    assert runner.stats.motion_events >= 1  # seen and written
    assert runner.spawned.played_shutter(), "a frame was taken; it must be audible as one"
    assert runner.stats.warnings == 0
    assert not runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played_siren()
    # The session is locked once, by arming, and by nothing on the motion path.
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


def _lift_the_case(runner):
    """Thirty frames of a textured scene, then the same scene shifted sideways."""
    import numpy as np

    rng = np.random.default_rng(5)
    texture = np.dstack([rng.integers(0, 255, size=(360, 640), dtype=np.uint8)] * 3)
    for index in range(30):
        runner.on_frame(texture, now=100.0 + index * 0.2)
    runner.on_frame(np.roll(texture, 60, axis=1), now=110.0)


def test_a_lifted_case_is_recorded_but_no_longer_sounds_the_siren(framed):
    """`scene_shift` left the siren set on the owner's instruction.

    It fired on the owner themselves walking back in — the camera sees the room change, not who
    changed it — and an alarm that greets you at your own door is an alarm you stop arming. The
    detection is unchanged: still a tamper event, still the burst of frames, still an alert.
    """
    runner, _blank, _cmds = framed
    _lift_the_case(runner)
    assert "scene_shift" in runner.stats.signals
    assert runner.stats.tamper_events >= 1
    assert not runner.spawned.played_siren()
    assert runner.spawned.played_shutter()  # the frames were still taken, and still say so


def test_a_lifted_case_sounds_the_siren_when_the_signal_is_configured_to(framed):
    runner, _blank, cmds = framed
    runner.cfg.tamper.siren_signals = ["ac_offline", "lid_closed", "scene_shift"]
    _lift_the_case(runner)
    assert runner.spawned.played_siren()
    assert "scene_shift" in runner.stats.signals
    assert cmds.ran("lock-session")


# --- the shutter: one click per frame actually taken ---------------------------------------------


def _drive(runner, frame, now):
    """One iteration of the real loop, as `pump` runs it: a frame, then a step.

    The step is what runs `hold_tick`, which is what notices a finished player — so a test that
    only calls `on_frame` leaves the first sound playing forever and every later one is refused
    as `busy`.
    """
    runner.spawned.advance(now)
    runner.on_frame(frame, now)
    runner.step(now)


def test_the_click_follows_the_cadence_not_the_frame_rate(framed):
    """Frames arrive five times a second and are written once every ten. Clicks follow writes.

    This is the whole reason the click hangs off the writer's frame counter rather than off the
    detector's verdict: motion is continuous, photography is not.
    """
    runner, blank, _cmds = framed
    for index in range(150):  # 30 s of continuous motion at 5 fps
        frame = blank()
        frame[120:260, 100 + (index % 8) * 25 : 230 + (index % 8) * 25] = 210
        _drive(runner, frame, 70.0 + index * 0.2)
    clicks = len(runner.spawned.shutters())
    # One when the event opens, then one per 10 s slot. Nowhere near one per frame.
    assert 2 <= clicks <= 5, f"expected a handful of clicks over 30 s, got {clicks}"


def test_a_frame_the_throttle_refused_makes_no_sound(framed):
    """The click reports a photograph, so there has to have been a photograph."""
    runner, blank, _cmds = framed
    frame = blank()
    frame[120:260, 100:230] = 210
    _drive(runner, frame, 70.0)
    before = len(runner.spawned.shutters())
    assert before >= 1
    # Well inside the 10 s throttle: frames keep arriving, none is written, nothing clicks.
    for index in range(1, 10):
        _drive(runner, frame, 70.0 + index * 0.2)
    assert len(runner.spawned.shutters()) == before


def test_the_click_never_interrupts_the_siren(framed):
    """A click that cut the alarm short to announce a photograph would be an own goal."""
    runner, blank, _cmds = framed
    runner.cfg.tamper.siren_signals = ["ac_offline", "lid_closed", "scene_shift"]
    _lift_the_case(runner)
    assert runner.spawned.played_siren()
    playing = runner.responder.playing
    assert playing is not None
    # The tamper burst writes a frame a second while the siren runs. Not one of them may cut in,
    # and the refusal has to be the shutter yielding rather than the fake never finishing.
    for index in range(1, 6):
        runner.on_frame(blank(90), now=111.0 + index)
        assert runner.responder.playing is playing, "the siren was cut short by a shutter click"


def test_an_unarmed_room_is_not_photographed_out_loud(wired):
    """The gate that holds the siren holds the click too: no clicking at the owner's desk."""
    runner, _sysfs, session, _cmds = wired
    session.locked = False  # the owner is sitting here
    runner.step(0.0)
    result = runner.responder.on_capture(now=1.0)
    assert not result.played
    assert not runner.spawned.played_shutter()


def test_the_click_can_be_switched_off(framed):
    runner, blank, _cmds = framed
    runner.cfg.sound.shutter_on_capture = False
    frame = blank()
    frame[120:260, 100:230] = 210
    runner.on_frame(frame, now=70.0)
    assert runner.stats.motion_events >= 1  # still photographed
    assert not runner.spawned.played_shutter()


def test_a_light_switch_raises_no_alarm_but_is_still_photographed(framed):
    """A light event is one frame, and a frame taken is a frame announced. Nothing more."""
    runner, blank, _cmds = framed
    runner.on_frame(blank(215), now=100.0)
    assert runner.stats.light_events == 1
    assert runner.spawned.played_shutter()
    assert not runner.spawned.played_siren()
    assert not runner.spawned.played("warn-vi.ogg")


def test_light_can_be_configured_to_warn(framed):
    runner, blank, _cmds = framed
    runner.cfg.sound.warn_on_motion = True
    runner.cfg.sound.warn_on_light = True
    runner.on_frame(blank(215), now=100.0)
    assert runner.spawned.played("warn-vi.ogg")
    assert not runner.spawned.played_siren()


def test_warning_is_requested_once_per_event_not_once_per_frame(framed):
    """Asking per frame re-enters the responder five times a second and buries the journal."""
    runner, blank, _cmds = framed
    runner.cfg.sound.warn_on_motion = True
    for index in range(12):
        frame = blank()
        frame[120:260, 100 + index * 15 : 230 + index * 15] = 210
        runner.on_frame(frame, now=70.0 + index * 0.2)
    warnings = [p for p in runner.spawned if any("warn-" in a for a in p.argv)]
    assert len(warnings) == 1, f"expected one warning burst, got {len(warnings)}"


def test_a_new_event_warns_again(framed):
    runner, blank, _cmds = framed
    runner.cfg.sound.warn_on_motion = True
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


def _press_the_power_button(runner, monkeypatch):
    """Arm the trap with one power-button press waiting in the grabbed device's queue."""
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


def test_a_power_button_press_is_blocked_and_recorded_but_silent_by_default(wired, monkeypatch):
    """The press is still taken away from the desktop, still a tamper event, still alerted.

    It just makes no noise: `power_button_pressed` left the siren set with `scene_shift`, on the
    owner's instruction, because both of them fired on the owner's own return. The cost is real
    and belongs in a test rather than in a comment — this is the one act that can end the trap,
    and it is now silent unless the signal is put back.
    """
    runner, _sysfs, _session, _cmds = wired
    _press_the_power_button(runner, monkeypatch)

    assert "power_button_pressed" in runner.stats.signals
    assert runner.stats.tamper_events >= 1
    assert not runner.spawned.played_siren()


def test_a_power_button_press_sounds_the_siren_when_configured_to(wired, monkeypatch):
    """The owner's earlier idea, kept one config line away: if the press cannot switch the
    machine off, let it set off the alarm."""
    runner, _sysfs, _session, cmds = wired
    runner.cfg.tamper.siren_signals = ["ac_offline", "lid_closed", "power_button_pressed"]
    _press_the_power_button(runner, monkeypatch)

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


def test_the_siren_waits_for_the_first_frame_to_be_acknowledged(framed, sysfs):
    """Evidence first, noise second — wired, not just wireable.

    `SoundResponder.set_ack_waiter` existed from the start and nothing but a test had ever called
    it, so every manifest written in the field said `sound_evidence_confirmed: false` and the
    siren fired before anything left the box. This asserts the production wiring, because the
    failure is invisible: the siren still sounds, the promise just quietly does not hold.
    """
    runner, _blank, _cmds = framed
    order: list[str] = []

    class Watching:
        """Acknowledges whatever is queued, and records when it was asked."""

        def __init__(self, spool):
            self.spool = spool

        def drain(self, **_kwargs):
            from camtrap.uploader import UploadReport

            report = UploadReport()
            for path in self.spool.pending():
                order.append(f"sent:{path.name}")
                report.acknowledged.append(path.name)
                report.sent.append(path.name)
                self.spool.acknowledge(path.name)
            return report

        def heartbeat(self, line):
            return True

    runner.uploader = Watching(runner.spool)
    runner.responder._spawn = lambda argv, duration: (
        order.append("sound") or FakeProcess(argv=argv, duration=duration, started=0.0)
    )
    sysfs.set_ac(0)
    runner.step(80.0)
    runner.step(81.5)  # the debounce confirms the pull

    assert runner.stats.sirens == 1
    assert "sound" in order, "the siren must still play"
    frames_sent = [item for item in order if item.startswith("sent:") and item.endswith(".jpg")]
    assert frames_sent, "the tamper frame must have been offered to the receiver"
    assert order.index(frames_sent[0]) < order.index("sound"), (
        f"evidence must leave before the noise starts, got {order}"
    )


def test_a_dead_uplink_does_not_delay_the_siren_past_the_cap(framed, sysfs):
    """The wait is bounded. An uplink that only fails must not hold the sound hostage."""
    runner, _blank, _cmds = framed
    runner.cfg.sound.delay_max_sec = 1.0
    slept: list[float] = []
    runner.sleep = slept.append
    elapsed = {"t": 100.0}

    def clock():
        elapsed["t"] += 0.3  # every look at the clock costs time, as it does in the real thing
        return elapsed["t"]

    class OnlyFails:
        def drain(self, **_kwargs):
            from camtrap.uploader import UploadReport

            return UploadReport(failed=["evt_x_000.jpg"])

        def heartbeat(self, line):
            return True

    runner.uploader = OnlyFails()
    runner.clock = clock
    sysfs.set_ac(0)
    runner.step(100.0)
    runner.step(101.5)

    assert runner.stats.sirens == 1, "the siren fires anyway"
    assert sum(slept) <= 2.0, f"the wait must stay bounded, slept {slept}"

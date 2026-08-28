"""The check that stands between the owner and walking out with a broken trap."""

import pytest

from camtrap import modes


@pytest.fixture
def ready_cfg(cfg):
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"warn")
    cfg.camera.device = "/dev/null"  # exists, so the device check gets past its first gate
    return cfg


def _rows(rows):
    return {name: (ok, detail) for name, ok, detail in rows}


def test_missing_sound_files_block_arming(cfg, monkeypatch):
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (True, "fake"))
    ready, rows = modes.preflight(cfg)
    assert not ready
    assert _rows(rows)["sound files"][0] is False


def test_a_silent_audio_path_blocks_arming(ready_cfg, monkeypatch):
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (False, "no sink"))
    ready, rows = modes.preflight(ready_cfg)
    assert not ready
    assert _rows(rows)["speakers"] == (False, "no sink")


def test_a_missing_receiver_is_a_warning_not_a_blocker(ready_cfg, monkeypatch):
    """Frames still accumulate locally and the siren needs no network at all."""
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (True, "sink"))
    from camtrap import selftest

    monkeypatch.setattr(
        selftest, "check_camera", lambda cfg: selftest.Check("camera", selftest.OK, "1280x720")
    )
    ready_cfg.upload.local_inbox = ""
    ready_cfg.upload.ssh_target = ""
    ready, rows = modes.preflight(ready_cfg)
    assert ready, "no receiver must not stop the trap from arming"
    assert _rows(rows)["receiver"][0] is True


def test_a_dead_camera_blocks_arming(ready_cfg, monkeypatch):
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (True, "sink"))
    ready_cfg.camera.device = "/nonexistent/video9"
    ready, rows = modes.preflight(ready_cfg)
    assert not ready
    assert _rows(rows)["camera"][0] is False


def test_probe_can_be_skipped(ready_cfg, monkeypatch):
    from camtrap import selftest

    monkeypatch.setattr(
        selftest, "check_camera", lambda cfg: selftest.Check("camera", selftest.OK, "1280x720")
    )
    called = []
    monkeypatch.setattr(
        modes, "audio_probe", lambda cfg, restore=False: called.append(1) or (True, "x")
    )
    ready, rows = modes.preflight(ready_cfg, probe=False)
    assert ready and not called
    assert _rows(rows)["speakers"][1] == "probe skipped"


def test_audio_probe_reports_a_missing_sink(cfg, monkeypatch):
    from camtrap.player import AudioPath

    monkeypatch.setattr(AudioPath, "prepare", lambda self, *, volume_pct: None)
    ok, detail = modes.audio_probe(cfg)
    assert not ok and "no sink" in detail


def test_audio_probe_reports_a_missing_siren_file(cfg, monkeypatch):
    from camtrap.player import AudioPath

    monkeypatch.setattr(AudioPath, "prepare", lambda self, *, volume_pct: "sink")
    ok, detail = modes.audio_probe(cfg)
    assert not ok and "no siren file" in detail


def test_a_standalone_check_puts_the_audio_profile_back(cfg, monkeypatch):
    """`guard check` must not leave the card on the speakers; arming deliberately does."""
    restored = []

    from camtrap.player import AudioPath

    monkeypatch.setattr(AudioPath, "prepare", lambda self, *, volume_pct: "sink")
    monkeypatch.setattr(AudioPath, "restore_profile", lambda self: restored.append(1) or True)
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")

    modes.audio_probe(cfg, restore=True)
    assert restored, "a check restores"
    restored.clear()
    modes.audio_probe(cfg, restore=False)
    assert not restored, "arming keeps the speakers"


def test_watch_does_not_write_to_the_cloud_folder(cfg):
    """A rehearsal must not publish frames into a folder that syncs off the machine.

    This is a real incident, not a hypothetical: test runs put 60 frames of the owner's room into
    the cloud folder because mega is in the default sink list.
    """
    cfg.upload.sinks = ["prod", "mega"]
    # No sounds generated in cfg, so watch() bails out right after adjusting the config.
    assert modes.watch(cfg, minutes=0.01) == 1
    assert cfg.upload.sinks == ["prod"]


def test_drill_does_not_write_to_the_cloud_folder(cfg):
    cfg.upload.sinks = ["prod", "mega"]
    assert modes.drill(cfg, seconds=0.01) == 1
    assert cfg.upload.sinks == ["prod"]


def test_watch_arms_itself_when_paused_and_restores_after(cfg):
    """In `paused` the gate refuses every stage before the detector is consulted, so an
    observation run would report CLEAN without having measured anything."""
    from camtrap import state

    state.write_mode(cfg.root, state.MODE_PAUSED)
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"s")
    cfg.shutter_path.write_bytes(b"c")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"w")
    cfg.camera.device = "/nonexistent/video9"  # bail out right after arming

    modes.watch(cfg, minutes=0.01)
    assert state.read_mode(cfg.root).paused, "the paused state must be put back"


# --- the audible policy, stated before the owner walks out ---------------------------------------


def test_preflight_states_what_will_and_will_not_make_a_noise(ready_cfg, monkeypatch):
    """Both halves of this policy were narrowed by hand, so the check says them out loud.

    A trap that is quieter than its owner remembers is worse than one that is louder.
    """
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (True, "sink"))
    _ready, rows = modes.preflight(ready_cfg)
    ok, detail = _rows(rows)["sound policy"]
    assert ok
    assert "ac_offline" in detail and "lid_closed" in detail
    assert "scene_shift" not in detail
    assert "capture: click" in detail
    assert "voice: off" in detail


def test_a_typo_in_the_signal_set_blocks_arming(ready_cfg, monkeypatch):
    """The one failure with no evidence: silence at the moment the siren was meant to sound."""
    monkeypatch.setattr(modes, "audio_probe", lambda cfg, restore=False: (True, "sink"))
    ready_cfg.tamper.siren_signals = ["ac_offline", "lid_close"]  # missing the d
    ready, rows = modes.preflight(ready_cfg)
    assert not ready
    ok, detail = _rows(rows)["sound policy"]
    assert ok is False
    assert "lid_close" in detail

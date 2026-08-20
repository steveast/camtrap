"""The check that stands between the owner and walking out with a broken trap."""

import pytest

from camtrap import runner


@pytest.fixture
def ready_cfg(cfg):
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"warn")
    cfg.camera.device = "/dev/null"  # exists, so the device check gets past its first gate
    return cfg


def _rows(rows):
    return {name: (ok, detail) for name, ok, detail in rows}


def test_missing_sound_files_block_arming(cfg, monkeypatch):
    monkeypatch.setattr(runner, "audio_probe", lambda cfg: (True, "fake"))
    ready, rows = runner.preflight(cfg)
    assert not ready
    assert _rows(rows)["sound files"][0] is False


def test_a_silent_audio_path_blocks_arming(ready_cfg, monkeypatch):
    monkeypatch.setattr(runner, "audio_probe", lambda cfg: (False, "no sink"))
    ready, rows = runner.preflight(ready_cfg)
    assert not ready
    assert _rows(rows)["speakers"] == (False, "no sink")


def test_a_missing_receiver_is_a_warning_not_a_blocker(ready_cfg, monkeypatch):
    """Frames still accumulate locally and the siren needs no network at all."""
    monkeypatch.setattr(runner, "audio_probe", lambda cfg: (True, "sink"))
    from camtrap import selftest

    monkeypatch.setattr(
        selftest, "check_camera", lambda cfg: selftest.Check("camera", selftest.OK, "1280x720")
    )
    ready_cfg.upload.local_inbox = ""
    ready_cfg.upload.ssh_target = ""
    ready, rows = runner.preflight(ready_cfg)
    assert ready, "no receiver must not stop the trap from arming"
    assert _rows(rows)["receiver"][0] is True


def test_a_dead_camera_blocks_arming(ready_cfg, monkeypatch):
    monkeypatch.setattr(runner, "audio_probe", lambda cfg: (True, "sink"))
    ready_cfg.camera.device = "/nonexistent/video9"
    ready, rows = runner.preflight(ready_cfg)
    assert not ready
    assert _rows(rows)["camera"][0] is False


def test_probe_can_be_skipped(ready_cfg, monkeypatch):
    from camtrap import selftest

    monkeypatch.setattr(
        selftest, "check_camera", lambda cfg: selftest.Check("camera", selftest.OK, "1280x720")
    )
    called = []
    monkeypatch.setattr(runner, "audio_probe", lambda cfg: called.append(1) or (True, "x"))
    ready, rows = runner.preflight(ready_cfg, probe=False)
    assert ready and not called
    assert _rows(rows)["speakers"][1] == "probe skipped"


def test_audio_probe_reports_a_missing_sink(cfg, monkeypatch):
    from camtrap.player import AudioPath

    monkeypatch.setattr(AudioPath, "prepare", lambda self, *, volume_pct: None)
    ok, detail = runner.audio_probe(cfg)
    assert not ok and "no sink" in detail


def test_audio_probe_reports_a_missing_siren_file(cfg, monkeypatch):
    from camtrap.player import AudioPath

    monkeypatch.setattr(AudioPath, "prepare", lambda self, *, volume_pct: "sink")
    ok, detail = runner.audio_probe(cfg)
    assert not ok and "no siren file" in detail

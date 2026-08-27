"""S0: config overrides and `camtrap status` (spec section 6, plan S0)."""

import json

import pytest

from camtrap import cli, log
from camtrap import config as config_mod


def test_defaults_match_spec(cfg):
    # 1080p at quality 95: measured to cost nothing in frame rate on this camera, and the frame
    # may end up in front of an investigator looking at a face.
    assert cfg.camera.width == 1920
    assert cfg.camera.height == 1080
    assert cfg.event.jpeg_quality == 95
    assert cfg.camera.target_fps == 5
    assert cfg.detector.warmup_sec == 20.0
    # Replaying 29 real captures: at 3.0 % one empty-room event of 22 fired, at 2.0 % four did.
    # The threshold stays until the curtain is masked; one frame at 7 % is motion outright, which
    # is above every empty-room frame recorded and below every event with a person in it.
    assert cfg.detector.min_area_pct == 3.0
    assert cfg.detector.instant_area_pct == 7.0
    assert cfg.detector.min_motion_frames == 2
    assert cfg.detector.motion_window_frames == 5
    # One frame at once, then one every 10 s while the event lasts. The boost that used to jump
    # this throttle is off — 0 means disabled, and it is what turned the cadence into a burst.
    assert cfg.event.snapshot_interval_sec == 10.0
    assert cfg.event.boost_area_pct == 0.0
    assert cfg.event.max_frames_per_event == 60
    assert cfg.spool.max_mb == 1024
    assert cfg.spool.retention_days == 14
    assert cfg.sound.siren_sec == 6.0
    assert cfg.sound.volume_pct == 100
    assert cfg.sound.warn_volume_pct == 85
    assert cfg.sound.delay_max_sec == 3.0
    assert cfg.sound.hold_poll_ms == 250
    assert cfg.arming.mode == "on_lock"
    assert cfg.arming.grace_after_unlock_sec == 300.0
    # The audible policy, narrowed after the first hotel run: nothing speaks on motion, and only
    # the two unambiguous signals scream. Both are one config line away from coming back.
    assert cfg.sound.warn_on_motion is False
    assert cfg.tamper.siren_signals == ["ac_offline", "lid_closed"]


def test_toml_overrides_only_named_keys(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text(
        """
        [sound]
        warn_langs = ["th", "en"]
        volume_pct = 70

        [detector]
        min_area_pct = 1.5
        """
    )
    cfg = config_mod.load(path)
    assert cfg.sound.warn_langs == ["th", "en"]
    assert cfg.sound.volume_pct == 70
    assert cfg.detector.min_area_pct == 1.5
    # untouched keys keep their defaults
    assert cfg.sound.siren_sec == 6.0
    assert cfg.detector.warmup_sec == 20.0


def test_unknown_config_key_is_an_error(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[sound]\nvolume_pctt = 70\n")
    with pytest.raises(ValueError, match="unknown config key"):
        config_mod.load(path)


def test_paths_are_derived_from_state_dir(cfg):
    assert cfg.spool_dir == cfg.root / "spool"
    assert cfg.warn_path("vi").name == "warn-vi.ogg"


def test_log_emits_one_parseable_line(capsys):
    log.emit("event", type="tamper", signals="ac_offline", played=1)
    out = capsys.readouterr().out.strip()
    assert out.startswith("event ")
    fields = dict(part.split("=", 1) for part in out.split()[1:])
    assert fields["type"] == "tamper"
    assert fields["signals"] == "ac_offline"
    assert fields["played"] == "1"


def test_log_quotes_values_with_spaces(capsys):
    log.emit("drop", reason="spool full, mid-event frame")
    out = capsys.readouterr().out.strip()
    assert 'reason="spool full, mid-event frame"' in out


def test_status_reports_mode_and_sound_readiness(cfg, capsys):
    rc = cli.main(["status", "--json"], cfg=cfg)
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "armed"
    assert payload["arming_mode"] == "on_lock"
    assert payload["spool_depth"] == 0
    assert payload["sound_ok"] is False  # no siren file generated in tmp_path
    assert payload["warn_langs"] == ["vi", "en"]
    assert "siren" in payload["missing_sounds"]

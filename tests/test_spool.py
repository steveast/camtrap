"""S2.4: the spool — priority, cap, drops, retention (spec 3.5, 7)."""

import time

import pytest

from camtrap.spool import Spool


def _write(cfg, name, size=1024, age_days=0.0):
    path = cfg.spool_dir / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    if age_days:
        stamp = time.time() - age_days * 86400
        import os

        os.utime(path, (stamp, stamp))
    return path


@pytest.fixture
def spool(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    return Spool(cfg)


def test_pending_puts_manifests_and_first_frames_first(spool, cfg):
    _write(cfg, "evt_A_004.jpg")
    _write(cfg, "evt_A_000.jpg")
    _write(cfg, "evt_A.json")
    _write(cfg, "evt_A_002.jpg")
    names = [p.name for p in spool.pending()]
    assert names[0] == "evt_A.json"
    assert names[1] == "evt_A_000.jpg"
    assert names[2:] == ["evt_A_002.jpg", "evt_A_004.jpg"]


def test_tamper_events_jump_ahead_of_everything(spool, cfg):
    _write(cfg, "evt_A_000.jpg")
    _write(cfg, "evt_A.json")
    _write(cfg, "evt_B_000.jpg")
    _write(cfg, "evt_B.json")
    spool.mark_tamper("evt_B")
    names = [p.name for p in spool.pending()]
    assert names[0].startswith("evt_B")
    assert names[1].startswith("evt_B")


def test_overflow_drops_mid_event_frames_and_keeps_the_first(spool, cfg, capsys):
    cfg.spool.max_mb = 1
    for index in range(40):
        _write(cfg, f"evt_A_{index:03d}.jpg", size=64 * 1024)
    _write(cfg, "evt_A.json", size=1024)
    dropped = spool.enforce_cap()
    remaining = {p.name for p in cfg.spool_dir.iterdir()}
    assert "evt_A_000.jpg" in remaining, "the first frame of an event is never dropped"
    assert "evt_A.json" in remaining
    assert dropped, "something had to go"
    assert all("_000.jpg" not in name for name in dropped)
    assert "drop" in capsys.readouterr().out


def test_overflow_prefers_frames_already_copied_to_the_cloud(spool, cfg):
    cfg.spool.max_mb = 1
    for index in range(1, 30):
        _write(cfg, f"evt_A_{index:03d}.jpg", size=64 * 1024)
    spool.mark_copied("evt_A_010.jpg")
    spool.mark_copied("evt_A_011.jpg")
    dropped = spool.enforce_cap()
    assert "evt_A_010.jpg" in dropped
    assert "evt_A_011.jpg" in dropped


def test_ack_removes_only_the_acknowledged_file(spool, cfg):
    _write(cfg, "evt_A_000.jpg")
    _write(cfg, "evt_A_001.jpg")
    spool.acknowledge("evt_A_000.jpg")
    remaining = {p.name for p in cfg.spool_dir.iterdir()}
    assert remaining == {"evt_A_001.jpg"}


def test_a_cloud_copy_never_frees_a_frame(spool, cfg):
    _write(cfg, "evt_A_000.jpg")
    spool.mark_copied("evt_A_000.jpg")
    assert (cfg.spool_dir / "evt_A_000.jpg").exists()
    assert [p.name for p in spool.pending()] == ["evt_A_000.jpg"]


def test_retention_removes_old_files_only(spool, cfg):
    _write(cfg, "evt_old_000.jpg", age_days=20)
    _write(cfg, "evt_new_000.jpg", age_days=1)
    removed = spool.enforce_retention()
    assert removed == ["evt_old_000.jpg"]
    assert (cfg.spool_dir / "evt_new_000.jpg").exists()


def test_depth_and_bytes_are_reported(spool, cfg):
    _write(cfg, "evt_A_000.jpg", size=2048)
    _write(cfg, "evt_A.json", size=512)
    assert spool.depth() == 2
    assert spool.total_bytes() == 2560

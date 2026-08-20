"""S4.1: the heartbeat carries readiness, not just liveness (spec 3.6)."""

import pytest

from camtrap.arming import Arming
from camtrap.heartbeat import HeartbeatSender, build
from camtrap.spool import Spool
from camtrap.tamper import TamperMonitor


class Session:
    def __init__(self, locked=True):
        self.locked = locked

    def locked_hint(self):
        return self.locked


@pytest.fixture
def parts(cfg, sysfs):
    cfg.tamper.ac_online_paths = [str(sysfs.ac)]
    cfg.tamper.lid_state_path = str(sysfs.lid)
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    return TamperMonitor(cfg), Spool(cfg), Arming(cfg, session=Session())


def _fields(line):
    return dict(part.split("=", 1) for part in line.strip().split())


def test_heartbeat_reports_power_lid_and_sound(cfg, parts, sysfs):
    monitor, spool, arming = parts
    line = build(cfg, started=0.0, now=90.0, monitor=monitor, spool=spool, arming=arming).render()
    fields = _fields(line)
    assert fields["mode"] == "armed"
    assert fields["uptime"] == "90"
    assert fields["ac_online"] == "1"
    assert fields["lid"] == "open"
    assert fields["sound_ok"] == "0"  # nothing rendered in tmp_path
    assert "siren" in fields["missing"]
    assert fields["langs"] == "vi,en"


def test_heartbeat_follows_the_hardware(cfg, parts, sysfs):
    monitor, spool, _arming = parts
    sysfs.set_ac(0)
    sysfs.set_lid("closed")
    fields = _fields(build(cfg, started=0.0, now=1.0, monitor=monitor, spool=spool).render())
    assert fields["ac_online"] == "0"
    assert fields["lid"] == "closed"


def test_sound_ok_turns_true_once_files_exist(cfg, parts):
    monitor, spool, _arming = parts
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"s")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"w")
    fields = _fields(build(cfg, started=0.0, now=1.0, monitor=monitor, spool=spool).render())
    assert fields["sound_ok"] == "1"
    assert fields["missing"] == "-"


def test_a_missing_language_keeps_sound_ok_false(cfg, parts):
    monitor, spool, _arming = parts
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"s")
    cfg.warn_path("vi").write_bytes(b"w")  # 'en' deliberately absent
    fields = _fields(build(cfg, started=0.0, now=1.0, monitor=monitor, spool=spool).render())
    assert fields["sound_ok"] == "0"
    assert fields["missing"] == "warn-en"


def test_arming_state_is_included(cfg, parts):
    monitor, spool, arming = parts
    arming.poll(now=0.0)
    fields = _fields(
        build(cfg, started=0.0, now=10.0, monitor=monitor, spool=spool, arming=arming).render()
    )
    assert fields["armed"] == "0"
    assert fields["arm_reason"] == "exit_delay"


def test_spool_depth_is_reported(cfg, parts):
    monitor, spool, _arming = parts
    (cfg.spool_dir / "evt_A_000.jpg").write_bytes(b"x" * 2048)
    fields = _fields(build(cfg, started=0.0, now=1.0, monitor=monitor, spool=spool).render())
    assert fields["spool"] == "1"


def test_sender_respects_the_interval(cfg):
    class FakeUploader:
        def __init__(self):
            self.lines = []
            self.ok = True

        def heartbeat(self, line):
            self.lines.append(line)
            return self.ok

    uploader = FakeUploader()
    sender = HeartbeatSender(cfg, uploader)
    heartbeat = build(cfg, started=0.0, now=0.0)
    assert sender.maybe_send(heartbeat, now=0.0)
    assert not sender.maybe_send(heartbeat, now=30.0)
    assert sender.maybe_send(heartbeat, now=61.0)
    assert len(uploader.lines) == 2


def test_a_failed_send_is_retried_next_tick(cfg, capsys):
    class FailingUploader:
        def heartbeat(self, line):
            return False

    sender = HeartbeatSender(cfg, FailingUploader())
    heartbeat = build(cfg, started=0.0, now=0.0)
    assert not sender.maybe_send(heartbeat, now=0.0)
    assert "heartbeat_failed" in capsys.readouterr().out
    # _last was not advanced, so the next tick tries again immediately
    assert sender.due(now=1.0)

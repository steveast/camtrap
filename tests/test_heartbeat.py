"""S4.1: the heartbeat carries readiness, not just liveness (spec 3.6)."""

from typing import ClassVar

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
    cfg.shutter_path.write_bytes(b"c")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"w")
    fields = _fields(build(cfg, started=0.0, now=1.0, monitor=monitor, spool=spool).render())
    assert fields["sound_ok"] == "1"
    assert fields["missing"] == "-"


def test_a_missing_language_keeps_sound_ok_false(cfg, parts):
    monitor, spool, _arming = parts
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"s")
    cfg.shutter_path.write_bytes(b"c")
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


class _Sink:
    name = "prod"


def test_sender_respects_the_interval(cfg):
    class FakeUploader:
        sinks: ClassVar[list] = [_Sink()]

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


def test_a_failed_send_is_retried_on_the_next_due_tick_not_in_a_loop(cfg, capsys):
    class FailingUploader:
        sinks: ClassVar[list] = [_Sink()]
        attempts = 0

        def heartbeat(self, line):
            type(self).attempts += 1
            return False

    uploader = FailingUploader()
    sender = HeartbeatSender(cfg, uploader)
    heartbeat = build(cfg, started=0.0, now=0.0)
    assert not sender.maybe_send(heartbeat, now=0.0)
    assert "heartbeat_failed" in capsys.readouterr().out

    # The run loop calls this every 250 ms; it must not retry — or log — on every one of them.
    for tick in range(1, 40):
        sender.maybe_send(heartbeat, now=tick * 0.25)
    assert FailingUploader.attempts == 1
    assert "heartbeat_failed" not in capsys.readouterr().out

    assert sender.due(now=61.0)
    sender.maybe_send(heartbeat, now=61.0)
    assert FailingUploader.attempts == 2


def test_recovery_is_logged_once(cfg, capsys):
    class FlakyUploader:
        sinks: ClassVar[list] = [_Sink()]

        def __init__(self):
            self.ok = False

        def heartbeat(self, line):
            return self.ok

    uploader = FlakyUploader()
    sender = HeartbeatSender(cfg, uploader)
    heartbeat = build(cfg, started=0.0, now=0.0)
    sender.maybe_send(heartbeat, now=0.0)
    capsys.readouterr()
    uploader.ok = True
    sender.maybe_send(heartbeat, now=61.0)
    assert "heartbeat_recovered" in capsys.readouterr().out


def test_no_receiver_configured_is_reported_once_not_every_tick(cfg, capsys):
    class NoSinks:
        sinks: ClassVar[list] = []

        def heartbeat(self, line):
            raise AssertionError("must not try to send with no receiver")

    sender = HeartbeatSender(cfg, NoSinks())
    heartbeat = build(cfg, started=0.0, now=0.0)
    for tick in range(400):
        sender.maybe_send(heartbeat, now=tick * 0.25)
    out = capsys.readouterr().out
    assert out.count("heartbeat_skip") == 1


def test_a_sink_that_cannot_deliver_says_so_in_the_heartbeat(cfg, tmp_path):
    """Unreadiness has to be visible BEFORE the event, on the machine that may walk out.

    `sound_ok` is here for exactly this reason. A missing or group-readable Telegram token means
    clips silently never reach the chat, and the only other trace would be a line in a log file
    on the laptop.
    """
    from camtrap.uploader import TelegramSink, Uploader

    cfg.video.telegram_env_file = str(tmp_path / "absent.env")
    uploader = Uploader(cfg, Spool(cfg), sinks=[TelegramSink(cfg)])
    line = build(cfg, started=0.0, now=10.0, uploader=uploader).render()
    assert "telegram_ok=0" in line
    assert "absent.env" in line


def test_a_ready_sink_reports_ready(cfg, tmp_path):
    from camtrap.uploader import TelegramSink, Uploader

    env = tmp_path / "telegram.env"
    env.write_text("TELEGRAM_BOT_TOKEN=1:A\nTELEGRAM_CHAT_ID=7\n")
    env.chmod(0o600)
    cfg.video.telegram_env_file = str(env)
    uploader = Uploader(cfg, Spool(cfg), sinks=[TelegramSink(cfg)])
    line = build(cfg, started=0.0, now=10.0, uploader=uploader).render()
    assert "telegram_ok=1" in line
    assert "telegram_why" not in line, "no reason is given when there is nothing wrong"

"""The agent must outlive the terminal that started it (incident 2026-08-20 22:31).

A run died when its terminal closed. Because the process was killed by SIGHUP, the receiver never
learned it had stopped: the last heartbeat still said `armed`, which is the signature of a stolen
laptop, and the poller repeated that alert eighteen times overnight.
"""

import signal
from pathlib import Path

from camtrap import log, runner


def test_sighup_and_sigpipe_are_ignored(cfg, monkeypatch):
    installed = {}

    def fake_signal(sig, handler):
        installed[sig] = handler

    monkeypatch.setattr(runner.signal, "signal", fake_signal)
    monkeypatch.setattr(runner.atexit, "register", lambda fn: None)
    runner.harden_process(cfg)
    assert installed[signal.SIGHUP] is signal.SIG_IGN
    assert installed[signal.SIGPIPE] is signal.SIG_IGN


def test_a_final_heartbeat_is_registered_for_exit(cfg, monkeypatch):
    monkeypatch.setattr(runner.signal, "signal", lambda sig, handler: None)
    registered = []
    monkeypatch.setattr(runner.atexit, "register", lambda fn: registered.append(fn))
    runner.harden_process(cfg)
    assert registered, "however the process ends, the receiver must be told"


def test_logging_survives_a_dead_stdout(cfg, tmp_path, monkeypatch):
    """Writing to a closed terminal used to raise — and take the trap down with it."""
    log_file = tmp_path / "camtrap.log"
    log.set_file(str(log_file))

    class DeadStdout:
        def write(self, *_args):
            raise OSError("terminal is gone")

        def flush(self):
            raise OSError("terminal is gone")

    log.set_stream(DeadStdout())
    try:
        log.emit("event_begin", id="evt_x", type="tamper")  # must not raise
    finally:
        log.set_stream(None)
        log.set_file(None)

    assert "evt_x" in log_file.read_text(), "the file keeps the record"


def test_the_log_file_is_created_with_its_parents(tmp_path):
    target = tmp_path / "deep" / "nested" / "camtrap.log"
    log.set_file(str(target))
    try:
        log.emit("start", mode="test")
    finally:
        log.set_file(None)
    assert target.exists()
    assert "start" in target.read_text()


def test_an_unwritable_log_file_is_not_fatal(tmp_path):
    log.set_file(str(tmp_path / "nope" / "\0bad"))  # invalid path
    try:
        log.emit("start", mode="test")  # must not raise
    finally:
        log.set_file(None)


def test_cli_accepts_a_log_file(cfg, tmp_path):
    from camtrap import cli

    target = tmp_path / "cli.log"
    rc = cli.main(["--log-file", str(target), "status"], cfg=cfg)
    log.set_file(None)
    assert rc == 0
    assert Path(target).exists()


# --- powering the laptop off is not a theft ----------------------------------------------------


def test_a_deliberate_shutdown_marks_the_offline_expected(cfg, monkeypatch):
    """The owner powers the machine off daily. Alerting on that trains them to ignore alerts."""
    from camtrap import state

    monkeypatch.setattr(runner, "system_is_stopping", lambda: True)
    published = []
    monkeypatch.setattr(runner, "publish_heartbeat", lambda c: published.append(c) or True)

    state.write_mode(cfg.root, state.MODE_ARMED)
    runner._final_word(cfg)

    assert state.read_mode(cfg.root).paused, "a shutdown is an expected offline"
    assert published, "and the receiver has to be told before we go"


def test_a_crash_does_not_mark_the_offline_expected(cfg, monkeypatch):
    """If the machine is not shutting down, silence stays suspicious — that is the whole point."""
    from camtrap import state

    monkeypatch.setattr(runner, "system_is_stopping", lambda: False)
    monkeypatch.setattr(runner, "publish_heartbeat", lambda c: True)

    state.write_mode(cfg.root, state.MODE_ARMED)
    runner._final_word(cfg)

    assert not state.read_mode(cfg.root).paused, "an unexplained stop must stay an alert"


def test_system_is_stopping_reads_systemd(monkeypatch):
    class Result:
        stdout = "stopping\n"

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Result())
    assert runner.system_is_stopping() is True

    class Running:
        stdout = "running\n"

    monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Running())
    assert runner.system_is_stopping() is False


def test_a_missing_systemctl_is_not_a_shutdown(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no systemctl")

    monkeypatch.setattr(runner.subprocess, "run", boom)
    assert runner.system_is_stopping() is False

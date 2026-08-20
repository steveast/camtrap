"""`camtrap pause` must reach the receiver, not just the local state dir (spec 3.8)."""

from pathlib import Path

from camtrap import cli, state


def test_pause_publishes_a_heartbeat_to_the_receiver(cfg, capsys):
    """Otherwise the poller keeps seeing `armed` and alerts on an offline the owner asked for."""
    rc = cli.main(["pause"], cfg=cfg)
    assert rc == 0
    assert state.read_mode(cfg.root).paused
    heartbeat = Path(cfg.upload.local_inbox).parent / "state" / "heartbeat"
    assert heartbeat.exists()
    assert "mode=paused" in heartbeat.read_text()


def test_resume_publishes_too(cfg):
    cli.main(["pause"], cfg=cfg)
    cli.main(["resume"], cfg=cfg)
    heartbeat = Path(cfg.upload.local_inbox).parent / "state" / "heartbeat"
    assert "mode=armed" in heartbeat.read_text()


def test_pause_reports_a_failure_to_reach_the_receiver(cfg, capsys):
    cfg.upload.local_inbox = ""
    cfg.upload.ssh_target = ""  # no receiver at all
    rc = cli.main(["pause"], cfg=cfg)
    assert rc == 1
    assert "could not tell the receiver" in capsys.readouterr().out
    # the local mode still changed: the agent itself must go quiet regardless
    assert state.read_mode(cfg.root).paused

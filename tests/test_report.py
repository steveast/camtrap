"""The summary that answers the empty-room checkpoints (plan checkpoints 2 and 4)."""

from camtrap import report


def test_parses_a_structured_line():
    record, fields = report.parse_line("event_begin id=evt_1 type=tamper signals=ac_offline")
    assert record == "event_begin"
    assert fields["type"] == "tamper"
    assert fields["signals"] == "ac_offline"


def test_parses_quoted_values():
    record, fields = report.parse_line('drop reason="spool cap" file=evt_A_003.jpg')
    assert record == "drop"
    assert fields["reason"] == "spool cap"


def test_ignores_human_text():
    assert report.parse_line("  1. pull the power cable      -> siren") is None
    assert report.parse_line("") is None


def test_an_empty_room_run_reports_zero_audible():
    lines = [
        "start mode=armed arming=on_still",
        "camera device=/dev/video0",
        "stop frames=430000 tamper=0 sirens=0 warnings=0",
    ]
    summary = report.summarise(lines)
    assert summary.runs == 1
    assert summary.frames == 430000
    assert summary.noise == 0
    assert "must be 0" in report.render(summary, source="x")


def test_audible_events_are_counted_separately():
    lines = [
        "sound stage=warning files=2 volume=85",
        "sound stage=siren files=1 volume=100",
        "sound stage=siren files=1 volume=100",
    ]
    summary = report.summarise(lines)
    assert summary.warnings == 1
    assert summary.sirens == 2
    assert summary.noise == 3


def test_defeated_silencing_attempts_are_surfaced():
    lines = [
        "sound_hold undone=mute sink=speaker",
        "sound_hold undone=volume:20 sink=speaker",
        "sound_hold undone=mute,volume:10 sink=speaker",
    ]
    summary = report.summarise(lines)
    assert summary.holds["mute"] == 2
    assert summary.holds["volume"] == 2
    assert "silencing attempts defeated" in report.render(summary, source="x")


def test_refusals_are_shown_as_the_safety_net():
    lines = ["sound_skip stage=warning reason=not_armed", "sound_skip stage=siren reason=paused"]
    summary = report.summarise(lines)
    assert summary.refusals["not_armed"] == 1
    rendered = report.render(summary, source="x")
    assert "safety net" in rendered


def test_signals_events_and_problems_are_tallied():
    lines = [
        "event_begin id=a type=motion",
        "event_begin id=b type=tamper signals=ac_offline",
        "event_truncated id=b cap=60",
        "tamper signal=ac_offline",
        "tamper signal=lid_closed",
        "drop file=evt_b_004.jpg reason=cap",
        "heartbeat_failed reason=x",
        "camera_reopen device=/dev/video0 reopens=2",
        "camera_gone failures=5",
    ]
    summary = report.summarise(lines)
    assert summary.events["motion"] == 1 and summary.events["tamper"] == 1
    assert summary.tamper_signals["lid_closed"] == 1
    assert summary.truncated == 1 and summary.drops == 1
    assert summary.heartbeat_failures == 1
    assert summary.camera_reopens == 1
    assert any("camera_gone" in error for error in summary.errors)


def test_a_missing_log_is_not_an_error(tmp_path):
    summary, source = report.from_file(tmp_path / "nope.log")
    assert summary.runs == 0
    assert "missing" in source

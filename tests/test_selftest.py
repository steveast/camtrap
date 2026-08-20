"""S6.2: the readiness check, and the rule that a check leaves nothing changed."""

from tests.fakes import FakeRunner

from camtrap import selftest
from camtrap.arming import LogindSession
from camtrap.player import AudioPath


def test_missing_sound_files_fail_with_a_command_to_fix_them(cfg):
    checks = selftest.check_sounds(cfg)
    files = next(c for c in checks if c.name == "sound:files")
    assert files.verdict == selftest.FAIL
    assert "make-siren" in files.hint


def test_present_sound_files_pass(cfg):
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"s")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"w")
    files = next(c for c in selftest.check_sounds(cfg) if c.name == "sound:files")
    assert files.verdict == selftest.OK


def test_no_warning_languages_is_a_warning_not_a_pass(cfg):
    cfg.sound.warn_langs = []
    langs = next(c for c in selftest.check_sounds(cfg) if c.name == "sound:langs")
    assert langs.verdict == selftest.WARN


def test_missing_camera_device_fails(cfg):
    cfg.camera.device = "/nonexistent/video9"
    check = selftest.check_camera(cfg)
    assert check.verdict == selftest.FAIL
    assert "missing" in check.detail


def test_sysrq_flags_a_dangerous_mask(monkeypatch, tmp_path):
    path = tmp_path / "sysrq"
    path.write_text("438\n")
    monkeypatch.setattr(selftest, "Path", lambda _p: path)
    check = selftest.check_sysrq()
    assert check.verdict == selftest.WARN
    assert "Alt+SysRq" in check.hint


def test_sysrq_accepts_sync_only(monkeypatch, tmp_path):
    path = tmp_path / "sysrq"
    path.write_text("16\n")
    monkeypatch.setattr(selftest, "Path", lambda _p: path)
    assert selftest.check_sysrq().verdict == selftest.OK


def test_render_shows_hints_only_for_problems(cfg):
    checks = [
        selftest.Check("a", selftest.OK, "fine", hint="unused"),
        selftest.Check("b", selftest.FAIL, "broken", hint="do this"),
    ]
    rendered = selftest.render(checks)
    assert "do this" in rendered
    assert "unused" not in rendered


def test_audio_path_restores_the_previous_profile(cfg):
    """A check must not leave the card on a different profile than it found."""
    runner = FakeRunner()
    path = AudioPath(cfg, runner=runner)
    path.prepare(volume_pct=85)
    assert path.restore_profile()
    switches = runner.commands("pactl set-card-profile")
    assert len(switches) == 2
    assert "Speaker" in " ".join(switches[0])
    assert "Headphones" in " ".join(switches[1]), "the original profile must be put back"


def test_restore_is_a_no_op_when_nothing_was_switched(cfg):
    cfg.sound.card = "explicit-card"
    path = AudioPath(cfg, runner=FakeRunner())
    assert path.restore_profile() is False


# --- session resolution ------------------------------------------------------------------------


def test_locked_hint_uses_xdg_session_id(monkeypatch, cfg):
    calls = []

    def fake_run(self, args):
        calls.append(args)
        if args[:2] == ["show-session", "7"] and args[-2] == "-p" and args[2] == "7":
            pass
        if "LockedHint" in args:
            return "yes"
        if "Id" in args:
            return "7"
        return None

    monkeypatch.setenv("XDG_SESSION_ID", "7")
    monkeypatch.setattr(LogindSession, "_run", fake_run)
    assert LogindSession(cfg).locked_hint() is True
    assert any("7" in call for call in calls)


def test_locked_hint_falls_back_to_the_display_session(monkeypatch, cfg):
    def fake_run(self, args):
        if args[0] == "show-user":
            return "2"
        if "Id" in args:
            return "2" if args[1] == "2" else None
        if "LockedHint" in args:
            return "no"
        return None

    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    monkeypatch.setenv("USER", "steve")
    monkeypatch.setattr(LogindSession, "_run", fake_run)
    assert LogindSession(cfg).locked_hint() is False


def test_unresolvable_session_reports_none(monkeypatch, cfg):
    monkeypatch.delenv("XDG_SESSION_ID", raising=False)
    monkeypatch.setattr(LogindSession, "_run", lambda self, args: None)
    assert LogindSession(cfg).locked_hint() is None

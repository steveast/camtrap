"""S1.2/S1.3/S1.6/S1.9: the audio path, the hold layer, and the two stages.

Spec 3.4. The rules under test are the ones that decide whether the trap is real:
  - the sink is chosen explicitly, never "the default" (which here is a USB dongle)
  - a mute pressed mid-burst is undone within one hold tick
  - motion gets the voice, tamper gets the siren, and only tamper locks the session
"""

import pytest
from tests.fakes import FakeProcess, FakeRunner

from camtrap.player import AudioPath, SoundResponder, Stage


def _spawn_fake(argv, duration):
    return FakeProcess(argv=argv, duration=duration, started=0.0)


@pytest.fixture
def runner():
    return FakeRunner()


@pytest.fixture
def sound_files(cfg):
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")
    for lang in cfg.sound.warn_langs:
        cfg.warn_path(lang).write_bytes(b"warn")
    return cfg


@pytest.fixture
def responder(sound_files, runner):
    spawned: list[FakeProcess] = []

    def spawn(argv, duration):
        proc = FakeProcess(argv=argv, duration=duration, started=0.0)
        spawned.append(proc)
        return proc

    r = SoundResponder(sound_files, runner=runner, spawn=spawn)
    r.spawned = spawned  # type: ignore[attr-defined]
    return r


# --- audio path ----------------------------------------------------------------


def test_audio_path_picks_the_speaker_sink_not_the_default(cfg, runner):
    path = AudioPath(cfg, runner=runner)
    sink = path.prepare(volume_pct=100)
    assert "Speaker" in sink
    assert "Actions_X99_PRO" not in sink  # the USB dongle must never be chosen
    assert runner.ran("set-card-profile")
    assert runner.ran("set-sink-mute")
    assert runner.ran("set-sink-volume")


def test_audio_path_switches_the_card_profile_away_from_headphones(cfg, runner):
    AudioPath(cfg, runner=runner).prepare(volume_pct=100)
    profile_calls = runner.commands("pactl set-card-profile")
    assert profile_calls, "must switch to a profile that has a Speaker port"
    assert "Speaker" in " ".join(profile_calls[0])


def test_audio_path_honours_an_explicit_sink(cfg, runner):
    cfg.sound.sink = "explicit-sink"
    sink = AudioPath(cfg, runner=runner).prepare(volume_pct=70)
    assert sink == "explicit-sink"
    assert runner.ran("set-sink-volume explicit-sink 70%")


def test_audio_path_disables_auto_mute_so_a_jack_cannot_silence_speakers(cfg, runner):
    AudioPath(cfg, runner=runner).prepare(volume_pct=100)
    assert runner.ran("Auto-Mute Mode")


def test_audio_path_survives_a_failing_mixer_command(cfg, runner):
    runner.fail.add("Auto-Mute Mode")
    sink = AudioPath(cfg, runner=runner).prepare(volume_pct=100)
    assert "Speaker" in sink  # best-effort: a missing ALSA control is not fatal


# --- hold layer ----------------------------------------------------------------


def test_hold_tick_reasserts_mute_and_volume(responder, runner):
    responder.on_tamper(["ac_offline"], now=0.0)
    before = runner.count("set-sink-mute")
    responder.hold_tick(now=0.3)
    assert runner.count("set-sink-mute") > before
    assert runner.ran("set-sink-volume")


def test_hold_tick_does_nothing_when_silent(responder, runner):
    calls = len(runner.calls)
    responder.hold_tick(now=1.0)
    assert len(runner.calls) == calls


def test_hold_tick_respects_the_poll_interval(responder, runner):
    responder.on_tamper(["ac_offline"], now=0.0)
    responder.hold_tick(now=0.01)  # sooner than hold_poll_ms
    before = runner.count("set-sink-mute")
    responder.hold_tick(now=0.02)
    assert runner.count("set-sink-mute") == before


# --- two stages ----------------------------------------------------------------


def test_motion_plays_the_warning_in_language_order(responder):
    played = responder.on_motion(now=0.0)
    assert played.stage is Stage.WARNING
    assert [call.lang for call in played.calls] == ["vi", "en"]
    assert all("warn-" in call.path for call in played.calls)


def test_motion_never_plays_the_siren(responder, runner):
    responder.on_motion(now=0.0)
    assert not runner.ran("siren.ogg")


def test_motion_does_not_lock_the_session(responder, runner):
    responder.on_motion(now=0.0)
    assert not runner.ran("lock-session")


def test_tamper_plays_the_siren_and_locks_the_session(responder, runner):
    played = responder.on_tamper(["ac_offline"], now=0.0)
    assert played.stage is Stage.SIREN
    # Shutter first — "you have just been photographed" — and then the alarm.
    assert [call.lang for call in played.calls] == ["shutter", ""]
    assert "shutter.ogg" in played.calls[0].path
    assert "siren.ogg" in played.calls[1].path
    assert runner.ran("lock-session")


def test_tamper_lock_can_be_disabled_for_debugging(sound_files, runner):
    sound_files.sound.lock_session_on_tamper = False
    r = SoundResponder(sound_files, runner=runner, spawn=_spawn_fake)
    r.on_tamper(["ac_offline"], now=0.0)
    assert not runner.ran("lock-session")


def test_tamper_mid_warning_cuts_the_warning_off(responder):
    responder.on_motion(now=0.0)
    warning_proc = responder.spawned[-1]
    responder.on_tamper(["lid_closed"], now=1.0)
    assert warning_proc.killed, "the siren must not queue behind a warning"
    assert responder.spawned[-1] is not warning_proc


def test_warning_uses_its_own_volume(responder, runner):
    responder.on_motion(now=0.0)
    assert runner.ran("set-sink-volume") and runner.ran("85%")


def test_siren_uses_full_volume(responder, runner):
    responder.on_tamper(["ac_offline"], now=0.0)
    assert runner.ran("100%")


# --- limits --------------------------------------------------------------------


def test_warning_cooldown_blocks_a_second_warning(responder):
    assert responder.on_motion(now=0.0).played
    assert not responder.on_motion(now=30.0).played
    assert responder.on_motion(now=200.0).played  # cooldown is 120 s


def test_siren_cooldown_and_per_event_cap(responder):
    assert responder.on_tamper(["ac_offline"], now=0.0).played
    assert not responder.on_tamper(["ac_offline"], now=10.0).played
    assert responder.on_tamper(["ac_offline"], now=70.0).played
    assert responder.on_tamper(["ac_offline"], now=140.0).played
    # max_per_event = 3, so the fourth burst of the same event is refused
    result = responder.on_tamper(["ac_offline"], now=210.0)
    assert not result.played and result.reason == "max_per_event"


def test_new_event_resets_the_per_event_cap(responder):
    for tick in (0.0, 70.0, 140.0):
        responder.on_tamper(["ac_offline"], now=tick)
    responder.end_event()
    result = responder.on_tamper(["lid_closed"], now=210.0)
    assert result.played


def test_hourly_cap_is_enforced(responder):
    responder.cfg.sound.max_per_event = 100
    now = 0.0
    played = 0
    for _ in range(20):
        if responder.on_tamper(["ac_offline"], now=now).played:
            played += 1
        now += 61.0
    assert played == responder.cfg.sound.max_per_hour


def test_paused_mode_is_silent(responder):
    responder.set_gate(lambda stage, now: (False, "paused"))
    assert not responder.on_tamper(["ac_offline"], now=0.0).played
    assert not responder.on_motion(now=0.0).played


def test_gate_can_allow_the_siren_while_holding_the_warning(responder):
    responder.set_gate(lambda stage, now: (stage is Stage.SIREN, "warmup"))
    assert responder.on_tamper(["ac_offline"], now=0.0).played
    assert not responder.on_motion(now=10.0).played


def test_missing_sound_file_reports_instead_of_going_quiet(cfg, runner):
    cfg.sounds_dir.mkdir(parents=True, exist_ok=True)
    cfg.siren_path.write_bytes(b"siren")
    cfg.shutter_path.write_bytes(b"click")  # warn files deliberately absent
    r = SoundResponder(cfg, runner=runner, spawn=_spawn_fake)
    result = r.on_motion(now=0.0)
    assert not result.played
    assert result.reason == "missing_file"


# --- evidence first ------------------------------------------------------------


def test_siren_waits_for_the_first_frame_then_plays(responder):
    responder.set_ack_waiter(lambda timeout: True)
    result = responder.on_tamper(["ac_offline"], now=0.0)
    assert result.played and result.evidence_confirmed


def test_siren_plays_anyway_when_the_receiver_is_unreachable(responder):
    responder.set_ack_waiter(lambda timeout: False)
    result = responder.on_tamper(["ac_offline"], now=0.0)
    assert result.played and not result.evidence_confirmed


def test_repeated_refusals_are_logged_once(responder, capsys):
    responder.set_gate(lambda stage, now: (False, "not_armed"))
    for tick in range(5):
        responder.on_motion(now=float(tick))
    out = capsys.readouterr().out
    assert out.count("sound_skip") == 1, "a refusal repeated per frame must not spam the journal"


def test_a_changed_reason_is_logged_again(responder, capsys):
    reasons = iter(["not_armed", "not_armed", "paused", "paused"])
    responder.set_gate(lambda stage, now: (False, next(reasons)))
    for tick in range(4):
        responder.on_motion(now=float(tick))
    out = capsys.readouterr().out
    assert out.count("sound_skip") == 2
    assert "reason=not_armed" in out and "reason=paused" in out


# --- cooldown is per signal: the lid and the cable are two different alarms --------------------


def test_a_different_signal_breaks_through_the_cooldown(responder):
    """Closing the lid then pulling the cable is what a thief actually does."""
    assert responder.on_tamper(["lid_closed"], now=0.0).played
    # same signal again inside the window: refused
    assert not responder.on_tamper(["lid_closed"], now=20.0).played
    # a different kind of interference: sounds immediately
    result = responder.on_tamper(["ac_offline"], now=21.0)
    assert result.played, "a new signal must not wait out another signal's cooldown"


def test_a_repeated_signal_still_honours_its_own_cooldown(responder):
    assert responder.on_tamper(["ac_offline"], now=0.0).played
    assert not responder.on_tamper(["ac_offline"], now=30.0).played
    assert responder.on_tamper(["ac_offline"], now=70.0).played


def test_new_signals_cannot_machine_gun_the_siren(responder):
    """A flapping sensor producing new names must still be floored."""
    assert responder.on_tamper(["lid_closed"], now=0.0).played
    result = responder.on_tamper(["ac_offline"], now=1.0)
    assert not result.played and result.reason == "retrigger_floor"
    assert responder.on_tamper(["ac_offline"], now=5.0).played


# --- one file per pw-play invocation: a queue, not a playlist argument -------------------------


def test_files_play_one_after_another(responder):
    """pw-play takes ONE file; passing several silently played only the first, which is why the
    English half of the warning never sounded."""
    result = responder.on_motion(now=0.0)
    assert [c.lang for c in result.calls] == ["vi", "en"]

    first = responder.spawned[-1]
    assert len([a for a in first.argv if a.endswith(".ogg")]) == 1, "one file per invocation"
    assert "warn-vi.ogg" in " ".join(first.argv)

    # when the first finishes, the hold tick starts the second
    first.advance(31.0)  # past warn_timeout_sec, so poll() reports it done
    assert responder.hold_tick(now=1.0)
    second = responder.spawned[-1]
    assert second is not first
    assert "warn-en.ogg" in " ".join(second.argv)


def test_the_siren_follows_the_shutter(responder):
    responder.on_tamper(["ac_offline"], now=0.0)
    first = responder.spawned[-1]
    assert "shutter.ogg" in " ".join(first.argv)
    first.advance(10.0)  # siren_sec is 6, so the click has long finished
    responder.hold_tick(now=0.5)
    assert "siren.ogg" in " ".join(responder.spawned[-1].argv)


def test_a_siren_preempting_a_warning_drops_the_queued_languages(responder):
    responder.on_motion(now=0.0)
    responder.on_tamper(["lid_closed"], now=1.0)
    # the queued English file must not surface after the siren
    played = " ".join(responder.spawned[-1].argv)
    assert "shutter.ogg" in played or "siren.ogg" in played
    assert not any("warn-en" in " ".join(p.argv) for p in responder.spawned[-1:])


def test_the_shutter_can_be_switched_off(sound_files, runner):
    sound_files.sound.shutter_before_siren = False
    r = SoundResponder(sound_files, runner=runner, spawn=_spawn_fake)
    result = r.on_tamper(["ac_offline"], now=0.0)
    assert [c.lang for c in result.calls] == [""]
    assert "siren.ogg" in result.calls[0].path

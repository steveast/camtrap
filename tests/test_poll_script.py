"""S4.3/S4.4: the poller's behaviour, driven by a fake receiver (spec 3.7).

The fake stands in for `ssh <target> <verb>`: it answers list/state/manifest and records what the
poller asked it to send. Nothing here touches the network, the real VPS or Telegram.
"""

import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "pi" / "camtrap-poll.sh"


@pytest.fixture
def rig(tmp_path):
    """A fake receiver plus the environment the poller needs."""
    fake = tmp_path / "fake-ssh.sh"
    outbox = tmp_path / "sent"
    outbox.mkdir()
    listing = tmp_path / "listing.txt"
    state = tmp_path / "state.txt"
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    listing.write_text("")
    state.write_text("mode=armed sound_ok=1\nhb_age=10\n")

    fake.write_text(
        f"""#!/bin/sh
set -eu
# Real ssh consumes its own options before the remote command; so must the stand-in, or the
# poller's connection-reuse flags would be mistaken for the verb.
while [ $# -gt 0 ]; do
  case "$1" in
    -o) shift 2 ;;
    -i|-p|-F) shift 2 ;;
    -*) shift ;;
    *) break ;;
  esac
done
verb="$1"
case "$verb" in
  list) cat {listing} ;;
  state) cat {state} ;;
  manifest*) name=$(echo "$verb" | cut -d' ' -f2); cat {manifests}/"$name" ;;
  send-photo*)
      name=$(echo "$verb" | cut -d' ' -f2)
      cat > {outbox}/photo-"$name".txt
      if [ -f {tmp_path}/fail-send ]; then exit 1; fi
      echo "ok send-photo $name" ;;
  send-album*)
      names=$(echo "$verb" | cut -d' ' -f2)
      n=$(ls {outbox} | grep -c '^album-' || true)
      {{ echo "$names"; cat; }} > {outbox}/album-"$n".txt
      if [ -f {tmp_path}/fail-send ]; then exit 1; fi
      echo "ok send-album" ;;
  send-message)
      n=$(ls {outbox} | grep -c '^message-' || true)
      cat > {outbox}/message-"$n".txt
      if [ -f {tmp_path}/fail-send ]; then exit 1; fi
      echo "ok send-message" ;;
  *) echo "unknown $verb" >&2; exit 1 ;;
esac
"""
    )
    fake.chmod(0o755)

    env = {
        "PATH": "/usr/bin:/bin",
        "TELEGRAM_BOT_TOKEN": "TESTTOKEN",
        "TELEGRAM_CHAT_ID": "42",
        "CAMTRAP_SSH": f"sh {fake}",
        "CAMTRAP_STATE_DIR": str(tmp_path / "poll-state"),
        "CAMTRAP_POLL_ENV": str(tmp_path / "absent.env"),
        "HB_STALE_SEC": "300",
        "REPEAT_SEC": "1800",
        "CAMTRAP_TZ": "UTC",
    }

    class Rig:
        def __init__(self):
            self.env = env
            self.listing = listing
            self.state = state
            self.manifests = manifests
            self.outbox = outbox
            self.tmp = tmp_path

        def run(self):
            return subprocess.run(
                ["sh", str(SCRIPT)], capture_output=True, env=self.env, check=False
            )

        def sent(self):
            return sorted(p.name for p in self.outbox.iterdir())

        def deliveries(self):
            """Photos and albums both count as one event delivered."""
            return [n for n in self.sent() if n.startswith(("photo-", "album-"))]

        def body(self, name):
            return (self.outbox / name).read_text()

        def add_event(
            self, event, kind, frames=3, signals=None, sound=None, mtime=1787000000, key=None
        ):
            rows = "".join(
                f"{event}_{index:03d}.jpg 1024 {mtime}.0\n" for index in range(max(1, frames))
            )
            self.listing.write_text(
                self.listing.read_text() + rows + f"{event}.json 200 {mtime}.0\n"
            )
            self._key = key
            sig = "" if not signals else '"signals":' + str(list(signals)).replace("'", '"') + ","
            snd = "" if not sound else f'"sound_stage":"{sound}",'
            key_field = f'"key_frame":"{key}",' if key else ""
            (self.manifests / f"{event}.json").write_text(
                f'{{"type":"{kind}",{sig}{snd}{key_field}"frames":{frames}}}'
            )

        def set_state(self, **fields):
            hb_age = fields.pop("hb_age", 10)
            body = " ".join(f"{k}={v}" for k, v in fields.items())
            self.state.write_text(f"{body}\nhb_age={hb_age}\n")

    return Rig()


def test_syntax_is_posix_clean():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_a_motion_event_is_delivered_with_its_frames(rig):
    rig.add_event("evt_20260820T101010Z", "motion", frames=4)
    result = rig.run()
    assert result.returncode == 0, result.stderr.decode()
    assert rig.deliveries(), "the event must be delivered"
    body = rig.body(rig.deliveries()[0])
    assert "📷" in body and "movement in the room" in body
    assert "frames: 4" in body


def test_one_event_is_one_photograph(rig):
    """`ALBUM_MAX = 1` since 2026-08-31: the key frame, and nothing beside it.

    Six photographs of one visit is five more than the alert needs, and the whole event is on the
    receiver and in the warehouse regardless. The album stays in the script behind the number.
    """
    rig.add_event("evt_20260820T111500Z", "motion", frames=8, key="evt_20260820T111500Z_005.jpg")
    rig.run()
    assert rig.sent() == ["photo-evt_20260820T111500Z_005.jpg.txt"], rig.sent()
    assert not [name for name in rig.sent() if name.startswith("album-")]


def test_the_key_frame_leads_the_album(rig):
    """The key frame leads, then the newest frames. `_000` is the room before anything happened."""
    rig.env["ALBUM_MAX"] = "6"  # the album is off by default; this is the knob it lives behind
    rig.add_event("evt_20260820T111500Z", "motion", frames=8, key="evt_20260820T111500Z_005.jpg")
    rig.run()
    album = [n for n in rig.sent() if n.startswith("album-")]
    assert album, "several frames should travel as one group"
    names = rig.body(album[0]).splitlines()[0]
    assert names.startswith("evt_20260820T111500Z_005.jpg"), names
    assert "evt_20260820T111500Z_007.jpg" in names, "the newest frames follow the key one"
    assert "evt_20260820T111500Z_000.jpg" not in names, (
        "the oldest pre-buffer frame is an empty room; it belongs on the receiver, not in the alert"
    )


def test_a_key_frame_that_has_not_arrived_yet_never_falls_back_to_the_empty_room(rig):
    """Measured in production: two of four alerts that afternoon led with `_000`.

    The manifest sorts ahead of the frames it names, so it reaches the receiver naming a frame that
    is still uploading. The fallback then chose `_000` — by construction the oldest pre-buffer
    frame, the room before anyone was in it. Whatever is happening is in the LATEST frame.
    """
    rig.add_event(
        "evt_20260820T112000Z", "motion", frames=4, key="evt_20260820T112000Z_005.jpg"
    )  # key names a frame the listing does not have yet
    rig.run()
    sent = [n for n in rig.sent() if n.startswith(("album-", "photo-"))]
    assert sent, "it must still deliver something"
    names = rig.body(sent[0]).splitlines()[0] if sent[0].startswith("album-") else sent[0]
    assert "_003.jpg" in names, f"the newest frame present should lead, got {names}"
    assert not names.startswith("evt_20260820T112000Z_000.jpg"), names


def test_a_single_frame_event_falls_back_to_one_photo(rig):
    rig.add_event("evt_20260820T112000Z", "light", frames=1)
    rig.run()
    assert any(n.startswith("photo-") for n in rig.sent())


def test_a_tamper_event_gets_its_own_icon_and_signals(rig):
    rig.add_event(
        "evt_20260820T111111Z",
        "tamper",
        frames=2,
        signals=["ac_offline", "lid_closed"],
        sound="siren",
    )
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "🚨" in body
    assert "ac_offline" in body and "lid_closed" in body
    assert "sound: siren" in body


def test_an_event_is_not_sent_twice(rig):
    rig.add_event("evt_20260820T101010Z", "motion")
    rig.run()
    first = rig.sent()
    rig.run()
    assert rig.sent() == first


def test_a_failed_send_is_retried_next_tick(rig):
    rig.add_event("evt_20260820T101010Z", "motion")
    (rig.tmp / "fail-send").write_text("x")
    rig.run()
    marker = Path(rig.env["CAMTRAP_STATE_DIR"]) / "sent-evt_20260820T101010Z"
    assert not marker.exists(), "state must not advance on a failed send"
    (rig.tmp / "fail-send").unlink()
    rig.run()
    assert marker.exists()


def test_stale_heartbeat_alerts_once_then_holds(rig):
    rig.set_state(mode="armed", sound_ok=1, hb_age=900)
    rig.run()
    messages = [n for n in rig.sent() if n.startswith("message-")]
    assert len(messages) == 1
    assert "went silent" in rig.body(messages[0])
    rig.run()  # inside REPEAT_SEC: no new alert
    assert len([n for n in rig.sent() if n.startswith("message-")]) == 1


def test_recovery_sends_green_once(rig):
    rig.set_state(mode="armed", sound_ok=1, hb_age=900)
    rig.run()
    rig.set_state(mode="armed", sound_ok=1, hb_age=5)
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("🟢" in body for body in bodies)
    before = len(bodies)
    rig.run()
    assert len([n for n in rig.sent() if n.startswith("message-")]) == before


def test_paused_mode_suppresses_the_silence_alert(rig):
    rig.set_state(mode="paused", sound_ok=1, hb_age=9000)
    rig.run()
    assert [n for n in rig.sent() if n.startswith("message-")] == []


def test_tamper_then_silence_is_one_linked_story(rig):
    rig.add_event("evt_20260820T111111Z", "tamper", signals=["ac_offline"])
    rig.run()  # records the tamper
    rig.set_state(mode="armed", sound_ok=1, hb_age=900)
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    linked = [b for b in bodies if "handled at" in b and "went silent" in b]
    assert len(linked) == 1, bodies


def test_sound_not_ready_raises_its_own_alert(rig):
    rig.set_state(mode="armed", sound_ok=0, hb_age=5)
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("siren will not fire" in body for body in bodies)


def test_missing_heartbeat_is_reported(rig):
    rig.state.write_text("hb_age=-1\n")
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("no heartbeat" in body for body in bodies)


def test_token_is_not_written_to_the_state_dir(rig):
    rig.add_event("evt_20260820T101010Z", "motion")
    rig.run()
    state_dir = Path(rig.env["CAMTRAP_STATE_DIR"])
    for path in state_dir.rglob("*"):
        if path.is_file():
            assert "TESTTOKEN" not in path.read_text()


def test_missing_required_env_fails_loudly(rig):
    env = dict(rig.env)
    del env["TELEGRAM_BOT_TOKEN"]
    result = subprocess.run(["sh", str(SCRIPT)], capture_output=True, env=env, check=False)
    assert result.returncode != 0
    assert b"TELEGRAM_BOT_TOKEN" in result.stderr


def test_the_first_observation_is_recorded_silently(rig):
    """Announcing "not armed" on the poller's very first tick is noise, not information."""
    rig.set_state(mode="armed", sound_ok=1, hb_age=5, armed=0, arm_reason="waiting_for_quiet")
    rig.run()
    assert [n for n in rig.sent() if n.startswith("message-")] == []


def test_arming_transitions_are_announced(rig):
    rig.set_state(mode="armed", sound_ok=1, hb_age=5, armed=0, arm_reason="waiting_for_quiet")
    rig.run()
    rig.set_state(mode="armed", sound_ok=1, hb_age=5, armed=1, arm_reason="-")
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("🛡" in body and "armed at" in body for body in bodies)


def test_disarming_is_announced_once(rig):
    rig.set_state(mode="armed", sound_ok=1, hb_age=5, armed=1, arm_reason="-")
    rig.run()
    rig.set_state(mode="armed", sound_ok=1, hb_age=5, armed=0, arm_reason="unlock_grace")
    rig.run()
    before = len([n for n in rig.sent() if n.startswith("message-")])
    rig.run()  # unchanged: no repeat
    assert len([n for n in rig.sent() if n.startswith("message-")]) == before
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("🔓" in body and "unlock_grace" in body for body in bodies)


def test_an_unclosed_event_is_flagged_in_the_caption(rig):
    """An event still running when the agent died means the laptop left the room."""
    rig.listing.write_text(
        "evt_20260820T121212Z_000.jpg 1024 1787000000.0\n"
        "evt_20260820T121212Z.json 200 1787000000.0\n"
    )
    (rig.manifests / "evt_20260820T121212Z.json").write_text(
        '{"type":"tamper","closed":false,"signals":["ac_offline"],"frames":5}'
    )
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "still running when the agent stopped" in body
    assert "🚨" in body


def test_a_closed_event_carries_no_such_note(rig):
    rig.listing.write_text(
        "evt_20260820T131313Z_000.jpg 1024 1787000000.0\n"
        "evt_20260820T131313Z.json 200 1787000000.0\n"
    )
    (rig.manifests / "evt_20260820T131313Z.json").write_text(
        '{"type":"motion","closed":true,"frames":9}'
    )
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "still running" not in body


def test_every_event_is_sent_by_default(rig):
    """The owner's call: no cap. A missed event cannot be recovered from a summary."""
    for index in range(7):
        rig.add_event(f"evt_2026082019{index:02d}00Z", "motion", frames=3)
        rig.run()
    assert len(rig.deliveries()) == 7, f"all seven must be sent, got {len(rig.deliveries())}"
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert not [b for b in bodies if "not sent individually" in b], "no summary when uncapped"


def test_the_cap_can_still_be_switched_on(rig):
    """Kept as a valve for a hotel room where a curtain fires every two minutes."""
    rig.env["MOTION_ALERTS_PER_HOUR"] = "3"
    for index in range(6):
        rig.add_event(f"evt_2026082014{index:02d}00Z", "motion", frames=3)
        rig.run()
    assert len(rig.deliveries()) == 3, f"cap is 3, sent {len(rig.deliveries())}"


def test_tamper_is_never_capped(rig):
    rig.env["MOTION_ALERTS_PER_HOUR"] = "1"
    rig.add_event("evt_20260820150000Z", "motion")
    rig.run()
    for index in range(3):
        rig.add_event(f"evt_2026082016{index:02d}00Z", "tamper", signals=["ac_offline"])
        rig.run()
    assert len(rig.deliveries()) == 4, "one motion plus three tampers must all get through"


def test_suppressed_events_arrive_as_a_summary(rig):
    rig.env["MOTION_ALERTS_PER_HOUR"] = "1"
    rig.env["SUMMARY_MIN_SEC"] = "0"
    for index in range(4):
        rig.add_event(f"evt_2026082017{index:02d}00Z", "motion")
        rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    summaries = [b for b in bodies if "not sent individually" in b]
    assert len(summaries) >= 1
    assert "📊" in summaries[0]


def test_a_capped_event_is_not_re_examined(rig):
    rig.env["MOTION_ALERTS_PER_HOUR"] = "0"
    rig.add_event("evt_20260820180000Z", "motion")
    rig.run()
    marker = Path(rig.env["CAMTRAP_STATE_DIR"]) / "sent-evt_20260820180000Z"
    assert marker.exists(), "a suppressed event must still be marked seen"


def test_reminders_back_off_instead_of_repeating_all_night(rig):
    """Eighteen identical alerts through one night is how alerting gets muted."""
    rig.env["REPEAT_SEC"] = "1"
    rig.env["REPEAT_MAX_SEC"] = "8"
    rig.set_state(mode="armed", sound_ok=1, hb_age=9000)

    import time as _time

    sent_at = []
    for _ in range(6):
        before = len([n for n in rig.sent() if n.startswith("message-")])
        rig.run()
        after = len([n for n in rig.sent() if n.startswith("message-")])
        if after > before:
            sent_at.append(_time.time())
        _time.sleep(1.1)

    # 1 s, then 2 s, then 4 s: six ticks a second apart cannot produce six alerts
    assert 2 <= len(sent_at) <= 4, f"expected backoff, got {len(sent_at)} alerts"


def test_a_long_running_failure_says_how_long(rig):
    rig.env["REPEAT_SEC"] = "0"
    rig.set_state(mode="armed", sound_ok=1, hb_age=9000)
    rig.run()
    marker = Path(rig.env["CAMTRAP_STATE_DIR"]) / "fail-hb"
    first, last, repeats = marker.read_text().split()
    marker.write_text(f"{int(first) - 7200} {last} {repeats}")
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("unresolved for 2h" in b for b in bodies), bodies


def test_recovery_still_reports_immediately(rig):
    rig.env["REPEAT_SEC"] = "0"
    rig.set_state(mode="armed", sound_ok=1, hb_age=9000)
    rig.run()
    rig.set_state(mode="armed", sound_ok=1, hb_age=5)
    rig.run()
    bodies = [rig.body(n) for n in rig.sent() if n.startswith("message-")]
    assert any("🟢" in b for b in bodies)


def test_a_power_button_press_gets_its_own_wording(rig):
    """Not "handling": someone reached for the one control that ends the alarm."""
    rig.add_event(
        "evt_20260821T070000Z",
        "tamper",
        frames=12,
        signals=["power_button_pressed", "ac_offline"],
        sound="siren",
        key="evt_20260821T070000Z_004.jpg",
    )
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "🆘" in body
    assert "POWER BUTTON PRESSED" in body
    assert "may be the last ones" in body, "the reader has to know the stakes"
    assert "🚨" not in body, "the generic tamper wording must not also appear"


def test_other_tamper_signals_keep_the_ordinary_wording(rig):
    rig.add_event("evt_20260821T071000Z", "tamper", frames=4, signals=["ac_offline"], sound="siren")
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "🚨" in body
    assert "being handled" in body
    assert "POWER BUTTON" not in body
    assert "last ones" not in body


def test_motion_is_unaffected(rig):
    rig.add_event("evt_20260821T072000Z", "motion", frames=6)
    rig.run()
    body = rig.body(rig.deliveries()[0])
    assert "📷" in body and "movement in the room" in body
    assert "🆘" not in body

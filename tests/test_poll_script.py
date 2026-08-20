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

        def body(self, name):
            return (self.outbox / name).read_text()

        def add_event(self, event, kind, frames=3, signals=None, sound=None, mtime=1787000000):
            self.listing.write_text(
                self.listing.read_text()
                + f"{event}_000.jpg 1024 {mtime}.0\n{event}.json 200 {mtime}.0\n"
            )
            sig = "" if not signals else '"signals":' + str(list(signals)).replace("'", '"') + ","
            snd = "" if not sound else f'"sound_stage":"{sound}",'
            (self.manifests / f"{event}.json").write_text(
                f'{{"type":"{kind}",{sig}{snd}"frames":{frames}}}'
            )

        def set_state(self, **fields):
            hb_age = fields.pop("hb_age", 10)
            body = " ".join(f"{k}={v}" for k, v in fields.items())
            self.state.write_text(f"{body}\nhb_age={hb_age}\n")

    return Rig()


def test_syntax_is_posix_clean():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_a_motion_event_is_sent_as_a_photo(rig):
    rig.add_event("evt_20260820T101010Z", "motion", frames=4)
    result = rig.run()
    assert result.returncode == 0, result.stderr.decode()
    assert "photo-evt_20260820T101010Z_000.jpg.txt" in rig.sent()
    body = rig.body("photo-evt_20260820T101010Z_000.jpg.txt")
    assert body.startswith("TESTTOKEN\n42\n")
    assert "📷" in body and "movement in the room" in body
    assert "frames: 4" in body


def test_a_tamper_event_gets_its_own_icon_and_signals(rig):
    rig.add_event(
        "evt_20260820T111111Z",
        "tamper",
        frames=2,
        signals=["ac_offline", "lid_closed"],
        sound="siren",
    )
    rig.run()
    body = rig.body("photo-evt_20260820T111111Z_000.jpg.txt")
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

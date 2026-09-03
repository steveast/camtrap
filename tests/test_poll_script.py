"""S4.3/S4.4: the poller's behaviour, driven by a fake receiver (spec 3.7).

The fake stands in for `ssh <target> <verb>`: it answers list/state/manifest and records what the
poller asked it to send. Nothing here touches the network, the real VPS or Telegram.
"""

import subprocess
import time
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
            self,
            event,
            kind,
            frames=3,
            signals=None,
            sound=None,
            mtime=1787000000,
            key=None,
            prebuffer=None,
            closed=None,
            clip_segments=None,
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
            # `prebuffer` says where the run-up ends and the event proper starts. None leaves it
            # out entirely, which is what a manifest from an agent older than the field looks
            # like — the poller has to cope with those too.
            pre = "" if prebuffer is None else f'"prebuffer":{prebuffer},'
            shut = "" if closed is None else f'"closed":{str(closed).lower()},'
            clip = (
                ""
                if clip_segments is None
                else f'"clip_segments":{clip_segments},"clip_bytes":{clip_segments * 1400000},'
            )
            (self.manifests / f"{event}.json").write_text(
                f'{{"type":"{kind}",{sig}{snd}{key_field}{pre}{shut}{clip}"frames":{frames}}}'
            )

        def marker(self, event):
            return Path(self.env["CAMTRAP_STATE_DIR"]) / f"sent-{event}"

        def album_names(self, name):
            """The comma-separated names the fake receiver was asked to group."""
            return self.body(name).splitlines()[0].split(",")

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


def test_a_visit_is_delivered_frame_by_frame_in_one_group(rig):
    """The reversal of 2026-08-31, on the owner's instruction: every frame of the visit arrives.

    One photograph per event was an answer to a chat full of near-identical albums, and it was the
    wrong knob — it cut the record rather than the noise. The agent takes a frame every 5 s; those
    frames now reach the chat, grouped into one message instead of dropped.
    """
    rig.add_event("evt_20260903T111500Z", "motion", frames=9, prebuffer=5)
    rig.run()
    album = [name for name in rig.sent() if name.startswith("album-")]
    assert album, f"the frames of the visit should travel as one group, got {rig.sent()}"
    names = rig.album_names(album[0])
    assert names == [
        "evt_20260903T111500Z_005.jpg",
        "evt_20260903T111500Z_006.jpg",
        "evt_20260903T111500Z_007.jpg",
        "evt_20260903T111500Z_008.jpg",
    ], names
    assert "movement in the room" in rig.body(album[0]), "the group carries the alert caption"


def test_the_pre_buffer_is_not_part_of_the_stream(rig):
    """The run-up is the room BEFORE anything happened, and it stays on the receiver.

    Five near-identical photographs of an empty room per event is not what "show me the whole
    visit" meant; the manifest's `prebuffer` is what tells the poller where the visit starts.
    """
    rig.add_event("evt_20260903T112000Z", "motion", frames=8, prebuffer=5)
    rig.run()
    sent = "".join(rig.body(name) for name in rig.deliveries())
    for index in range(5):
        assert f"_{index:03d}.jpg" not in sent, f"_{index:03d} is the empty room before the visit"


def test_later_frames_of_an_open_event_arrive_as_a_follow_up(rig):
    """A visit that lasts keeps delivering. The alert is not the end of the story."""
    event = "evt_20260903T113000Z"
    fresh = int(time.time())
    rig.add_event(event, "motion", frames=8, prebuffer=5, mtime=fresh)
    rig.run()
    first = rig.deliveries()
    assert first, "the visit must be delivered"

    rig.listing.write_text("")  # re-list with the frames that arrived since
    rig.add_event(event, "motion", frames=18, prebuffer=5, mtime=fresh)
    rig.run()
    later = [name for name in rig.deliveries() if name not in first]
    assert later, "frames taken after the alert must still reach the chat"
    body = "".join(rig.body(name) for name in later)
    assert "continues" in body, body
    assert "_008.jpg" in body, "the follow-up starts where the alert stopped"
    assert "_007.jpg" not in rig.album_names(later[0]), "already delivered, not sent twice"
    assert "_017.jpg" in body, "the newest frame is what the follow-up is for"


def test_a_follow_up_waits_until_it_can_fill_a_group(rig):
    """Passes run every 15 s and the cadence writes a frame every 5 s.

    Sending each pass's three frames as its own message is four messages a minute for one visit —
    the complaint that cut the cadence from 5 s to 30 s in the first place. The frames are not
    dropped, they are grouped: what is held is stated in the log and goes out with the next group.
    The ALERT never waits; this applies to the follow-ups behind it.
    """
    event = "evt_20260903T120000Z"
    fresh = int(time.time())
    rig.add_event(event, "motion", frames=8, prebuffer=5, mtime=fresh)
    rig.run()
    first = rig.deliveries()
    assert first, "the alert goes with whatever has arrived"

    rig.listing.write_text("")
    rig.add_event(event, "motion", frames=11, prebuffer=5, mtime=fresh)
    rig.run()
    assert rig.deliveries() == first, "three more frames is not worth a message of its own"

    rig.listing.write_text("")
    rig.add_event(event, "motion", frames=18, prebuffer=5, mtime=fresh)
    rig.run()
    later = [name for name in rig.deliveries() if name not in first]
    assert later, "once the group can be filled it goes"
    names = rig.album_names(later[0])
    assert len(names) == 10, names
    assert names[0].endswith("_008.jpg"), "and it starts where the alert stopped — nothing skipped"


def test_a_closed_visit_flushes_its_tail(rig):
    """The last frames of a visit are few by definition and must not wait for a tenth."""
    event = "evt_20260903T121000Z"
    fresh = int(time.time())
    rig.add_event(event, "motion", frames=8, prebuffer=5, mtime=fresh, closed=False)
    rig.run()
    first = rig.deliveries()

    rig.listing.write_text("")
    rig.add_event(event, "motion", frames=11, prebuffer=5, mtime=fresh, closed=True)
    rig.run()
    later = [name for name in rig.deliveries() if name not in first]
    assert later, "a closed event sends what is left, however little"


def test_a_visit_that_never_closes_still_flushes_its_tail(rig):
    """The agent was carried off mid-event, so `closed` will never arrive.

    Without the tail timer those frames would sit on the receiver waiting for a group that cannot
    be completed — and they are the most interesting frames in the spool, because whatever the
    agent was watching is what stopped it.
    """
    event = "evt_20260903T122000Z"
    fresh = int(time.time())
    rig.add_event(event, "motion", frames=8, prebuffer=5, mtime=fresh)
    rig.run()
    first = rig.deliveries()

    rig.listing.write_text("")  # nothing new for longer than STREAM_TAIL_SEC
    rig.add_event(event, "motion", frames=11, prebuffer=5, mtime=fresh - 600)
    rig.run()
    later = [name for name in rig.deliveries() if name not in first]
    assert later, "a visit that stopped producing frames must not strand its tail"


def test_the_alert_says_a_clip_exists(rig):
    """The clip is in the warehouse and nowhere near the chat, so the alert has to mention it.

    A photograph that arrives without saying there is a minute of video beside it is a photograph
    nobody thinks to look behind.
    """
    rig.add_event("evt_20260903T130000Z", "motion", frames=8, prebuffer=5, clip_segments=4)
    rig.run()
    body = "".join(rig.body(name) for name in rig.deliveries())
    assert "clip: 4 segment(s)" in body, body
    assert "warehouse" in body


def test_no_clip_means_no_clip_line(rig):
    """An event with no clip must not claim one — including a manifest from before clips existed."""
    rig.add_event("evt_20260903T131000Z", "motion", frames=8, prebuffer=5)
    rig.run()
    body = "".join(rig.body(name) for name in rig.deliveries())
    assert "clip:" not in body, body


def test_a_frame_is_delivered_exactly_once(rig):
    """Nothing new on the receiver means nothing new in the chat — the flood guard."""
    rig.add_event("evt_20260903T114000Z", "motion", frames=8, prebuffer=5)
    rig.run()
    first = rig.sent()
    rig.run()
    assert rig.sent() == first, "a pass that finds no new frame must send nothing"


def test_a_group_is_capped_at_telegrams_ceiling(rig):
    """sendMediaGroup takes 2-10 items, and a long visit has more frames than that."""
    rig.add_event("evt_20260903T115000Z", "motion", frames=40, prebuffer=5)
    rig.run()
    albums = [name for name in rig.sent() if name.startswith("album-")]
    assert albums, rig.sent()
    for name in albums:
        assert len(rig.album_names(name)) <= 10, rig.album_names(name)
    # STREAM_BATCHES_MAX bounds the pass, so one long visit cannot spend the whole tick — and the
    # marker records how far it got, so the next pass carries on rather than starting over.
    assert len(albums) == 3, f"three groups per pass, got {len(albums)}"
    assert rig.marker("evt_20260903T115000Z").read_text().split()[1] == "34"


def test_a_single_new_frame_goes_as_a_photo(rig):
    """A group of one is refused by Telegram, so one frame travels as a photo."""
    rig.add_event("evt_20260903T116000Z", "motion", frames=6, prebuffer=5)
    rig.run()
    assert rig.sent() == ["photo-evt_20260903T116000Z_005.jpg.txt"], rig.sent()


def test_a_marker_from_before_the_stream_is_adopted_not_replayed(rig):
    """Deploying this must not empty yesterday afternoon into the chat.

    The old marker recorded only WHEN an event was alerted, never which frames went. Reading a
    missing index as "none delivered" would re-send every frame still on the receiver — days of
    them, all at once, which is how a person learns to mute the alerts entirely.
    """
    event = "evt_20260903T117000Z"
    rig.add_event(event, "motion", frames=20, prebuffer=5)
    rig.marker(event).parent.mkdir(parents=True, exist_ok=True)
    rig.marker(event).write_text("1787000000\n")  # the old one-field format
    rig.run()
    assert not rig.deliveries(), f"nothing should be replayed, got {rig.sent()}"
    assert rig.marker(event).read_text().split()[1] == "19", "and it adopts what is there now"


def test_stream_off_restores_one_photograph_per_event(rig):
    """`STREAM=0` is the revert path, and it keeps the behaviour it replaced.

    `ALBUM_MAX = 1` since 2026-08-31: the key frame, and nothing beside it.
    """
    rig.env["STREAM"] = "0"
    rig.add_event("evt_20260820T111500Z", "motion", frames=8, key="evt_20260820T111500Z_005.jpg")
    rig.run()
    assert rig.sent() == ["photo-evt_20260820T111500Z_005.jpg.txt"], rig.sent()
    assert not [name for name in rig.sent() if name.startswith("album-")]


def test_a_failed_group_does_not_advance_the_marker(rig):
    """State mutates only after a successful send — the discipline the whole script is built on."""
    event = "evt_20260903T118000Z"
    rig.add_event(event, "motion", frames=9, prebuffer=5)
    (rig.tmp / "fail-send").write_text("x")
    rig.run()
    assert not rig.marker(event).exists(), "a refused group is a retry, not a delivered frame"
    (rig.tmp / "fail-send").unlink()
    rig.run()
    assert rig.marker(event).read_text().split()[1] == "8"


def test_the_hourly_cap_counts_visits_not_photographs(rig):
    """One long visit must not exhaust a cap that is meant to limit ALERTS."""
    rig.env["MOTION_ALERTS_PER_HOUR"] = "1"
    event = "evt_20260903T119000Z"
    rig.add_event(event, "motion", frames=8, prebuffer=5)
    rig.run()
    assert rig.deliveries(), "the first visit is under the cap"
    rig.listing.write_text("")
    rig.add_event(event, "motion", frames=14, prebuffer=5)
    rig.run()
    body = "".join(rig.body(name) for name in rig.deliveries())
    assert "_013.jpg" in body, "the same visit continuing is not a second alert"


def test_the_key_frame_leads_the_album(rig):
    """The key frame leads, then the newest frames. `_000` is the room before anything happened."""
    rig.env["STREAM"] = "0"  # the lead-and-album shape, which the stream replaced
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

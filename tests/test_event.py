"""S2.3: events — pre-buffer, throttling, truncation, manifest (spec 3.2, 7)."""

import json

import numpy as np
import pytest

from camtrap.detector import EventKind
from camtrap.event import EventWriter


def _frame(value=60):
    return np.full((72, 128, 3), value, dtype=np.uint8)


@pytest.fixture
def writer(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    return EventWriter(cfg)


def test_prebuffer_keeps_the_frames_before_the_trigger(writer):
    for index in range(10):
        writer.observe(_frame(index * 10), now=float(index))  # 1 fps into the ring
    event = writer.begin(EventKind.MOTION, now=10.0, frame=_frame(200))
    assert event.frames_written == writer.cfg.event.prebuffer_frames + 1
    names = sorted(p.name for p in writer.cfg.spool_dir.glob("*.jpg"))
    assert len(names) == 6  # 5 pre-buffer plus the trigger frame


def test_throttling_holds_the_cadence_over_a_minute(writer):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    written_at_start = event.frames_written
    # time from the index, not by accumulation: 300 additions of 0.2 drift below 60.0 and
    # silently drop the last throttle slot
    for index in range(1, 301):  # 60 s at 5 fps
        writer.feed(_frame(), now=index * 0.2)
    added = event.frames_written - written_at_start
    interval = writer.cfg.event.snapshot_interval_sec
    assert added == int(60.0 / interval), f"expected one frame per {interval:g} s, got {added}"


def test_event_closes_after_the_gap(writer):
    writer.observe(_frame(), now=0.0)
    writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    assert writer.active is not None
    writer.feed(_frame(), now=10.0)
    closed = writer.maybe_close(now=45.0)
    assert closed is not None
    assert writer.active is None


def test_motion_extends_the_event_rather_than_starting_a_new_one(writer):
    writer.observe(_frame(), now=0.0)
    first = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    writer.note_motion(now=20.0)
    assert writer.maybe_close(now=40.0) is None  # gap measured from the last motion
    second = writer.begin(EventKind.MOTION, now=41.0, frame=_frame())
    assert second is first


def test_truncation_is_recorded_not_silent(cfg):
    cfg.event.max_frames_per_event = 8
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    now = 0.0
    for _ in range(200):
        now += 5.1
        writer.feed(_frame(), now=now)
    event = writer.maybe_close(now=now + 60.0)
    assert event.truncated
    assert event.frames_written == 8
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["truncated"] is True
    assert manifest["frames"] == 8


def test_manifest_records_the_shape_of_the_event(writer):
    writer.observe(_frame(), now=0.0)
    writer.begin(EventKind.TAMPER, now=1.0, frame=_frame(), signals=["ac_offline", "lid_closed"])
    writer.feed(_frame(), now=6.5)
    writer.mark_sound(stage="siren", latency_ms=820, evidence_confirmed=True)
    closed = writer.maybe_close(now=60.0)
    manifest = json.loads((writer.cfg.spool_dir / f"{closed.event_id}.json").read_text())
    assert manifest["type"] == "tamper"
    assert manifest["signals"] == ["ac_offline", "lid_closed"]
    assert manifest["sound_played"] is True
    assert manifest["sound_stage"] == "siren"
    assert manifest["sound_latency_ms"] == 820
    assert manifest["sound_evidence_confirmed"] is True
    assert manifest["frames"] == closed.frames_written
    assert manifest["agent_version"]
    assert manifest["started"] and manifest["ended"]


def test_light_event_is_a_single_frame(writer):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.LIGHT, now=1.0, frame=_frame(220))
    writer.feed(_frame(), now=6.5)
    assert event.frames_written == 1, "light is one frame: no pre-buffer, no series"
    closed = writer.maybe_close(now=2.0)
    assert closed is not None and closed.kind is EventKind.LIGHT


def test_first_frame_is_named_so_priority_is_visible(writer):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.TAMPER, now=1.0, frame=_frame())
    first = sorted(writer.cfg.spool_dir.glob(f"{event.event_id}_*.jpg"))[0]
    assert first.name.endswith("_000.jpg")


def test_two_events_in_the_same_second_get_distinct_ids(cfg):
    """Second-resolution ids would otherwise collide and overwrite each other's frames."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    frozen = 1787000000.0
    writer = EventWriter(cfg, wall_clock=lambda: frozen)
    writer.observe(_frame(), now=0.0)
    first = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    writer.close(now=1.0)
    second = writer.begin(EventKind.MOTION, now=2.0, frame=_frame())
    assert first.event_id != second.event_id
    writer.close(now=3.0)
    manifests = sorted(p.name for p in cfg.spool_dir.glob("evt_*.json"))
    assert len(manifests) == 2


def test_an_id_already_on_disk_is_not_reused(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    frozen = 1787000000.0
    stale = cfg.spool_dir / "evt_20260813T221320Z.json"
    stale.write_text("{}")
    writer = EventWriter(cfg, wall_clock=lambda: frozen)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    assert event.event_id != "evt_20260813T221320Z"


def test_manifest_exists_from_the_moment_the_event_starts(writer, cfg):
    """A tamper event whose agent dies mid-run must still arrive typed, not as plain motion."""
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.TAMPER, now=1.0, frame=_frame(), signals=["ac_offline"])
    manifest_path = cfg.spool_dir / f"{event.event_id}.json"
    assert manifest_path.exists(), "no manifest until close = a lost tamper classification"
    manifest = json.loads(manifest_path.read_text())
    assert manifest["type"] == "tamper"
    assert manifest["signals"] == ["ac_offline"]
    assert manifest["closed"] is False


def test_closing_marks_the_manifest_closed(writer, cfg):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    closed = writer.close(now=40.0)
    manifest = json.loads((cfg.spool_dir / f"{closed.event_id}.json").read_text())
    assert manifest["closed"] is True
    assert manifest["id"] == event.event_id


def test_escalation_to_tamper_is_persisted_immediately(writer, cfg):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    writer.begin(EventKind.TAMPER, now=5.0, signals=["lid_closed"])
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["type"] == "tamper"
    assert "lid_closed" in manifest["signals"]
    assert manifest["closed"] is False


def test_sound_is_recorded_in_the_manifest_without_waiting_for_close(writer, cfg):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.TAMPER, now=0.0, frame=_frame(), signals=["ac_offline"])
    writer.mark_sound(stage="siren", latency_ms=900, evidence_confirmed=False)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["sound_stage"] == "siren"
    assert manifest["sound_played"] is True


def test_the_shipped_cadence_is_one_frame_at_once_then_one_every_interval(cfg):
    """The owner's instruction after the first hotel run, as a test — 5 s, then 10 s, now 30 s.

    The boost is what this replaces. With it on, a person in frame produced a frame a second and
    the six-photo album that reached Telegram was six views of the same moment. The timings come
    from the knob: the cadence has been raised twice, and each time this test was the thing that
    had to be re-read to find out whether it still said anything.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame(), changed_pct=11.0)
    at_once = event.frames_written  # the pre-buffer plus the trigger frame, taken immediately
    assert at_once > 0

    # A change far above the trigger threshold no longer buys extra frames.
    interval = cfg.event.snapshot_interval_sec
    assert writer.feed(_frame(), now=1.0, changed_pct=40.0) is False
    assert writer.feed(_frame(), now=interval - 0.1, changed_pct=40.0) is False
    assert writer.feed(_frame(), now=interval, changed_pct=40.0) is True
    assert writer.feed(_frame(), now=interval * 2 - 1.0, changed_pct=40.0) is False
    assert writer.feed(_frame(), now=interval * 2, changed_pct=40.0) is True
    assert event.frames_written == at_once + 2
    assert event.boosted_frames == 0


def test_a_big_change_jumps_the_throttle(cfg):
    """A curtain-triggered event must not swallow the person crossing the room.

    With a plain 5 s throttle, someone visible for two seconds lands between snapshots: the event
    exists and documents the curtain.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    cfg.event.boost_area_pct = 4.0
    cfg.event.boost_min_interval_sec = 1.0
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    baseline = event.frames_written

    # small change 1 s in: throttled away
    assert writer.feed(_frame(), now=1.0, changed_pct=1.0) is False
    # big change 1.5 s in: taken immediately
    assert writer.feed(_frame(), now=1.5, changed_pct=9.0) is True
    assert event.frames_written == baseline + 1
    assert event.boosted_frames == 1


def test_the_boost_has_its_own_floor(cfg):
    """Otherwise a person in frame would produce five frames a second.

    The boost ships disabled, so this test turns it on: it documents what the knob does for
    whoever sets it, not what a default run does.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    cfg.event.boost_area_pct = 4.0
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    assert writer.feed(_frame(), now=1.2, changed_pct=20.0) is True
    assert writer.feed(_frame(), now=1.4, changed_pct=20.0) is False  # inside the 1 s floor
    assert writer.feed(_frame(), now=2.5, changed_pct=20.0) is True


def test_boosted_frames_are_recorded_in_the_manifest(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    cfg.event.boost_area_pct = 4.0
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    writer.feed(_frame(), now=2.0, changed_pct=15.0)
    writer.close(now=60.0)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["boosted_frames"] == 1


def test_the_manifest_says_where_the_pre_buffer_ends(cfg):
    """The poller streams the frames of a visit and must not stream the room before it.

    Without this the receiver-side rule is guesswork: `_000` is the oldest pre-buffer frame by
    construction, but how many follow it depends on `prebuffer_frames` and on how long the ring
    had been filling. The count is written where the poller can read it.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    for tick in range(6):
        writer.observe(_frame(50), now=float(tick))
    event = writer.begin(EventKind.MOTION, now=6.0, frame=_frame(90), changed_pct=9.0)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["prebuffer"] == cfg.event.prebuffer_frames
    assert manifest["frames"] == cfg.event.prebuffer_frames + 1, "the run-up, then the trigger"


def test_a_light_event_has_no_pre_buffer_to_skip(cfg):
    """One frame, and it is the event proper: before the switch there was nothing to see."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    for tick in range(6):
        writer.observe(_frame(10), now=float(tick))
    event = writer.begin(EventKind.LIGHT, now=6.0, frame=_frame(200), changed_pct=99.0)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["prebuffer"] == 0
    assert manifest["frames"] == 1


def test_the_manifest_names_the_most_changed_frame_not_the_oldest(cfg):
    """The first frame by number is the oldest pre-buffer frame: an empty room five seconds early.

    Sending that as "the photo" is why a person walking into the room appeared to go
    unphotographed — the capture was fine, the wrong frame was delivered.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    for tick in range(6):
        writer.observe(_frame(50), now=float(tick))  # quiet room into the ring
    interval = cfg.event.snapshot_interval_sec
    event = writer.begin(EventKind.MOTION, now=6.0, frame=_frame(60), changed_pct=1.2)
    # the person, one cadence slot later
    writer.feed(_frame(200), now=6.0 + interval, changed_pct=11.4)
    writer.feed(_frame(70), now=6.0 + interval * 2, changed_pct=2.0)
    writer.close(now=6.0 + interval * 3)

    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_changed_pct"] == 11.4
    assert manifest["key_frame"].endswith("_006.jpg"), manifest["key_frame"]
    assert manifest["key_frame"] != f"{event.event_id}_000.jpg"


def test_key_frame_defaults_to_the_trigger_when_nothing_beats_it(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=1.0, frame=_frame(220), changed_pct=8.0)
    writer.close(now=60.0)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_frame"].endswith("_001.jpg")  # index 0 is the single pre-buffer frame
    assert manifest["key_changed_pct"] == 8.0


def test_a_dark_frame_never_leads_however_much_of_it_changed(cfg):
    """The failure of 26 August: the alert led with a photograph of darkness.

    A light coming on in a dark room changes ~99 % of the pixels, so the transition frame won on
    raw change — and it is the one frame where the sensor has not caught up. Measured on that
    event: mean luma 14 on the frame that was sent, 119 two frames later.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(6), now=0.0)  # a dark room
    event = writer.begin(EventKind.MOTION, now=1.0, frame=_frame(14), changed_pct=99.2)
    # the light is on, the person is in
    writer.feed(_frame(119), now=1.0 + cfg.event.snapshot_interval_sec, changed_pct=30.0)
    writer.close(now=90.0)

    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_frame"].endswith("_002.jpg"), manifest["key_frame"]
    assert manifest["key_changed_pct"] == 30.0


def test_the_frames_that_open_an_event_do_not_lead_when_better_ones_follow(cfg):
    """A detector wakes up after motion has begun, so it opens on a back in a doorway.

    The frames are still written — this only decides which one leads the alert.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(120), now=0.0)
    event = writer.begin(EventKind.MOTION, now=1.0, frame=_frame(120), changed_pct=20.0)
    # The next cadence slot is at +10 s, which is already outside the 5 s settle window: on the
    # shipped numbers every frame after the trigger has settled by the time it is written.
    writer.feed(_frame(120), now=1.0 + cfg.event.snapshot_interval_sec, changed_pct=12.0)
    writer.close(now=60.0)

    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_frame"].endswith("_002.jpg"), manifest["key_frame"]
    assert manifest["frames"] == 3  # nothing was dropped, only re-ranked


def test_an_event_that_is_dark_throughout_still_names_its_best_frame(cfg):
    """A weight, not a veto: a night with the light off must still lead with something."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(6), now=0.0)
    event = writer.begin(EventKind.MOTION, now=1.0, frame=_frame(6), changed_pct=4.0)
    writer.feed(_frame(6), now=2.0 + cfg.event.snapshot_interval_sec, changed_pct=18.0)
    writer.close(now=90.0)

    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_frame"].endswith("_002.jpg"), manifest["key_frame"]


def test_a_tamper_event_with_no_change_leads_with_the_trigger_not_the_empty_room(cfg):
    """A cable pull carries no changed_pct, so nothing scored and index 0 won by default —
    the oldest pre-buffer frame, which is by construction the room before anyone was in it."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    for tick in range(5):
        writer.observe(_frame(100), now=float(tick))
    event = writer.begin(EventKind.TAMPER, now=5.0, frame=_frame(110), signals=["ac_offline"])
    writer.close(now=60.0)

    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["key_frame"].endswith("_005.jpg"), manifest["key_frame"]

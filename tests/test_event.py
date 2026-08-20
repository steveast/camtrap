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


def test_throttling_gives_twelve_frames_per_minute(writer):
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    written_at_start = event.frames_written
    # time from the index, not by accumulation: 300 additions of 0.2 drift below 60.0 and
    # silently drop the last throttle slot
    for index in range(1, 301):  # 60 s at 5 fps
        writer.feed(_frame(), now=index * 0.2)
    added = event.frames_written - written_at_start
    assert added == 12, f"expected one frame per 5 s, got {added}"


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
    """Otherwise a person in frame would produce five frames a second."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    assert writer.feed(_frame(), now=1.2, changed_pct=20.0) is True
    assert writer.feed(_frame(), now=1.4, changed_pct=20.0) is False  # inside the 1 s floor
    assert writer.feed(_frame(), now=2.5, changed_pct=20.0) is True


def test_boosted_frames_are_recorded_in_the_manifest(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    writer.observe(_frame(), now=0.0)
    event = writer.begin(EventKind.MOTION, now=0.0, frame=_frame())
    writer.feed(_frame(), now=2.0, changed_pct=15.0)
    writer.close(now=60.0)
    manifest = json.loads((cfg.spool_dir / f"{event.event_id}.json").read_text())
    assert manifest["boosted_frames"] == 1


def test_the_manifest_names_the_most_changed_frame_not_the_oldest(cfg):
    """The first frame by number is the oldest pre-buffer frame: an empty room five seconds early.

    Sending that as "the photo" is why a person walking into the room appeared to go
    unphotographed — the capture was fine, the wrong frame was delivered.
    """
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    writer = EventWriter(cfg)
    for tick in range(6):
        writer.observe(_frame(50), now=float(tick))  # quiet room into the ring
    event = writer.begin(EventKind.MOTION, now=6.0, frame=_frame(60), changed_pct=1.2)
    writer.feed(_frame(200), now=12.0, changed_pct=11.4)  # the person
    writer.feed(_frame(70), now=18.0, changed_pct=2.0)
    writer.close(now=60.0)

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

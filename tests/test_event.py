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

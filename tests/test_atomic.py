"""Nothing the agent writes may be readable half-finished.

The expected end of a run is the power being pulled mid-write. What must not happen is a torn file
that still looks like evidence: a truncated JPEG that gets uploaded, or a manifest missing the
field that says this was tamper rather than a cleaner walking past.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from camtrap.atomic import PART_SUFFIX, write_atomic
from camtrap.detector import EventKind
from camtrap.event import EventWriter
from camtrap.spool import Spool


def test_a_failed_write_leaves_the_previous_file_untouched(tmp_path):
    target = tmp_path / "manifest.json"
    target.write_text('{"type": "tamper"}')
    # A directory where the temporary file belongs: the write cannot complete.
    (tmp_path / ("manifest.json" + PART_SUFFIX)).mkdir()

    assert write_atomic(target, '{"type": "motion"}') is False
    assert json.loads(target.read_text())["type"] == "tamper", "the old truth must survive"


def test_the_spool_never_offers_a_file_that_is_still_being_written(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    (cfg.spool_dir / "evt_1_000.jpg").write_bytes(b"done")
    (cfg.spool_dir / ("evt_1_001.jpg" + PART_SUFFIX)).write_bytes(b"half a frame")

    spool = Spool(cfg)
    names = [path.name for path in spool.pending()]
    assert names == ["evt_1_000.jpg"]
    assert spool.depth() == 1
    assert spool.total_bytes() == 4, "work in progress must not count against the cap either"


@pytest.mark.parametrize("quality", [95, 40])
def test_frames_appear_whole_or_not_at_all(cfg, quality):
    """Written through a sibling .part, so the name only ever points at a complete JPEG."""
    import cv2

    cfg.event.jpeg_quality = quality
    writer = EventWriter(cfg)
    frame = np.random.default_rng(7).integers(0, 255, (240, 320, 3), dtype=np.uint8)
    writer.begin(EventKind.TAMPER, now=1.0, frame=frame, signals=["ac_offline"])

    frames = sorted(cfg.spool_dir.glob("evt_*.jpg"))
    assert frames, "the tamper frame must be on disk"
    assert not list(cfg.spool_dir.glob(f"*{PART_SUFFIX}")), "no leftovers"
    for path in frames:
        decoded = cv2.imread(str(path))
        assert decoded is not None and decoded.shape == frame.shape, f"{path.name} is not a JPEG"


def test_the_manifest_survives_a_write_that_cannot_finish(cfg):
    """The event type is the one field an alert cannot do without; a torn manifest loses it."""
    writer = EventWriter(cfg)
    frame = np.zeros((240, 320, 3), dtype=np.uint8)
    event = writer.begin(EventKind.TAMPER, now=1.0, frame=frame, signals=["lid_closed"])
    manifest = cfg.spool_dir / f"{event.event_id}.json"
    assert json.loads(manifest.read_text())["type"] == "tamper"

    # Block the rename, then provoke another manifest write.
    (cfg.spool_dir / (manifest.name + PART_SUFFIX)).mkdir()
    writer.mark_sound(stage="siren", latency_ms=120, evidence_confirmed=True)

    payload = json.loads(manifest.read_text())
    assert payload["type"] == "tamper", "a blocked update must not destroy what was there"
    assert payload["signals"] == ["lid_closed"]

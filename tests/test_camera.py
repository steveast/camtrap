"""S2.1: capture, decimation by clock, and reconnection (spec 3.1).

Decimation is deliberately time-based. Counting frames assumes the camera delivers capture_fps,
and this one does not: it advertises 30 fps for MJPEG and measured 12.5 fps in evening light,
because UVC cameras lengthen exposure as it gets dark. Counting six frames per analysis then
halved the effective rate and left every analysed frame stale — the "huge delay".
"""

import numpy as np
import pytest

from camtrap.camera import Camera


class FakeClock:
    """Time advances only when the fake camera spends it."""

    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def spend(self, seconds):
        self.now += seconds

    #: Sleeping spends time too. A fake sleep that leaves the clock still is the test lying, and
    #: it hides real waits — reopen pacing is measured against this clock.
    sleep = spend


class FakeCapture:
    """Mirrors cv2.VideoCapture: grab() pulls a frame, read() also decodes it."""

    def __init__(self, clock, frames, *, frame_interval=0.08, fail_after=None):
        self.clock = clock
        self._frames = frames
        self._interval = frame_interval
        self._index = 0
        self._fail_after = fail_after
        self.released = False
        self.props = {}
        self.grabbed = 0
        self.decoded = 0

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def grab(self):
        if self._fail_after is not None and self._index >= self._fail_after:
            return False
        self._index += 1
        self.grabbed += 1
        self.clock.spend(self._interval)
        return True

    def read(self):
        if self._fail_after is not None and self._index >= self._fail_after:
            return False, None
        frame = self._frames[self._index % len(self._frames)]
        self._index += 1
        self.decoded += 1
        self.clock.spend(self._interval)
        return True, frame

    def release(self):
        self.released = True


@pytest.fixture
def frames():
    return [np.full((72, 128, 3), value, dtype=np.uint8) for value in (10, 20, 30)]


def test_a_fast_camera_is_decimated_to_the_target_rate(cfg, frames):
    """30 fps in, 5 fps analysed."""
    clock = FakeClock()
    cfg.camera.target_fps = 5
    capture = FakeCapture(clock, frames, frame_interval=1 / 30)
    camera = Camera(cfg, opener=lambda device: capture, clock=clock)
    produced = list(camera.frames(limit=10))
    assert len(produced) == 10
    elapsed = clock.now
    rate = len(produced) / elapsed
    assert 4.0 <= rate <= 6.0, f"expected ~5 fps, got {rate:.1f}"


def test_skipped_frames_are_grabbed_not_decoded(cfg, frames):
    """Decoding what we throw away is what backs up the driver queue."""
    clock = FakeClock()
    cfg.camera.target_fps = 5
    capture = FakeCapture(clock, frames, frame_interval=1 / 30)
    camera = Camera(cfg, opener=lambda device: capture, clock=clock)
    list(camera.frames(limit=6))
    assert capture.decoded == 6
    assert capture.grabbed > capture.decoded, "the rest must leave the queue undecoded"


def test_a_camera_at_twelve_fps_still_feeds_the_target(cfg, frames):
    """The real measurement from this device in evening light: 12.5 fps, target 5."""
    clock = FakeClock()
    cfg.camera.target_fps = 5
    capture = FakeCapture(clock, frames, frame_interval=0.08)  # 12.5 fps
    camera = Camera(cfg, opener=lambda device: capture, clock=clock)
    produced = list(camera.frames(limit=10))
    rate = len(produced) / clock.now
    assert rate >= 4.0, f"expected at least the target rate, got {rate:.1f}"


def test_a_camera_slower_than_the_target_loses_nothing(cfg, frames):
    """At 3 fps against a 5 fps target there is nothing to throw away — take every frame."""
    clock = FakeClock()
    cfg.camera.target_fps = 5
    capture = FakeCapture(clock, frames, frame_interval=1 / 3)
    camera = Camera(cfg, opener=lambda device: capture, clock=clock)
    produced = list(camera.frames(limit=8))
    assert len(produced) == 8
    # the first couple of frames use the configured guess; after that the stride must collapse to 1
    assert capture.grabbed <= 6, f"a slow camera must not have frames skipped ({capture.grabbed})"


def test_measured_fps_reports_the_real_rate(cfg, frames):
    clock = FakeClock()
    cfg.camera.target_fps = 5
    camera = Camera(
        cfg, opener=lambda d: FakeCapture(clock, frames, frame_interval=1 / 30), clock=clock
    )
    list(camera.frames(limit=8))
    measured = camera.measured_fps()
    assert measured is not None
    assert 4.0 <= measured <= 6.0


def test_a_dropped_device_is_reopened(cfg, frames):
    clock = FakeClock()
    captures = [FakeCapture(clock, frames, fail_after=2), FakeCapture(clock, frames)]
    opened = []

    def opener(device):
        capture = captures[min(len(opened), len(captures) - 1)]
        opened.append(capture)
        return capture

    cfg.camera.target_fps = 30  # take everything, so failures show up immediately
    camera = Camera(cfg, opener=opener, clock=clock, sleep=clock.sleep)
    produced = list(camera.frames(limit=4))
    assert len(produced) == 4
    assert camera.status.reopens >= 1
    assert captures[0].released


def test_giving_up_marks_the_camera_gone(cfg, frames):
    clock = FakeClock()
    cfg.camera.max_reopen_attempts = 3
    cfg.camera.target_fps = 30
    camera = Camera(
        cfg,
        opener=lambda device: FakeCapture(clock, frames, fail_after=0),
        clock=clock,
        sleep=clock.sleep,
    )
    assert list(camera.frames(limit=2)) == []
    assert camera.status.gone


def test_the_driver_queue_is_kept_shallow(cfg):
    """A deep queue means the frame we decode is already old."""
    assert cfg.camera.buffer_frames == 1

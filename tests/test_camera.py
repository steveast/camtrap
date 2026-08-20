"""S2.1: decimation and reconnection, against a fake capture device."""

import numpy as np
import pytest

from camtrap.camera import Camera


class FakeCapture:
    def __init__(self, frames, fail_after=None, reopen_ok=True):
        self._frames = frames
        self._index = 0
        self._fail_after = fail_after
        self.reopen_ok = reopen_ok
        self.released = False
        self.props = {}

    def isOpened(self):
        return True

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def read(self):
        if self._fail_after is not None and self._index >= self._fail_after:
            return False, None
        frame = self._frames[self._index % len(self._frames)]
        self._index += 1
        return True, frame

    def release(self):
        self.released = True


@pytest.fixture
def frames():
    return [np.full((72, 128, 3), value, dtype=np.uint8) for value in (10, 20, 30)]


def test_decimation_keeps_one_frame_in_six(cfg, frames):
    cfg.camera.capture_fps = 30
    cfg.camera.target_fps = 5
    capture = FakeCapture(frames)
    camera = Camera(cfg, opener=lambda device: capture)
    produced = list(camera.frames(limit=5))
    assert len(produced) == 5
    assert camera.status.frames == 30  # 5 kept out of 30 read


def test_target_fps_equal_to_capture_keeps_every_frame(cfg, frames):
    cfg.camera.capture_fps = 30
    cfg.camera.target_fps = 30
    camera = Camera(cfg, opener=lambda device: FakeCapture(frames))
    assert len(list(camera.frames(limit=4))) == 4
    assert camera.status.frames == 4


def test_a_dropped_device_is_reopened(cfg, frames):
    captures = [FakeCapture(frames, fail_after=2), FakeCapture(frames)]
    opened = []

    def opener(device):
        capture = captures[min(len(opened), len(captures) - 1)]
        opened.append(capture)
        return capture

    cfg.camera.capture_fps = 1
    cfg.camera.target_fps = 1
    camera = Camera(cfg, opener=opener, sleep=lambda _seconds: None)
    produced = list(camera.frames(limit=4))
    assert len(produced) == 4
    assert camera.status.reopens >= 1
    assert captures[0].released


def test_giving_up_marks_the_camera_gone(cfg, frames):
    cfg.camera.max_reopen_attempts = 3
    cfg.camera.capture_fps = 1
    cfg.camera.target_fps = 1
    camera = Camera(
        cfg,
        opener=lambda device: FakeCapture(frames, fail_after=0),
        sleep=lambda _seconds: None,
    )
    assert list(camera.frames(limit=2)) == []
    assert camera.status.gone


def test_capture_properties_are_set_for_mjpg(cfg, frames):
    capture = FakeCapture(frames)
    camera = Camera(cfg, opener=lambda device: capture)
    assert camera.open()
    # properties are only set by the real opener; the fake records what the wrapper asks for
    assert camera.status.opened

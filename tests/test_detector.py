"""S2.2: the detector, against synthetic frames only — no camera in CI (spec 3.1, 3.3, 7).

The interesting cases are the ones that must NOT fire: sensor noise, and a light switch. Both
change many pixels; neither is a person walking in.
"""

import numpy as np
import pytest

from camtrap.detector import Detector, EventKind


def _frame(value=40, size=(360, 640)):
    return np.full((*size, 3), value, dtype=np.uint8)


def _noisy(rng, value=40, size=(360, 640), amplitude=3):
    base = np.full((*size, 3), value, dtype=np.int16)
    base += rng.integers(-amplitude, amplitude + 1, size=(*size, 3), dtype=np.int16)
    return np.clip(base, 0, 255).astype(np.uint8)


@pytest.fixture
def detector(cfg):
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 2
    return Detector(cfg)


def _settle(detector, frames=30, now=0.0, rng=None, step=0.2):
    """Let MOG2 learn the background."""
    for index in range(frames):
        frame = _noisy(rng) if rng is not None else _frame()
        detector.submit(frame, now=now + index * step)


def test_static_background_never_fires(detector):
    _settle(detector)
    for index in range(20):
        result = detector.submit(_frame(), now=10.0 + index * 0.2)
        assert result.kind is EventKind.NONE


def test_sensor_noise_never_fires(detector):
    rng = np.random.default_rng(7)
    _settle(detector, frames=40, rng=rng)
    for index in range(40):
        result = detector.submit(_noisy(rng), now=20.0 + index * 0.2)
        assert result.kind is EventKind.NONE, f"noise fired at frame {index}"


def test_moving_rectangle_fires_motion(detector):
    _settle(detector)
    kinds = []
    for index in range(6):
        frame = _frame()
        x = 100 + index * 25
        frame[120:260, x : x + 130] = 210
        kinds.append(detector.submit(frame, now=10.0 + index * 0.2).kind)
    assert EventKind.MOTION in kinds


def test_motion_needs_min_motion_frames_in_a_row(cfg):
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 4
    detector = Detector(cfg)
    _settle(detector)
    frame = _frame()
    frame[100:300, 100:400] = 220
    kinds = [detector.submit(frame, now=10.0 + i * 0.2).kind for i in range(3)]
    assert EventKind.MOTION not in kinds  # three frames is not enough
    assert detector.submit(frame, now=11.0).kind is EventKind.MOTION


def test_brightness_step_is_light_not_motion(detector):
    _settle(detector)
    result = detector.submit(_frame(200), now=10.0)
    assert result.kind is EventKind.LIGHT
    assert result.changed_pct > 70.0


def test_light_is_reported_once_not_as_a_series(detector):
    _settle(detector)
    first = detector.submit(_frame(200), now=10.0)
    assert first.kind is EventKind.LIGHT
    second = detector.submit(_frame(200), now=10.2)
    assert second.kind is not EventKind.LIGHT


def test_ignore_mask_excludes_a_region(cfg):
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 2
    # mask out the right half, in analysis coordinates
    cfg.detector.ignore_mask = [[[320, 0], [640, 0], [640, 360], [320, 360]]]
    detector = Detector(cfg)
    _settle(detector)
    kinds = []
    for index in range(6):
        frame = _frame()
        frame[100:300, 400:600] = 220  # entirely inside the masked half
        kinds.append(detector.submit(frame, now=10.0 + index * 0.2).kind)
    assert EventKind.MOTION not in kinds


def test_mask_still_sees_motion_outside_it(cfg):
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 2
    cfg.detector.ignore_mask = [[[320, 0], [640, 0], [640, 360], [320, 360]]]
    detector = Detector(cfg)
    _settle(detector)
    kinds = []
    for index in range(6):
        frame = _frame()
        frame[100:300, 20:260] = 220
        kinds.append(detector.submit(frame, now=10.0 + index * 0.2).kind)
    assert EventKind.MOTION in kinds


def test_nothing_fires_during_warmup(cfg):
    cfg.detector.warmup_sec = 20.0
    cfg.detector.min_motion_frames = 1
    detector = Detector(cfg)
    for index in range(40):
        frame = _frame()
        frame[100:300, 100 + index : 300 + index] = 220
        result = detector.submit(frame, now=index * 0.2)  # up to t=7.8 s
        assert result.kind is EventKind.NONE
    assert detector.warming_up(now=19.0)
    assert not detector.warming_up(now=21.0)


def test_calibrate_returns_noise_statistics(detector):
    rng = np.random.default_rng(3)
    stats = detector.calibrate([_noisy(rng) for _ in range(20)])
    assert stats.frames == 20
    assert stats.p99 >= stats.mean
    assert stats.recommended_min_area_pct > stats.p99


def test_textureless_frames_never_become_tamper(detector):
    """A dark room or a wall in the lens has nothing to correlate: refuse to guess.

    Guessing here would put a siren in an empty room at 3am, which is the one failure this
    project cannot afford.
    """
    _settle(detector)
    result = detector.submit(_frame(5), now=10.0)
    assert result.kind is EventKind.LIGHT
    assert result.textureless


def test_a_shifted_textured_scene_is_tamper(detector):
    rng = np.random.default_rng(11)
    texture = rng.integers(0, 255, size=(360, 640), dtype=np.uint8)
    frame = np.dstack([texture] * 3)
    for index in range(30):
        detector.submit(frame, now=index * 0.2)
    shifted = np.roll(frame, 40, axis=1)
    result = detector.submit(shifted, now=10.0)
    assert result.kind is EventKind.TAMPER
    assert result.shift_px >= detector.cfg.detector.move_shift_px


def test_a_brightened_textured_scene_is_light(detector):
    rng = np.random.default_rng(11)
    texture = rng.integers(0, 120, size=(360, 640), dtype=np.uint8)
    frame = np.dstack([texture] * 3)
    for index in range(30):
        detector.submit(frame, now=index * 0.2)
    brighter = np.clip(frame.astype(np.int16) + 110, 0, 255).astype(np.uint8)
    result = detector.submit(brighter, now=10.0)
    assert result.kind is EventKind.LIGHT
    assert result.shift_px < detector.cfg.detector.move_shift_px

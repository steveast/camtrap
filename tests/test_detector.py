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


def test_one_loud_frame_is_enough(cfg):
    """A person close to the lens is unmistakable in a single frame; waiting only costs time.

    Measured live in the owner's room: the curtain in the wind peaked at 1.62 % of the frame, a
    person produced 3.05-22.07 %. `instant_area_pct` sits between the two, so nothing that has
    ever moved on its own in that room can reach it.
    """
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 4  # deliberately demanding; the loud frame outranks it
    detector = Detector(cfg)
    _settle(detector)
    frame = _frame()
    frame[100:300, 100:400] = 220  # ~26 % of the frame: far above any curtain
    result = detector.submit(frame, now=10.0)
    assert result.kind is EventKind.MOTION
    assert result.detail == "instant"
    assert result.changed_pct >= cfg.detector.instant_area_pct


def test_a_spiky_signal_is_still_motion(cfg):
    """The bug the owner reported as "it reacts, but very late".

    Real captures show a person's mask flickering — 11.3 % on one frame, 0.0 % on the next as they
    pass behind furniture. The old rule needed frames above the threshold to be CONSECUTIVE, so it
    threw the spike away and waited for the next burst. Confirmation is now counted over a window.
    """
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 2
    cfg.detector.instant_area_pct = 0.0  # disabled, so only the window is under test
    detector = Detector(cfg)
    _settle(detector)

    quiet, moving = _frame(), _frame()
    moving[150:200, 150:280] = 210  # measures ~3.9 %: above the area threshold, below "loud"
    kinds = []
    for index, frame in enumerate([moving, quiet, quiet, moving]):
        kinds.append(detector.submit(frame, now=10.0 + index * 0.15).kind)
    assert EventKind.MOTION in kinds, (
        "two above-threshold frames within the window are motion, adjacent or not"
    )


def test_a_curtain_sized_change_never_fires(cfg):
    """1.6 % was the loudest the curtain ever measured. It must stay silent however long it goes on."""
    cfg.detector.warmup_sec = 0.0
    detector = Detector(cfg)
    _settle(detector)
    for index in range(30):
        frame = _frame()
        # Sized to measure ~1.8 % after dilation and blur, which is above the loudest curtain
        # ever recorded live here (1.62 %) — so this is the pessimistic version of that curtain.
        x = 400 + (index % 3) * 4
        frame[40:95, x : x + 45] = 200
        assert detector.submit(frame, now=10.0 + index * 0.15).kind is EventKind.NONE


def test_a_single_above_threshold_frame_is_not_enough(cfg):
    """The window forgives gaps; it does not accept one quiet frame as proof."""
    cfg.detector.warmup_sec = 0.0
    cfg.detector.min_motion_frames = 2
    cfg.detector.instant_area_pct = 0.0
    detector = Detector(cfg)
    _settle(detector)
    frame = _frame()
    frame[150:200, 150:280] = 210  # ~3.9 %, one frame only
    assert detector.submit(frame, now=10.0).kind is EventKind.NONE


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


def test_activity_map_finds_a_region_that_moves_by_itself(cfg):
    """The curtain case: a fixed strip that never settles must come back as a box to mask.

    Not built on the MOG2 foreground on purpose — a background model adapts to a curtain swaying
    all afternoon and then reports nothing, which is exactly the region worth masking.
    """
    cfg.detector.warmup_sec = 0.0
    detector = Detector(cfg)
    frames = []
    for index in range(60):
        frame = _frame(60)
        # A curtain moves as a body: one strip whose brightness swings. Random noise would be
        # flattened by the blur and is not what a curtain looks like.
        frame[40:300, 90:200] = 90 if index % 2 else 210
        frames.append(frame)

    activity = detector.activity_map(frames)
    assert activity.frames > 40
    assert activity.boxes, "a region moving in every frame must be proposed"
    (x0, y0), (x1, _), _, _ = activity.boxes[0]
    assert 75 <= x0 <= 95, activity.boxes[0]
    assert 195 <= x1 <= 215, activity.boxes[0]
    assert y0 <= 40
    assert activity.hot_pct > 5.0


def test_activity_map_proposes_nothing_for_a_still_room(cfg):
    cfg.detector.warmup_sec = 0.0
    detector = Detector(cfg)
    rng = np.random.default_rng(9)
    frames = [_noisy(rng) for _ in range(60)]
    activity = detector.activity_map(frames)
    assert activity.boxes == []


def test_tracing_records_what_the_detector_saw(cfg, capsys):
    """`--trace` is the instrument for "why did it not react": the per-frame number, or guesswork.

    The `log_ticks` config knob existed from S0 and nothing ever read it, so there was no way to
    see the changed_pct of a frame that did NOT fire — which is exactly the frame in question.
    """
    from camtrap.runner import Runner

    cfg.log_ticks = True
    cfg.detector.warmup_sec = 0.0
    runner = Runner(cfg, clock=lambda: 0.0)
    for index in range(3):
        runner.on_frame(_frame(), now=index * 0.15)
    lines = [line for line in capsys.readouterr().out.splitlines() if line.startswith("frame ")]
    assert len(lines) == 3, "one line per analysed frame"
    assert "pct=" in lines[0] and "above=" in lines[0]

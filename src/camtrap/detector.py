"""Motion detection, and telling a light switch apart from a lifted laptop.

Spec 3.1 and 3.3. MOG2 rather than a frame difference, because a difference sees only the moment
of movement and goes blind when someone walks in and *stands still* — which is exactly the frame
worth having, a face rather than a blur.

Anything that changes almost the whole frame is not motion: it is the light being switched, or the
case being lifted. `phaseCorrelate` separates them — a lighting change keeps the scene geometry, so
the correlation peak stays at zero; a lifted case shifts it. A smeared, unrecognisable frame counts
as movement too, because light does not smear.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from enum import Enum

import cv2
import numpy as np

from .config import Config


class EventKind(Enum):
    NONE = "none"
    MOTION = "motion"
    LIGHT = "light"
    TAMPER = "tamper"


@dataclass
class Detection:
    kind: EventKind
    changed_pct: float = 0.0
    shift_px: float = 0.0
    response: float = 0.0
    detail: str = ""
    textureless: bool = False


@dataclass
class ActivityMap:
    """Where an empty room moves by itself, and the boxes that would cover it."""

    frames: int
    hot_pct: float
    boxes: list[list[list[int]]]
    heat_path: str = ""


@dataclass
class NoiseStats:
    frames: int
    mean: float
    p99: float
    recommended_min_area_pct: float


class Detector:
    """Stateful: keeps the background model, the confirmation window and the previous grey frame."""

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._bg = cv2.createBackgroundSubtractorMOG2(
            history=cfg.detector.mog2_history,
            varThreshold=cfg.detector.mog2_var_threshold,
            detectShadows=False,
        )
        #: Whether each of the last motion_window_frames frames was above the area threshold.
        #: A window rather than a run, because a person's mask flickers and a run loses the spike.
        self._recent: deque[bool] = deque(maxlen=max(1, cfg.detector.motion_window_frames))
        self._started: float | None = None
        self._mask: np.ndarray | None = None
        self._prev_grey: np.ndarray | None = None
        self._global_active = False
        self._seen = 0

    # --- helpers -------------------------------------------------------------

    def warming_up(self, *, now: float) -> bool:
        if self._started is None:
            return True
        return now - self._started < self.cfg.detector.warmup_sec

    def _prepare(self, frame: np.ndarray) -> np.ndarray:
        width = self.cfg.detector.analysis_width
        if frame.shape[1] != width:
            height = int(frame.shape[0] * width / frame.shape[1])
            frame = cv2.resize(frame, (width, height), interpolation=cv2.INTER_AREA)
        grey = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        kernel = self.cfg.detector.blur_kernel | 1  # OpenCV wants an odd kernel
        return cv2.GaussianBlur(grey, (kernel, kernel), 0)

    def _ignore_mask(self, shape: tuple[int, int]) -> np.ndarray | None:
        polygons = self.cfg.detector.ignore_mask
        if not polygons:
            return None
        if self._mask is not None and self._mask.shape == shape:
            return self._mask
        mask = np.full(shape, 255, dtype=np.uint8)
        for polygon in polygons:
            points = np.array(polygon, dtype=np.int32)
            cv2.fillPoly(mask, [points], 0)
        self._mask = mask
        return mask

    # --- the pipeline --------------------------------------------------------

    def submit(self, frame: np.ndarray, *, now: float) -> Detection:
        if self._started is None:
            self._started = now
        grey = self._prepare(frame)
        mask = self._ignore_mask(grey.shape)

        foreground = self._bg.apply(grey)
        _, foreground = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
        foreground = cv2.morphologyEx(
            foreground, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
        )
        foreground = cv2.dilate(foreground, None, iterations=2)
        if mask is not None:
            foreground = cv2.bitwise_and(foreground, mask)
            considered = int(np.count_nonzero(mask))
        else:
            considered = foreground.size

        changed = int(np.count_nonzero(foreground))
        changed_pct = 100.0 * changed / max(1, considered)

        prev_grey, self._prev_grey = self._prev_grey, grey
        self._seen += 1

        if self._seen <= self.cfg.detector.min_model_frames:
            self._recent.clear()
            return Detection(EventKind.NONE, changed_pct=changed_pct, detail="model_priming")

        if self.warming_up(now=now):
            self._recent.clear()
            return Detection(EventKind.NONE, changed_pct=changed_pct, detail="warmup")

        if changed_pct >= self.cfg.detector.shift_check_pct:
            if self._global_active:
                # Already reported this upheaval; do not turn it into a series.
                return Detection(EventKind.NONE, changed_pct=changed_pct, detail="global_repeat")
            verdict = self._classify_global(grey, prev_grey, changed_pct)
            if verdict.kind is EventKind.TAMPER or (
                changed_pct >= self.cfg.detector.global_change_pct
            ):
                self._global_active = True
                self._recent.clear()
                return verdict
            # A large but geometry-preserving change below the light threshold: treat it as
            # ordinary motion (someone close to the lens) rather than swallowing it.

        self._global_active = False

        instant = self.cfg.detector.instant_area_pct
        if instant and changed_pct >= instant:
            # No curtain has ever reached this here. Confirming it would only cost time.
            self._recent.clear()
            return Detection(EventKind.MOTION, changed_pct=changed_pct, detail="instant")

        self._recent.append(changed_pct >= self.cfg.detector.min_area_pct)
        if sum(self._recent) >= self.cfg.detector.min_motion_frames:
            self._recent.clear()
            return Detection(EventKind.MOTION, changed_pct=changed_pct)
        return Detection(EventKind.NONE, changed_pct=changed_pct)

    def _classify_global(
        self, grey: np.ndarray, prev_grey: np.ndarray | None, changed_pct: float
    ) -> Detection:
        """Light switch or lifted case? (spec 3.3)"""
        if prev_grey is None:
            return Detection(EventKind.LIGHT, changed_pct=changed_pct, detail="no_previous_frame")

        floor = self.cfg.detector.min_texture_std
        if float(prev_grey.std()) < floor or float(grey.std()) < floor:
            # No structure to correlate: cannot tell a shift from a light switch. Report the
            # quiet one. A missed lift is recoverable; a siren in an empty dark room is not.
            return Detection(
                EventKind.LIGHT,
                changed_pct=changed_pct,
                detail="no texture to correlate",
                textureless=True,
            )

        shift, response = self.scene_shift(prev_grey, grey)
        if shift >= self.cfg.detector.move_shift_px:
            return Detection(
                EventKind.TAMPER,
                changed_pct=changed_pct,
                shift_px=shift,
                response=response,
                detail="scene shifted",
            )
        if response < self.cfg.detector.move_response_min:
            return Detection(
                EventKind.TAMPER,
                changed_pct=changed_pct,
                shift_px=shift,
                response=response,
                detail="scene unrecognisable",
            )
        return Detection(
            EventKind.LIGHT,
            changed_pct=changed_pct,
            shift_px=shift,
            response=response,
            detail="geometry preserved",
        )

    @staticmethod
    def scene_shift(previous: np.ndarray, current: np.ndarray) -> tuple[float, float]:
        """Global translation between two frames, plus the confidence of the correlation peak."""
        a = previous.astype(np.float32)
        b = current.astype(np.float32)
        # Normalise away brightness so a light switch does not read as a shift.
        a = (a - a.mean()) / max(1e-6, a.std())
        b = (b - b.mean()) / max(1e-6, b.std())
        window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
        (dx, dy), response = cv2.phaseCorrelate(a, b, window)
        return float((dx**2 + dy**2) ** 0.5), float(response)

    def activity_map(self, frames: list[np.ndarray], *, min_box_pct: float = 0.3) -> ActivityMap:
        """Accumulate where the picture is unstable, then propose ignore polygons.

        Deliberately NOT the MOG2 foreground: the background model adapts to anything that moves
        continuously, so a curtain swaying all afternoon slowly becomes "background" and the map
        comes back empty — which is exactly the region worth masking. Per-pixel variance over time
        does not adapt away, and it answers the actual question: where does this frame never
        settle.

        Variance is accumulated with Welford, so two minutes at 5 fps costs two frames of memory
        rather than six hundred.
        """
        mean: np.ndarray | None = None
        m2: np.ndarray | None = None
        counted = 0
        for frame in frames:
            grey = self._prepare(frame).astype(np.float32)
            if mean is None:
                mean = np.zeros_like(grey)
                m2 = np.zeros_like(grey)
            counted += 1
            delta = grey - mean
            mean += delta / counted
            m2 += delta * (grey - mean)

        if mean is None or m2 is None or counted < 5:
            return ActivityMap(frames=counted, hot_pct=0.0, boxes=[])

        std = np.sqrt(m2 / max(1, counted - 1))
        # Threshold against the *background* noise level, not a percentile of the whole frame: a
        # curtain can occupy 12 % of the view, which puts the 99th percentile inside the curtain
        # and finds nothing. Median plus a robust spread measures what a still wall looks like,
        # and the absolute floor stops a perfectly still room from reporting sensor noise.
        median = float(np.median(std))
        mad = float(np.median(np.abs(std - median))) or 0.5
        threshold = max(median + 6.0 * mad, self.cfg.detector.activity_std_floor)
        hot = (std > threshold).astype(np.uint8) * 255
        hot = cv2.morphologyEx(hot, cv2.MORPH_CLOSE, np.ones((11, 11), np.uint8))
        hot = cv2.morphologyEx(hot, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))

        contours, _ = cv2.findContours(hot, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        area_floor = hot.size * min_box_pct / 100.0
        boxes: list[list[list[int]]] = []
        for contour in sorted(contours, key=cv2.contourArea, reverse=True):
            if cv2.contourArea(contour) < area_floor:
                continue
            x, y, w, h = cv2.boundingRect(contour)
            pad = 4
            x0, y0 = max(0, x - pad), max(0, y - pad)
            x1, y1 = min(hot.shape[1], x + w + pad), min(hot.shape[0], y + h + pad)
            boxes.append([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
            if len(boxes) >= 6:
                break

        return ActivityMap(
            frames=counted,
            hot_pct=100.0 * float(np.count_nonzero(hot)) / hot.size,
            boxes=boxes,
        )

    # --- calibration ---------------------------------------------------------

    def calibrate(self, frames: list[np.ndarray]) -> NoiseStats:
        """Measure how much an empty room moves by itself, and suggest a threshold above it."""
        percentages: list[float] = []
        for index, frame in enumerate(frames):
            grey = self._prepare(frame)
            foreground = self._bg.apply(grey)
            _, foreground = cv2.threshold(foreground, 200, 255, cv2.THRESH_BINARY)
            if index < 5:
                continue  # let the model settle before measuring
            percentages.append(100.0 * np.count_nonzero(foreground) / foreground.size)
        if not percentages:
            return NoiseStats(len(frames), 0.0, 0.0, self.cfg.detector.min_area_pct)
        values = np.array(percentages)
        p99 = float(np.percentile(values, 99))
        # Three times the 99th percentile, floored at 0.2 %: comfortably above self-motion but
        # still well under a person entering the frame.
        recommended = max(0.2, round(p99 * 3.0 + 0.05, 2))
        return NoiseStats(len(frames), float(values.mean()), p99, recommended)

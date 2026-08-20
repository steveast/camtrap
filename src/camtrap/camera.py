"""V4L2 capture with decimation and reconnection.

The device offers MJPG at 1280x720 only at 30 fps (YUYV caps at 10), so the 5 fps the spec asks
for is decimation here rather than a driver setting: capture every frame, analyse every sixth.

A camera that disappears mid-run is a tamper-class event, not just an error — so the loop reports
it upwards instead of dying quietly.
"""

from __future__ import annotations

import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass

import cv2
import numpy as np

from . import log
from .config import Config


@dataclass
class CameraStatus:
    opened: bool = False
    frames: int = 0
    reopens: int = 0
    last_frame_at: float = 0.0
    gone: bool = False


class Camera:
    """Wraps cv2.VideoCapture so the run loop never touches OpenCV directly."""

    def __init__(
        self,
        cfg: Config,
        *,
        opener: Callable[[str], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.cfg = cfg
        self._opener = opener if opener is not None else self._open_v4l2
        self.clock = clock
        self.sleep = sleep
        self._cap: object | None = None
        self.status = CameraStatus()
        self._analysed: deque[float] = deque(maxlen=40)
        self._raw_interval: float | None = None
        self._last_raw: float | None = None
        self._stride = max(1, cfg.camera.capture_fps // max(1, cfg.camera.target_fps))
        self._counter = 0

    # --- device --------------------------------------------------------------

    def _open_v4l2(self, device: str):
        cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        if not cap.isOpened():
            return cap
        fourcc = self.cfg.camera.fourcc
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, self.cfg.camera.width)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self.cfg.camera.height)
        cap.set(cv2.CAP_PROP_FPS, self.cfg.camera.capture_fps)
        # Keep the driver queue short. With a deep queue the frame we finally decode is seconds
        # old: motion is reported late and the snapshot shows where the person *was*.
        cap.set(cv2.CAP_PROP_BUFFERSIZE, self.cfg.camera.buffer_frames)
        return cap

    def open(self) -> bool:
        self._cap = self._opener(self.cfg.camera.device)
        opened = bool(self._cap is not None and self._cap.isOpened())
        first = self.status.reopens == 0 and self.status.frames == 0
        self.status.opened = opened
        if opened:
            log.emit(
                "camera" if first else "camera_reopen",
                device=self.cfg.camera.device,
                fourcc=self.cfg.camera.fourcc,
                stride=self._stride,
                reopens=self.status.reopens,
            )
        else:
            log.emit("camera_error", device=self.cfg.camera.device, reason="cannot open")
        return opened

    def release(self) -> None:
        if self._cap is not None:
            with __import__("contextlib").suppress(Exception):
                self._cap.release()
        self._cap = None
        self.status.opened = False

    # --- frames --------------------------------------------------------------

    def read(self) -> np.ndarray | None:
        """Decode one frame. Prefer read_decimated() in a loop: it skips decoding entirely."""
        if self._cap is None and not self.open():
            return None
        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        self.status.frames += 1
        self.status.last_frame_at = self.clock()
        self._note_raw_frame()
        return frame

    def _skip(self, count: int) -> bool:
        """Pull frames off the driver without decoding them (grab, not retrieve).

        This is the difference between decoding 30 MJPEG frames a second and decoding 5. The
        skipped frames still have to leave the queue, or the queue backs up and every frame we do
        decode is stale.
        """
        assert self._cap is not None
        for _ in range(count):
            if not self._cap.grab():
                return False
            self.status.frames += 1
            self._note_raw_frame()
        return True

    def _note_raw_frame(self) -> None:
        """Exponentially smoothed interval between frames as the device delivers them."""
        now = self.clock()
        if self._last_raw is not None:
            delta = now - self._last_raw
            if 0 < delta < 5.0:
                self._raw_interval = (
                    delta if self._raw_interval is None else 0.8 * self._raw_interval + 0.2 * delta
                )
        self._last_raw = now

    def frames(self, *, limit: int | None = None):
        """Yield frames at roughly target_fps, with a stride derived from the MEASURED rate.

        A fixed stride of capture_fps // target_fps assumes the camera delivers what it advertises.
        This one advertises 30 fps for MJPEG and measured 12.5 fps in evening light, because UVC
        cameras lengthen exposure as light drops. The fixed stride then waited for six frames —
        480 ms per analysis instead of 200 ms — and the driver queue backed up, so every frame
        analysed was already old. That was the "huge delay".

        So: measure the delivery interval, skip only as many frames as that rate justifies, and
        when the camera is slower than the target, skip nothing at all.
        """
        produced = 0
        failures = 0
        while limit is None or produced < limit:
            if self._cap is None and not self.open():
                frame = None
            else:
                skip = self._stride_now() - 1
                frame = self.read() if skip <= 0 or self._skip(skip) else None
            if frame is None:
                failures += 1
                self.release()
                self.status.reopens += 1
                attempts = self.cfg.camera.max_reopen_attempts
                if attempts and failures > attempts:
                    self.status.gone = True
                    log.emit("camera_gone", failures=failures)
                    return
                self.sleep(self.cfg.camera.reopen_delay_sec)
                continue
            failures = 0
            self._analysed.append(self.clock())
            produced += 1
            yield frame

    def _stride_now(self) -> int:
        """How many camera frames to consume per analysed frame, from the measured raw rate."""
        raw = self.raw_fps()
        if raw is None:
            # Nothing measured yet: trust the configured numbers for the first few frames.
            return max(1, self.cfg.camera.capture_fps // max(1, self.cfg.camera.target_fps))
        return max(1, int(raw / max(0.1, self.cfg.camera.target_fps)))

    def raw_fps(self) -> float | None:
        """Delivery rate of the device itself, decoded or skipped."""
        if self._raw_interval is None or self._raw_interval <= 0:
            return None
        return 1.0 / self._raw_interval

    def measured_fps(self) -> float | None:
        """Actual delivery rate of analysed frames, for the heartbeat and for selftest."""
        stamps = list(self._analysed)
        if len(stamps) < 3:
            return None
        span = stamps[-1] - stamps[0]
        return (len(stamps) - 1) / span if span > 0 else None

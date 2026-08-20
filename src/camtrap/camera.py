"""V4L2 capture with decimation and reconnection.

The device offers MJPG at 1280x720 only at 30 fps (YUYV caps at 10), so the 5 fps the spec asks
for is decimation here rather than a driver setting: capture every frame, analyse every sixth.

A camera that disappears mid-run is a tamper-class event, not just an error — so the loop reports
it upwards instead of dying quietly.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
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
        return cap

    def open(self) -> bool:
        self._cap = self._opener(self.cfg.camera.device)
        opened = bool(self._cap is not None and self._cap.isOpened())
        self.status.opened = opened
        if opened:
            log.emit(
                "camera",
                device=self.cfg.camera.device,
                fourcc=self.cfg.camera.fourcc,
                stride=self._stride,
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
        if self._cap is None and not self.open():
            return None
        assert self._cap is not None
        ok, frame = self._cap.read()
        if not ok or frame is None:
            return None
        self.status.frames += 1
        self.status.last_frame_at = self.clock()
        return frame

    def frames(self, *, limit: int | None = None) -> Iterator[np.ndarray]:
        """Yield decimated frames, reopening the device if it drops off the bus."""
        produced = 0
        failures = 0
        while limit is None or produced < limit:
            frame = self.read()
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
            self._counter += 1
            if self._counter % self._stride:
                continue
            produced += 1
            yield frame

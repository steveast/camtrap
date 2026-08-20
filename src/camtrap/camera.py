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
        return True

    def frames(self, *, limit: int | None = None) -> Iterator[np.ndarray]:
        """Yield decimated frames, reopening the device if it drops off the bus."""
        produced = 0
        failures = 0
        while limit is None or produced < limit:
            if (self._cap is None and not self.open()) or (
                self._stride > 1 and not self._skip(self._stride - 1)
            ):
                frame = None
            else:
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
            produced += 1
            yield frame

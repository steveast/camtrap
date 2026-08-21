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
        self._failures = 0
        self._next_attempt = float("-inf")
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
        # A camera that is gone stays gone for hours; one line per retry would be the loudest
        # thing in the log and would fill the log file with nothing new. Once the verdict is in,
        # stay quiet — `camera_back` and `camera_gone` mark the transitions that matter.
        quiet = self.status.gone
        if opened and not quiet:
            log.emit(
                "camera" if first else "camera_reopen",
                device=self.cfg.camera.device,
                fourcc=self.cfg.camera.fourcc,
                stride=self._stride,
                reopens=self.status.reopens,
            )
        elif not opened and not quiet:
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

    def next_frame(self) -> np.ndarray | None:
        """One attempt at one frame. Returns None on failure — never blocks indefinitely.

        This is the contract the run loop needs. The previous generator retried internally forever
        (max_reopen_attempts defaults to "retry forever"), so a camera that stopped delivering meant
        the loop never got control back: tamper polling, the siren and the heartbeat all lived
        inside that loop. A stub device measured 446 reopen attempts and zero frames yielded.
        Losing the eyes must not cost the ears.
        """
        if self._cap is None:
            # Reopening a V4L2 device is expensive, so it gets its own pace. The loop keeps
            # ticking at its own rate meanwhile — that is the whole point of answering None.
            if self.clock() < self._next_attempt:
                return None
            if not self.open():
                self._note_failure()
                return None
        skip = self._stride_now() - 1
        frame = self.read() if skip <= 0 or self._skip(skip) else None
        if frame is None:
            self._note_failure()
            return None
        if self._failures or self.status.gone:
            # The device came back. Say so, and stop claiming it is gone.
            log.emit("camera_back", after_failures=self._failures)
            self._failures = 0
            self.status.gone = False
        self._analysed.append(self.clock())
        return frame

    def _note_failure(self) -> None:
        """Release, count, and decide whether the camera counts as gone — but keep trying."""
        self.release()
        self._next_attempt = self.clock() + self.cfg.camera.reopen_delay_sec
        self._failures += 1
        self.status.reopens += 1
        attempts = self.cfg.camera.max_reopen_attempts
        if attempts and self._failures >= attempts and not self.status.gone:
            self.status.gone = True
            log.emit("camera_gone", failures=self._failures)

    def frames(self, *, limit: int | None = None):
        """Yield decoded frames, skipping failures. Prefer next_frame() in the run loop.

        Kept for the tools that only care about pictures (calibrate, mask, suggest-mask): it waits
        out reopen delays on the caller's behalf, and gives up once the camera is declared gone so
        it cannot hang a command forever.
        """
        produced = 0
        while limit is None or produced < limit:
            frame = self.next_frame()
            if frame is None:
                if self.status.gone:
                    return
                self.sleep(self.cfg.camera.reopen_delay_sec)
                continue
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

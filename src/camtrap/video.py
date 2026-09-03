"""Recording the visit as a clip, from the frames the run loop has already decoded.

Three constraints shape every decision in this module, and none of them is negotiable.

**The camera streams to one reader.** A second ffmpeg opening `/dev/video0` alongside the run
loop does not get frames, so the encoder is fed what the loop has already decoded. That fixes the
clip's frame rate at `camera.target_fps` — 5 — and it means the clip costs no extra capture.

**The run loop must never block on the encoder.** Tamper polling, the siren and the heartbeat all
live in that loop. A pipe write that waits for ffmpeg is a trap that stopped watching, so the
hand-off is a bounded queue and a writer thread: when the queue is full the frame is DROPPED and
counted. A gap in a clip is a cost; a loop stuck in `write()` is a failure.

**A clip is not the alert.** It does not exist as a file until its first segment closes, and the
laptop may be out of the room by then. The photograph remains what the alert carries and what the
siren waits for; this module is the record behind it.

Hence segments rather than one file: a killed process loses at most `segment_sec` instead of
everything (an mp4 whose `moov` box was never written does not play at all), and each closed
segment is a complete artefact that can leave on its own.
"""

from __future__ import annotations

import os
import queue
import shutil
import subprocess
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import log
from .config import Config

#: Every mp4 the segment muxer finishes ends with a `moov` box; it is written on close. A segment
#: still being written has `ftyp` and `mdat` and no `moov`, so this is the difference between a
#: file that plays and a file that looks like evidence and cannot be opened.
_MOOV = b"moov"


@dataclass
class ClipStatus:
    event_id: str = ""
    running: bool = False
    segments_ready: int = 0
    frames_submitted: int = 0
    frames_dropped: int = 0
    bytes_ready: int = 0
    encoder_failed: bool = False
    started: float = 0.0
    geometry: str = ""
    #: Set once per process, so a missing ffmpeg is reported by the heartbeat rather than
    #: rediscovered on every event.
    unavailable_reason: str = ""


@dataclass
class _Encoder:
    """One ffmpeg process and the thread that feeds it."""

    proc: object
    thread: threading.Thread
    frames: queue.Queue = field(default_factory=queue.Queue)
    failed: bool = False
    #: Asked to stop. A flag rather than a sentinel value in the queue, because the queue is
    #: BOUNDED and full exactly when the encoder is behind: a `put_nowait` sentinel is silently
    #: refused in that case, the writer never closes stdin, ffmpeg waits for input that will
    #: never come and has to be killed — losing the last segment of every busy event. The pump
    #: polls this instead, so stopping works whether the queue is full or empty, and the frames
    #: already queued still reach the encoder.
    stopping: threading.Event = field(default_factory=threading.Event)


class ClipRecorder:
    """Owns the encoder, the staging directory, and when a segment is safe to send."""

    def __init__(
        self,
        cfg: Config,
        *,
        spawn: Callable[[list[str]], object] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        self._spawn = spawn if spawn is not None else self._default_spawn
        self.status = ClipStatus()
        self._encoder: _Encoder | None = None
        self._event_id = ""
        self._deadline = float("inf")
        self._armed = False

    # --- process -------------------------------------------------------------

    @staticmethod
    def _default_spawn(argv: list[str]) -> object:
        return subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )

    def available(self) -> tuple[bool, str]:
        """Whether a clip can be recorded at all. Checked before an event, not during one."""
        if not self.cfg.video.enabled:
            return False, "disabled"
        argv = self.cfg.video.ffmpeg_cmd
        if not argv:
            return False, "no ffmpeg command configured"
        if shutil.which(argv[0]) is None and not Path(argv[0]).exists():
            return False, f"{argv[0]} not found"
        # Refused here rather than discovered later: a clip whose sink cannot be built can never
        # be delivered and never be freed, so it would sit in the spool until the cap dropped it —
        # disk and CPU spent on an artefact that was never going anywhere.
        #
        # `prod` is not in the list on purpose. The receiver is out of this path by the owner's
        # decision, and naming it here would quietly put a stream of segments back into an inbox
        # whose cap is there to protect the photographs.
        if not self.cfg.video.sinks:
            return False, "no sink configured for clips"
        unknown = [name for name in self.cfg.video.sinks if name not in ("mega", "telegram")]
        if unknown:
            return False, f"unknown clip sink: {','.join(unknown)}"
        return True, ""

    def _argv(self, width: int, height: int) -> list[str]:
        video = self.cfg.video
        # A keyframe exactly at every segment boundary. The segment muxer cuts at keyframes, so
        # without this a 15 s segment_time against the default GOP comes out as 10 or 20.
        gop = max(1, int(video.fps * video.segment_sec))
        argv = [
            *video.ffmpeg_cmd,
            "-hide_banner",
            "-loglevel",
            "error",
            # There is no audio input to disable, and `-an` says so a second time on purpose:
            # this project promises it never records audio, and the promise should survive
            # someone editing this argv later.
            "-f",
            "rawvideo",
            "-pix_fmt",
            "bgr24",
            "-s",
            f"{width}x{height}",
            "-framerate",
            str(video.fps),
            "-i",
            "-",
            "-an",
        ]
        if video.scale_width and video.scale_width < width:
            height_at = int(height * video.scale_width / width) // 2 * 2
            argv += ["-vf", f"scale={video.scale_width}:{height_at}"]
        argv += [
            "-c:v",
            "libx264",
            "-preset",
            video.preset,
            "-crf",
            str(video.crf),
            "-maxrate",
            f"{video.maxrate_kbps}k",
            "-bufsize",
            f"{video.maxrate_kbps * 2}k",
            "-g",
            str(gop),
            "-pix_fmt",
            "yuv420p",
            "-f",
            "segment",
            "-segment_time",
            str(video.segment_sec),
            "-segment_format",
            "mp4",
            "-reset_timestamps",
            "1",
            str(self.cfg.video_staging_dir / f"{self._event_id}_v%03d.mp4"),
        ]
        return argv

    # --- lifecycle -----------------------------------------------------------

    def begin(self, event_id: str, *, now: float) -> None:
        """Arm for this event. ffmpeg starts on the first frame: that is what knows the geometry."""
        if self._encoder is not None:
            return
        ok, why = self.available()
        if not ok:
            if self.status.unavailable_reason != why:
                self.status.unavailable_reason = why
                log.emit("clip_unavailable", why=why)
            return
        self.cfg.video_staging_dir.mkdir(parents=True, exist_ok=True)
        self._event_id = event_id
        self._deadline = now + self.cfg.video.clip_sec
        self._armed = True
        self.status = ClipStatus(
            event_id=event_id,
            running=False,
            started=now,
            unavailable_reason=self.status.unavailable_reason,
        )

    def _start(self, frame) -> bool:
        height, width = frame.shape[:2]
        argv = self._argv(width, height)
        try:
            proc = self._spawn(argv)
        except (OSError, ValueError) as exc:
            log.emit("clip_error", id=self._event_id, why=f"spawn: {exc}")
            self._armed = False
            self.status.encoder_failed = True
            return False
        frames: queue.Queue = queue.Queue(maxsize=max(1, self.cfg.video.queue_frames))
        encoder = _Encoder(proc=proc, thread=threading.Thread(), frames=frames)
        encoder.thread = threading.Thread(
            target=self._pump, args=(encoder,), name="camtrap-clip", daemon=True
        )
        self._encoder = encoder
        encoder.thread.start()
        self.status.running = True
        self.status.geometry = f"{width}x{height}"
        log.emit(
            "clip_begin",
            id=self._event_id,
            geometry=self.status.geometry,
            fps=self.cfg.video.fps,
            segment=self.cfg.video.segment_sec,
            clip=self.cfg.video.clip_sec,
        )
        return True

    def _pump(self, encoder: _Encoder) -> None:
        """The only place that writes to ffmpeg. Runs in its own thread so a stall stays here."""
        stdin = getattr(encoder.proc, "stdin", None)
        while True:
            try:
                payload = encoder.frames.get(timeout=0.2)
            except queue.Empty:
                # Nothing queued: the moment to notice we were asked to stop. Draining first and
                # only then leaving is what lets the tail of a clip through.
                if encoder.stopping.is_set():
                    break
                continue
            if payload is None:
                break
            if stdin is None or encoder.failed:
                continue
            try:
                stdin.write(payload)
            except (BrokenPipeError, ValueError, OSError) as exc:
                encoder.failed = True
                log.emit("clip_error", id=self._event_id, why=f"write: {type(exc).__name__}")
        # Closing stdin is what tells ffmpeg the stream is over and makes it write the last moov.
        try:
            if stdin is not None:
                stdin.close()
        except (BrokenPipeError, OSError):
            pass

    # --- frames --------------------------------------------------------------

    def submit(self, frame, *, now: float) -> bool:
        """Hand one decoded frame to the encoder. Never blocks; returns whether it was taken."""
        if not self._armed:
            return False
        if self._encoder is None and not self._start(frame):
            return False
        encoder = self._encoder
        assert encoder is not None
        if encoder.failed:
            return False
        self.status.frames_submitted += 1
        try:
            encoder.frames.put_nowait(frame.tobytes())
        except queue.Full:
            self.status.frames_dropped += 1
            # One line per drop would be the loudest thing in the log on a slow machine, and the
            # count is in the heartbeat and the manifest. Say it once per event.
            if self.status.frames_dropped == 1:
                log.emit("clip_drop", id=self._event_id, why="encoder behind")
            return False
        return True

    # --- segments ------------------------------------------------------------

    def _staged(self) -> list[Path]:
        root = self.cfg.video_staging_dir
        if not root.exists():
            return []
        return sorted(path for path in root.glob("*_v*.mp4") if path.is_file())

    @staticmethod
    def _looks_complete(path: Path) -> bool:
        """Whether the muxer has closed this file.

        Read the tail rather than the whole segment: `moov` is written last, and a 1.3 MB read per
        tick per segment would be the most expensive thing this module does.
        """
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                if size > 65536:
                    fh.seek(-65536, os.SEEK_END)
                return _MOOV in fh.read()
        except OSError:
            return False

    def collect(self, *, now: float, final: bool = False) -> list[Path]:
        """Move every finished segment into the spool and return what moved.

        A segment is finished when a LATER one exists — the segment muxer gives no other signal
        that it has moved on — and, as a second opinion, when the file itself carries the `moov`
        box that is written on close. Either test alone would be enough on a good day: together
        they also cover the run that was killed mid-segment, whose last file looks finished by
        position and is not playable.
        """
        staged = self._staged()
        if not staged:
            return []
        moved: list[Path] = []
        # Everything but the newest, unless the encoder is done and there is no newer one coming.
        candidates = staged if final else staged[:-1]
        for path in candidates:
            if not self._looks_complete(path):
                if final:
                    log.emit("clip_incomplete", file=path.name, bytes=path.stat().st_size)
                    with __import__("contextlib").suppress(OSError):
                        path.unlink()
                continue
            target = self.cfg.spool_dir / path.name
            try:
                self.cfg.spool_dir.mkdir(parents=True, exist_ok=True)
                size = path.stat().st_size
                # Same filesystem by construction (both under the state dir), so this is a rename:
                # the file appears in the spool whole or not at all, which is what the uploader
                # listing it a moment later depends on.
                path.replace(target)
            except OSError as exc:
                log.emit("clip_error", file=path.name, why=f"move: {exc}")
                continue
            moved.append(target)
            self.status.segments_ready += 1
            self.status.bytes_ready += size
            log.emit("clip_segment", id=self._event_id, file=target.name, bytes=size)
        return moved

    def tick(self, *, now: float) -> list[Path]:
        """Called from the run loop: harvest segments, and stop once the clip is a minute long."""
        if self._encoder is None and not self._armed:
            return []
        if self._armed and now >= self._deadline:
            return self.finish(now=now, reason="clip_full")
        return self.collect(now=now)

    def finish(self, *, now: float, reason: str = "event_end") -> list[Path]:
        """Close the encoder and flush what it wrote. Safe to call when nothing is running."""
        encoder = self._encoder
        self._armed = False
        self._deadline = float("inf")
        if encoder is None:
            self.status.running = False
            return self.collect(now=now, final=True)
        encoder.stopping.set()
        encoder.thread.join(timeout=self.cfg.video.stop_timeout_sec)
        proc = encoder.proc
        try:
            proc.wait(timeout=self.cfg.video.stop_timeout_sec)  # type: ignore[attr-defined]
        except Exception:
            # A hung encoder must not hold the run loop, and it must not be left holding the
            # segment either: killing it costs the last segment, which `collect` then discards
            # for having no moov box rather than shipping something unplayable.
            log.emit("clip_error", id=self._event_id, why="encoder did not exit; killing")
            with __import__("contextlib").suppress(Exception):
                proc.kill()  # type: ignore[attr-defined]
            with __import__("contextlib").suppress(Exception):
                proc.wait(timeout=2.0)  # type: ignore[attr-defined]
        self._encoder = None
        self.status.running = False
        moved = self.collect(now=now, final=True)
        log.emit(
            "clip_end",
            id=self._event_id,
            reason=reason,
            segments=self.status.segments_ready,
            bytes=self.status.bytes_ready,
            submitted=self.status.frames_submitted,
            dropped=self.status.frames_dropped,
        )
        return moved

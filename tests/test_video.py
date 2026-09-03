"""The clip recorder: what it hands the spool, and what it refuses to hand it.

Two layers here. Most of it runs against a fake process, because the contract that matters is
"the run loop is never blocked and a half-written segment is never sent" and that has nothing to
do with H.264. One test at the end runs real ffmpeg on real frames: the argv is the other half of
the contract, and nothing but ffmpeg can say whether it is right.
"""

from __future__ import annotations

import shutil
import subprocess
import threading
import time

import numpy as np
import pytest

from camtrap.video import ClipRecorder


def _frame(value: int = 40, width: int = 320, height: int = 180) -> np.ndarray:
    return np.full((height, width, 3), value, dtype=np.uint8)


class FakeProc:
    """A process that accepts bytes, and can be told to misbehave."""

    def __init__(self, argv: list[str]) -> None:
        self.argv = argv
        self.written = 0
        self.killed = False
        self.waits = 0
        self.stdin = self
        self.hang = False
        self.break_pipe = False
        self.closed = False

    # --- the stdin half ---
    def write(self, payload: bytes) -> int:
        if self.break_pipe:
            raise BrokenPipeError("fake")
        self.written += len(payload)
        return len(payload)

    def close(self) -> None:
        self.closed = True

    # --- the process half ---
    def wait(self, timeout: float | None = None) -> int:
        self.waits += 1
        if self.hang:
            raise subprocess.TimeoutExpired("ffmpeg", timeout or 0)
        return 0

    def kill(self) -> None:
        self.killed = True


@pytest.fixture
def rig(cfg):
    """A recorder whose ffmpeg is a fake, and the staging dir it writes into."""
    cfg.video.ffmpeg_cmd = ["/bin/true"]  # `available()` only checks that it exists
    spawned: list[FakeProc] = []

    def spawn(argv):
        proc = FakeProc(argv)
        spawned.append(proc)
        return proc

    recorder = ClipRecorder(cfg, spawn=spawn, clock=lambda: 0.0)

    class Rig:
        def __init__(self):
            self.cfg = cfg
            self.recorder = recorder
            self.spawned = spawned

        def segment(self, index: int, *, event: str, closed: bool = True, size: int = 2048):
            """Write a segment into staging the way the muxer would."""
            path = cfg.video_staging_dir / f"{event}_v{index:03d}.mp4"
            path.parent.mkdir(parents=True, exist_ok=True)
            body = b"\x00\x00\x00\x18ftypisom" + b"m" * size
            if closed:
                body += b"\x00\x00\x00\x08moov"
            path.write_bytes(body)
            return path

        def drain_thread(self):
            """Let the writer thread catch up — it is the only thing that touches the pipe."""
            for _ in range(200):
                if all(p.frames.empty() for p in [self.recorder._encoder] if p):
                    return
                time.sleep(0.005)

        def spool_names(self):
            root = cfg.spool_dir
            return sorted(p.name for p in root.iterdir()) if root.exists() else []

    return Rig()


def test_a_frame_reaches_the_encoder(rig):
    rig.recorder.begin("evt_A", now=0.0)
    assert rig.recorder.submit(_frame(), now=0.0)
    rig.drain_thread()
    assert rig.spawned, "the encoder starts on the first frame, which is what knows the geometry"
    assert rig.recorder.status.geometry == "320x180"
    assert rig.spawned[0].written == 320 * 180 * 3


def test_the_geometry_comes_from_the_frame_not_the_config(rig):
    """A camera that hands back something other than what was asked for must not corrupt the clip.

    ffmpeg is told the raw frame size up front and cannot detect a mismatch: it would read the
    stream at the wrong stride and produce a sheared, diagonally smeared clip that still plays.
    """
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(width=640, height=360), now=0.0)
    argv = rig.spawned[0].argv
    assert "640x360" in argv
    assert argv[argv.index("-s") + 1] == "640x360"


def test_the_loop_is_never_blocked_by_a_stalled_encoder(cfg):
    """The whole reason for the thread and the bounded queue.

    A pipe write that waits on ffmpeg is a run loop that has stopped polling for tamper. This
    recorder answers immediately, drops the frame, and counts it.

    The encoder is stalled by parking the writer thread INSIDE `write`, which is what a real
    stall is. Stuffing the queue behind the thread's back was the first version of this test and
    it was a race: `FakeProc.write` returns instantly, so the thread emptied the queue again
    before the assertions ran and every submit succeeded. It passed alone and failed in the
    suite, which is the worst way for a test to be wrong.
    """
    cfg.video.ffmpeg_cmd = ["/bin/true"]
    cfg.video.queue_frames = 1

    class Stalled(FakeProc):
        def __init__(self, argv):
            super().__init__(argv)
            self.release = threading.Event()

        def write(self, payload):
            self.release.wait(timeout=5.0)
            return super().write(payload)

    procs: list[Stalled] = []

    def spawn(argv):
        procs.append(Stalled(argv))
        return procs[-1]

    recorder = ClipRecorder(cfg, spawn=spawn)
    recorder.begin("evt_A", now=0.0)
    recorder.submit(_frame(), now=0.0)  # the thread takes this one and parks in write()
    for _ in range(100):  # wait until it really is parked, rather than assuming it
        if recorder._encoder.frames.empty():  # taken off the queue means it is inside write()
            break
        time.sleep(0.005)
    recorder.submit(_frame(), now=0.1)  # fills the queue of 1

    started = time.monotonic()
    taken = [recorder.submit(_frame(), now=0.2) for _ in range(20)]
    elapsed = time.monotonic() - started
    procs[0].release.set()

    assert elapsed < 0.5, f"submit must not wait on the encoder, took {elapsed:.2f}s"
    assert not any(taken), "every frame offered to a full queue is dropped"
    assert recorder.status.frames_dropped == 20, recorder.status.frames_dropped


def test_a_broken_pipe_does_not_raise_into_the_loop(rig):
    """ffmpeg dying mid-event is an event with a short clip, not a trap that stopped."""
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.spawned[0].break_pipe = True
    for tick in range(5):
        rig.recorder.submit(_frame(), now=0.1 * tick)
    rig.drain_thread()
    assert rig.recorder.submit(_frame(), now=1.0) is False


def test_only_a_finished_segment_reaches_the_spool(rig):
    """The uploader lists the spool. A segment still being written must not be in it.

    Sending a truncated mp4 is worse than sending nothing: it has no `moov` box, so it does not
    play, and it arrives looking exactly like evidence that does.
    """
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.segment(0, event="evt_A", closed=True)
    rig.segment(1, event="evt_A", closed=False)  # the one ffmpeg is still writing

    moved = rig.recorder.collect(now=1.0)

    assert [p.name for p in moved] == ["evt_A_v000.mp4"]
    assert rig.spool_names() == ["evt_A_v000.mp4"]
    assert (rig.cfg.video_staging_dir / "evt_A_v001.mp4").exists(), "the open one stays in staging"


def test_the_newest_segment_waits_even_when_it_looks_finished(rig):
    """Position and content have to agree. Only the second opinion is not enough on its own."""
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.segment(0, event="evt_A", closed=True)
    assert rig.recorder.collect(now=1.0) == [], "a lone segment is the one being written"


def test_finishing_flushes_the_last_segment(rig):
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.segment(0, event="evt_A", closed=True)
    rig.segment(1, event="evt_A", closed=True)
    moved = rig.recorder.finish(now=2.0)
    assert [p.name for p in moved] == ["evt_A_v000.mp4", "evt_A_v001.mp4"]
    assert not list(rig.cfg.video_staging_dir.iterdir())


def test_an_unplayable_last_segment_is_discarded_not_shipped(rig):
    """A run killed mid-segment leaves a file that is finished by position and broken in fact."""
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.segment(0, event="evt_A", closed=True)
    rig.segment(1, event="evt_A", closed=False)
    moved = rig.recorder.finish(now=2.0)
    assert [p.name for p in moved] == ["evt_A_v000.mp4"]
    assert rig.spool_names() == ["evt_A_v000.mp4"]
    assert not list(rig.cfg.video_staging_dir.iterdir()), "and it does not sit there for ever"


def test_the_clip_stops_after_clip_sec(rig):
    """One minute from the trigger, on the owner's instruction: the visit goes on in photographs."""
    rig.cfg.video.clip_sec = 60.0
    rig.recorder.begin("evt_A", now=100.0)
    rig.recorder.submit(_frame(), now=100.0)
    rig.segment(0, event="evt_A", closed=True)
    assert rig.recorder.tick(now=140.0) == [], "still recording at +40 s"
    assert rig.recorder.status.running
    rig.recorder.tick(now=161.0)
    assert not rig.recorder.status.running, "and stopped at +61 s"
    assert rig.spool_names() == ["evt_A_v000.mp4"]


def test_a_hung_encoder_is_killed_rather_than_waited_on(rig):
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    rig.spawned[0].hang = True
    rig.cfg.video.stop_timeout_sec = 0.05
    rig.recorder.finish(now=1.0)
    assert rig.spawned[0].killed, "the run loop cannot wait on a stuck ffmpeg"


def test_a_missing_ffmpeg_is_reported_once_and_records_nothing(cfg):
    """A trap that silently cannot encode is the failure this project refuses to ship.

    `sound_ok` exists for the same reason: unreadiness has to be visible before the event.
    """
    cfg.video.ffmpeg_cmd = ["/nonexistent/ffmpeg"]
    recorder = ClipRecorder(cfg, spawn=lambda argv: pytest.fail("must not spawn"))
    ok, why = recorder.available()
    assert not ok and "not found" in why
    recorder.begin("evt_A", now=0.0)
    assert recorder.submit(_frame(), now=0.0) is False
    assert recorder.status.unavailable_reason


def test_a_clip_with_nowhere_to_go_is_not_recorded(cfg):
    """A clip whose sink cannot be built could never be delivered and never be freed.

    It would sit in the spool spending disk and CPU until the cap dropped it, having been encoded
    for nobody.
    """
    cfg.video.ffmpeg_cmd = ["/bin/true"]
    cfg.video.sinks = []
    recorder = ClipRecorder(cfg, spawn=lambda argv: pytest.fail("must not spawn"))
    ok, why = recorder.available()
    assert not ok and "no sink" in why
    recorder.begin("evt_A", now=0.0)
    assert recorder.submit(_frame(), now=0.0) is False


def test_the_receiver_is_not_an_option_for_clips(cfg):
    """Naming it would put a stream of segments back into the inbox the cap protects."""
    cfg.video.ffmpeg_cmd = ["/bin/true"]
    cfg.video.sinks = ["prod"]
    recorder = ClipRecorder(cfg, spawn=lambda argv: pytest.fail("must not spawn"))
    ok, why = recorder.available()
    assert not ok and "prod" in why


def test_disabled_records_nothing(cfg):
    cfg.video.enabled = False
    recorder = ClipRecorder(cfg, spawn=lambda argv: pytest.fail("must not spawn"))
    recorder.begin("evt_A", now=0.0)
    assert recorder.submit(_frame(), now=0.0) is False


def test_audio_is_refused_structurally_and_by_flag(rig):
    """The one promise in the README that a court might care about.

    There is no audio input in the argv at all — the input is a raw video pipe — and `-an` says it
    again. Both halves are asserted, because either one alone is an edit away from being wrong.
    """
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    argv = rig.spawned[0].argv
    assert "-an" in argv
    assert not [arg for arg in argv if arg.startswith("alsa") or arg in ("-f", "pulse")][1:2] or (
        argv[argv.index("-f") + 1] == "rawvideo"
    )
    assert "pulse" not in argv and "alsa" not in argv and "default" not in argv


def test_the_bitrate_has_a_ceiling(rig):
    """A dark, grainy room encodes to several times a lit one. The bound has to be a number."""
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    argv = rig.spawned[0].argv
    assert argv[argv.index("-maxrate") + 1] == f"{rig.cfg.video.maxrate_kbps}k"
    assert argv[argv.index("-bufsize") + 1] == f"{rig.cfg.video.maxrate_kbps * 2}k"


def test_a_keyframe_lands_on_every_segment_boundary(rig):
    """The segment muxer cuts at keyframes, so the GOP is what decides the segment length."""
    rig.recorder.begin("evt_A", now=0.0)
    rig.recorder.submit(_frame(), now=0.0)
    argv = rig.spawned[0].argv
    gop = int(argv[argv.index("-g") + 1])
    assert gop == int(rig.cfg.video.fps * rig.cfg.video.segment_sec)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_produces_playable_segments(cfg):
    """The argv against the real encoder. Nothing else can say whether it is right.

    Deliberately not a mock: every previous version of this argv that was wrong was wrong in a way
    a fake process accepted happily — a bad `-s`, a segment format the muxer refuses, a filter
    graph that cannot be built.
    """
    cfg.video.segment_sec = 1.0
    cfg.video.clip_sec = 60.0
    cfg.video.fps = 5
    recorder = ClipRecorder(cfg)
    recorder.begin("evt_REAL", now=0.0)
    for index in range(20):  # 4 s at 5 fps -> several segments
        frame = _frame(30 + index * 5, width=320, height=180)
        frame[40:140, index * 10 : index * 10 + 60] = 200
        recorder.submit(frame, now=index * 0.2)
        # The run loop hands over a frame every 200 ms; this loop is 10x faster than that, so it
        # deliberately leans on the queue. What is asserted below is the artefact, not that every
        # single submit was taken — the drop path has its own test.
        time.sleep(0.02)
    moved = recorder.finish(now=4.0)

    assert moved, "real ffmpeg should have produced at least one segment"
    for path in moved:
        assert path.stat().st_size > 0
        probe = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height,nb_read_packets",
                "-count_packets",
                "-of",
                "csv=p=0",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        assert probe.returncode == 0, probe.stderr
        assert "h264" in probe.stdout, probe.stdout
        assert "320,180" in probe.stdout, probe.stdout
    assert recorder.status.frames_submitted >= 15, (
        f"most frames should reach the encoder, got {recorder.status.frames_submitted}"
    )


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")
def test_real_ffmpeg_writes_no_audio_stream(cfg):
    """The promise, verified on the artefact rather than on the argv."""
    cfg.video.segment_sec = 1.0
    recorder = ClipRecorder(cfg)
    recorder.begin("evt_SILENT", now=0.0)
    for index in range(10):
        recorder.submit(_frame(60, width=320, height=180), now=index * 0.2)
        time.sleep(0.02)
    moved = recorder.finish(now=2.0)
    assert moved
    probe = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "csv=p=0",
            str(moved[0]),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert probe.returncode == 0, probe.stderr
    assert "audio" not in probe.stdout, probe.stdout

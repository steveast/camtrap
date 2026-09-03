"""One command that answers: would this trap actually work if I left the room now?

Every check reports a verdict and, when it fails, what to do about it. Failing loudly at home is
the entire point — a trap that discovers its problems in the hotel room has already failed.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from . import sounds
from .config import Config
from .player import AudioPath
from .state import read_mode

OK = "ok"
WARN = "warn"
FAIL = "fail"


@dataclass
class Check:
    name: str
    verdict: str
    detail: str = ""
    hint: str = ""


def _which(*names: str) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    return None


def check_tools() -> list[Check]:
    checks: list[Check] = []
    for name, why in (
        ("pw-play", "plays the siren and the warning"),
        ("pactl", "forces the sink, profile, mute and volume"),
        ("loginctl", "reads the lock state and locks the session on tamper"),
        ("systemd-inhibit", "keeps a closed lid from suspending the machine"),
        ("ffmpeg", "generates the siren and encodes the clips"),
        ("curl", "sends a clip to the chat from this laptop"),
        ("espeak-ng", "generates the spoken warning"),
    ):
        found = _which(name)
        checks.append(
            Check(
                f"tool:{name}",
                OK if found else FAIL,
                found or "not found",
                "" if found else f"install {name} — {why}",
            )
        )
    return checks


def check_camera(cfg: Config) -> Check:
    device = Path(cfg.camera.device)
    if not device.exists():
        return Check("camera", FAIL, f"{device} missing", "is the camera connected?")
    from .camera import Camera

    camera = Camera(cfg)
    if not camera.open():
        return Check("camera", FAIL, "cannot open", "another process may hold the device")
    try:
        frame = camera.read()
    finally:
        camera.release()
    if frame is None:
        return Check("camera", FAIL, "opened but no frames", "try a different fourcc or resolution")
    height, width = frame.shape[:2]
    return Check("camera", OK, f"{width}x{height}")


def check_sounds(cfg: Config) -> list[Check]:
    missing = sounds.missing_sounds(cfg)
    checks = [
        Check(
            "sound:files",
            OK if not missing else FAIL,
            "all present" if not missing else f"missing {','.join(missing)}",
            "" if not missing else "tools/make-siren.sh --mode yelp && tools/make-warning.sh --all",
        )
    ]
    if not cfg.sound.warn_langs:
        checks.append(
            Check("sound:langs", WARN, "no warning languages", "set sound.warn_langs in config")
        )
    else:
        checks.append(Check("sound:langs", OK, ",".join(cfg.sound.warn_langs)))
    return checks


def check_audio_path(cfg: Config) -> Check:
    if not _which("pactl"):
        return Check("sound:sink", FAIL, "pactl missing")
    path = AudioPath(cfg)
    sink = path.prepare(volume_pct=cfg.sound.warn_volume_pct)
    # A check must not leave the machine reconfigured: put the card profile back.
    path.restore_profile()
    if not sink:
        return Check("sound:sink", FAIL, "no sink found", "check `pactl list short sinks`")
    if "Speaker" not in sink and not cfg.sound.sink:
        return Check(
            "sound:sink",
            WARN,
            sink,
            "not the built-in speakers: unplug the jack, or set sound.sink explicitly",
        )
    return Check("sound:sink", OK, sink)


def check_session(cfg: Config) -> Check:
    from .arming import LogindSession

    locked = LogindSession(cfg).locked_hint()
    if locked is None:
        return Check(
            "arming:session",
            FAIL if cfg.arming.mode == "on_lock" else WARN,
            "LockedHint unavailable",
            "on_lock arming needs logind; use arming.mode = 'manual' otherwise",
        )
    return Check("arming:session", OK, "locked" if locked else "unlocked")


def check_sysrq() -> Check:
    try:
        value = Path("/proc/sys/kernel/sysrq").read_text().strip()
    except OSError:
        return Check("sysrq", WARN, "unreadable")
    try:
        mask = int(value)
    except ValueError:
        return Check("sysrq", WARN, value)
    # bit 6 (64) = signalling processes, bit 7 (128) = reboot/poweroff: either is a one-keystroke
    # kill for the siren.
    dangerous = mask & 0b11000000
    if mask == 1 or dangerous:
        return Check(
            "sysrq",
            WARN,
            f"kernel.sysrq={mask}",
            "Alt+SysRq+B/F can kill the siren; 16 (sync only) is the safe value",
        )
    return Check("sysrq", OK, f"kernel.sysrq={mask}")


def check_receiver(cfg: Config) -> Check:
    if cfg.upload.local_inbox:
        inbox = Path(cfg.upload.local_inbox)
        return Check("receiver", OK if inbox.parent.exists() else WARN, f"local {inbox}")
    if not cfg.upload.ssh_target:
        return Check("receiver", WARN, "no ssh target configured", "frames stay in the spool")
    argv = list(cfg.upload.ssh_cmd)
    if cfg.upload.ssh_key:
        argv += ["-i", cfg.upload.ssh_key]
    argv += [*cfg.upload.ssh_options, cfg.upload.ssh_target, "ping"]
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("receiver", FAIL, str(exc))
    if result.returncode != 0 or "ok ping" not in result.stdout:
        return Check("receiver", FAIL, (result.stderr or result.stdout).strip()[:80])
    return Check("receiver", OK, cfg.upload.ssh_target)


def check_inhibit() -> Check:
    if not _which("systemd-inhibit"):
        return Check("inhibit", FAIL, "systemd-inhibit missing")
    from .inhibit import Inhibitor

    inhibitor = Inhibitor(why="camtrap selftest")
    if not inhibitor.start():
        return Check("inhibit", FAIL, "cannot hold an inhibitor")
    active = inhibitor.active
    inhibitor.stop()
    return Check("inhibit", OK if active else FAIL, "held and released" if active else "died")


def check_spool(cfg: Config) -> Check:
    from .spool import Spool

    spool = Spool(cfg)
    depth = spool.depth()
    return Check("spool", OK, f"{depth} files, {spool.total_bytes() / 1048576:.1f} MB")


def check_video(cfg: Config) -> Check:
    """Prove the encoder actually encodes, rather than that ffmpeg is on the PATH.

    The audio probe plays a real burst through the real speakers for the same reason: walking away
    believing in a trap that cannot record is worse than knowing it is broken. This runs a handful
    of synthetic frames through the real argv and looks for a playable segment, which is what
    catches a build of ffmpeg without libx264 — where every check short of encoding says yes.

    A failure here is a WARNING, not a blocker. The clip is the record; the photographs are the
    alert and the evidence, and they do not go through this path at all.
    """
    import copy
    import tempfile

    import numpy as np

    from .video import ClipRecorder

    if not cfg.video.enabled:
        return Check("video", WARN, "disabled", "set video.enabled = true for clips")
    probe_cfg = copy.deepcopy(cfg)
    with tempfile.TemporaryDirectory(prefix="camtrap-video-") as tmp:
        probe_cfg.state_dir = tmp
        probe_cfg.video.segment_sec = 0.4
        recorder = ClipRecorder(probe_cfg)
        ok, why = recorder.available()
        if not ok:
            return Check("video", WARN, why, "no clips will be recorded")
        recorder.begin("evt_probe", now=0.0)
        for index in range(6):
            frame = np.full((180, 320, 3), 30 + index * 20, dtype=np.uint8)
            recorder.submit(frame, now=index * 0.2)
        segments = recorder.finish(now=2.0)
        if not segments:
            return Check(
                "video",
                WARN,
                "encoder produced nothing",
                "check `ffmpeg -encoders | grep libx264`",
            )
    shape = f"{int(cfg.video.clip_sec)}s clips in {int(cfg.video.segment_sec)}s segments"
    detail = f"h264 {shape} -> {','.join(cfg.video.sinks)}"
    if "telegram" in cfg.video.sinks:
        from .uploader import TelegramSink

        ok, why = TelegramSink(cfg).available()
        if not ok:
            # A warning, not a failure: the segments still reach the warehouse. But it has to be
            # said out loud, because "the clips silently never reached the chat" is exactly the
            # kind of unreadiness this whole check exists to surface before the event.
            return Check("video", WARN, f"{detail}; chat unavailable: {why}", "see docs/runbook.md")
    return Check("video", OK, detail)


def run(cfg: Config) -> tuple[list[Check], int]:
    checks: list[Check] = []
    checks.extend(check_tools())
    checks.append(check_camera(cfg))
    checks.extend(check_sounds(cfg))
    checks.append(check_audio_path(cfg))
    checks.append(check_session(cfg))
    checks.append(check_sysrq())
    checks.append(check_inhibit())
    checks.append(check_receiver(cfg))
    checks.append(check_video(cfg))
    checks.append(check_spool(cfg))
    checks.append(Check("mode", OK, read_mode(cfg.root).name))
    failures = sum(1 for check in checks if check.verdict == FAIL)
    return checks, failures


def render(checks: list[Check]) -> str:
    icons = {OK: "ok  ", WARN: "warn", FAIL: "FAIL"}
    lines = []
    for check in checks:
        line = f"[{icons[check.verdict]}] {check.name:18} {check.detail}"
        lines.append(line.rstrip())
        if check.hint and check.verdict != OK:
            lines.append(f"{'':7} -> {check.hint}")
    return "\n".join(lines)

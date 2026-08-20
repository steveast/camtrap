"""Configuration: in-code defaults, overridden by ~/.config/camtrap/config.toml.

Every threshold from SPEC.md section 3 lives here, and every filesystem path the agent reads is
a field rather than a literal. Tests point those fields at a temporary directory; a machine with
a different sysfs layout is handled by config rather than by code.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any


def _xdg(var: str, default: str) -> Path:
    return Path(os.environ.get(var) or Path.home() / default)


def data_dir() -> Path:
    return _xdg("XDG_DATA_HOME", ".local/share") / "camtrap"


def config_path() -> Path:
    return _xdg("XDG_CONFIG_HOME", ".config") / "camtrap" / "config.toml"


@dataclass
class CameraConfig:
    device: str = "/dev/video0"
    width: int = 1280
    height: int = 720
    # The driver only offers 30 fps for MJPG at 1280x720 (10 for YUYV), so the 5 fps the spec
    # asks for is decimation in code: keep every capture_fps // target_fps-th frame.
    capture_fps: int = 30
    target_fps: int = 5
    fourcc: str = "MJPG"
    # Driver queue depth. 1 means "always give me the newest frame"; a deeper queue trades
    # latency for smoothness, which is the wrong trade for a trap.
    buffer_frames: int = 1
    reopen_delay_sec: float = 2.0
    max_reopen_attempts: int = 0  # 0 = retry forever


@dataclass
class DetectorConfig:
    warmup_sec: float = 20.0
    min_area_pct: float = 0.8
    min_motion_frames: int = 3
    global_change_pct: float = 70.0
    blur_kernel: int = 21
    analysis_width: int = 640
    # Frames the background model must see before any verdict is issued. Independent of
    # warmup_sec: with warm-up disabled the very first frame is 100 % foreground, which would
    # otherwise be reported as a light change on every start.
    min_model_frames: int = 5
    mog2_history: int = 500
    mog2_var_threshold: float = 16.0
    # Polygons excluded from analysis, in analysis-frame coordinates: [[[x, y], ...], ...]
    ignore_mask: list[list[list[int]]] = field(default_factory=list)
    # Scene-shift arbitration (3.3): a shift longer than this many pixels means the case moved.
    move_shift_px: float = 12.0
    move_response_min: float = 0.15
    # Below this greyscale standard deviation a frame carries no structure to correlate — a dark
    # room, or a wall filling the lens. Phase correlation is meaningless there, so movement is
    # NOT inferred: refusing to guess costs a missed tamper, guessing costs a siren at 3am.
    min_texture_std: float = 3.0
    #: Per-pixel temporal standard deviation below which a region counts as still, for
    #: `suggest-mask`. Sensor noise on this camera sits near 2.
    activity_std_floor: float = 6.0
    # Above this share of changed pixels the scene is checked for a global shift. Deliberately
    # lower than global_change_pct: lifting the laptop can change well under 70 % of the frame
    # while still moving the whole scene, and that is a tamper, not motion.
    shift_check_pct: float = 40.0
    als_jump_pct: float = 25.0


@dataclass
class EventConfig:
    snapshot_interval_sec: float = 5.0
    prebuffer_frames: int = 5
    prebuffer_interval_sec: float = 1.0
    event_gap_sec: float = 30.0
    max_frames_per_event: int = 60
    jpeg_quality: int = 85
    # A snapshot every snapshot_interval_sec is right for a curtain twitching for a minute, and
    # wrong for a person crossing the room in two seconds: they land between frames and the event
    # ends up documenting the curtain. So a change well above the trigger threshold takes a frame
    # immediately, subject to its own shorter floor.
    boost_area_pct: float = 4.0
    boost_min_interval_sec: float = 1.0


@dataclass
class SpoolConfig:
    max_mb: int = 512
    retention_days: int = 14
    upload_retry_max_sec: float = 300.0
    upload_retry_base_sec: float = 2.0


@dataclass
class TamperConfig:
    poll_sec: float = 1.0
    debounce_sec: float = 1.5
    ac_online_paths: list[str] = field(
        default_factory=lambda: [
            "/sys/class/power_supply/ADP1/online",
            "/sys/class/power_supply/ucsi-source-psy-USBC000:001/online",
            "/sys/class/power_supply/ucsi-source-psy-USBC000:002/online",
        ]
    )
    lid_state_path: str = "/proc/acpi/button/lid/LID0/state"
    als_paths: list[str] = field(
        default_factory=lambda: [
            "/sys/bus/iio/devices/iio:device0/in_illuminance_raw",
            "/sys/bus/iio/devices/iio:device1/in_illuminance_raw",
        ]
    )
    camera_gone_is_tamper: bool = True
    camera_gone_plays_siren: bool = False


@dataclass
class SoundConfig:
    # Stage 2 — the siren.
    siren_file: str = ""  # empty => data_dir()/sounds/siren.ogg
    siren_sec: float = 6.0
    siren_mode: str = "yelp"
    volume_pct: int = 100
    cooldown_sec: float = 60.0
    # The cooldown is per signal, not global: closing the lid and then pulling the cable are two
    # different pieces of interference and each deserves its own alarm. This is the floor between
    # any two bursts, so a flapping sensor still cannot machine-gun the siren.
    retrigger_min_sec: float = 4.0
    max_per_event: int = 3
    max_per_hour: int = 10
    # Stage 1 — the spoken warning.
    warn_langs: list[str] = field(default_factory=lambda: ["vi", "en"])
    warn_dir: str = ""  # empty => data_dir()/sounds
    warn_volume_pct: int = 85
    warn_cooldown_sec: float = 120.0
    warn_max_per_hour: int = 10
    warn_on_light: bool = False
    # Upper bound on how long the whole warning may take; also the kill timeout for a hung
    # player process. Two rendered languages are ~12 s together, so 30 s is generous.
    warn_timeout_sec: float = 30.0
    # Shared.
    delay_max_sec: float = 3.0
    hold_poll_ms: int = 250
    sink: str = ""  # empty => pick the built-in speakers by port
    card: str = ""  # empty => discover the card that owns a Speaker port
    player_cmd: list[str] = field(default_factory=lambda: ["pw-play"])
    pactl_cmd: list[str] = field(default_factory=lambda: ["pactl"])
    lock_session_on_tamper: bool = True
    loginctl_cmd: list[str] = field(default_factory=lambda: ["loginctl"])
    grab_external_input: bool = False
    # Observation mode: decide and log exactly as in a real run, but play nothing. This is how a
    # false-positive test gets measured without a siren going off in an empty flat.
    dry_run: bool = False


@dataclass
class ArmingConfig:
    # on_lock  — armed once the screen is locked (unattended machine)
    # on_still — armed once the room has been still for arm_when_still_sec (owner walked out)
    # always   — armed for the lifetime of the run
    # manual   — armed only by `camtrap arm`
    mode: str = "on_lock"
    exit_delay_sec: float = 60.0
    grace_after_unlock_sec: float = 300.0
    session_poll_sec: float = 2.0
    # on_still: how long the frame has to stay quiet before arming, and the backstop for a room
    # that never goes quiet (a curtain, a fan) so the trap still ends up armed.
    arm_when_still_sec: float = 30.0
    arm_deadline_sec: float = 300.0
    # A short, quiet burst that proves the path to the speakers actually carries audio.
    audio_probe_sec: float = 0.4
    audio_probe_volume_pct: int = 20


@dataclass
class UploadConfig:
    sinks: list[str] = field(default_factory=lambda: ["prod", "mega"])
    ssh_cmd: list[str] = field(default_factory=lambda: ["ssh"])
    ssh_target: str = ""
    ssh_key: str = ""
    ssh_options: list[str] = field(
        default_factory=lambda: [
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            # One connection reused across a burst of frames. Without this every artefact pays a
            # full handshake — 60 of them on hotel wifi — and the server starts refusing with
            # rc=255 when they arrive back to back.
            "-o",
            "ControlMaster=auto",
            "-o",
            "ControlPersist=120",
        ]
    )
    #: Multiplexing socket. %C is a hash of host/port/user, so one socket per destination.
    ssh_control_path: str = ""  # empty => <runtime dir>/camtrap-ssh-%C
    # Test/offline transport: a local directory standing in for the receiver.
    local_inbox: str = ""
    mega_dir: str = ""  # empty => ~/MEGA/camtrap
    heartbeat_sec: float = 60.0


@dataclass
class Config:
    camera: CameraConfig = field(default_factory=CameraConfig)
    detector: DetectorConfig = field(default_factory=DetectorConfig)
    event: EventConfig = field(default_factory=EventConfig)
    spool: SpoolConfig = field(default_factory=SpoolConfig)
    tamper: TamperConfig = field(default_factory=TamperConfig)
    sound: SoundConfig = field(default_factory=SoundConfig)
    arming: ArmingConfig = field(default_factory=ArmingConfig)
    upload: UploadConfig = field(default_factory=UploadConfig)
    state_dir: str = ""  # empty => data_dir()
    log_ticks: bool = False

    # --- derived paths -------------------------------------------------------

    @property
    def root(self) -> Path:
        return Path(self.state_dir) if self.state_dir else data_dir()

    @property
    def spool_dir(self) -> Path:
        return self.root / "spool"

    @property
    def sounds_dir(self) -> Path:
        return Path(self.sound.warn_dir) if self.sound.warn_dir else self.root / "sounds"

    @property
    def siren_path(self) -> Path:
        if self.sound.siren_file:
            return Path(self.sound.siren_file)
        return self.sounds_dir / "siren.ogg"

    def warn_path(self, lang: str) -> Path:
        return self.sounds_dir / f"warn-{lang}.ogg"

    @property
    def mega_dir(self) -> Path:
        return Path(self.upload.mega_dir) if self.upload.mega_dir else Path.home() / "MEGA/camtrap"

    @property
    def mode_file(self) -> Path:
        return self.root / "mode"

    @property
    def arm_file(self) -> Path:
        return self.root / "armed"


def _apply(section: Any, values: dict[str, Any], where: str) -> None:
    known = {f.name for f in fields(section)}
    for key, value in values.items():
        if key not in known:
            raise ValueError(f"unknown config key: {where}.{key}")
        setattr(section, key, value)


def load(path: Path | None = None) -> Config:
    """Load config from TOML, falling back to defaults for anything absent."""
    cfg = Config()
    target = path if path is not None else config_path()
    if not target.exists():
        return cfg
    with target.open("rb") as fh:
        raw = tomllib.load(fh)
    for key, value in raw.items():
        current = getattr(cfg, key, None)
        if is_dataclass(current) and isinstance(value, dict):
            _apply(current, value, key)
        elif hasattr(cfg, key):
            setattr(cfg, key, value)
        else:
            raise ValueError(f"unknown config section: {key}")
    return cfg

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
    # Measured on this camera: 1920x1080 costs nothing in frame rate (8.3 fps either way — the
    # limit is exposure in room light, not resolution), and a face is the whole point of the
    # evidence. 293 KB per frame at quality 95 against 88 KB at 720p/85.
    width: int = 1920
    height: int = 1080
    # The driver only offers 30 fps for MJPG at 1280x720 (10 for YUYV), so the 5 fps the spec
    # asks for is decimation in code: keep every capture_fps // target_fps-th frame.
    capture_fps: int = 30
    target_fps: int = 5
    fourcc: str = "MJPG"
    # Driver queue depth. 1 means "always give me the newest frame"; a deeper queue trades
    # latency for smoothness, which is the wrong trade for a trap.
    buffer_frames: int = 1
    reopen_delay_sec: float = 2.0
    # After this many consecutive failures the camera counts as gone — which raises a tamper
    # signal and shows up in the heartbeat. Reopening never stops (a USB glitch should self-heal);
    # 0 disables the verdict entirely, which used to be the default and meant `camera_gone` could
    # never fire at all.
    max_reopen_attempts: int = 5


@dataclass
class DetectorConfig:
    warmup_sec: float = 20.0
    # Measured in the owner's room: a curtain moving in the wind changes 0.9-1.3 % of the frame,
    # a person entering changes 10-11 %. A 0.8 % threshold therefore fired on the curtain all
    # afternoon and buried the person in the same event.
    #
    # This is the number that limits sensitivity, and lowering it is NOT free: replaying 29 real
    # captures, 3.0 % gave 1 false event out of 22 empty-room ones, 2.0 % gave 4. Lower it only
    # once `guard mask` covers the curtain — masked pixels are excluded from the percentage, which
    # is what lets the threshold drop without the curtain coming back. Calibrate per room with
    # `guard calibrate`.
    min_area_pct: float = 3.0
    #: One frame this loud is motion on its own, with no confirmation. Above every empty-room frame
    #: in the recorded captures (the loudest curtain replayed at 5.6 %) and below every event with
    #: a person in it (9.0 % and up), so waiting for a second frame only costs time. 0 disables it.
    instant_area_pct: float = 7.0
    # Confirmation is counted over a window, not as a run of consecutive frames. A person at 7 fps
    # gives a spiky mask — real captures show 11.3 % followed immediately by 0.0 % as they pass
    # behind furniture or stop — and a consecutive rule threw those away, then waited for the next
    # burst. That wait was the reported delay. Two frames out of five is the same evidence without
    # demanding they be adjacent.
    min_motion_frames: int = 2
    motion_window_frames: int = 5
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
    # One frame the moment the event opens, then one every snapshot_interval_sec for as long as
    # the event stays open. 10 s rather than 5 on the owner's instruction after the first hotel
    # run: at 5 s with the boost on, a two-minute event produced dozens of near-identical frames
    # and the album that reached Telegram was six views of the same second.
    snapshot_interval_sec: float = 10.0
    prebuffer_frames: int = 5
    prebuffer_interval_sec: float = 1.0
    event_gap_sec: float = 30.0
    max_frames_per_event: int = 60
    # 95, not 85: this frame may end up in front of a police officer looking at a face. The extra
    # 120 KB is worth more than the disk it costs.
    jpeg_quality: int = 95
    # The boost let a change well above the trigger threshold jump the throttle and take a frame
    # every boost_min_interval_sec. **0 disables it**, which is the default now: the owner asked
    # for a plain cadence — one frame at once, then one every snapshot_interval_sec — and the
    # boost is precisely what turns that cadence into a burst. Set it above 0 to get it back.
    boost_area_pct: float = 0.0
    boost_min_interval_sec: float = 1.0
    # After a tamper signal the throttle is suspended: whoever pulled the cable or pressed the
    # power button is in the room NOW, and a frame every 5 s is how you end up with a photo of
    # the door closing behind them. One frame a second for ten seconds instead.
    tamper_burst_sec: float = 10.0
    tamper_burst_interval_sec: float = 1.0
    # --- which frame is THE photo ------------------------------------------------------------
    # Frames written inside this window after the event opens are kept as evidence but do not
    # compete to lead the alert. Measured on the hotel event of 26 August: the frame that opened
    # it showed a back in a doorway at +1 s, and the face arrived at +5 s. The trigger frame is
    # the one thing a detector is guaranteed to catch late.
    key_settle_sec: float = 5.0
    # Mean luma (0-255) outside which a frame shows nothing anyone can use. On that same event the
    # frames taken before the light came on measured 5.9-14.0 and the lit ones 100-120, so this is
    # not a fine judgement — and the alert led with a black room because 99 % of its pixels had
    # changed, which is what a light coming on looks like to a background model.
    key_min_luma: float = 25.0
    key_max_luma: float = 245.0


@dataclass
class SpoolConfig:
    # Frames are ~293 KB at 1080p/q95, so a 60-frame event is ~17 MB. 1 GB holds a trip's worth.
    max_mb: int = 1024
    retention_days: int = 14
    upload_retry_max_sec: float = 300.0
    upload_retry_base_sec: float = 2.0
    #: How often the spool is drained from the run loop. The loop ticks every 250 ms and each
    #: drain lists and sorts the whole spool, so draining per tick spent the capture path's time
    #: on a queue that had not changed. Evidence does not wait on this: a tamper signal drains
    #: straight away, before the siren.
    drain_interval_sec: float = 1.0


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
    #: Which signals sound the siren. Everything else still opens a tamper event, still takes the
    #: burst of frames and still alerts — it just does it quietly.
    #:
    #: Narrowed to the two unambiguous ones on the owner's instruction after the first hotel run.
    #: `scene_shift` and `power_button_pressed` used to be in here; both fired on the owner
    #: themselves coming back into the room, and an alarm that greets you at the door is an alarm
    #: you stop arming. `camera_gone` was never in it: a USB glitch is more plausible than a hand
    #: on the cable of a built-in camera (spec section 10, item 5).
    #:
    #: The cost is stated rather than hidden: a press on the power button is the one act that can
    #: end the trap, and it is now silent. Add "power_button_pressed" back to sound on it.
    siren_signals: list[str] = field(default_factory=lambda: ["ac_offline", "lid_closed"])


@dataclass
class SoundConfig:
    # Stage 2 — the siren.
    siren_file: str = ""  # empty => data_dir()/sounds/siren.ogg
    siren_sec: float = 6.0
    siren_mode: str = "yelp"
    # A camera-shutter click before the siren. Recognisable in any country without a word of
    # language: it says "you have just been photographed", and only then does the alarm start.
    shutter_before_siren: bool = True
    shutter_file: str = ""  # empty => data_dir()/sounds/shutter.ogg
    volume_pct: int = 100
    cooldown_sec: float = 60.0
    # The cooldown is per signal, not global: closing the lid and then pulling the cable are two
    # different pieces of interference and each deserves its own alarm. This is the floor between
    # any two bursts, so a flapping sensor still cannot machine-gun the siren.
    retrigger_min_sec: float = 4.0
    max_per_event: int = 3
    max_per_hour: int = 10
    # Stage 1 — the spoken warning.
    #
    # OFF by default since the first hotel run, on the owner's instruction. The trap keeps both
    # eyes and its voice — the rendered files stay, `warn-test` still plays them, and one line
    # flips it back — but nothing speaks on motion any more. A room where the trap talks to
    # everyone who walks past it is a room where the trap gets switched off, and stage 2 is the
    # part that has to be believed.
    warn_on_motion: bool = False
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
    # Lock the screen when the trap arms. The owner has left the room; an unlocked session is a
    # way into the machine and a way to stop the agent.
    lock_on_arm: bool = True
    # Take exclusive control of the power buttons while armed. systemd-inhibit is not enough here:
    # KDE's PowerDevil holds handle-power-key in block mode and acts on the press itself (with
    # PowerButtonAction=1 it suspends, which kills the trap outright). Grabbing the evdev device
    # means neither logind nor the desktop ever sees the event. Holding the button for several
    # seconds still cuts power in hardware — nothing in software can prevent that.
    grab_power_button: bool = True
    # Observation mode: decide and log exactly as in a real run, but play nothing. This is how a
    # false-positive test gets measured without a siren going off in an empty flat.
    dry_run: bool = False


@dataclass
class ArmingConfig:
    #: How often the session lock state is read. Every read is a `loginctl` subprocess — 1.9 ms
    #: measured on this machine — and the loop ticks four times a second, so polling per tick meant
    #: ~345k spawns a day, each one able to block the loop for its 5 s timeout. A second of lag on
    #: noticing the screen lock is invisible next to a 60 s exit delay.
    lock_poll_sec: float = 1.0
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
    # The cloud copy is a warehouse, not the evidence: it exists so a full event survives if the
    # receiver is unreachable. Recompressing to 720p/q75 turns a 17 MB event into ~4 MB, which
    # matters because that folder syncs over hotel wifi. Originals stay on the receiver and in
    # the spool untouched; manifests are copied verbatim.
    mega_recompress: bool = True
    mega_width: int = 1280
    mega_quality: int = 75
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
    #: Where the agent writes its own journal. Empty means stdout only. Set by `guard` so that
    #: closing the terminal cannot break the agent's logging — or the agent.
    log_file: str = ""

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
    def shutter_path(self) -> Path:
        if self.sound.shutter_file:
            return Path(self.sound.shutter_file)
        return self.sounds_dir / "shutter.ogg"

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

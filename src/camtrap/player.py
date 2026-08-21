"""The audible response: a spoken warning on motion, a police siren on tamper.

Spec 3.4. Three rules shape this module:

* **The sink is explicit.** "Play to the default sink" is a bug on the target machine, where the
  default is a USB dongle that will not be in the hotel room and the card's active profile routes
  to headphones. The agent switches to a profile with a Speaker port and plays there.
* **A sound that a mute key silences is not a sound.** While anything plays, the whole audio path
  is re-asserted every `hold_poll_ms`.
* **Stage 1 is a notice, stage 2 is an alarm.** Motion gets the voice at 85 %; tampering gets the
  siren at 100 %, plus a session lock. Stage 1 never locks anything.
"""

from __future__ import annotations

import shlex
import subprocess
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol

from . import log
from .config import Config


class Stage(Enum):
    WARNING = "warning"
    SIREN = "siren"


class _Proc(Protocol):
    def poll(self) -> int | None: ...
    def kill(self) -> None: ...
    def wait(self, timeout: float | None = None) -> int: ...


@dataclass
class Result:
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


Runner = Callable[..., object]
Spawn = Callable[[list[str], float], _Proc]
Gate = Callable[[Stage, float], tuple[bool, str]]
AckWaiter = Callable[[float], bool]


@dataclass
class PlayCall:
    path: str
    lang: str = ""


@dataclass
class Played:
    stage: Stage | None = None
    played: bool = False
    reason: str = ""
    calls: list[PlayCall] = field(default_factory=list)
    evidence_confirmed: bool = False
    dry_run: bool = False


def _default_runner(cmd: list[str], *, timeout: float | None = None):
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _default_spawn(argv: list[str], duration: float) -> _Proc:
    return subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


class AudioPath:
    """Finds and forces the built-in speakers: profile, sink, mute, volume, auto-mute."""

    def __init__(self, cfg: Config, runner: Runner | None = None) -> None:
        self.cfg = cfg
        self._run = runner if runner is not None else _default_runner
        self._sink: str | None = None
        self._card: str | None = None
        self._prev_profile: str | None = None

    # --- discovery -----------------------------------------------------------

    def _pactl(self, *args: str):
        return self._run([*self.cfg.sound.pactl_cmd, *args])

    def _find_card_and_profile(self) -> tuple[str | None, str | None]:
        if self.cfg.sound.card:
            return self.cfg.sound.card, None
        result = self._pactl("list", "cards")
        text = getattr(result, "stdout", "") or ""
        self._prev_profile = self._active_profile(text)
        card: str | None = None
        best: tuple[str | None, str | None] = (None, None)
        profiles: list[str] = []
        has_speaker = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Card #"):
                if card and has_speaker:
                    speaker_profiles = [p for p in profiles if "Speaker" in p]
                    if speaker_profiles:
                        return card, speaker_profiles[0]
                card, profiles, has_speaker = None, [], False
            elif line.startswith("Name: "):
                card = line.removeprefix("Name: ").strip()
            elif line.startswith("[Out] Speaker"):
                has_speaker = True
            elif ": sinks:" in line or (":" in line and "priority" in line and "(" in line):
                profiles.append(line.split(":")[0].strip())
        if card and has_speaker:
            speaker_profiles = [p for p in profiles if "Speaker" in p]
            if speaker_profiles:
                return card, speaker_profiles[0]
        return best

    @staticmethod
    def _active_profile(text: str) -> str | None:
        """The profile in force before we touch anything, so a selftest can put it back."""
        current: str | None = None
        has_speaker = False
        for raw in text.splitlines():
            line = raw.strip()
            if line.startswith("Card #"):
                if has_speaker and current:
                    return current
                current, has_speaker = None, False
            elif line.startswith("Active Profile: "):
                current = line.removeprefix("Active Profile: ").strip()
            elif line.startswith("[Out] Speaker"):
                has_speaker = True
        return current if has_speaker else None

    def restore_profile(self) -> bool:
        """Undo the profile switch. Used by selftest: a check must not leave the system changed."""
        if not (self._card and self._prev_profile):
            return False
        self._pactl("set-card-profile", self._card, self._prev_profile)
        return True

    def _find_sink(self) -> str | None:
        if self.cfg.sound.sink:
            return self.cfg.sound.sink
        result = self._pactl("list", "short", "sinks")
        text = getattr(result, "stdout", "") or ""
        rows = [line.split("\t") for line in text.splitlines() if line.strip()]
        names = [row[1] for row in rows if len(row) > 1]
        for name in names:
            if "Speaker" in name:
                return name
        # No speaker sink yet: fall back to a non-USB analog output rather than the dongle.
        for name in names:
            if "usb-" not in name and "hdmi" not in name.lower():
                return name
        return names[0] if names else None

    # --- forcing -------------------------------------------------------------

    def prepare(self, *, volume_pct: int) -> str | None:
        card, profile = self._find_card_and_profile()
        if card and profile:
            self._pactl("set-card-profile", card, profile)
            self._card = card
        sink = self._find_sink()
        self._sink = sink
        if sink:
            self.reassert(volume_pct=volume_pct)
        else:
            log.emit("sound_error", reason="no sink found")
        return sink

    def reassert(self, *, volume_pct: int) -> list[str]:
        """Undo whatever a keypress just did: unmute, restore volume, keep speakers routed.

        Returns the list of things that actually had to be undone, so a drill can show that the
        defence works rather than asserting that it should.
        """
        if not self._sink:
            return []
        undone: list[str] = []
        if self._is_muted():
            undone.append("mute")
        low = self._volume_below(volume_pct)
        if low is not None:
            undone.append(f"volume:{low}")
        self._pactl("set-sink-mute", self._sink, "0")
        self._pactl("set-sink-volume", self._sink, f"{volume_pct}%")
        self._disable_auto_mute()
        if undone:
            log.emit("sound_hold", undone=",".join(undone), sink=self._sink)
        return undone

    def _is_muted(self) -> bool:
        result = self._pactl("get-sink-mute", self._sink or "")
        text = (getattr(result, "stdout", "") or "").strip().lower()
        return text.endswith("yes")

    def _volume_below(self, wanted_pct: int) -> int | None:
        """Current volume if someone turned it down below the level we asked for."""
        result = self._pactl("get-sink-volume", self._sink or "")
        text = getattr(result, "stdout", "") or ""
        for token in text.replace("/", " ").split():
            if token.endswith("%"):
                try:
                    current = int(token[:-1])
                except ValueError:
                    continue
                return current if current < wanted_pct - 2 else None
        return None

    def _disable_auto_mute(self) -> None:
        # ALSA control: with Auto-Mute enabled, a plugged jack silences the speakers.
        self._run(["amixer", "-q", "sset", "Auto-Mute Mode", "Disabled"])

    @property
    def sink(self) -> str | None:
        return self._sink


class SoundResponder:
    """Decides which stage to play, enforces the limits, and holds the sound on."""

    def __init__(
        self,
        cfg: Config,
        *,
        runner: Runner | None = None,
        spawn: Spawn | None = None,
        audio: AudioPath | None = None,
    ) -> None:
        self.cfg = cfg
        # `is not None` rather than `or`: an injected callable may be falsy (a callable list, a
        # Mock with __bool__), and silently falling back to the real player would spawn pw-play
        # inside a test run.
        self._run = runner if runner is not None else _default_runner
        self._spawn = spawn if spawn is not None else _default_spawn
        self.audio = audio if audio is not None else AudioPath(cfg, runner=self._run)
        self._gate: Gate = lambda stage, now: (True, "")
        self._ack: AckWaiter | None = None
        self._proc: _Proc | None = None
        self._playing_stage: Stage | None = None
        #: Files still to play. pw-play takes ONE file per invocation — passing several silently
        #: plays only the first, which is why the English half of the warning never sounded. So the
        #: player keeps a queue and starts the next file when the current one exits.
        self._queue: list[PlayCall] = []
        self._hold_last = 0.0
        self._last_warn = float("-inf")
        self._last_siren = float("-inf")
        self._siren_this_event = 0
        self._siren_times: deque[float] = deque()
        self._warn_times: deque[float] = deque()
        self._siren_by_signal: dict[str, float] = {}
        self._last_skip: tuple[str, str] | None = None

    # --- wiring --------------------------------------------------------------

    def set_gate(self, gate: Gate) -> None:
        """Arming/pause/warm-up policy: returns (allowed, reason) per stage."""
        self._gate = gate

    def set_ack_waiter(self, waiter: AckWaiter | None) -> None:
        """Evidence first: block up to delay_max_sec for the first frame to be acknowledged."""
        self._ack = waiter

    def end_event(self) -> None:
        self._siren_this_event = 0

    # --- stages --------------------------------------------------------------

    def on_motion(self, *, now: float) -> Played:
        return self._respond(Stage.WARNING, now=now, signals=[])

    def on_tamper(self, signals: list[str], *, now: float) -> Played:
        return self._respond(Stage.SIREN, now=now, signals=signals)

    def _fresh_signals(self, signals: list[str], now: float) -> list[str]:
        """Signals not already answered with a siren inside the cooldown window."""
        window = self.cfg.sound.cooldown_sec
        return [s for s in signals if now - self._siren_by_signal.get(s, float("-inf")) >= window]

    def _respond(self, stage: Stage, *, now: float, signals: list[str]) -> Played:
        allowed, reason = self._gate(stage, now)
        if not allowed:
            self._log_skip(stage, reason or "gate")
            return Played(stage=stage, played=False, reason=reason or "gate")

        blocked = self._limit_reason(stage, now, signals)
        if blocked:
            self._log_skip(stage, blocked)
            return Played(stage=stage, played=False, reason=blocked)

        paths = self._files(stage)
        if paths is None:
            log.emit("sound_error", stage=stage.value, reason="missing_file")
            return Played(stage=stage, played=False, reason="missing_file")

        confirmed = False
        if self._ack is not None:
            confirmed = bool(self._ack(self.cfg.sound.delay_max_sec))

        if stage is Stage.SIREN and not self.cfg.sound.dry_run:
            self._stop_current(reason="preempted_by_siren")
            if self.cfg.sound.lock_session_on_tamper:
                self._run([*self.cfg.sound.loginctl_cmd, "lock-session"])

        self._last_skip = None
        volume = (
            self.cfg.sound.volume_pct if stage is Stage.SIREN else self.cfg.sound.warn_volume_pct
        )

        if self.cfg.sound.dry_run:
            # Everything above ran for real — the gate, the limits, the file check — so the log
            # records what a live run would have done, and the room stays quiet.
            self._record(stage, now, signals)
            log.emit(
                "sound_would_play",
                stage=stage.value,
                files=len(paths),
                volume=volume,
                signals=",".join(signals) or "-",
            )
            return Played(
                stage=stage, played=True, calls=paths, evidence_confirmed=confirmed, dry_run=True
            )

        self.audio.prepare(volume_pct=volume)
        self._play(paths, stage=stage, now=now)
        self._record(stage, now, signals)

        log.emit(
            "sound",
            stage=stage.value,
            files=len(paths),
            volume=volume,
            signals=",".join(signals) or "-",
            evidence=confirmed,
        )
        return Played(stage=stage, played=True, calls=paths, evidence_confirmed=confirmed)

    def _log_skip(self, stage: Stage, reason: str) -> None:
        """Log a refusal once per (stage, reason) run. Repeating it per frame buries the journal
        and hides the lines that matter — events, drops, retries, mode changes."""
        key = (stage.value, reason)
        if key == self._last_skip:
            return
        self._last_skip = key
        log.emit("sound_skip", stage=stage.value, reason=reason)

    # --- playback ------------------------------------------------------------

    def _files(self, stage: Stage) -> list[PlayCall] | None:
        if stage is Stage.SIREN:
            path = self.cfg.siren_path
            if not path.exists():
                return None
            calls: list[PlayCall] = []
            if self.cfg.sound.shutter_before_siren and self.cfg.shutter_path.exists():
                # Shutter first: "photographed", then the alarm.
                calls.append(PlayCall(str(self.cfg.shutter_path), lang="shutter"))
            calls.append(PlayCall(str(path)))
            return calls
        calls: list[PlayCall] = []
        for lang in self.cfg.sound.warn_langs:
            path = self.cfg.warn_path(lang)
            if not path.exists():
                return None
            calls.append(PlayCall(str(path), lang=lang))
        return calls or None

    def _play(self, calls: list[PlayCall], *, stage: Stage, now: float) -> None:
        """Queue the files and start the first one.

        pw-play takes ONE file per invocation. Passing several silently played only the first,
        which is why the English half of the warning never sounded even though the log said two
        files. So the player holds a queue and hold_tick starts the next file when the current
        process exits.
        """
        self._queue = list(calls)
        self._playing_stage = stage
        self._hold_last = now
        self._start_next(stage)

    def _start_next(self, stage: Stage) -> bool:
        """Start the next queued file. Returns False when the queue is empty."""
        if not self._queue:
            self._proc = None
            self._playing_stage = None
            return False
        call = self._queue.pop(0)
        # The duration doubles as a kill timeout: a hung pw-play must not hold the stage.
        duration = (
            self.cfg.sound.siren_sec if stage is Stage.SIREN else self.cfg.sound.warn_timeout_sec
        )
        argv = list(self.cfg.sound.player_cmd)
        sink = self.audio.sink
        if sink:
            argv += ["--target", sink]
        argv.append(call.path)
        self._proc = self._spawn(argv, duration)
        self._playing_stage = stage
        return True

    def _stop_current(self, *, reason: str) -> None:
        self._queue = []
        if self._proc is not None and self._proc.poll() is None:
            self._proc.kill()
            stage = (self._playing_stage or Stage.WARNING).value
            log.emit("sound_stop", stage=stage, reason=reason)
        self._proc = None
        self._playing_stage = None

    def hold_tick(self, *, now: float) -> bool:
        """Re-assert the audio path while a sound is playing. Returns True if it acted."""
        if self._proc is None or self._playing_stage is None:
            return False
        if self._proc.poll() is not None:
            # Current file finished: move to the next one, if any.
            stage = self._playing_stage
            if stage is not None and self._start_next(stage):
                self._hold_last = now
                return True
            self._proc = None
            self._playing_stage = None
            return False
        if (now - self._hold_last) * 1000.0 < self.cfg.sound.hold_poll_ms:
            return False
        volume = (
            self.cfg.sound.volume_pct
            if self._playing_stage is Stage.SIREN
            else self.cfg.sound.warn_volume_pct
        )
        self.audio.reassert(volume_pct=volume)
        self._hold_last = now
        return True

    # --- limits --------------------------------------------------------------

    def _limit_reason(self, stage: Stage, now: float, signals: list[str] | None = None) -> str:
        if stage is Stage.SIREN:
            # A new KIND of interference is a new alarm. Closing the lid and then pulling the
            # cable used to leave the second one silent for a minute, which is exactly the
            # sequence a thief performs.
            fresh = self._fresh_signals(signals or [], now)
            floor = self.cfg.sound.retrigger_min_sec
            if fresh:
                if now - self._last_siren < floor:
                    return "retrigger_floor"
            elif now - self._last_siren < self.cfg.sound.cooldown_sec:
                return "cooldown"
            if self._siren_this_event >= self.cfg.sound.max_per_event:
                return "max_per_event"
            if self._within_hour(self._siren_times, now) >= self.cfg.sound.max_per_hour:
                return "max_per_hour"
            return ""
        if now - self._last_warn < self.cfg.sound.warn_cooldown_sec:
            return "cooldown"
        if self._within_hour(self._warn_times, now) >= self.cfg.sound.warn_max_per_hour:
            return "max_per_hour"
        return ""

    @staticmethod
    def _within_hour(times: deque[float], now: float) -> int:
        while times and now - times[0] > 3600.0:
            times.popleft()
        return len(times)

    def _record(self, stage: Stage, now: float, signals: list[str] | None = None) -> None:
        if stage is Stage.SIREN:
            self._last_siren = now
            self._siren_this_event += 1
            self._siren_times.append(now)
            for signal_name in signals or []:
                self._siren_by_signal[signal_name] = now
        else:
            self._last_warn = now
            self._warn_times.append(now)

    # --- diagnostics ---------------------------------------------------------

    def describe_command(self, stage: Stage) -> str:
        calls = self._files(stage) or []
        argv = list(self.cfg.sound.player_cmd)
        if self.audio.sink:
            argv += ["--target", self.audio.sink]
        argv += [call.path for call in calls]
        return shlex.join(argv)

    @property
    def playing(self) -> Stage | None:
        if self._proc is not None and self._proc.poll() is None:
            return self._playing_stage
        return None


def siren_path_missing(cfg: Config) -> bool:
    return not Path(cfg.siren_path).exists()

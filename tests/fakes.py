"""Fakes shared by the sound tests: a command runner and a session stand-in."""

from __future__ import annotations

from dataclasses import dataclass, field

PACTL_CARDS = """\
Card #0
\tName: alsa_card.usb-Actions_X99_PRO-01
\tActive Profile: output:analog-stereo
\tProfiles:
\t\toutput:analog-stereo: Analog Stereo Output (priority: 6500)
\tPorts:
\t\tanalog-output: Analog Output (type: Analog, priority: 9900)
Card #1
\tName: alsa_card.pci-0000_00_1f.3-platform-skl_hda_dsp_generic
\tActive Profile: HiFi (HDMI1, HDMI2, HDMI3, Headphones, Mic1, Mic2)
\tProfiles:
\t\tHiFi (HDMI1, HDMI2, HDMI3, Mic1, Mic2, Speaker): sinks: 4 (priority 3000)
\t\tHiFi (HDMI1, HDMI2, HDMI3, Headphones, Mic1, Mic2): sinks: 4 (priority 2900)
\tPorts:
\t\t[Out] Speaker: Speaker (type: Speaker, priority: 100)
\t\t[Out] HDMI1: HDMI / DisplayPort 1 Output (type: HDMI, priority: 500)
"""

_USB_SINK = "alsa_output.usb-Actions_X99_PRO-01.analog-stereo"
_CARD = "alsa_output.pci-0000_00_1f.3-platform-skl_hda_dsp_generic"
_TAIL = "PipeWire\ts32le 2ch 48000Hz\tSUSPENDED"

SINKS_HEADPHONES = (
    f"53\t{_USB_SINK}\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    f"70\t{_CARD}.HiFi__Headphones__sink\t{_TAIL}\n"
)

SINKS_SPEAKER = (
    f"53\t{_USB_SINK}\tPipeWire\ts16le 2ch 48000Hz\tRUNNING\n"
    f"88\t{_CARD}.HiFi__Speaker__sink\t{_TAIL}\n"
)


@dataclass
class Result:
    returncode: int = 0
    stdout: str = ""
    stderr: str = ""


@dataclass
class FakeRunner:
    """Records every command and answers pactl queries from canned output."""

    calls: list[list[str]] = field(default_factory=list)
    profile_set: bool = False
    fail: set[str] = field(default_factory=set)
    playing: list[str] = field(default_factory=list)

    def __call__(self, cmd: list[str], *, timeout: float | None = None) -> Result:
        self.calls.append(list(cmd))
        joined = " ".join(cmd)
        for needle in self.fail:
            if needle in joined:
                return Result(returncode=1, stderr=f"fake failure: {needle}")
        if cmd[:3] == ["pactl", "list", "cards"]:
            return Result(stdout=PACTL_CARDS)
        if cmd[:4] == ["pactl", "list", "short", "sinks"]:
            return Result(stdout=SINKS_SPEAKER if self.profile_set else SINKS_HEADPHONES)
        if cmd[:2] == ["pactl", "set-card-profile"]:
            self.profile_set = True
            return Result()
        return Result()

    # --- helpers for assertions ---------------------------------------------

    def commands(self, prefix: str) -> list[list[str]]:
        return [c for c in self.calls if " ".join(c).startswith(prefix)]

    def ran(self, needle: str) -> bool:
        return any(needle in " ".join(c) for c in self.calls)

    def count(self, needle: str) -> int:
        return sum(1 for c in self.calls if needle in " ".join(c))


@dataclass
class FakeProcess:
    """Stands in for a pw-play subprocess."""

    argv: list[str]
    duration: float
    started: float
    killed: bool = False
    _now: float = 0.0

    def poll(self) -> int | None:
        if self.killed:
            return -9
        return 0 if self._now - self.started >= self.duration else None

    def advance(self, now: float) -> None:
        self._now = now

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        return -9 if self.killed else 0

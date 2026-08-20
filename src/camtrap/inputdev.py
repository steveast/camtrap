"""Input devices that can silence a sound, and the optional exclusive grab.

The built-in keyboard reports KEY_MUTE, KEY_VOLUMEDOWN and KEY_POWER from the same device as the
letters, so a grab of *that* device would take away the owner's only way to type the unlock
password. Only external devices are ever grabbed, and only for the duration of a burst; the kernel
releases a grab when the descriptor closes, so killing the agent cannot leave input captured.
"""

from __future__ import annotations

import contextlib
import fcntl
import os
from dataclasses import dataclass
from pathlib import Path

from . import log

KEY_MUTE = 113
KEY_VOLUMEDOWN = 114
KEY_VOLUMEUP = 115
KEY_POWER = 116
KEY_A = 30

EVIOCGRAB = 0x40044590

SYSFS_INPUT = "/sys/class/input"


@dataclass
class InputDevice:
    event: str
    name: str
    keys: frozenset[int]
    #: Where the character device lives. A field rather than a constant so tests never open the
    #: real /dev/input — the owner is in the `input` group, so a stray open would actually grab.
    dev_root: str = "/dev/input"

    @property
    def path(self) -> str:
        return str(Path(self.dev_root) / self.event)

    @property
    def can_mute(self) -> bool:
        return bool(self.keys & {KEY_MUTE, KEY_VOLUMEDOWN, KEY_POWER})

    @property
    def is_keyboard(self) -> bool:
        """A device that types letters: never grabbed, it is the way back in."""
        return KEY_A in self.keys


def _parse_bitmap(text: str) -> frozenset[int]:
    bits = 0
    for index, word in enumerate(reversed(text.split())):
        try:
            bits |= int(word, 16) << (64 * index)
        except ValueError:
            return frozenset()
    return frozenset(bit for bit in range(bits.bit_length()) if bits >> bit & 1)


def scan(sysfs_root: str = SYSFS_INPUT, dev_root: str = "/dev/input") -> list[InputDevice]:
    """List input devices that report mute, volume or power keys."""
    devices: list[InputDevice] = []
    root = Path(sysfs_root)
    if not root.exists():
        return devices
    for entry in sorted(root.glob("event*")):
        device_dir = entry / "device"
        try:
            name = (device_dir / "name").read_text().strip()
            caps = (device_dir / "capabilities" / "key").read_text().strip()
        except OSError:
            continue
        keys = _parse_bitmap(caps)
        device = InputDevice(event=entry.name, name=name, keys=keys, dev_root=dev_root)
        if device.can_mute:
            devices.append(device)
    return devices


def grabbable(devices: list[InputDevice]) -> list[InputDevice]:
    """External devices only — never the keyboard the owner unlocks with."""
    return [d for d in devices if d.can_mute and not d.is_keyboard]


class Grab:
    """Exclusive grab over a set of devices, released on close (and therefore on kill -9)."""

    def __init__(self, devices: list[InputDevice]) -> None:
        self._devices = devices
        self._fds: list[int] = []

    def __enter__(self) -> Grab:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    def acquire(self) -> int:
        for device in self._devices:
            try:
                fd = os.open(device.path, os.O_RDONLY | os.O_NONBLOCK)
                fcntl.ioctl(fd, EVIOCGRAB, 1)
            except OSError as exc:
                log.emit("input_grab_failed", device=device.event, error=str(exc))
                continue
            self._fds.append(fd)
            log.emit("input_grab", device=device.event, name=device.name)
        return len(self._fds)

    def release(self) -> None:
        for fd in self._fds:
            with contextlib.suppress(OSError):
                fcntl.ioctl(fd, EVIOCGRAB, 0)
            os.close(fd)
        self._fds = []

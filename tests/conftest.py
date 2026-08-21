import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from camtrap import config as config_mod  # noqa: E402


@pytest.fixture
def cfg(tmp_path):
    """A Config with every path pointed inside tmp_path — no real sysfs, no real audio."""
    c = config_mod.Config()
    c.state_dir = str(tmp_path / "state")
    c.sound.warn_dir = str(tmp_path / "sounds")
    c.sound.siren_file = str(tmp_path / "sounds" / "siren.ogg")
    c.upload.local_inbox = str(tmp_path / "inbox")
    c.upload.mega_dir = str(tmp_path / "mega")
    (tmp_path / "sounds").mkdir(parents=True, exist_ok=True)
    (tmp_path / "state").mkdir(parents=True, exist_ok=True)
    # Never touch the real machine from a test: arming would otherwise lock the developer's screen
    # and take an exclusive grab of the actual power buttons. Tests that exercise those switch
    # them on explicitly.
    c.sound.lock_on_arm = False
    c.sound.grab_power_button = False
    return c


@pytest.fixture
def sysfs(tmp_path):
    """Fake sysfs: writable files standing in for power, lid and ALS."""

    class Fake:
        def __init__(self, root: Path):
            self.root = root
            self.root.mkdir(parents=True, exist_ok=True)
            self.ac = root / "ac_online"
            self.usbc1 = root / "usbc1_online"
            self.lid = root / "lid_state"
            self.als0 = root / "als0"
            self.als1 = root / "als1"
            self.set_ac(1)
            self.set_usbc(0)
            self.set_lid("open")
            self.set_als(3000)

        def set_ac(self, value: int) -> None:
            self.ac.write_text(f"{value}\n")

        def set_usbc(self, value: int) -> None:
            self.usbc1.write_text(f"{value}\n")

        def set_lid(self, state: str) -> None:
            self.lid.write_text(f"state:      {state}\n")

        def set_als(self, value: int) -> None:
            self.als0.write_text(f"{value}\n")
            self.als1.write_text(f"{value}\n")

    return Fake(tmp_path / "sysfs")

"""S1.7: which devices can silence a sound, and which may never be grabbed."""

from camtrap import inputdev
from camtrap.inputdev import KEY_A, KEY_MUTE, KEY_POWER, KEY_VOLUMEDOWN, InputDevice


def _make_device(tmp_path, event, name, keys):
    device = tmp_path / event / "device"
    (device / "capabilities").mkdir(parents=True)
    (device / "name").write_text(name + "\n")
    bits = 0
    for key in keys:
        bits |= 1 << key
    words = []
    while bits:
        words.append(f"{bits & 0xFFFFFFFFFFFFFFFF:x}")
        bits >>= 64
    (device / "capabilities" / "key").write_text(" ".join(reversed(words)) + "\n")


def test_scan_lists_only_devices_that_can_silence(tmp_path):
    _make_device(tmp_path, "event0", "Built-in keyboard", [KEY_A, KEY_MUTE, KEY_POWER])
    _make_device(tmp_path, "event1", "Dongle Consumer Control", [KEY_MUTE, KEY_VOLUMEDOWN])
    _make_device(tmp_path, "event2", "Touchpad", [])
    found = {d.event: d for d in inputdev.scan(str(tmp_path))}
    assert set(found) == {"event0", "event1"}
    assert found["event0"].is_keyboard
    assert not found["event1"].is_keyboard


def test_the_builtin_keyboard_is_never_grabbable(tmp_path):
    _make_device(tmp_path, "event0", "AT Translated Set 2 keyboard", [KEY_A, KEY_MUTE, KEY_POWER])
    _make_device(tmp_path, "event1", "Dongle Consumer Control", [KEY_MUTE])
    grabbable = inputdev.grabbable(inputdev.scan(str(tmp_path)))
    assert [d.event for d in grabbable] == ["event1"]


def test_unreadable_device_is_skipped(tmp_path):
    (tmp_path / "event9").mkdir()
    assert inputdev.scan(str(tmp_path)) == []


def test_missing_sysfs_root_is_not_an_error():
    assert inputdev.scan("/nonexistent/input") == []


def test_grab_reports_failures_without_raising(tmp_path, capsys):
    device = InputDevice(
        event="event0", name="ghost", keys=frozenset({KEY_MUTE}), dev_root=str(tmp_path / "dev")
    )
    grab = inputdev.Grab([device])
    assert grab.acquire() == 0  # /dev/input/event0 not openable in the test environment
    grab.release()
    assert "input_grab_failed" in capsys.readouterr().out

"""S1.1: tamper signals from fake sysfs (spec 3.3, plan S1.1).

Judged on being *held*, not on the instant: a 1->0->1 bounce between polls must not fire, which
is the same discipline the external prober uses. USB-C PD ports flap on their own.
"""

import pytest

from camtrap.tamper import Signal, TamperMonitor


@pytest.fixture
def monitor(cfg, sysfs):
    cfg.tamper.ac_online_paths = [str(sysfs.ac), str(sysfs.usbc1)]
    cfg.tamper.lid_state_path = str(sysfs.lid)
    cfg.tamper.als_paths = [str(sysfs.als0), str(sysfs.als1)]
    cfg.tamper.debounce_sec = 1.0
    return TamperMonitor(cfg)


def test_no_signals_while_nothing_changes(monitor):
    assert monitor.poll(now=0.0) == []
    assert monitor.poll(now=1.0) == []
    assert monitor.poll(now=2.0) == []


def test_cable_pull_fires_once_after_debounce(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_ac(0)
    # seen but not yet held long enough
    assert monitor.poll(now=0.5) == []
    fired = monitor.poll(now=1.6)
    assert [s.name for s in fired] == ["ac_offline"]
    # held further: no repeats, the event already happened
    assert monitor.poll(now=3.0) == []
    assert monitor.poll(now=10.0) == []


def test_bounce_between_polls_does_not_fire(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_ac(0)
    assert monitor.poll(now=0.4) == []
    sysfs.set_ac(1)  # PD port flapped back before the debounce elapsed
    assert monitor.poll(now=1.5) == []
    assert monitor.poll(now=2.5) == []


def test_replugging_does_not_fire_and_rearms(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_ac(0)
    monitor.poll(now=0.5)
    assert [s.name for s in monitor.poll(now=1.6)] == ["ac_offline"]
    sysfs.set_ac(1)  # cable back in: not a tamper signal
    assert monitor.poll(now=2.6) == []
    assert monitor.poll(now=3.6) == []
    # and a second pull fires again
    sysfs.set_ac(0)
    monitor.poll(now=4.0)
    assert [s.name for s in monitor.poll(now=5.1)] == ["ac_offline"]


def test_power_present_while_any_source_is_online(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_ac(0)
    sysfs.set_usbc(1)  # charging over USB-C instead of the barrel jack
    assert monitor.poll(now=1.0) == []
    assert monitor.poll(now=2.0) == []


def test_lid_close_fires_and_reopen_does_not(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_lid("closed")
    monitor.poll(now=0.5)
    fired = monitor.poll(now=1.6)
    assert [s.name for s in fired] == ["lid_closed"]
    sysfs.set_lid("open")
    assert monitor.poll(now=3.0) == []


def test_two_signals_at_once_are_both_reported(monitor, sysfs):
    monitor.poll(now=0.0)
    sysfs.set_ac(0)
    sysfs.set_lid("closed")
    monitor.poll(now=0.5)
    names = sorted(s.name for s in monitor.poll(now=1.6))
    assert names == ["ac_offline", "lid_closed"]


def test_missing_paths_are_tolerated_not_fatal(cfg, tmp_path):
    cfg.tamper.ac_online_paths = [str(tmp_path / "nope")]
    cfg.tamper.lid_state_path = str(tmp_path / "also-nope")
    cfg.tamper.als_paths = []
    monitor = TamperMonitor(cfg)
    assert monitor.poll(now=0.0) == []
    assert monitor.read_als() is None


def test_als_average_is_read(monitor, sysfs):
    sysfs.set_als(1500)
    assert monitor.read_als() == pytest.approx(1500.0)


def test_external_signal_is_debounce_free(monitor):
    # camera disappearance is reported by the capture loop, which already knows it is real
    fired = monitor.report_external("camera_gone", detail="no frames for 5s", now=1.0)
    assert fired == Signal("camera_gone", "no frames for 5s")
    assert monitor.poll(now=2.0) == []

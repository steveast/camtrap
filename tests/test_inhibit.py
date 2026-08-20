"""S1.4: the sleep inhibitor must cover lid and power key, or the trap dies on a closed lid."""

from camtrap.inhibit import WHAT, Inhibitor


def test_inhibit_covers_lid_power_sleep_and_idle():
    assert "handle-lid-switch" in WHAT
    assert "handle-power-key" in WHAT
    assert "sleep" in WHAT
    assert "idle" in WHAT


def test_inhibitor_starts_and_stops(monkeypatch):
    started = {}

    class FakeProc:
        def __init__(self, argv, **kwargs):
            started["argv"] = argv
            self._alive = True

        def poll(self):
            return None if self._alive else 0

        def terminate(self):
            self._alive = False

        def wait(self, timeout=None):
            return 0

        def kill(self):
            self._alive = False

    monkeypatch.setattr("camtrap.inhibit.subprocess.Popen", FakeProc)
    inhibitor = Inhibitor()
    assert inhibitor.start()
    assert inhibitor.active
    assert "--mode=block" in started["argv"]
    assert any("handle-power-key" in arg for arg in started["argv"])
    inhibitor.stop()
    assert not inhibitor.active


def test_missing_systemd_inhibit_is_reported_not_fatal(monkeypatch):
    def boom(*args, **kwargs):
        raise OSError("no systemd-inhibit")

    monkeypatch.setattr("camtrap.inhibit.subprocess.Popen", boom)
    assert Inhibitor().start() is False

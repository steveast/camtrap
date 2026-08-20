"""S3.2/S3.3/S3.4: delivery, acknowledgement, and what may never free a frame (spec 3.5, 7)."""

from pathlib import Path

import pytest

from camtrap.spool import Spool
from camtrap.uploader import LocalSink, MegaSink, ProdSink, Uploader


def _write(cfg, name, size=256):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    path = cfg.spool_dir / name
    path.write_bytes(b"x" * size)
    return path


@pytest.fixture
def spool(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    return Spool(cfg)


@pytest.fixture
def local(cfg):
    return LocalSink(cfg, Path(cfg.upload.local_inbox))


def test_acknowledged_frame_leaves_the_spool(cfg, spool, local):
    _write(cfg, "evt_A_000.jpg")
    report = Uploader(cfg, spool, sinks=[local]).drain()
    assert report.acknowledged == ["evt_A_000.jpg"]
    assert spool.depth() == 0
    assert (Path(cfg.upload.local_inbox) / "evt_A_000.jpg").exists()


def test_an_unreachable_sink_never_trims_the_spool(cfg, spool, local):
    _write(cfg, "evt_A_000.jpg")
    _write(cfg, "evt_A_001.jpg")
    local.available = False
    report = Uploader(cfg, spool, sinks=[local]).drain()
    assert report.acknowledged == []
    assert spool.depth() == 2, "frames must survive an outage"


def test_everything_drains_once_the_link_is_back(cfg, spool, local):
    for index in range(4):
        _write(cfg, f"evt_A_{index:03d}.jpg")
    uploader = Uploader(cfg, spool, sinks=[local])
    local.available = False
    uploader.drain(now=0.0)
    assert spool.depth() == 4
    local.available = True
    report = uploader.drain(now=1000.0)  # past the backoff
    assert len(report.acknowledged) == 4
    assert spool.depth() == 0


def test_a_cloud_copy_alone_never_frees_a_frame(cfg, spool):
    _write(cfg, "evt_A_000.jpg")
    report = Uploader(cfg, spool, sinks=[MegaSink(cfg)]).drain()
    assert report.copied == ["evt_A_000.jpg"]
    assert report.acknowledged == []
    assert spool.depth() == 1
    assert (cfg.mega_dir / "evt_A_000.jpg").exists()


def test_prod_failure_does_not_stop_the_cloud_copy(cfg, spool, local):
    _write(cfg, "evt_A_000.jpg")
    local.available = False
    Uploader(cfg, spool, sinks=[local, MegaSink(cfg)]).drain()
    assert (cfg.mega_dir / "evt_A_000.jpg").exists()
    assert spool.depth() == 1


def test_a_missing_cloud_folder_does_not_stop_delivery(cfg, spool, local, monkeypatch):
    _write(cfg, "evt_A_000.jpg")
    mega = MegaSink(cfg)
    monkeypatch.setattr(
        type(mega),
        "send",
        lambda self, path: __import__("camtrap.uploader", fromlist=["x"]).SinkResult(
            False, "no folder"
        ),
    )
    report = Uploader(cfg, spool, sinks=[local, mega]).drain()
    assert report.acknowledged == ["evt_A_000.jpg"]
    assert spool.depth() == 0


def test_local_sink_writes_the_heartbeat_where_the_poller_looks(cfg, local):
    """Receiver layout: frames in inbox/, status in state/ — the poller reads state/heartbeat."""
    assert local.heartbeat("mode=armed sound_ok=1\n").ok
    inbox = Path(cfg.upload.local_inbox)
    assert (inbox.parent / "state" / "heartbeat").exists()
    assert not (inbox / "heartbeat").exists()


def test_priority_order_is_respected(cfg, spool, local):
    _write(cfg, "evt_A_003.jpg")
    _write(cfg, "evt_A_000.jpg")
    _write(cfg, "evt_A.json")
    spool.mark_tamper("evt_B")
    _write(cfg, "evt_B_000.jpg")
    report = Uploader(cfg, spool, sinks=[local]).drain()
    assert report.sent[0] == "evt_B_000.jpg"
    assert report.sent[1] == "evt_A.json"
    assert report.sent[2] == "evt_A_000.jpg"


def test_backoff_grows_and_caps(cfg, spool, local):
    _write(cfg, "evt_A_000.jpg")
    cfg.spool.upload_retry_base_sec = 2.0
    cfg.spool.upload_retry_max_sec = 10.0
    uploader = Uploader(cfg, spool, sinks=[local])
    local.available = False
    for attempt in range(6):
        uploader._next_attempt = 0.0  # pretend the wait elapsed
        uploader.drain(now=float(attempt))
    assert uploader._failures == 6
    uploader._note_failure(now=0.0)
    assert uploader._next_attempt <= 10.0


def test_mid_batch_failure_resumes_without_duplicates(cfg, spool):
    for index in range(4):
        _write(cfg, f"evt_A_{index:03d}.jpg")

    class Flaky:
        name = "prod"

        def __init__(self):
            self.sent = []
            self.fail_after = 2

        def send(self, path):
            from camtrap.uploader import SinkResult

            if len(self.sent) >= self.fail_after:
                return SinkResult(False, "link dropped")
            self.sent.append(path.name)
            return SinkResult(True, "stored", acknowledged=True)

        def heartbeat(self, line):
            from camtrap.uploader import SinkResult

            return SinkResult(True)

    sink = Flaky()
    uploader = Uploader(cfg, spool, sinks=[sink])
    uploader.drain(now=0.0)
    assert spool.depth() == 2
    sink.fail_after = 99
    uploader.drain(now=1000.0)
    assert spool.depth() == 0
    assert len(sink.sent) == 4
    assert len(set(sink.sent)) == 4, "no frame is sent twice"


# --- the ssh sink parses the receiver's reply rather than trusting the exit code ---------------


class FakeSsh:
    def __init__(self, reply=b"", returncode=0):
        self.reply = reply
        self.returncode = returncode
        self.calls = []

    def __call__(self, argv, *, payload, timeout):
        self.calls.append((argv, payload))

        class R:
            pass

        r = R()
        r.returncode = self.returncode
        r.stdout = self.reply
        r.stderr = b""
        return r


def test_prod_sink_accepts_a_matching_reply(cfg):
    from hashlib import sha256

    path = _write(cfg, "evt_A_000.jpg", size=8)
    digest = sha256(path.read_bytes()).hexdigest()
    ssh = FakeSsh(reply=f"ok evt_A_000.jpg 8 {digest}".encode())
    cfg.upload.ssh_target = "user@vps"
    assert ProdSink(cfg, runner=ssh).send(path).acknowledged


def test_prod_sink_rejects_a_size_mismatch(cfg):
    from hashlib import sha256

    path = _write(cfg, "evt_A_000.jpg", size=8)
    digest = sha256(path.read_bytes()).hexdigest()
    ssh = FakeSsh(reply=f"ok evt_A_000.jpg 4 {digest}".encode())
    cfg.upload.ssh_target = "user@vps"
    result = ProdSink(cfg, runner=ssh).send(path)
    assert not result.acknowledged and "size mismatch" in result.detail


def test_prod_sink_rejects_a_checksum_mismatch(cfg):
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"ok evt_A_000.jpg 8 " + b"0" * 64)
    cfg.upload.ssh_target = "user@vps"
    result = ProdSink(cfg, runner=ssh).send(path)
    assert not result.acknowledged and "checksum" in result.detail


def test_prod_sink_rejects_a_silent_success(cfg):
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"")  # rc=0 but nothing stored
    cfg.upload.ssh_target = "user@vps"
    result = ProdSink(cfg, runner=ssh).send(path)
    assert not result.acknowledged and "unexpected reply" in result.detail


def test_ssh_argv_carries_key_and_options(cfg):
    path = _write(cfg, "evt_A_000.jpg", size=8)
    cfg.upload.ssh_target = "user@vps"
    cfg.upload.ssh_key = "/home/x/.ssh/camtrap"
    ssh = FakeSsh(reply=b"")
    ProdSink(cfg, runner=ssh).send(path)
    argv = ssh.calls[0][0]
    assert "-i" in argv and "/home/x/.ssh/camtrap" in argv
    assert "BatchMode=yes" in argv
    assert argv[-1] == "put-frame evt_A_000.jpg"


# --- a sink is untrusted code: it must never end the run ---------------------------------------


def test_a_broken_inbox_is_reported_not_raised(cfg, spool, tmp_path):
    """A plain file where the inbox directory should be: report it, do not raise."""
    blocked = tmp_path / "blocked-inbox"
    blocked.write_text("not a directory")
    _write(cfg, "evt_A_000.jpg")
    sink = LocalSink(cfg, blocked)
    result = sink.send(cfg.spool_dir / "evt_A_000.jpg")
    assert not result.ok and "unusable" in result.detail
    assert spool.depth() == 1, "the frame stays until someone acknowledges it"


def test_a_sink_that_raises_does_not_stop_the_drain(cfg, spool, local):
    _write(cfg, "evt_A_000.jpg")

    class Exploding:
        name = "mega"

        def send(self, path):
            raise RuntimeError("boom")

        def heartbeat(self, line):
            raise RuntimeError("boom")

    report = Uploader(cfg, spool, sinks=[Exploding(), local]).drain()
    # the working sink still delivered, and the frame was freed by its acknowledgement
    assert report.acknowledged == ["evt_A_000.jpg"]
    assert spool.depth() == 0


def test_an_exploding_sink_is_logged_by_name(cfg, spool, capsys):
    _write(cfg, "evt_A_000.jpg")

    class Exploding:
        name = "prod"

        def send(self, path):
            raise OSError("disk on fire")

        def heartbeat(self, line):
            raise OSError("disk on fire")

    uploader = Uploader(cfg, spool, sinks=[Exploding()])
    uploader.drain()
    out = capsys.readouterr().out
    assert "upload_failed" in out and "OSError" in out
    assert spool.depth() == 1


def test_heartbeat_survives_an_exploding_sink(cfg, spool, capsys):
    class Exploding:
        name = "prod"

        def send(self, path):
            raise RuntimeError("no")

        def heartbeat(self, line):
            raise RuntimeError("no")

    assert Uploader(cfg, spool, sinks=[Exploding()]).heartbeat("x=1\n") is False
    assert "heartbeat_error" in capsys.readouterr().out

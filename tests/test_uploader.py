"""S3.2/S3.3/S3.4: delivery, acknowledgement, and what may never free a frame (spec 3.5, 7)."""

from pathlib import Path

import pytest

from camtrap.spool import Spool
from camtrap.uploader import LocalSink, MegaSink, ProdSink, TelegramSink, Uploader


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


def test_a_dead_receiver_does_not_starve_the_warehouse(cfg, spool, local):
    """The whole queue reaches the cloud copy, not just the artefact prod choked on.

    Seen for real: an afternoon of 15 events, prod answering rc=127 to every put, and the
    warehouse holding exactly one manifest — the head of the queue — because the pass stopped at
    the first failure. The sink that exists for an unreachable receiver was disabled by the
    receiver being unreachable.
    """
    for index in range(4):
        _write(cfg, f"evt_A_{index:03d}.jpg")
    local.available = False
    Uploader(cfg, spool, sinks=[local, MegaSink(cfg)]).drain()
    copied = sorted(path.name for path in cfg.mega_dir.iterdir())
    assert copied == [f"evt_A_{index:03d}.jpg" for index in range(4)]
    assert spool.depth() == 4, "a cloud copy is still not an acknowledgement"


def test_the_warehouse_keeps_filling_while_prod_sits_in_backoff(cfg, spool, local):
    """Backoff is the receiver's, not the queue's: five quiet minutes must not cost five minutes
    of warehouse copies."""
    _write(cfg, "evt_A_000.jpg")
    local.available = False
    uploader = Uploader(cfg, spool, sinks=[local, MegaSink(cfg)])
    uploader.drain(now=0.0)  # prod fails, backoff starts
    _write(cfg, "evt_A_001.jpg")
    report = uploader.drain(now=1.0)  # still inside the backoff window
    assert report.copied == ["evt_A_001.jpg"]
    assert (cfg.mega_dir / "evt_A_001.jpg").exists()


def test_a_copied_frame_is_not_copied_again(cfg, spool, local):
    """Recompression is not free, and an outage lasts thousands of ticks."""
    _write(cfg, "evt_A_000.jpg")
    local.available = False
    uploader = Uploader(cfg, spool, sinks=[local, MegaSink(cfg)])
    assert uploader.drain(now=0.0).copied == ["evt_A_000.jpg"]
    assert uploader.drain(now=1000.0).copied == [], "already in the warehouse"


def test_a_frame_prod_was_never_asked_about_is_not_reported_failed(cfg, spool):
    """`failed` is what the tamper path waits on. A sink that was never asked cannot be the
    reason a siren is held back."""
    _write(cfg, "evt_A_000.jpg")
    report = Uploader(cfg, spool, sinks=[MegaSink(cfg)]).drain()
    assert report.copied == ["evt_A_000.jpg"]
    assert report.failed == [], "nothing here can acknowledge, so nothing here can be pending"


def test_the_batch_budget_counts_work_not_files(cfg, spool, local):
    """A queue whose head is already in the warehouse must still advance."""
    for index in range(4):
        _write(cfg, f"evt_A_{index:03d}.jpg")
    local.available = False
    mega = MegaSink(cfg)
    uploader = Uploader(cfg, spool, sinks=[local, mega])
    assert len(uploader.drain(now=0.0, limit=2).copied) == 2
    # The first two are done; the same limit must reach the two behind them, not re-examine them.
    assert sorted(uploader.drain(now=1.0, limit=2).copied) == ["evt_A_002.jpg", "evt_A_003.jpg"]


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


def test_ssh_offers_the_restricted_key_and_no_other(cfg):
    """`-i` alone is not a restriction, and the difference is a whole run's evidence.

    An admin key for the same host in the desktop's ssh-agent is offered first, gets a normal
    shell instead of the forced command, and turns every verb into rc=127 — with the frames
    piling up in the spool because nothing is ever acknowledged. Seen for real: 197 artefacts,
    fourteen retries, none delivered.
    """
    cfg.upload.ssh_target = "user@vps"
    cfg.upload.ssh_key = "/home/x/.ssh/camtrap"
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"")
    ProdSink(cfg, runner=ssh).send(path)
    argv = " ".join(ssh.calls[0][0])
    assert "IdentitiesOnly=yes" in argv, "-i must be the only identity, not merely one of them"
    assert "IdentityAgent=none" in argv, "the trap must not be able to reach an admin key"


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


def test_ssh_reuses_one_connection_for_a_burst(cfg, monkeypatch):
    """60 frames must not mean 60 handshakes: the server starts refusing with rc=255."""
    monkeypatch.setenv("XDG_RUNTIME_DIR", "/run/user/1000")
    cfg.upload.ssh_target = "user@vps"
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"")
    ProdSink(cfg, runner=ssh).send(path)
    argv = " ".join(ssh.calls[0][0])
    assert "ControlMaster=auto" in argv
    assert "ControlPersist=120" in argv
    assert "ControlPath=/run/user/1000/camtrap-ssh-%C" in argv


def test_an_explicit_control_path_is_honoured(cfg, monkeypatch):
    cfg.upload.ssh_target = "user@vps"
    cfg.upload.ssh_control_path = "/tmp/my-socket-%C"
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"")
    ProdSink(cfg, runner=ssh).send(path)
    assert "ControlPath=/tmp/my-socket-%C" in " ".join(ssh.calls[0][0])


def test_no_control_path_when_multiplexing_is_disabled(cfg):
    cfg.upload.ssh_target = "user@vps"
    cfg.upload.ssh_options = ["-o", "BatchMode=yes"]
    path = _write(cfg, "evt_A_000.jpg", size=8)
    ssh = FakeSsh(reply=b"")
    ProdSink(cfg, runner=ssh).send(path)
    assert "ControlPath" not in " ".join(ssh.calls[0][0])


# --- the cloud copy is a warehouse, not the evidence ------------------------------------------


def _real_jpeg(path, width=1920, height=1080, quality=95):
    import cv2
    import numpy as np

    rng = np.random.default_rng(3)
    frame = rng.integers(0, 255, size=(height, width, 3), dtype=np.uint8)
    cv2.imwrite(str(path), frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    return path


def test_the_cloud_copy_is_recompressed(cfg):
    """17 MB an event over hotel wifi is not a warehouse, it is a bottleneck."""
    import cv2

    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    source = _real_jpeg(cfg.spool_dir / "evt_A_000.jpg")
    original_bytes = source.stat().st_size

    assert MegaSink(cfg).send(source).ok
    copy = cfg.mega_dir / "evt_A_000.jpg"
    assert copy.exists()
    # Random-noise fixtures barely compress, so assert the geometry and that it did not grow.
    assert cv2.imread(str(copy)).shape[1] == cfg.upload.mega_width
    assert copy.stat().st_size <= original_bytes
    # and the original is untouched
    assert source.stat().st_size == original_bytes
    assert cv2.imread(str(source)).shape[1] == 1920


def test_a_cloud_folder_pointing_at_the_spool_is_refused(cfg):
    """Recompressing in place would destroy the evidence-grade original."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    cfg.upload.mega_dir = str(cfg.spool_dir)
    source = _real_jpeg(cfg.spool_dir / "evt_A_000.jpg")
    before = source.stat().st_size
    result = MegaSink(cfg).send(source)
    assert not result.ok and "spool" in result.detail
    assert source.stat().st_size == before


def test_manifests_are_copied_verbatim(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    manifest = cfg.spool_dir / "evt_A.json"
    manifest.write_text('{"type":"tamper"}')
    assert MegaSink(cfg).send(manifest).ok
    assert (cfg.mega_dir / "evt_A.json").read_text() == '{"type":"tamper"}'


def test_recompression_can_be_switched_off(cfg):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    cfg.upload.mega_recompress = False
    source = _real_jpeg(cfg.spool_dir / "evt_A_000.jpg")
    MegaSink(cfg).send(source)
    assert (cfg.mega_dir / "evt_A_000.jpg").stat().st_size == source.stat().st_size


def test_an_unreadable_frame_still_gets_copied(cfg):
    """Better a verbatim copy than no copy: this sink is the fallback for a dead receiver."""
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    broken = cfg.spool_dir / "evt_A_001.jpg"
    broken.write_bytes(b"not really a jpeg")
    assert MegaSink(cfg).send(broken).ok
    assert (cfg.mega_dir / "evt_A_001.jpg").read_bytes() == b"not really a jpeg"


def test_the_cloud_copy_still_never_acknowledges(cfg, spool):
    cfg.spool_dir.mkdir(parents=True, exist_ok=True)
    _real_jpeg(cfg.spool_dir / "evt_A_000.jpg")
    report = Uploader(cfg, spool, sinks=[MegaSink(cfg)]).drain()
    assert report.copied and not report.acknowledged
    assert spool.depth() == 1


# --- the clip path: from this laptop to the chat ------------------------------------------------


def _creds(cfg, *, mode=0o600, token="123:ABC", chat="42"):
    path = cfg.telegram_env_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat}\n")
    path.chmod(mode)
    return path


class FakeCurl:
    """Records what curl was asked to do, and what it was told on stdin."""

    def __init__(self, *, body='{"ok":true,"result":{}}', code=0, stderr=""):
        self.calls: list[tuple[list[str], str]] = []
        self.body = body
        self.code = code
        self.stderr = stderr

    def __call__(self, argv, *, payload, timeout):
        self.calls.append((argv, payload))

        class Result:
            returncode = self.code
            stdout = self.body
            stderr = self.stderr

        return Result()


@pytest.fixture
def tg(cfg, tmp_path):
    cfg.video.telegram_env_file = str(tmp_path / "telegram.env")
    return cfg


def test_a_clip_segment_is_sent_and_acknowledged(tg, spool):
    _creds(tg)
    path = _write(tg, "evt_A_v000.mp4", size=4096)
    curl = FakeCurl()
    result = TelegramSink(tg, runner=curl).send(path)
    assert result.ok and result.acknowledged, result.detail
    _argv, config = curl.calls[0]
    assert "sendVideo" in config
    assert f"video=@{path}" in config
    assert 'form = "chat_id=42"' in config


def test_the_token_never_appears_in_argv(tg):
    """/proc is world-readable, so an argv is published to every process on this machine.

    Putting the bot token in a URL on the command line would hand it to any local account — which
    would be a second, avoidable leak on top of the one the owner deliberately accepted by moving
    the token onto a machine assumed to be stolen.
    """
    _creds(tg, token="SECRET123:TOKEN")
    path = _write(tg, "evt_A_v000.mp4", size=1024)
    curl = FakeCurl()
    TelegramSink(tg, runner=curl).send(path)
    argv, config = curl.calls[0]
    assert not any("SECRET123" in arg for arg in argv), argv
    assert argv[-2:] == ["--config", "-"], "the token arrives on stdin"
    assert "SECRET123" in config, "and it does arrive"


def test_a_world_readable_token_file_is_refused(tg):
    """The way ssh refuses a group-readable key, and for the same reason."""
    _creds(tg, mode=0o644)
    path = _write(tg, "evt_A_v000.mp4")
    curl = FakeCurl()
    result = TelegramSink(tg, runner=curl).send(path)
    assert not result.ok
    assert "readable by others" in result.detail
    assert not curl.calls, "nothing is sent with a token anyone could have read"


def test_a_missing_token_file_says_which_file(tg):
    path = _write(tg, "evt_A_v000.mp4")
    result = TelegramSink(tg, runner=FakeCurl()).send(path)
    assert not result.ok and "telegram.env" in result.detail


def test_a_photograph_is_never_sent_to_the_chat_from_the_laptop(tg):
    """Photographs go to a receiver this machine cannot delete from. That is the whole point."""
    _creds(tg)
    path = _write(tg, "evt_A_000.jpg")
    curl = FakeCurl()
    result = TelegramSink(tg, runner=curl).send(path)
    assert not result.ok and "clips only" in result.detail
    assert not curl.calls


def test_an_oversized_clip_is_refused_before_the_uplink_pays_for_it(tg):
    _creds(tg)
    tg.video.telegram_max_mb = 1
    path = _write(tg, "evt_A_v000.mp4", size=2 * 1024 * 1024)
    curl = FakeCurl()
    result = TelegramSink(tg, runner=curl).send(path)
    assert not result.ok and "cap" in result.detail
    assert not curl.calls, "Telegram would refuse it at 50 MB; do not spend a hotel uplink on that"


def test_a_refusal_from_telegram_is_not_an_acknowledgement(tg):
    _creds(tg)
    path = _write(tg, "evt_A_v000.mp4")
    result = TelegramSink(
        tg, runner=FakeCurl(body='{"ok":false,"description":"chat not found"}')
    ).send(path)
    assert not result.ok and not result.acknowledged


def test_a_blocked_network_is_a_failure_not_a_hang(tg):
    """api.telegram.org is unreachable from the owner's home network. curl says so with an rc."""
    _creds(tg)
    path = _write(tg, "evt_A_v000.mp4")
    result = TelegramSink(tg, runner=FakeCurl(code=7, stderr="Failed to connect")).send(path)
    assert not result.ok and "rc=7" in result.detail


def test_the_connect_timeout_is_short_and_the_upload_timeout_is_not(tg):
    """This runs inside the run loop's housekeeping, where a siren may be due.

    A dead network has to fail in seconds; an upload in progress has to be allowed to finish.
    """
    _creds(tg)
    path = _write(tg, "evt_A_v000.mp4")
    curl = FakeCurl()
    TelegramSink(tg, runner=curl).send(path)
    _argv, config = curl.calls[0]
    assert "connect-timeout = 5" in config
    assert f"max-time = {int(tg.video.telegram_timeout_sec)}" in config


def test_a_clip_goes_to_the_warehouse_and_the_chat_but_not_the_receiver(tg, spool, local):
    """The routing, end to end through the uploader rather than through one sink."""
    _creds(tg)
    _write(tg, "evt_A_v000.mp4", size=2048)
    curl = FakeCurl()
    uploader = Uploader(tg, spool, sinks=[local, MegaSink(tg), TelegramSink(tg, runner=curl)])
    report = uploader.drain()
    assert report.acknowledged == ["evt_A_v000.mp4"], (
        "the chat is a real delivery; the spool frees it"
    )
    assert spool.depth() == 0
    assert (tg.mega_dir / "evt_A_v000.mp4").exists(), "and the warehouse keeps the original"
    assert not (Path(tg.upload.local_inbox) / "evt_A_v000.mp4").exists(), (
        "the receiver is out of the clip path by the owner's decision"
    )
    assert len(curl.calls) == 1


def test_a_dead_chat_does_not_free_the_clip_and_backs_off(tg, spool, local):
    """A clip whose only real delivery failed must stay, and must not be retried every second."""
    _creds(tg)
    _write(tg, "evt_A_v000.mp4", size=2048)
    curl = FakeCurl(code=7)
    uploader = Uploader(tg, spool, sinks=[local, MegaSink(tg), TelegramSink(tg, runner=curl)])
    uploader.drain(now=100.0)
    assert spool.depth() == 1, "a cp into a sync folder is not a delivery"
    attempts = len(curl.calls)
    uploader.drain(now=100.5)
    assert len(curl.calls) == attempts, "the backoff holds it off the wire"
    uploader.drain(now=100.0 + tg.spool.upload_retry_max_sec + 1.0)
    assert len(curl.calls) > attempts, "and lets go once the wait has elapsed"


def test_an_unconfigured_chat_does_not_wedge_the_spool(tg, spool, local):
    """No token file means no acknowledgement will ever arrive, which is not the same as waiting.

    Left as "waiting", clip segments would pile up until the cap, and the spool would then drop
    one for every new one written — for the whole trip, on a run where nobody had filled in a
    token. The warehouse frees them instead, exactly as it did before the chat existed, and
    `telegram_ok=0` in the heartbeat says why they are not in the chat.
    """
    tg.video.telegram_env_file = str(tg.spool_dir.parent / "never-created.env")
    _write(tg, "evt_A_v000.mp4", size=2048)
    curl = FakeCurl()
    uploader = Uploader(tg, spool, sinks=[local, MegaSink(tg), TelegramSink(tg, runner=curl)])
    report = uploader.drain(now=0.0)
    assert report.acknowledged == ["evt_A_v000.mp4"]
    assert spool.depth() == 0
    assert (tg.mega_dir / "evt_A_v000.mp4").exists()
    assert not curl.calls, "nothing is attempted without a token"


def test_a_configured_chat_that_is_merely_offline_is_still_waited_for(tg, spool, local):
    """The distinction that makes the fallback safe: unusable is not the same as unreachable."""
    _creds(tg)
    _write(tg, "evt_A_v000.mp4", size=2048)
    uploader = Uploader(
        tg, spool, sinks=[local, MegaSink(tg), TelegramSink(tg, runner=FakeCurl(code=7))]
    )
    uploader.drain(now=0.0)
    assert spool.depth() == 1, "a delivery that is possible later must be waited for"

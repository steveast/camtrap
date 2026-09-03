"""Getting artefacts off the machine, and knowing when it is safe to forget them.

Two sinks, independent by design (spec 3.5):

* `prod` — ssh forced command. Its acknowledgement is the ONLY reason a file leaves the spool.
* `mega` — a copy into the cloud sync folder. Best effort, and explicitly not an acknowledgement:
  a successful `cp` means the file reached a sync folder, not that it reached the cloud.

One sink failing must not stop the other, and a frame stays in the spool until `prod` confirms it.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import log
from .atomic import write_atomic
from .config import Config


@dataclass
class SinkResult:
    ok: bool
    detail: str = ""
    acknowledged: bool = False


@dataclass
class UploadReport:
    sent: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    acknowledged: list[str] = field(default_factory=list)


#: Sinks whose success means the bytes are off this machine and something outside it said so.
#: `mega` is deliberately not one: a `cp` into a watched folder is not a delivery, because the
#: sync client does not expose its upload state. This set is what decides whether an artefact may
#: be freed from the spool by a copy — see `Uploader.drain`.
ACKNOWLEDGING = frozenset({"prod", "telegram"})


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


class ProdSink:
    """ssh transport whose reply is parsed, not assumed."""

    name = "prod"

    def __init__(self, cfg: Config, *, runner: Callable[..., object] | None = None) -> None:
        self.cfg = cfg
        self._run = runner if runner is not None else self._default_run

    @staticmethod
    def _default_run(argv: list[str], *, payload: bytes, timeout: float):
        return subprocess.run(
            argv, input=payload, capture_output=True, timeout=timeout, check=False
        )

    def _control_path(self) -> str:
        if self.cfg.upload.ssh_control_path:
            return self.cfg.upload.ssh_control_path
        runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
        return f"{runtime}/camtrap-ssh-%C"

    def _argv(self, verb: str) -> list[str]:
        upload = self.cfg.upload
        argv = list(upload.ssh_cmd)
        if upload.ssh_key:
            argv += ["-i", upload.ssh_key]
        argv += list(upload.ssh_options)
        if any("ControlMaster" in option for option in upload.ssh_options):
            argv += ["-o", f"ControlPath={self._control_path()}"]
        argv += [upload.ssh_target, verb]
        return argv

    def send(self, path: Path) -> SinkResult:
        payload = path.read_bytes()
        argv = self._argv(f"put-frame {path.name}")
        try:
            result = self._run(argv, payload=payload, timeout=60.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return SinkResult(False, f"transport: {exc}")
        code = getattr(result, "returncode", 1)
        raw = getattr(result, "stdout", b"") or b""
        reply = raw.decode(errors="replace").strip() if isinstance(raw, bytes) else str(raw).strip()
        if code != 0:
            return SinkResult(False, f"rc={code}")
        parts = reply.split()
        # "ok <name> <bytes> <sha256>" — verified rather than trusted: a receiver that stored a
        # truncated file must not free the only copy we have.
        if len(parts) < 4 or parts[0] != "ok" or parts[1] != path.name:
            return SinkResult(False, f"unexpected reply: {reply[:80]}")
        try:
            reported = int(parts[2])
        except ValueError:
            return SinkResult(False, f"bad size in reply: {reply[:80]}")
        if reported != len(payload):
            return SinkResult(False, f"size mismatch: sent {len(payload)}, stored {reported}")
        if parts[3] != _sha256(path):
            return SinkResult(False, "checksum mismatch")
        return SinkResult(True, "stored", acknowledged=True)

    def heartbeat(self, line: str) -> SinkResult:
        argv = self._argv("heartbeat")
        try:
            result = self._run(argv, payload=line.encode(), timeout=30.0)
        except (OSError, subprocess.SubprocessError) as exc:
            return SinkResult(False, f"transport: {exc}")
        if getattr(result, "returncode", 1) != 0:
            return SinkResult(False, "rc != 0")
        return SinkResult(True, "stored")


class LocalSink:
    """A directory standing in for the receiver: used by tests and by the offline shakedown."""

    name = "prod"

    def __init__(self, cfg: Config, inbox: Path) -> None:
        self.cfg = cfg
        self.inbox = inbox
        self.available = True

    def send(self, path: Path) -> SinkResult:
        if not self.available:
            return SinkResult(False, "sink unavailable")
        try:
            self.inbox.mkdir(parents=True, exist_ok=True)
            target = self.inbox / path.name
            tmp = target.with_suffix(target.suffix + ".part")
            shutil.copyfile(path, tmp)
            tmp.replace(target)
        except OSError as exc:
            return SinkResult(False, f"inbox unusable: {exc}")
        return SinkResult(True, "stored", acknowledged=True)

    def heartbeat(self, line: str) -> SinkResult:
        if not self.available:
            return SinkResult(False, "sink unavailable")
        # Mirror the receiver's layout: frames in inbox/, status in state/. The poller reads
        # state/heartbeat, so writing it beside the frames would leave it invisible.
        try:
            state = self.inbox.parent / "state"
            state.mkdir(parents=True, exist_ok=True)
            write_atomic(state / "heartbeat", line, durable=False)
        except OSError as exc:
            return SinkResult(False, f"state unusable: {exc}")
        return SinkResult(True, "stored")


class MegaSink:
    """Copy into the cloud sync folder. Never acknowledges — a cp is not a delivery.

    Frames are recompressed on the way in. This copy is a warehouse: its job is to hold a complete
    event when the receiver is unreachable, and it syncs over whatever wifi the hotel has. The
    evidence-grade original — the one that might be shown to police — stays on the receiver and in
    the spool, untouched. Manifests are copied verbatim; they are a few hundred bytes.
    """

    name = "mega"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def _recompress(self, path: Path, target: Path) -> bool:
        """Downscale and re-encode one frame; False if OpenCV could not read it."""
        import cv2  # local: delivery should not pay for OpenCV when this sink is unused

        frame = cv2.imread(str(path))
        if frame is None:
            return False
        height, width = frame.shape[:2]
        wanted = self.cfg.upload.mega_width
        if width > wanted:
            scale = wanted / width
            frame = cv2.resize(frame, (wanted, int(height * scale)), interpolation=cv2.INTER_AREA)
        params = [int(cv2.IMWRITE_JPEG_QUALITY), self.cfg.upload.mega_quality]
        ok, buffer = cv2.imencode(".jpg", frame, params)
        # That folder is watched by a sync client: a partially written file would be uploaded as
        # the finished article and never corrected.
        return bool(ok) and write_atomic(target, buffer.tobytes(), durable=False)

    def send(self, path: Path) -> SinkResult:
        target_dir = self.cfg.mega_dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / path.name
            if target.resolve() == path.resolve():
                # Misconfiguration: the cloud folder points at the spool. Recompressing in place
                # would destroy the original, and copyfile would raise SameFileError, which is not
                # an OSError and would escape the handler below.
                return SinkResult(False, "mega_dir is the spool directory")
            recompress = self.cfg.upload.mega_recompress and path.suffix.lower() in (
                ".jpg",
                ".jpeg",
            )
            if recompress and self._recompress(path, target):
                return SinkResult(True, "copied (recompressed)", acknowledged=False)
            shutil.copyfile(path, target)
        except OSError as exc:
            return SinkResult(False, f"copy failed: {exc}")
        return SinkResult(True, "copied", acknowledged=False)

    def heartbeat(self, line: str) -> SinkResult:
        return SinkResult(True, "skipped")

    def enforce_retention(self, *, now: float | None = None) -> list[str]:
        removed: list[str] = []
        cutoff = (now or time.time()) - self.cfg.spool.retention_days * 86400
        target_dir = self.cfg.mega_dir
        if not target_dir.exists():
            return removed
        for path in target_dir.iterdir():
            if path.is_file() and path.stat().st_mtime < cutoff:
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(path.name)
                log.emit("retention", sink="mega", file=path.name)
        return removed


class TelegramSink:
    """Clip segments straight from the laptop to the chat.

    This exists because the owner overrode the rule in spec section 8 that kept the bot token off
    this machine, and the override is worth understanding rather than just obeying. What it costs:
    a bot can delete its own messages for 48 hours, so whoever carries the laptop out can clear
    the chat and post a false all-clear. What it does NOT cost, and this is the part that makes it
    defensible: it destroys no evidence. The photographs are acknowledged by a receiver this
    machine cannot delete from, and the segments are in the warehouse. What is exposed is the
    chat, not the record.

    Two things are done about it here rather than in a document. The token is read only from a
    file the owner alone can read — the way ssh refuses a group-readable key — and it never
    appears in `argv`, because `/proc` is world-readable and `curl https://…/bot<TOKEN>/…` would
    publish it to every process on the machine. It goes in on stdin instead.
    """

    name = "telegram"
    API = "https://api.telegram.org"

    def __init__(self, cfg: Config, *, runner: Callable[..., object] | None = None) -> None:
        self.cfg = cfg
        self._run = runner if runner is not None else self._default_run
        self._creds: tuple[str, str] | None = None
        self._creds_error = ""

    @staticmethod
    def _default_run(argv: list[str], *, payload: str, timeout: float):
        return subprocess.run(
            argv, input=payload, capture_output=True, text=True, timeout=timeout, check=False
        )

    def credentials(self) -> tuple[str, str] | None:
        """Token and chat id from an env-style file, refused unless only the owner can read it."""
        if self._creds is not None:
            return self._creds
        path = self.cfg.telegram_env_path
        try:
            mode = path.stat().st_mode
        except OSError:
            self._creds_error = f"{path} missing"
            return None
        if mode & 0o077:
            # The reason this is fatal rather than a warning: the whole point of moving the token
            # onto a machine assumed to be stolen is that it is the owner's decision to take that
            # risk once, not to hand the token to every account on the box as well.
            self._creds_error = f"{path} is readable by others (mode {mode & 0o777:o}, want 600)"
            return None
        token = chat = ""
        try:
            for line in path.read_text().splitlines():
                key, _, value = line.partition("=")
                value = value.strip().strip("\"'")
                if key.strip() == "TELEGRAM_BOT_TOKEN":
                    token = value
                elif key.strip() == "TELEGRAM_CHAT_ID":
                    chat = value
        except OSError as exc:
            self._creds_error = f"{path} unreadable: {exc}"
            return None
        if not token or not chat:
            self._creds_error = f"{path} needs TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID"
            return None
        self._creds = (token, chat)
        self._creds_error = ""
        return self._creds

    def available(self) -> tuple[bool, str]:
        if self.credentials() is None:
            return False, self._creds_error
        return True, ""

    @staticmethod
    def _quote(value: str) -> str:
        """For a curl config file, where a quoted value takes backslash escapes."""
        return value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")

    def _caption(self, path: Path) -> str:
        size_mb = path.stat().st_size / 1048576
        stamp = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(path.stat().st_mtime))
        return f"🎥 {path.name}\n{stamp}\n{size_mb:.1f} MB"

    def send(self, path: Path) -> SinkResult:
        if path.suffix.lower() != ".mp4":
            # Photographs go to a receiver that acknowledges them and cannot be deleted from a
            # stolen laptop. Nothing else belongs on this path.
            return SinkResult(False, "telegram carries clips only")
        creds = self.credentials()
        if creds is None:
            return SinkResult(False, self._creds_error)
        token, chat = creds
        size = path.stat().st_size
        cap = self.cfg.video.telegram_max_mb * 1024 * 1024
        if size > cap:
            # Telegram would refuse it at 50 MB. Say so here instead of spending a hotel uplink
            # on a request that ends in a rejection — the segment is in the warehouse regardless.
            return SinkResult(
                False, f"{size / 1048576:.1f} MB over the {self.cfg.video.telegram_max_mb} MB cap"
            )
        # The config file arrives on stdin, so the token is never in argv: /proc is world-readable
        # and an argv is visible to every process on this machine.
        config = "\n".join(
            [
                f'url = "{self.API}/bot{token}/sendVideo"',
                f'form = "chat_id={self._quote(chat)}"',
                f'form = "caption={self._quote(self._caption(path))}"',
                f'form = "video=@{self._quote(str(path))};type=video/mp4"',
                'form = "supports_streaming=true"',
                "silent",
                "show-error",
                # A blocked network must fail fast: this runs in the run loop's housekeeping, and
                # api.telegram.org is unreachable from the owner's home network entirely. The
                # long timeout is for the upload itself, once a connection exists.
                "connect-timeout = 5",
                f"max-time = {int(self.cfg.video.telegram_timeout_sec)}",
                "",
            ]
        )
        argv = [*self.cfg.video.curl_cmd, "--config", "-"]
        try:
            result = self._run(
                argv, payload=config, timeout=self.cfg.video.telegram_timeout_sec + 15
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return SinkResult(False, f"transport: {exc}")
        code = getattr(result, "returncode", 1)
        body = (getattr(result, "stdout", "") or "").strip()
        if code != 0:
            return SinkResult(False, f"curl rc={code} {(getattr(result, 'stderr', '') or '')[:60]}")
        if '"ok":true' not in body.replace(" ", ""):
            # Never echo the body wholesale: an error reply from Telegram can quote the request.
            return SinkResult(False, f"telegram refused: {body[:80]}")
        return SinkResult(True, "sent", acknowledged=True)

    def heartbeat(self, line: str) -> SinkResult:
        return SinkResult(True, "skipped")


class Uploader:
    """Drains the spool in priority order, with backoff, and deletes only on acknowledgement."""

    def __init__(self, cfg: Config, spool, *, sinks: list[object] | None = None) -> None:
        self.cfg = cfg
        self.spool = spool
        self.sinks = sinks if sinks is not None else self._default_sinks()
        self._failures = 0
        self._next_attempt = 0.0
        #: Per-sink backoff. The receiver has had one since the beginning; the others needed one
        #: as soon as a sink could be slow AND unreachable. api.telegram.org is not reachable at
        #: all from the owner's home network, and this drain runs inside the run loop: without a
        #: backoff every pass would spend the connect timeout on a sink that is not coming back,
        #: once a second, while a siren might be due.
        self._sink_next: dict[str, float] = {}
        self._sink_fails: dict[str, int] = {}

    def _default_sinks(self) -> list[object]:
        sinks: list[object] = []
        names = self.cfg.sink_names
        if "prod" in names:
            if self.cfg.upload.local_inbox:
                sinks.append(LocalSink(self.cfg, Path(self.cfg.upload.local_inbox)))
            elif self.cfg.upload.ssh_target:
                sinks.append(ProdSink(self.cfg))
        if "mega" in names:
            sinks.append(MegaSink(self.cfg))
        if "telegram" in names:
            sinks.append(TelegramSink(self.cfg))
        return sinks

    def _sinks_for(self, path: Path) -> set[str]:
        """Which sinks an artefact is for.

        Clip segments are cloud-only by configuration (`video.sinks`), and the receiver is out of
        that path on purpose: its inbox cap of 512 MB was already 314 MB of photographs, and a
        stream of segments twenty times the size of a frame would evict them. Everything else
        goes to every configured sink, as it always has.
        """
        if path.suffix.lower() == ".mp4":
            return set(self.cfg.video.sinks)
        # A literal set, NOT the configured sinks: whether a photograph may be freed by a cloud
        # copy is a property of the artefact, not of what happens to be configured. Deriving it
        # from `self.sinks` made a run with only the warehouse configured start deleting frames
        # on `cp` — which is the one rule this module exists to enforce. Two tests caught it.
        return {"prod", "mega"}

    def _can_acknowledge(self, wanted: set[str]) -> bool:
        """Whether anything in this artefact's sink list could ever confirm it.

        "Could ever" excludes a sink that is configured but unusable, and that distinction has a
        concrete consequence: with `telegram` in `video.sinks` and no token file, clip segments
        would wait for an acknowledgement that cannot arrive, the spool would sit permanently at
        its cap, and it would drop a segment for every new one written. A sink whose own
        `available()` says no is not something to wait for — the warehouse copy frees the clip, as
        it did before this path existed, and `telegram_ok=0` in the heartbeat says why.

        A sink with no `available()` is assumed usable: the receiver's reachability is decided by
        trying it, and its backoff is what handles the answer.

        The candidates come from the artefact's sink list BY NAME, and an instantiated sink may
        only ever subtract itself. Deriving them from `self.sinks` instead is the same mistake
        twice over: a run configured with only the warehouse would then find nothing that can
        acknowledge a PHOTOGRAPH and start freeing frames on `cp`, which is the one rule this
        module exists to enforce. Two tests caught it both times.
        """
        candidates = set(wanted) & ACKNOWLEDGING
        for sink in self.sinks:
            if sink.name not in candidates:
                continue
            checker = getattr(sink, "available", None)
            if callable(checker) and not checker()[0]:
                candidates.discard(sink.name)
        return bool(candidates)

    def _release(self, path: Path, report: UploadReport, *, note_success: bool) -> None:
        """Free an artefact from the spool. The only place a file is removed on success."""
        self.spool.acknowledge(path.name)
        report.acknowledged.append(path.name)
        report.sent.append(path.name)
        if note_success:
            self._note_success()

    def backoff_ready(self, *, now: float) -> bool:
        return now >= self._next_attempt

    def _note_failure(self, *, now: float) -> None:
        self._failures += 1
        base = self.cfg.spool.upload_retry_base_sec
        delay = min(self.cfg.spool.upload_retry_max_sec, base * (2 ** (self._failures - 1)))
        self._next_attempt = now + delay
        log.emit("upload_retry", failures=self._failures, delay=delay)

    def _note_success(self) -> None:
        self._failures = 0
        self._next_attempt = 0.0

    def _note_sink_failure(self, name: str, *, now: float) -> None:
        if name == "prod":
            # The receiver has had its own backoff since the beginning, and that one is reported
            # and is what the tamper path's evidence wait reads. A second gate on the same sink
            # would silently halve its retry rate — which is how a test that had been passing for
            # weeks started counting two attempts where it expected six.
            return
        fails = self._sink_fails.get(name, 0) + 1
        self._sink_fails[name] = fails
        base = self.cfg.spool.upload_retry_base_sec
        delay = min(self.cfg.spool.upload_retry_max_sec, base * (2 ** (fails - 1)))
        self._sink_next[name] = now + delay

    def _note_sink_success(self, name: str) -> None:
        self._sink_fails.pop(name, None)
        self._sink_next.pop(name, None)

    def drain(self, *, now: float = 0.0, limit: int | None = None) -> UploadReport:
        """One pass over the queue. Failure is per sink, and so is the backoff.

        The receiver being unreachable used to stop the pass at the file it failed on, which
        quietly starved the sink that exists for exactly that situation: with `prod` refusing
        every put, the warehouse copy received the first artefact of an afternoon and none of the
        197 behind it. `limit` bounds the files actually worked on, not the files looked at —
        otherwise a queue whose head is already copied would be re-examined for ever while the
        backlog behind it never moved.
        """
        report = UploadReport()
        budget = limit if limit is not None else -1
        # A sink that fails is not asked again in this pass — 197 artefacts against a dead
        # receiver would spend the whole tick timing out — but the others carry on.
        spent: set[str] = set()
        if not self.backoff_ready(now=now):
            spent.add("prod")
        for name, ready_at in self._sink_next.items():
            if now < ready_at:
                spent.add(name)
        for path in self.spool.pending():
            if budget == 0:
                break
            if not path.exists():
                continue
            wanted = self._sinks_for(path)
            # Nothing in this artefact's sink list can acknowledge it, so the warehouse copy has
            # to be what frees it. A DELIBERATE exception to "a cp is not an acknowledgement":
            # that rule exists so an artefact is never freed while something could still confirm
            # it, and holding one that nothing will ever confirm would fill the spool and push out
            # the artefacts that can be. The cost is real and stated rather than hidden — such an
            # artefact is only ever as safe as the sync client.
            #
            # It stopped applying to clips the moment they got a sink that really answers: with
            # `telegram` in the list a segment waits for Telegram's `ok:true`, exactly as a
            # photograph waits for the receiver. The predicate is about what CAN acknowledge, not
            # about which sink happens to be the receiver.
            cloud_only = not self._can_acknowledge(wanted)
            work = [sink for sink in self.sinks if sink.name in wanted and sink.name not in spent]
            # Already in the warehouse: copying it again would recompress it again, every tick,
            # for as long as the receiver stays down.
            work = [
                sink for sink in work if sink.name != "mega" or not self.spool.is_copied(path.name)
            ]
            if not work:
                if cloud_only and self.spool.is_copied(path.name):
                    # Copied on an earlier pass and nothing else is coming for it.
                    self._release(path, report, note_success=False)
                    continue
                if len(spent) == len(self.sinks):
                    break
                continue  # nothing left to do for this file; it does not cost the batch budget
            acknowledged = False
            asked_for_ack = False
            copied_now = False
            for sink in work:
                try:
                    result = sink.send(path)
                except Exception as exc:
                    result = SinkResult(False, f"{type(exc).__name__}: {exc}")
                if result.ok:
                    self._note_sink_success(sink.name)
                    if sink.name == "mega":
                        report.copied.append(path.name)
                        self.spool.mark_copied(path.name)
                        copied_now = True
                    if result.acknowledged:
                        acknowledged = True
                    else:
                        asked_for_ack = asked_for_ack or sink.name == "prod"
                else:
                    spent.add(sink.name)
                    self._note_sink_failure(sink.name, now=now)
                    log.emit("upload_failed", sink=sink.name, file=path.name, why=result.detail)
                    if sink.name == "prod":
                        asked_for_ack = True
                        self._note_failure(now=now)
            budget -= 1
            if acknowledged:
                self._release(path, report, note_success=True)
            elif cloud_only and copied_now:
                # A cloud copy releasing an artefact says nothing about the receiver's health, so
                # it must not reset the receiver's backoff.
                self._release(path, report, note_success=False)
            elif asked_for_ack:
                # Only a file the receiver was actually asked about counts as failed: the tamper
                # path waits on this, and waiting out the siren delay for a sink that was never
                # asked would delay the alarm for nothing.
                report.failed.append(path.name)
            if len(spent) == len(self.sinks):
                break
        return report

    def heartbeat(self, line: str) -> bool:
        ok = False
        for sink in self.sinks:
            try:
                result = sink.heartbeat(line)
            except Exception as exc:
                log.emit("heartbeat_error", sink=sink.name, why=f"{type(exc).__name__}: {exc}")
                continue
            if result.ok and sink.name == "prod":
                ok = True
        return ok

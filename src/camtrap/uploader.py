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
            (state / "heartbeat").write_text(line)
        except OSError as exc:
            return SinkResult(False, f"state unusable: {exc}")
        return SinkResult(True, "stored")


class MegaSink:
    """Copy into the cloud sync folder. Never acknowledges — a cp is not a delivery."""

    name = "mega"

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg

    def send(self, path: Path) -> SinkResult:
        target_dir = self.cfg.mega_dir
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target_dir / path.name)
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


class Uploader:
    """Drains the spool in priority order, with backoff, and deletes only on acknowledgement."""

    def __init__(self, cfg: Config, spool, *, sinks: list[object] | None = None) -> None:
        self.cfg = cfg
        self.spool = spool
        self.sinks = sinks if sinks is not None else self._default_sinks()
        self._failures = 0
        self._next_attempt = 0.0

    def _default_sinks(self) -> list[object]:
        sinks: list[object] = []
        names = self.cfg.upload.sinks
        if "prod" in names:
            if self.cfg.upload.local_inbox:
                sinks.append(LocalSink(self.cfg, Path(self.cfg.upload.local_inbox)))
            elif self.cfg.upload.ssh_target:
                sinks.append(ProdSink(self.cfg))
        if "mega" in names:
            sinks.append(MegaSink(self.cfg))
        return sinks

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

    def drain(self, *, now: float = 0.0, limit: int | None = None) -> UploadReport:
        report = UploadReport()
        if not self.backoff_ready(now=now):
            return report
        for path in self.spool.pending()[: limit or None]:
            if not path.exists():
                continue
            acknowledged = False
            any_failure = False
            for sink in self.sinks:
                try:
                    result = sink.send(path)
                except Exception as exc:
                    result = SinkResult(False, f"{type(exc).__name__}: {exc}")
                if result.ok:
                    if sink.name == "mega":
                        report.copied.append(path.name)
                        self.spool.mark_copied(path.name)
                    if result.acknowledged:
                        acknowledged = True
                else:
                    any_failure = True
                    log.emit("upload_failed", sink=sink.name, file=path.name, why=result.detail)
            if acknowledged:
                # The only place a file is removed on success.
                self.spool.acknowledge(path.name)
                report.acknowledged.append(path.name)
                report.sent.append(path.name)
                self._note_success()
            else:
                report.failed.append(path.name)
                if any_failure:
                    self._note_failure(now=now)
                    break  # stop the batch; the next tick retries from the front of the queue
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

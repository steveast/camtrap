"""The spool: what is waiting to leave, in what order, and what gets dropped first.

Two rules from the spec that the code has to make impossible to break:

* a frame leaves the spool only when the receiver acknowledges it — a cloud copy is not an
  acknowledgement, because a successful `cp` only means the file reached a sync folder;
* the first frame of an event is never dropped, and every drop is logged. Silent truncation reads
  as "everything was captured" when it was not.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from stat import S_ISREG

from . import log
from .atomic import PART_SUFFIX
from .config import Config


@dataclass(frozen=True, slots=True)
class _Entry:
    """A spool file as it was when we last looked: path, size, mtime — stat'ed once."""

    path: Path
    size: int
    mtime: float


def _event_id(name: str) -> str:
    stem = name.rsplit(".", 1)[0]
    if "_" in stem and stem.rsplit("_", 1)[1].isdigit():
        return stem.rsplit("_", 1)[0]
    return stem


def _frame_index(name: str) -> int | None:
    stem = name.rsplit(".", 1)[0]
    if "_" in stem:
        tail = stem.rsplit("_", 1)[1]
        if tail.isdigit():
            return int(tail)
    return None


class Spool:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self._tamper: set[str] = set()
        self._copied: set[str] = set()

    # --- inventory -----------------------------------------------------------

    def _entries(self) -> list[_Entry]:
        """One listing, one stat per file, vanished files skipped.

        Every caller used to stat independently, and a file can disappear between the listing and
        the stat — the uploader deletes on acknowledgement from the same directory. An unhandled
        OSError here would have propagated out of housekeeping and into the run loop.
        """
        root = self.cfg.spool_dir
        if not root.exists():
            return []
        entries: list[_Entry] = []
        for path in root.iterdir():
            if path.name.endswith(PART_SUFFIX):
                continue  # a frame still being written is not ready to be sent
            try:
                stat = path.stat()
            except OSError:
                continue  # acknowledged and unlinked while we were listing
            if S_ISREG(stat.st_mode):  # a directory reports a size too; only files count
                entries.append(_Entry(path, stat.st_size, stat.st_mtime))
        return entries

    def _files(self) -> list[Path]:
        return [entry.path for entry in self._entries()]

    def depth(self) -> int:
        return len(self._entries())

    def total_bytes(self) -> int:
        return sum(entry.size for entry in self._entries())

    def mark_tamper(self, event_id: str) -> None:
        """Tamper artefacts jump the queue: the siren waits on their acknowledgement."""
        self._tamper.add(event_id)

    def mark_copied(self, name: str) -> None:
        """Record a best-effort cloud copy. Never a reason to delete anything."""
        self._copied.add(name)

    def is_copied(self, name: str) -> bool:
        return name in self._copied

    # --- ordering ------------------------------------------------------------

    def _sort_key(self, path: Path) -> tuple:
        name = path.name
        event = _event_id(name)
        index = _frame_index(name)
        is_manifest = name.endswith(".json")
        return (
            0 if event in self._tamper else 1,  # tamper first
            0 if is_manifest else 1,  # then the manifest
            0 if index == 0 else 1,  # then the first frame
            index if index is not None else 0,
            name,
        )

    def pending(self) -> list[Path]:
        return sorted(self._files(), key=self._sort_key)

    # --- removal -------------------------------------------------------------

    def acknowledge(self, name: str) -> bool:
        """Delete a file because the receiver confirmed it. The only reason to delete on success."""
        path = self.cfg.spool_dir / name
        try:
            path.unlink()
        except OSError:
            return False
        self._copied.discard(name)
        return True

    def _droppable(self, entries: list[_Entry]) -> list[_Entry]:
        """Mid-event frames, cloud-copied ones first. Manifests and first frames are excluded."""
        candidates = [
            entry
            for entry in entries
            if not entry.path.name.endswith(".json")
            and _frame_index(entry.path.name) not in (0, None)
        ]
        candidates.sort(
            key=lambda entry: (
                0 if entry.path.name in self._copied else 1,  # copied ones have a chance elsewhere
                entry.mtime,  # then oldest first
            )
        )
        return candidates

    def enforce_cap(self) -> list[str]:
        """Drop mid-event frames until the spool fits, counting down from one measurement.

        The total used to be recomputed inside the loop, which meant a full listing and a stat of
        every remaining file per dropped frame: on a spool that needed 943 drops that took 1.79 s,
        all of it inside the run loop, all of it while a siren might be due. Subtracting the size
        of what we just deleted gives the same answer for one listing.
        """
        cap = self.cfg.spool.max_mb * 1024 * 1024
        dropped: list[str] = []
        entries = self._entries()
        total = sum(entry.size for entry in entries)
        if total <= cap:
            return dropped
        for entry in self._droppable(entries):
            if total <= cap:
                break
            name = entry.path.name
            was_copied = name in self._copied
            try:
                entry.path.unlink()
            except OSError:
                continue
            total -= entry.size
            self._copied.discard(name)
            dropped.append(name)
            log.emit("drop", file=name, bytes=entry.size, reason="spool cap", copied=was_copied)
        if total > cap:
            log.emit("drop_incomplete", reason="only first frames and manifests remain")
        return dropped

    def enforce_retention(self, *, now: float | None = None) -> list[str]:
        cutoff = (now or time.time()) - self.cfg.spool.retention_days * 86400
        removed: list[str] = []
        for entry in self._entries():
            if entry.mtime < cutoff:
                name = entry.path.name
                try:
                    entry.path.unlink()
                except OSError:
                    continue
                removed.append(name)
                log.emit("retention", file=name, days=self.cfg.spool.retention_days)
        return sorted(removed)

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


#: What an artefact is, from its name alone. Three kinds share the spool and they are not
#: interchangeable: `evt_<ts>.json` is the manifest, `evt_<ts>_<nnn>.jpg` a photograph,
#: `evt_<ts>_v<nnn>.mp4` a clip segment.
MANIFEST = "manifest"
FRAME = "frame"
CLIP = "clip"


def _parts(name: str) -> tuple[str, str, int | None]:
    """(event id, kind, index). The index is None for a manifest and for anything unrecognised."""
    stem = name.rsplit(".", 1)[0]
    if name.endswith(".json"):
        return stem, MANIFEST, None
    if "_" in stem:
        head, tail = stem.rsplit("_", 1)
        if tail.isdigit():
            return head, FRAME, int(tail)
        if tail.startswith("v") and tail[1:].isdigit():
            return head, CLIP, int(tail[1:])
    return stem, MANIFEST, None


def _event_id(name: str) -> str:
    return _parts(name)[0]


def _frame_index(name: str) -> int | None:
    _event, kind, index = _parts(name)
    return index if kind == FRAME else None


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
        event, kind, index = _parts(name)
        return (
            0 if event in self._tamper else 1,  # tamper first
            0 if kind == MANIFEST else 1,  # then the manifest
            0 if (kind == FRAME and index == 0) else 1,  # then the first frame
            # Photographs ahead of clip segments of the same event. The photograph is the alert
            # and the thing a receiver acknowledges; a segment is the record behind it and is
            # twenty times the bytes, so sending it first would delay the alert on a slow link.
            0 if kind == FRAME else 1,
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
        """Mid-event frames and segments, cloud-copied first. Never a manifest or a first frame.

        A clip segment is droppable for the same reason a mid-event frame is, only more so: it is
        an order of magnitude more bytes than a photograph and the photographs are what the alert
        and the evidence rest on. Losing a segment costs the sequence; losing the first frame of
        an event costs knowing that anyone was there.
        """
        candidates = [
            entry
            for entry in entries
            if _parts(entry.path.name)[1] != MANIFEST
            and not (
                _parts(entry.path.name)[1] == FRAME and _parts(entry.path.name)[2] in (0, None)
            )
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

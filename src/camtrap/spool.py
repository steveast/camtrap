"""The spool: what is waiting to leave, in what order, and what gets dropped first.

Two rules from the spec that the code has to make impossible to break:

* a frame leaves the spool only when the receiver acknowledges it — a cloud copy is not an
  acknowledgement, because a successful `cp` only means the file reached a sync folder;
* the first frame of an event is never dropped, and every drop is logged. Silent truncation reads
  as "everything was captured" when it was not.
"""

from __future__ import annotations

import time
from pathlib import Path

from . import log
from .config import Config


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

    def _files(self) -> list[Path]:
        root = self.cfg.spool_dir
        if not root.exists():
            return []
        return [path for path in root.iterdir() if path.is_file()]

    def depth(self) -> int:
        return len(self._files())

    def total_bytes(self) -> int:
        return sum(path.stat().st_size for path in self._files())

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

    def _droppable(self) -> list[Path]:
        """Mid-event frames, cloud-copied ones first. Manifests and first frames are excluded."""
        candidates = [
            path
            for path in self._files()
            if not path.name.endswith(".json") and _frame_index(path.name) not in (0, None)
        ]
        candidates.sort(
            key=lambda path: (
                0 if path.name in self._copied else 1,  # copied ones have some chance elsewhere
                path.stat().st_mtime,  # then oldest first
            )
        )
        return candidates

    def enforce_cap(self) -> list[str]:
        cap = self.cfg.spool.max_mb * 1024 * 1024
        dropped: list[str] = []
        if self.total_bytes() <= cap:
            return dropped
        for path in self._droppable():
            if self.total_bytes() <= cap:
                break
            name = path.name
            size = path.stat().st_size
            try:
                path.unlink()
            except OSError:
                continue
            self._copied.discard(name)
            dropped.append(name)
            log.emit("drop", file=name, bytes=size, reason="spool cap", copied=name in self._copied)
        if self.total_bytes() > cap:
            log.emit("drop_incomplete", reason="only first frames and manifests remain")
        return dropped

    def enforce_retention(self, *, now: float | None = None) -> list[str]:
        cutoff = (now or time.time()) - self.cfg.spool.retention_days * 86400
        removed: list[str] = []
        for path in self._files():
            if path.stat().st_mtime < cutoff:
                name = path.name
                try:
                    path.unlink()
                except OSError:
                    continue
                removed.append(name)
                log.emit("retention", file=name, days=self.cfg.spool.retention_days)
        return sorted(removed)

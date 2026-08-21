"""Write a file so a reader never sees half of one.

Everything this agent writes is read by someone else while it is being written: the uploader lists
the spool and sends whatever is there, the MEGA client syncs its folder on its own schedule, and
`guard status` and the poller read the state files. A plain `write_text` is two visible states —
empty, then complete — with an arbitrary gap in between, and `cv2.imwrite` streams a JPEG straight
into the file the uploader is about to pick up. A half-sent frame is worse than a missing one: it
arrives looking like evidence.

The threat model makes this concrete rather than theoretical. The expected end of a run is the
power being pulled or the machine being carried off mid-write, which is exactly when a torn file
gets left behind — and the manifest is the file that says whether an event was tamper or motion.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from . import log

#: Suffix for work in progress. The spool skips these when it lists what to send.
PART_SUFFIX = ".part"


def write_atomic(path: Path, data: bytes | str, *, durable: bool = True) -> bool:
    """Write `data` to `path` as one indivisible step. Returns False instead of raising.

    The temporary file is a sibling, so `os.replace` stays on one filesystem and is atomic. With
    `durable` the bytes are flushed to the device before the rename: a frame that survives the
    rename but not the power cut would leave a name pointing at nothing.
    """
    payload = data.encode() if isinstance(data, str) else data
    tmp = path.with_name(path.name + PART_SUFFIX)
    try:
        with open(tmp, "wb") as handle:
            handle.write(payload)
            handle.flush()
            if durable:
                os.fsync(handle.fileno())
        os.replace(tmp, path)
        return True
    except OSError as exc:
        log.emit("write_failed", file=path.name, why=f"{type(exc).__name__}: {exc}")
        with contextlib.suppress(OSError):
            tmp.unlink()
        return False

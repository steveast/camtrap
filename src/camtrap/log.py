"""Structured stdout logging, one line per record, in the style of the external prober.

    event type=tamper signals=ac_offline stage=siren played=1 latency_ms=820

Detector ticks are not logged; events, drops, retries and mode changes are. Values containing
spaces are quoted so a line stays parseable by `awk '$1 == "event"'`.
"""

from __future__ import annotations

import contextlib
import sys
import time
from typing import Any, TextIO

# Resolved at write time, not at import: pytest's capsys and the systemd journal both replace
# sys.stdout after this module is imported.
_stream: TextIO | None = None
_file: TextIO | None = None


def set_stream(stream: TextIO | None) -> None:
    """Pin log output to a stream (selftest renderer); None restores sys.stdout."""
    global _stream
    _stream = stream


def set_file(path: str | None) -> None:
    """Also append every line to a file.

    An agent whose only journal is a terminal loses it when the terminal closes — and worse, the
    next write raises and takes the trap down with it. The file is the record; stdout is a
    convenience.
    """
    global _file
    if _file is not None:
        with contextlib.suppress(OSError):
            _file.close()
        _file = None
    if not path:
        return
    try:
        import pathlib

        target = pathlib.Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        _file = target.open("a", buffering=1, encoding="utf-8")
    except (OSError, ValueError):
        # A bad path in the config must not stop the agent from running: it would trade a missing
        # log for a missing trap.
        _file = None


def _format_value(value: Any) -> str:
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, float):
        text = f"{value:.3f}".rstrip("0").rstrip(".")
        return text or "0"
    text = str(value)
    if text == "":
        return '""'
    if any(ch.isspace() for ch in text) or '"' in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def emit(record: str, **fields: Any) -> None:
    parts = [record]
    parts.extend(f"{key}={_format_value(value)}" for key, value in fields.items())
    line = " ".join(parts)
    if _file is not None:
        with contextlib.suppress(OSError):
            _file.write(line + "\n")
    # The terminal may be gone. The file already has the line; losing stdout must not be fatal.
    with contextlib.suppress(OSError, ValueError):
        print(line, file=_stream or sys.stdout, flush=True)


def tick(record: str, **fields: Any) -> None:
    """A once-per-interval line; kept separate so it can be silenced by config."""
    emit(record, **fields)


def utc_stamp(when: float | None = None) -> str:
    return time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(when if when is not None else time.time()))

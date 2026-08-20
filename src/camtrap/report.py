"""Turn a run's log into the answer the empty-room checkpoints actually ask for.

After 24 hours the question is not "what happened" line by line — it is "did anything fire that
should not have". Reading ten thousand lines by hand is how a real false positive gets missed.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

RECORD = re.compile(r"^(?P<record>[a-z_]+)\s+(?P<fields>.*)$")
FIELD = re.compile(r'(\w+)=("[^"]*"|\S+)')


def parse_line(line: str) -> tuple[str, dict[str, str]] | None:
    match = RECORD.match(line.strip())
    if not match:
        return None
    fields = {k: v.strip('"') for k, v in FIELD.findall(match.group("fields"))}
    return match.group("record"), fields


@dataclass
class Summary:
    runs: int = 0
    frames: int = 0
    events: Counter = field(default_factory=Counter)
    tamper_signals: Counter = field(default_factory=Counter)
    sirens: int = 0
    warnings: int = 0
    refusals: Counter = field(default_factory=Counter)
    holds: Counter = field(default_factory=Counter)
    drops: int = 0
    truncated: int = 0
    heartbeat_failures: int = 0
    camera_reopens: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def noise(self) -> int:
        """Anything audible. In an empty room this must be zero."""
        return self.sirens + self.warnings


def summarise(lines: list[str]) -> Summary:
    summary = Summary()
    for line in lines:
        parsed = parse_line(line)
        if parsed is None:
            continue
        record, fields = parsed
        if record == "start":
            summary.runs += 1
        elif record == "stop":
            summary.frames += int(fields.get("frames", 0) or 0)
        elif record == "event_begin":
            summary.events[fields.get("type", "?")] += 1
        elif record == "event_truncated":
            summary.truncated += 1
        elif record == "tamper":
            summary.tamper_signals[fields.get("signal", "?")] += 1
        elif record == "sound":
            if fields.get("stage") == "siren":
                summary.sirens += 1
            else:
                summary.warnings += 1
        elif record == "sound_skip":
            summary.refusals[fields.get("reason", "?")] += 1
        elif record == "sound_hold":
            for item in (fields.get("undone") or "").split(","):
                if item:
                    summary.holds[item.split(":")[0]] += 1
        elif record == "drop":
            summary.drops += 1
        elif record == "heartbeat_failed":
            summary.heartbeat_failures += 1
        elif record in ("camera_error", "camera_gone"):
            summary.errors.append(line.strip())
        elif record == "camera_reopen":
            summary.camera_reopens += 1
        elif record in ("sound_error", "frame_error"):
            summary.errors.append(line.strip())
    return summary


def render(summary: Summary, *, source: str) -> str:
    lines = [f"log: {source}", ""]
    lines.append(f"runs                {summary.runs}")
    lines.append(f"frames analysed     {summary.frames}")
    lines.append("")
    lines.append("events")
    if summary.events:
        for kind, count in sorted(summary.events.items()):
            lines.append(f"  {kind:16} {count}")
    else:
        lines.append("  none")
    if summary.tamper_signals:
        lines.append("tamper signals")
        for name, count in sorted(summary.tamper_signals.items()):
            lines.append(f"  {name:16} {count}")
    lines.append("")
    lines.append(f"sirens played       {summary.sirens}")
    lines.append(f"warnings played     {summary.warnings}")
    lines.append(f"AUDIBLE TOTAL       {summary.noise}   <- must be 0 for an empty room")
    if summary.holds:
        lines.append("")
        lines.append("silencing attempts defeated")
        for what, count in sorted(summary.holds.items()):
            lines.append(f"  {what:16} {count}")
    if summary.refusals:
        lines.append("")
        lines.append("sound refused (this is the safety net working)")
        for reason, count in sorted(summary.refusals.items()):
            lines.append(f"  {reason:16} {count}")
    lines.append("")
    lines.append(f"spool drops         {summary.drops}")
    lines.append(f"events truncated    {summary.truncated}")
    lines.append(f"heartbeat failures  {summary.heartbeat_failures}")
    lines.append(f"camera reopens      {summary.camera_reopens}")
    if summary.errors:
        lines.append("")
        lines.append("errors")
        for error in summary.errors[-10:]:
            lines.append(f"  {error}")
    return "\n".join(lines)


def from_file(path: Path) -> tuple[Summary, str]:
    if not path.exists():
        return Summary(), f"{path} (missing)"
    lines = path.read_text(errors="replace").splitlines()
    return summarise(lines), str(path)

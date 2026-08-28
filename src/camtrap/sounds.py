"""Which sound files exist, and therefore whether the trap can make noise at all.

Kept apart from the player so that `status`, `selftest` and the heartbeat can answer
"is sound ready?" without touching PipeWire.
"""

from __future__ import annotations

from .config import Config


def missing_sounds(cfg: Config) -> list[str]:
    """Names of sounds that are configured but absent: 'siren', 'warn-vi', ...

    A language listed in warn_langs with no rendered file is a failure, not something to skip
    quietly at event time: a warning nobody hears is the same as no warning.
    """
    missing: list[str] = []
    if not cfg.siren_path.exists():
        missing.append("siren")
    wants_shutter = cfg.sound.shutter_before_siren or cfg.sound.shutter_on_capture
    if wants_shutter and not cfg.shutter_path.exists():
        missing.append("shutter")
    for lang in cfg.sound.warn_langs:
        if not cfg.warn_path(lang).exists():
            missing.append(f"warn-{lang}")
    return missing


def sound_ok(cfg: Config) -> bool:
    return not missing_sounds(cfg)

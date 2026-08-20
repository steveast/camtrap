"""Command parsing. Each subcommand is a thin wrapper over a module that does the work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__, log, sounds, state
from . import config as config_mod


def _spool_depth(cfg: config_mod.Config) -> int:
    spool = cfg.spool_dir
    if not spool.exists():
        return 0
    return sum(1 for path in spool.iterdir() if path.is_file())


def cmd_status(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    mode = state.read_mode(cfg.root)
    missing = sounds.missing_sounds(cfg)
    payload = {
        "version": __version__,
        "mode": mode.name,
        "arming_mode": cfg.arming.mode,
        "manual_arm": state.read_manual_arm(cfg.root) is not None,
        "spool_depth": _spool_depth(cfg),
        "spool_dir": str(cfg.spool_dir),
        "sound_ok": not missing,
        "missing_sounds": missing,
        "warn_langs": list(cfg.sound.warn_langs),
        "siren_path": str(cfg.siren_path),
        "state_dir": str(cfg.root),
        "camera": cfg.camera.device,
    }
    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        for key, value in payload.items():
            if isinstance(value, list):
                value = ",".join(str(item) for item in value) or "-"
            print(f"{key:16} {value}")
    return 0


def cmd_pause(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    state.write_mode(cfg.root, state.MODE_PAUSED)
    return 0


def cmd_resume(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    state.write_mode(cfg.root, state.MODE_ARMED)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="camtrap", description="laptop camera trap")
    parser.add_argument("--config", type=Path, default=None, help="path to config.toml")
    sub = parser.add_subparsers(dest="command", required=True)

    status = sub.add_parser("status", help="local state: mode, spool, sounds, languages")
    status.add_argument("--json", action="store_true", help="machine-readable output")
    status.set_defaults(func=cmd_status)

    pause = sub.add_parser("pause", help="mark an expected offline period")
    pause.set_defaults(func=cmd_pause)

    resume = sub.add_parser("resume", help="clear the expected offline mark")
    resume.set_defaults(func=cmd_resume)

    return parser


def main(argv: list[str] | None = None, *, cfg: config_mod.Config | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if cfg is None:
        cfg = config_mod.load(args.config)
    try:
        return int(args.func(cfg, args))
    except KeyboardInterrupt:
        log.emit("stop", reason="interrupt")
        return 130

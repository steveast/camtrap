"""Command parsing. Each subcommand is a thin wrapper over a module that does the work."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from . import __version__, inputdev, log, sounds, state
from . import config as config_mod
from .player import Stage


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


def cmd_run(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import run_forever

    missing = sounds.missing_sounds(cfg)
    if missing:
        # Loud at startup, not silent at event time: a trap that cannot make noise is no trap.
        log.emit("warn", reason="missing sound files", missing=",".join(missing))
    return run_forever(cfg)


def cmd_siren_test(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import sound_selftest

    return sound_selftest(cfg, Stage.SIREN)


def cmd_warn_test(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import sound_selftest

    return sound_selftest(cfg, Stage.WARNING)


def cmd_arm(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .arming import Arming

    Arming(cfg).arm_manually(now=__import__("time").monotonic())
    return 0


def cmd_disarm(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .arming import Arming

    Arming(cfg).disarm_manually()
    return 0


def cmd_input_scan(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    devices = inputdev.scan()
    grabbable = {d.event for d in inputdev.grabbable(devices)}
    if not devices:
        print("no input devices report mute/volume/power keys")
        return 0
    print(f"{'device':10} {'grab':5} {'keyboard':9} name")
    for device in devices:
        mark = "yes" if device.event in grabbable else "no"
        print(f"{device.event:10} {mark:5} {device.is_keyboard!s:9} {device.name}")
    print()
    print("Only non-keyboard devices are grabbable: the built-in keyboard is the owner's way")
    print("back in, and grabbing it would lock them out of typing the unlock password.")
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

    run = sub.add_parser("run", help="main mode (used by the systemd unit)")
    run.set_defaults(func=cmd_run)

    siren_test = sub.add_parser("siren-test", help="play the siren into the configured sink")
    siren_test.set_defaults(func=cmd_siren_test)

    warn_test = sub.add_parser("warn-test", help="play the spoken warning in every language")
    warn_test.set_defaults(func=cmd_warn_test)

    arm = sub.add_parser("arm", help="arm the alarm by hand")
    arm.set_defaults(func=cmd_arm)

    disarm = sub.add_parser("disarm", help="clear a manual arm")
    disarm.set_defaults(func=cmd_disarm)

    input_scan = sub.add_parser("input-scan", help="input devices reporting mute/volume keys")
    input_scan.set_defaults(func=cmd_input_scan)

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


if __name__ == "__main__":  # pragma: no cover - exercised via `python -m camtrap.cli`
    raise SystemExit(main())

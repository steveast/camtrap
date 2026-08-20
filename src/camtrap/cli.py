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


def cmd_watch(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import watch

    return watch(cfg, minutes=args.minutes, still=args.still)


def cmd_report(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from pathlib import Path as _Path

    from . import report

    path = _Path(args.log) if args.log else cfg.root / "logs" / "camtrap.log"
    summary, source = report.from_file(path)
    print(report.render(summary, source=source))
    if summary.noise:
        print()
        print(f"note: {summary.noise} audible event(s) in this log — expected only if someone")
        print("      was actually in the room. For an empty-room checkpoint this must be zero.")
    return 0


def cmd_drill(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import drill

    return drill(cfg, volume_pct=args.volume, siren_sec=args.siren_sec, seconds=args.seconds)


def cmd_preflight(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """Everything that must hold before walking out of the room."""
    from .runner import preflight

    # A standalone check restores the audio profile; arming keeps the speakers.
    ready, rows = preflight(cfg, probe=not args.no_probe, restore_audio=True)
    for name, ok, detail in rows:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name:12} {detail}")
    print()
    if not ready:
        print("NOT READY — fix the failures above before arming.")
        print("The point of this check is that leaving with a broken trap is worse than knowing.")
        return 1
    print("ready to arm")
    return 0


def cmd_arm_and_run(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """Preflight, then arm once the room goes quiet, then run. The command for walking out."""
    import time as _time

    from .runner import preflight, run_forever

    print("camtrap — preflight")
    ready, rows = preflight(cfg, probe=not args.no_probe)
    for name, ok, detail in rows:
        print(f"  [{'ok  ' if ok else 'FAIL'}] {name:12} {detail}")
    print()
    if not ready:
        print("NOT READY — nothing was armed. Fix the failures above.")
        return 1

    cfg.arming.mode = "on_still"
    if args.still is not None:
        cfg.arming.arm_when_still_sec = args.still
    state.write_mode(cfg.root, state.MODE_ARMED)

    quiet = int(cfg.arming.arm_when_still_sec)
    print(f"Armed as soon as the room is still for {quiet} s.")
    print("Take what you need and leave; the alarm waits for the room to empty.")
    print(
        f"Warning on motion in {','.join(cfg.sound.warn_langs) or 'no languages'}, "
        f"siren at {cfg.sound.volume_pct}% on tampering."
    )
    print("Unlock the screen when you come back — that disarms it.")
    print()
    _time.sleep(1.0)
    return run_forever(cfg)


def cmd_siren_test(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import sound_selftest

    return sound_selftest(cfg, Stage.SIREN, volume_pct=args.volume)


def cmd_warn_test(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from .runner import sound_selftest

    return sound_selftest(cfg, Stage.WARNING, volume_pct=args.volume)


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


def cmd_selftest(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    from . import selftest

    checks, failures = selftest.run(cfg)
    print(selftest.render(checks))
    print()
    if failures:
        print(f"{failures} check(s) failed — the trap is not ready")
    else:
        warns = sum(1 for check in checks if check.verdict == selftest.WARN)
        print("ready" + (f" ({warns} warning(s))" if warns else ""))
    return 1 if failures else 0


def cmd_calibrate(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """Measure how much an empty room moves by itself, and suggest a threshold above it."""
    from .camera import Camera
    from .detector import Detector

    camera = Camera(cfg)
    if not camera.open():
        print("cannot open camera", cfg.camera.device)
        return 1
    frames = []
    wanted = max(10, int(args.sec * cfg.camera.target_fps))
    try:
        for frame in camera.frames(limit=wanted):
            frames.append(frame)
    finally:
        camera.release()
    if len(frames) < 10:
        print(f"only {len(frames)} frames captured; need at least 10")
        return 1
    stats = Detector(cfg).calibrate(frames)
    print(f"frames            {stats.frames}")
    print(f"mean changed      {stats.mean:.3f} %")
    print(f"p99 changed       {stats.p99:.3f} %")
    print(f"recommended       min_area_pct = {stats.recommended_min_area_pct}")
    print()
    print("Run this in the room you will actually leave the laptop in, at the light level it")
    print("will have at night: the noise floor of a dark frame is not the noise floor of a lit")
    print("one, and the threshold has to clear the worse of the two.")
    return 0


def cmd_mask(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """Capture one frame and write it out so the ignore polygons can be drawn against it."""
    from .camera import Camera

    camera = Camera(cfg)
    if not camera.open():
        print("cannot open camera", cfg.camera.device)
        return 1
    try:
        frame = next(iter(camera.frames(limit=1)), None)
    finally:
        camera.release()
    if frame is None:
        print("no frame captured")
        return 1
    import cv2

    out = cfg.root / "mask-reference.jpg"
    out.parent.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(out), frame)
    height, width = frame.shape[:2]
    scale = cfg.detector.analysis_width / width
    print(f"reference frame   {out}  ({width}x{height})")
    print(f"analysis size     {cfg.detector.analysis_width}x{int(height * scale)}")
    print()
    print("Add polygons in ANALYSIS coordinates to ~/.config/camtrap/config.toml, e.g.:")
    print()
    print("  [detector]")
    print("  ignore_mask = [[[320, 0], [640, 0], [640, 360], [320, 360]]]")
    print()
    print("Mask the window, the curtain, a mirror, the gap under the door, and any indicator")
    print("light: every one of those moves on its own, and a warning nobody triggered is the")
    print("fastest way to stop trusting the trap.")
    return 0


def cmd_install(cfg: config_mod.Config, args: argparse.Namespace) -> int:
    """Install and enable the systemd --user unit from the repository copy."""
    import shutil
    import subprocess
    from pathlib import Path as _Path

    repo_unit = _Path(__file__).resolve().parents[2] / "deploy" / "systemd" / "camtrap.service"
    if not repo_unit.exists():
        print(f"unit not found: {repo_unit}")
        return 1
    unit_dir = (
        _Path(__import__("os").environ.get("XDG_CONFIG_HOME", _Path.home() / ".config"))
        / "systemd"
        / "user"
    )
    unit_dir.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(repo_unit, unit_dir / "camtrap.service")
    print(f"installed {unit_dir / 'camtrap.service'}")
    subprocess.run(["systemctl", "--user", "daemon-reload"], check=False)
    print("run: systemctl --user enable --now camtrap")
    print("first: camtrap selftest && camtrap siren-test && camtrap warn-test")
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

    watch_cmd = sub.add_parser("watch", help="empty-room run: real detection, sound suppressed")
    watch_cmd.add_argument("--minutes", type=float, default=30.0, help="how long to observe")
    watch_cmd.add_argument("--still", type=float, default=10.0, help="quiet seconds before arming")
    watch_cmd.set_defaults(func=cmd_watch)

    report_cmd = sub.add_parser("report", help="summarise a run's log")
    report_cmd.add_argument("--log", default=None, help="path to the log file")
    report_cmd.set_defaults(func=cmd_report)

    drill_cmd = sub.add_parser("drill", help="rehearse the physical checks: cable, mute, lid")
    drill_cmd.add_argument("--volume", type=int, default=40, help="siren volume for the drill")
    drill_cmd.add_argument("--siren-sec", type=float, default=2.0, help="burst length")
    drill_cmd.add_argument("--seconds", type=float, default=180.0, help="how long to stay armed")
    drill_cmd.set_defaults(func=cmd_drill)

    preflight_cmd = sub.add_parser("preflight", help="is this trap fit to be left alone?")
    preflight_cmd.add_argument("--no-probe", action="store_true", help="skip the audio probe")
    preflight_cmd.set_defaults(func=cmd_preflight)

    guard = sub.add_parser("guard", help="preflight, arm when the room goes quiet, then run")
    guard.add_argument("--still", type=float, default=None, help="seconds of quiet before arming")
    guard.add_argument("--no-probe", action="store_true", help="skip the audio probe")
    guard.set_defaults(func=cmd_arm_and_run)

    siren_test = sub.add_parser("siren-test", help="play the siren into the configured sink")
    siren_test.add_argument(
        "--volume", type=int, default=None, help="override the volume, e.g. 30 for a rehearsal"
    )
    siren_test.set_defaults(func=cmd_siren_test)

    warn_test = sub.add_parser("warn-test", help="play the spoken warning in every language")
    warn_test.add_argument("--volume", type=int, default=None, help="override the volume")
    warn_test.set_defaults(func=cmd_warn_test)

    arm = sub.add_parser("arm", help="arm the alarm by hand")
    arm.set_defaults(func=cmd_arm)

    disarm = sub.add_parser("disarm", help="clear a manual arm")
    disarm.set_defaults(func=cmd_disarm)

    input_scan = sub.add_parser("input-scan", help="input devices reporting mute/volume keys")
    input_scan.set_defaults(func=cmd_input_scan)

    selftest_cmd = sub.add_parser("selftest", help="camera, audio, arming, receiver, inhibitors")
    selftest_cmd.set_defaults(func=cmd_selftest)

    calibrate = sub.add_parser("calibrate", help="noise statistics, recommends MIN_AREA_PCT")
    calibrate.add_argument("--sec", type=float, default=30.0, help="seconds to sample")
    calibrate.set_defaults(func=cmd_calibrate)

    mask = sub.add_parser("mask", help="capture a reference frame for the ignore mask")
    mask.set_defaults(func=cmd_mask)

    install = sub.add_parser("install", help="install and enable the systemd --user unit")
    install.set_defaults(func=cmd_install)

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

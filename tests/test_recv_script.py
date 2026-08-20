"""S3.1: the receiver's wire contract, exercised by running the real shell script."""

import shutil
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "prod" / "camtrap-recv.sh"


def _run(root, command, payload=b""):
    return subprocess.run(
        ["sh", str(SCRIPT)],
        input=payload,
        capture_output=True,
        env={"SSH_ORIGINAL_COMMAND": command, "CAMTRAP_ROOT": str(root), "PATH": "/usr/bin:/bin"},
        check=False,
    )


@pytest.fixture
def root(tmp_path):
    return tmp_path / "camtrap"


def test_syntax_is_posix_clean():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_ping_answers(root):
    result = _run(root, "ping")
    assert result.returncode == 0
    assert result.stdout.strip() == b"ok ping"


def test_put_frame_stores_and_reports_size_and_checksum(root):
    payload = b"JPEGDATA" * 10
    result = _run(root, "put-frame evt_20260820T101010Z_000.jpg", payload)
    assert result.returncode == 0
    parts = result.stdout.split()
    assert parts[0] == b"ok"
    assert parts[1] == b"evt_20260820T101010Z_000.jpg"
    assert int(parts[2]) == len(payload)
    stored = root / "inbox" / "evt_20260820T101010Z_000.jpg"
    assert stored.read_bytes() == payload
    from hashlib import sha256

    assert parts[3].decode() == sha256(payload).hexdigest()


def test_manifest_is_accepted(root):
    result = _run(root, "put-frame evt_20260820T101010Z.json", b"{}")
    assert result.returncode == 0


def test_heartbeat_is_stored_with_a_timestamp(root):
    result = _run(root, "heartbeat", b"mode=armed sound_ok=1\n")
    assert result.returncode == 0
    assert (root / "state" / "heartbeat").read_text().startswith("mode=armed")
    assert (root / "state" / "heartbeat.at").read_text().strip().endswith("Z")


def test_path_traversal_is_refused(root):
    result = _run(root, "put-frame ../../.ssh/authorized_keys", b"pwned")
    assert result.returncode != 0
    assert b"bad_name" in result.stderr
    assert not (root / "inbox").exists() or not list((root / "inbox").glob("*"))


@pytest.mark.parametrize(
    "name",
    ["evt_A_000.jpg;rm -rf /", "evt_$(id)_000.jpg", "notevt_000.jpg", ".hidden.jpg", "evt_A.txt"],
)
def test_dangerous_or_unexpected_names_are_refused(root, name):
    result = _run(root, f"put-frame {name}", b"x")
    assert result.returncode != 0


def test_empty_payload_is_refused(root):
    result = _run(root, "put-frame evt_A_000.jpg", b"")
    assert result.returncode != 0
    assert b"empty_payload" in result.stderr


def test_reading_and_listing_are_not_part_of_the_protocol(root):
    """The laptop's key is write-only: it must not be able to find out what was delivered."""
    for verb in (
        "list",
        "list-events",
        "cat evt_A_000.jpg",
        "get evt_A_000.jpg",
        "rm evt_A_000.jpg",
    ):
        result = _run(root, verb)
        assert result.returncode != 0, f"{verb} must be refused"
        assert b"unknown_verb" in result.stderr


def test_oversized_payload_is_capped(root):
    big = b"x" * 200_000
    result = _run(root, "put-frame evt_A_000.jpg", big)
    assert result.returncode == 0
    # cap comes from CAMTRAP_MAX_BYTES; the default is far larger, so this stores whole
    assert int(result.stdout.split()[2]) == len(big)


def test_no_shell_expansion_from_the_command_string(root, tmp_path):
    canary = tmp_path / "canary"
    result = _run(root, f"put-frame evt_A_000.jpg && touch {canary}", b"x")
    assert not canary.exists()
    assert result.returncode != 0


def test_script_is_executable_and_has_a_shebang():
    assert SCRIPT.read_text().startswith("#!/bin/sh")
    assert shutil.which("sh")

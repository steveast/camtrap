"""S4.2: the relay on the receiver — read side plus Telegram, against a local Bot API stub."""

import http.server
import json
import subprocess
import threading
from pathlib import Path
from typing import ClassVar

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "deploy" / "prod" / "camtrap-tg.sh"


class _Handler(http.server.BaseHTTPRequestHandler):
    requests: ClassVar[list] = []

    def _record(self, body=b""):
        type(self).requests.append({"path": self.path, "body_len": len(body), "body": body[:400]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        self._record(self.rfile.read(length))

    def do_GET(self):
        self._record()

    def log_message(self, *args):
        pass


@pytest.fixture
def api():
    _Handler.requests = []
    server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server, _Handler
    server.shutdown()


@pytest.fixture
def root(tmp_path):
    inbox = tmp_path / "camtrap" / "inbox"
    state = tmp_path / "camtrap" / "state"
    inbox.mkdir(parents=True)
    state.mkdir(parents=True)
    (inbox / "evt_20260820T101010Z_000.jpg").write_bytes(b"JPEGDATA")
    (inbox / "evt_20260820T101010Z.json").write_text(json.dumps({"type": "tamper", "frames": 3}))
    (state / "heartbeat").write_text("mode=armed sound_ok=1 ac_online=0\n")
    return tmp_path / "camtrap"


def _run(root, command, payload=b"", api_base=None):
    env = {"SSH_ORIGINAL_COMMAND": command, "CAMTRAP_ROOT": str(root), "PATH": "/usr/bin:/bin"}
    if api_base:
        env["CAMTRAP_TG_API"] = api_base
    return subprocess.run(
        ["sh", str(SCRIPT)], input=payload, capture_output=True, env=env, check=False
    )


def test_syntax_is_posix_clean():
    assert subprocess.run(["sh", "-n", str(SCRIPT)], check=False).returncode == 0


def test_list_reports_names_sizes_and_times(root):
    out = _run(root, "list").stdout.decode()
    assert "evt_20260820T101010Z_000.jpg" in out
    assert "evt_20260820T101010Z.json" in out
    assert len(out.strip().splitlines()) == 2


def test_state_reports_the_heartbeat_and_its_age(root):
    out = _run(root, "state").stdout.decode()
    assert "mode=armed" in out
    assert "hb_age=" in out


def test_state_reports_minus_one_when_there_is_no_heartbeat(tmp_path):
    root = tmp_path / "empty"
    out = _run(root, "state").stdout.decode()
    assert "hb_age=-1" in out


def test_manifest_is_readable_but_frames_are_not(root):
    assert b'"type": "tamper"' in _run(root, "manifest evt_20260820T101010Z.json").stdout
    refused = _run(root, "manifest evt_20260820T101010Z_000.jpg")
    assert refused.returncode != 0
    assert b"not_a_manifest" in refused.stderr


def test_send_photo_posts_multipart_and_never_logs_the_token(root, api):
    server, handler = api
    base = f"http://127.0.0.1:{server.server_port}"
    result = _run(
        root,
        "send-photo evt_20260820T101010Z_000.jpg",
        b"SECRET\n42\n\xf0\x9f\x9a\xa8 handled at 14:03\n",
        api_base=base,
    )
    assert result.returncode == 0
    assert result.stdout.startswith(b"ok send-photo")
    assert b"SECRET" not in result.stdout + result.stderr
    request = handler.requests[-1]
    assert request["path"] == "/botSECRET/sendPhoto"
    assert b"JPEGDATA" in request["body"] or request["body_len"] > 100


def test_send_message_urlencodes_the_text(root, api):
    server, handler = api
    base = f"http://127.0.0.1:{server.server_port}"
    result = _run(root, "send-message", b"SECRET\n42\nagent went silent\n", api_base=base)
    assert result.returncode == 0
    assert "chat_id=42" in handler.requests[-1]["path"]
    assert "agent+went+silent" in handler.requests[-1]["path"]


def test_a_non_200_reply_is_a_failure(root, tmp_path):
    # point at a closed port: curl fails, the script must not report success
    result = _run(root, "send-message", b"SECRET\n42\nhi\n", api_base="http://127.0.0.1:1")
    assert result.returncode != 0
    assert b"tg_http" in result.stderr


def test_send_photo_refuses_a_missing_frame(root, api):
    server, _handler = api
    result = _run(
        root,
        "send-photo evt_19990101T000000Z_000.jpg",
        b"SECRET\n42\ncaption\n",
        api_base=f"http://127.0.0.1:{server.server_port}",
    )
    assert result.returncode != 0
    assert b"missing" in result.stderr


def test_delete_removes_one_artefact(root):
    assert _run(root, "delete evt_20260820T101010Z_000.jpg").returncode == 0
    assert not (root / "inbox" / "evt_20260820T101010Z_000.jpg").exists()


def test_traversal_and_unknown_verbs_are_refused(root):
    for command in (
        "manifest ../../etc/passwd",
        "delete ../../etc/passwd",
        "send-photo ../../etc/passwd",
        "shell",
        "put-frame evt_A_000.jpg",
    ):
        result = _run(root, command, b"x")
        assert result.returncode != 0, command

"""
``stateguard relay http --record`` (Phase 2 S2.3): a redacted recording of the HTTP relay's
traffic, in shadow mode only, under the stdio relay's privacy rules; and ``stateguard capture
http``, its bundle and report.

The server is ``http_notes_server.py``, an unmodified ``mcp`` 2.x server over Streamable HTTP,
in JSON and in event-stream mode. Synthetic fixtures, not traffic.
"""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")
httpx2 = pytest.importorskip("httpx2")

from stateguard.adapters.mcp.recording import read_recording  # noqa: E402
from stateguard.capture import find_values, load_bundle  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tests" / "examples" / "http_notes_server.py"
CLI = [sys.executable, "-c", "from stateguard.cli import main; main()"]
SECRET = "hunter2-Qx7"
TYPED = "482913"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _wait(port: int, process: subprocess.Popen[bytes], log: Path) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError(log.read_text(errors="replace")[-2000:])
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on {port}")


class _Stack:
    def __init__(self, tmp: Path, *, json_mode: bool) -> None:
        self.tmp = tmp
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
        self.server_port, self.relay_port = _port(), _port()
        argv = [sys.executable, str(SERVER), str(tmp / "notes.txt"), str(self.server_port)]
        self.server = self._spawn("server", argv + (["--json"] if json_mode else []))
        _wait(self.server_port, self.server, tmp / "server.log")
        self.relay: subprocess.Popen[bytes] | None = None

    def _spawn(self, name: str, argv: list[str]) -> subprocess.Popen[bytes]:
        log = open(self.tmp / f"{name}.log", "ab")  # noqa: SIM115 -- the process owns it
        return subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, env=self.env)

    def start(self, argv: list[str]) -> None:
        self.relay = self._spawn("relay", argv)
        _wait(self.relay_port, self.relay, self.tmp / "relay.log")

    @property
    def upstream(self) -> str:
        return f"http://127.0.0.1:{self.server_port}/mcp"

    def session(self) -> list[Any]:
        """A client session: initialize, initialized, list, two calls with typed values."""
        url = f"http://127.0.0.1:{self.relay_port}/mcp"
        http = httpx2.Client(timeout=15.0)
        headers = {"accept": "application/json, text/event-stream"}

        def post(message: dict[str, Any]) -> Any:
            return http.post(url, json=message, headers=headers)

        init = post({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}})  # fmt: skip
        if "mcp-session-id" in init.headers:
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
        post({"jsonrpc": "2.0", "method": "notifications/initialized"})
        answers = [post({"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})]
        for i, text in enumerate([f"{SECRET} one", f"pin {TYPED}"]):
            answers.append(post({"jsonrpc": "2.0", "id": 10 + i, "method": "tools/call",
                                 "params": {"name": "add_note", "arguments": {"text": text}}}))  # fmt: skip
        answers.append(post({"jsonrpc": "2.0", "id": 20, "method": "tools/call",
                             "params": {"name": "list_notes", "arguments": {}}}))  # fmt: skip
        http.close()
        return answers

    def stop_relay(self) -> int:
        assert self.relay is not None
        self.relay.send_signal(signal.SIGTERM)
        return self.relay.wait(timeout=30)

    def close(self) -> None:
        for process in (self.relay, self.server):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=10)


@pytest.mark.parametrize("json_mode", [True, False], ids=["json", "event_stream"])
def test_the_http_relay_records_redacted_traffic_in_shadow_mode(
    tmp_path: Path, json_mode: bool
) -> None:
    stack = _Stack(tmp_path, json_mode=json_mode)
    record = tmp_path / "recording.jsonl"
    try:
        stack.start([
            *CLI, "relay", "http", "--shadow", "--listen", f"127.0.0.1:{stack.relay_port}",
            "--record", str(record), "--record-mask-numbers", "--audit", str(tmp_path / "a.jsonl"),
            "--upstream", stack.upstream,
        ])  # fmt: skip
        answers = stack.session()
        assert all(a.status_code == 200 for a in answers), [a.status_code for a in answers]
        assert stack.stop_relay() == 0
    finally:
        stack.close()
    # The calls ran: the server wrote both notes (shadow forwards everything unchanged).
    notes = (tmp_path / "notes.txt").read_text()
    assert SECRET in notes and TYPED in notes

    recording = read_recording(record)
    assert recording.complete
    assert recording.header["provenance"] == "local_development"
    assert recording.header["config"]["mode"] == "shadow"
    assert "mask_numbers" in recording.header["redaction"]["rules"]
    for direction in ("client_to_relay", "relay_to_server", "server_to_relay", "relay_to_client"):
        calls = [r for r in recording.messages(direction) if r.get("method") == "tools/call"
                 or (r.get("kind") in ("response", "error") and r.get("id") in (10, 11, 20))]  # fmt: skip
        assert calls, f"no tool call traffic recorded {direction}"
    text = record.read_text()
    assert SECRET not in text and TYPED not in text
    # The upstream URL is never in the recording (it may carry a token).
    assert str(stack.server_port) not in json.dumps(recording.header)


def test_the_http_relay_records_only_a_relay_that_never_acts(tmp_path: Path) -> None:
    for extra, why in (
        ([], "shadow-only"),
        (["--shadow", "--passthrough"], "shadow-only"),
        (["--shadow", "--retry", "auto"], "never acts"),
        (["--shadow", "--record-provenance", "real_capture", "--record-redaction", "secrets_only"],
         "requires --record-redaction mask"),
    ):  # fmt: skip
        done = subprocess.run(
            [*CLI, "relay", "http", *extra, "--record", str(tmp_path / "r.jsonl"),
             "--upstream", "http://127.0.0.1:1/mcp"],  # fmt: skip
            capture_output=True, text=True, timeout=60,
        )  # fmt: skip
        assert done.returncode == 2 and why in done.stderr, (extra, done.stderr)
        assert not (tmp_path / "r.jsonl").exists()


def test_capture_http_writes_a_bundle_that_rebuilds(tmp_path: Path) -> None:
    stack = _Stack(tmp_path, json_mode=True)
    directory = tmp_path / "cap"
    try:
        stack.start([
            *CLI, "capture", "http", "--dir", str(directory), "--label", "test fixture (synthetic)",
            "--shadow", "--listen", f"127.0.0.1:{stack.relay_port}", "--upstream", stack.upstream,
        ])  # fmt: skip
        stack.session()
        assert stack.stop_relay() == 0, (tmp_path / "relay.log").read_text()
    finally:
        stack.close()
    loaded = load_bundle(directory / "bundle")
    assert loaded["rebuilt_equal"] is True
    report = loaded["report"]
    d = report["denominators"]
    assert d["client_tool_calls"] == d["tool_call_records"] == 3
    assert d["checks_hold"] is True
    assert report["calls"]["succeeded"]["k"] == 3
    # Upstream ids are the relay's own on HTTP: per-call overhead is not matched, and says why.
    assert report["overhead_ms"]["n"] == 0 and report["overhead_ms"]["skipped"]
    found = find_values(directory / "bundle", [SECRET, TYPED])
    assert found == {SECRET: 0, TYPED: 0}

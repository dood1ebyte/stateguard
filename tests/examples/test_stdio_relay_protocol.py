"""
The relay against a scripted server, byte by byte -- no MCP SDK needed.

Covers what a real SDK server does not make easy to provoke: a paginated
listing the relay fetches itself, a tool whose schema changes mid-session
with ``notifications/tools/list_changed``, a schema StateGuard cannot read,
an unknown tool, the protocol ``_meta`` on the relay's own requests, and
results passed through without being re-serialised.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"
SERVER = Path(__file__).parent / "fake_mcp_server.py"
META = {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}

pytestmark = pytest.mark.integration


class Session:
    def __init__(self, tmp: Path, *relay_args: str) -> None:
        self.server_log = tmp / "server.jsonl"
        self.audit = tmp / "audit.jsonl"
        self.proc = subprocess.Popen(
            [
                sys.executable,
                str(RELAY),
                "--audit",
                str(self.audit),
                *relay_args,
                "--",
                sys.executable,
                str(SERVER),
                str(self.server_log),
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=ROOT,
        )
        self._next = 100

    def request(self, method: str, params: dict[str, Any]) -> bytes:
        self._next += 1
        message = {
            "jsonrpc": "2.0",
            "id": self._next,
            "method": method,
            "params": {**params, "_meta": META},
        }
        assert self.proc.stdin and self.proc.stdout
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()
        while True:
            line = self.proc.stdout.readline()
            if json.loads(line).get("id") == self._next:
                return line

    def call(self, name: str, arguments: Any) -> tuple[bytes, dict[str, Any]]:
        line = self.request("tools/call", {"name": name, "arguments": arguments})
        return line, json.loads(line)["result"]

    def received(self) -> list[dict[str, Any]]:
        return [json.loads(entry) for entry in self.server_log.read_text().splitlines()]

    def decisions(self) -> list[dict[str, Any]]:
        return [
            json.loads(entry) for entry in self.audit.read_text().splitlines() if '"call"' in entry
        ]

    def close(self) -> None:
        assert self.proc.stdin
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


@pytest.fixture
def session(tmp_path: Path) -> Any:
    s = Session(tmp_path)
    yield s
    s.close()


def _echoed(result: dict[str, Any]) -> Any:
    return json.loads(result["content"][0]["text"])["arguments"]


def test_the_relay_lists_every_page_itself_with_the_client_s_protocol_meta(
    session: Session,
) -> None:
    session.request("server/discover", {})
    _, result = session.call("search", {"query": "x"})
    assert _echoed(result) == {"query": "x"}
    own = [m for m in session.received() if str(m.get("id", "")).startswith("stateguard-relay:")]
    assert [m["params"].get("cursor") for m in own] == [None, "p2"]
    assert all(m["params"]["_meta"] == META for m in own)


def test_repair_reaches_the_server_and_the_result_comes_back_byte_for_byte(
    session: Session,
) -> None:
    session.request("server/discover", {})
    line, result = session.call("search", {"qury": "cats", "limit": "3"})
    assert _echoed(result) == {"query": "cats", "limit": 3}
    assert line.startswith(b'{"jsonrpc": "2.0",   "id": ')  # the server's own spacing, untouched
    forwarded = [m for m in session.received() if m.get("method") == "tools/call"][-1]
    assert forwarded["params"]["_meta"] == META  # preserved through the repair


def test_a_schema_change_mid_session_is_picked_up(session: Session) -> None:
    session.request("server/discover", {})
    _, before = session.call("search", {"query": "x", "limit": 8})
    assert "isError" not in before
    session.call("flip", {})  # server tightens limit and adds a required 'index'
    deadline = time.time() + 10
    while time.time() < deadline:
        synced = [
            json.loads(entry)
            for entry in session.audit.read_text().splitlines()
            if "tools_synced" in entry
        ]
        if len(synced) >= 2:
            break
        time.sleep(0.05)
    _, after = session.call("search", {"query": "x", "limit": 8})
    assert after["isError"] is True
    assert after["resultType"] == "complete"  # shaped for the request's protocol
    assert "index" in after["content"][0]["text"]
    change = next(e for e in synced[1:] for c in e["changes"] if c["tool"] == "search")
    assert any(c["tool"] == "search" and c["breaking"] for c in change["changes"])


def test_an_unreadable_schema_is_refused_and_never_reaches_the_server(session: Session) -> None:
    session.request("server/discover", {})
    _, result = session.call("broken", {"a": 1})
    assert (
        result["isError"] is True and "cannot read the input schema" in result["content"][0]["text"]
    )
    assert not [m for m in session.received() if m.get("params", {}).get("name") == "broken"]


def test_unreadable_forward_policy(tmp_path: Path) -> None:
    s = Session(tmp_path, "--unreadable", "forward")
    try:
        s.request("server/discover", {})
        _, result = s.call("broken", {"a": 1})
        assert _echoed(result) == {"a": 1}
        assert s.decisions()[-1]["action"] == "unvalidated"
    finally:
        s.close()


def test_an_unknown_tool_is_the_server_s_to_answer(session: Session) -> None:
    session.request("server/discover", {})
    _, result = session.call("nope", {"x": 1})
    assert _echoed(result) == {"x": 1}
    assert session.decisions()[-1]["action"] == "unvalidated"

"""
End to end: ``examples/mcp/keyed_mcpserver.py``, an ``MCPServer`` (the SDK's
high-level server) that adopts the contract with one middleware, as a real
process on stdio and on Streamable HTTP. The server dies (``os._exit``) at a
chosen point; a supervisor (the test) restarts it; an **unmodified** SDK
client resends with the same key. Ground truth is the notes file, read
directly.

Over HTTP there is no relay: a contract-aware client plus a keyed server is
the whole recovery path.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

import anyio  # noqa: E402

from stateguard.adapters.mcp.idempotency import (  # noqa: E402
    IDEMPOTENCY_CAPABILITY,
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "examples" / "mcp" / "keyed_mcpserver.py"
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"


def _argv(tmp: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(SERVER),
        "--db",
        str(tmp / "notes.db"),
        "--notes",
        str(tmp / "notes.txt"),
        "--fault-file",
        str(tmp / "fault.fired"),
        *extra,
    ]


def _notes(tmp: Path) -> list[str]:
    path = tmp / "notes.txt"
    return path.read_text().split("\n")[:-1] if path.exists() else []


def _entries(tmp: Path) -> list[str]:
    return [line.split("\t")[0] for line in _notes(tmp)]


def _outcome(result: Any) -> Any:
    return (result.meta or {}).get(IDEMPOTENCY_OUTCOME)


# -- stdio ---------------------------------------------------------------------------------


def _stdio(command: list[str], body: Any) -> Any:
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run() -> Any:
        params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        async with (
            stdio_client(params, errlog=open(os.devnull, "w")) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await body(session)

    return anyio.run(run)


def test_one_middleware_adopts_the_contract(tmp_path: Path) -> None:
    from mcp.shared.exceptions import MCPError

    async def body(session: Any) -> Any:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        keyed = [
            await session.call_tool("add_note", {"text": "a"}, meta={IDEMPOTENCY_KEY: "k1"})
            for _ in range(2)
        ]
        plain = await session.call_tool("add_note", {"text": "plain"})
        try:
            await session.call_tool("add_note", {"text": "b"}, meta={IDEMPOTENCY_KEY: "k1"})
            mismatch = None
        except MCPError as exc:
            mismatch = exc.error.data
        return tools, keyed, plain, mismatch

    tools, keyed, plain, mismatch = _stdio(_argv(tmp_path), body)
    assert (tools["add_note"].meta or {}).get(IDEMPOTENCY_CAPABILITY) == {"version": 1}
    assert IDEMPOTENCY_CAPABILITY not in (tools["list_notes"].meta or {})
    assert [_outcome(r) for r in keyed] == ["executed", "replayed"]
    assert _outcome(plain) is None and not plain.is_error
    assert mismatch[IDEMPOTENCY_OUTCOME] == "rejected_mismatch"
    # The effect carries its key (current_key()); the unkeyed call's does not.
    assert _notes(tmp_path) == ["a\tk1", "plain"]


def test_through_the_relay_a_crash_after_the_effect_is_recovered(tmp_path: Path) -> None:
    relay = [
        sys.executable,
        str(RELAY),
        "--audit",
        str(tmp_path / "audit.jsonl"),
        "--retry",
        "auto",
        "--restart",
        "2",
        "--",
        *_argv(tmp_path, "--fault", "after_effect"),
    ]

    async def body(session: Any) -> Any:
        return await session.call_tool("add_note", {"text": "via-relay"})

    result = _stdio(relay, body)
    assert not result.is_error and _outcome(result) == "recovered"
    assert (tmp_path / "fault.fired").exists()
    assert _entries(tmp_path) == ["via-relay"]
    events = [json.loads(line) for line in (tmp_path / "audit.jsonl").read_text().splitlines()]
    assert any(e.get("event") == "retry_decision" and e.get("acted") for e in events)


# -- Streamable HTTP ------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


class HttpServer:
    def __init__(self, tmp: Path, *extra: str) -> None:
        self.tmp, self.extra, self.port = tmp, extra, _free_port()
        self.proc: subprocess.Popen[bytes] | None = None

    def start(self) -> HttpServer:
        self.proc = subprocess.Popen(
            _argv(self.tmp, "--http", str(self.port), *self.extra),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", self.port), 0.2).close()
                return self
            except OSError:
                time.sleep(0.1)
        raise RuntimeError("the server did not start")

    def wait_dead(self) -> int:
        assert self.proc is not None
        return self.proc.wait(timeout=30)

    def stop(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            self.proc.terminate()
            self.proc.wait(timeout=30)


def _http_call(port: int, text: str, key: str | None) -> Any:
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run() -> Any:
        async with (
            streamable_http_client(f"http://127.0.0.1:{port}/mcp") as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            meta = {IDEMPOTENCY_KEY: key} if key else None
            with anyio.fail_after(20):
                return await session.call_tool("add_note", {"text": text}, meta=meta)

    return anyio.run(run)


def _crash_then_resend(tmp: Path, point: str, key: str | None) -> Any:
    server = HttpServer(tmp, "--fault", point).start()
    try:
        with pytest.raises(BaseException):  # noqa: B017 -- the connection dies with the server
            _http_call(server.port, "pay", key)
        assert server.wait_dead() == 137
        server.start()  # the supervisor restarts it
        return _http_call(server.port, "pay", key)  # the client resends, same key
    finally:
        server.stop()


@pytest.mark.parametrize(
    "point, outcome",
    [
        ("after_begin", "executed"),  # claimed, never acted: absent, executed once
        ("after_effect", "recovered"),  # the key's own line is there
        ("after_record", "replayed"),
    ],
)
def test_over_http_every_crash_window_ends_in_exactly_one_effect(
    tmp_path: Path, point: str, outcome: str
) -> None:
    result = _crash_then_resend(tmp_path, point, key="http-key")
    assert not result.is_error and _outcome(result) == outcome
    assert _notes(tmp_path) == ["pay\thttp-key"]


def test_over_http_a_resend_without_a_key_duplicates(tmp_path: Path) -> None:
    """The hazard the contract removes: the same crash, a blind resend, two effects."""
    result = _crash_then_resend(tmp_path, "after_effect", key=None)
    assert not result.is_error
    assert _entries(tmp_path) == ["pay", "pay"]

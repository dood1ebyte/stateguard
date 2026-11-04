"""
``examples/mcp/http_proxy.py``, StateGuard's Streamable HTTP relay, end to end:
an unmodified ``mcp`` 2.x server (``http_notes_server.py``) in its own
process, the TEST-ONLY fault proxy (``benchmarks/runtime/http_fault_proxy.py``)
in front of it, the relay in front of that, and the SDK's own client. Ground
truth is the notes file the server writes, and the fault proxy's log of what
reached the server.

The relay only ever resends what cannot run twice: a request that never
reached the server, one the server refused for a lost session, a declared
read, or a call whose key the server honours. A cut response stream is
resumed where the server numbered its events. Anything else is answered
"outcome unknown", and never resent.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp.server.mcpserver")

import anyio  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "tests" / "examples" / "http_notes_server.py"
FAULTS = ROOT / "benchmarks" / "runtime" / "http_fault_proxy.py"
RELAY = ROOT / "examples" / "mcp" / "http_proxy.py"
OUTCOME = "io.stateguard/idempotencyOutcome"
STATE = "io.stateguard/executionState"


def _port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for(port: int, timeout: float = 15.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as sock:
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(f"nothing listening on {port}")


class Rig:
    """Server, fault proxy and relay, each a real process."""

    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.server_port, self.proxy_port, self.relay_port = _port(), _port(), _port()
        self.procs: dict[str, subprocess.Popen[bytes]] = {}
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}

    def _spawn(self, name: str, argv: list[str], env: dict[str, str] | None = None) -> None:
        log = open(self.tmp / f"{name}.log", "ab")  # noqa: SIM115 -- closed by the process's end
        self.procs[name] = subprocess.Popen(
            argv, stdout=log, stderr=subprocess.STDOUT, env=env or self.env
        )

    def server(self, *, keyed: bool = False, resumable: bool = False) -> None:
        argv = [str(SERVER), str(self.tmp / "notes.txt"), str(self.server_port)]
        if resumable:
            argv.append("--resumable")
        env = dict(self.env)
        if keyed:
            env.update(
                STATEGUARD_IDEMPOTENCY_DB=str(self.tmp / "keys.db"),
                STATEGUARD_KEYED_TOOLS="add_note",
            )
            argv = ["-m", "stateguard.adapters.mcp.inject", *argv]
        self._spawn("server", [sys.executable, *argv], env)
        _wait_for(self.server_port)

    def kill_server(self) -> None:
        proc = self.procs.pop("server")
        proc.kill()
        proc.wait(timeout=10)

    def faults(self, mode: str, tool: str = "add_note") -> None:
        self._spawn(
            "faults",
            [
                sys.executable,
                str(FAULTS),
                "--listen",
                f"127.0.0.1:{self.proxy_port}",
                "--upstream",
                f"127.0.0.1:{self.server_port}",
                "--mode",
                mode,
                "--tool",
                tool,
                "--fault-file",
                str(self.tmp / "fault.fired"),
                "--log",
                str(self.tmp / "faults.txt"),
            ],
        )
        _wait_for(self.proxy_port)

    def relay(self, *extra: str) -> None:
        self._spawn(
            "relay",
            [
                sys.executable,
                str(RELAY),
                "--listen",
                f"127.0.0.1:{self.relay_port}",
                "--audit",
                str(self.tmp / "audit.jsonl"),
                *extra,
                "--",
                f"http://127.0.0.1:{self.proxy_port}/mcp",
            ],
        )
        _wait_for(self.relay_port)

    @property
    def relay_url(self) -> str:
        return f"http://127.0.0.1:{self.relay_port}/mcp"

    @property
    def direct_url(self) -> str:  # through the faults, without the relay
        return f"http://127.0.0.1:{self.proxy_port}/mcp"

    def notes(self) -> list[str]:
        path = self.tmp / "notes.txt"
        return path.read_text().split("\n")[:-1] if path.exists() else []

    def calls_reaching_server(self) -> int:
        path = self.tmp / "faults.txt"
        return sum(1 for line in path.read_text().splitlines() if line.startswith("call "))

    def audit(self) -> list[dict[str, Any]]:
        path = self.tmp / "audit.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def events(self) -> list[str]:
        return [entry["event"] for entry in self.audit()]

    def close(self) -> None:
        for proc in self.procs.values():
            proc.kill()
            proc.wait(timeout=10)


@pytest.fixture
def rig(tmp_path: Path) -> Iterator[Rig]:
    rig = Rig(tmp_path)
    yield rig
    rig.close()


def _session(url: str, body: Any, *, survive: bool = False) -> Any:
    """
    One client session running *body*. With *survive*, a session the client
    itself tears down (its transport raising, as without the relay) is the
    returned exception instead of the test's.
    """
    from mcp.client.session import ClientSession
    from mcp.client.streamable_http import streamable_http_client

    async def run() -> Any:
        async with (
            streamable_http_client(url) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            await session.list_tools()
            with anyio.fail_after(30):
                return await body(session)

    if not survive:
        return anyio.run(run)
    try:
        return anyio.run(run)
    except BaseException as exc:  # noqa: BLE001 -- the client's own crash is the outcome
        if isinstance(exc, KeyboardInterrupt):
            raise
        return exc


def _add_note(text: str = "a") -> Any:
    from mcp.shared.exceptions import MCPError

    async def body(session: Any) -> Any:
        try:
            return await session.call_tool("add_note", {"text": text})
        except MCPError as exc:
            return exc

    return body


def _ok(answer: Any) -> bool:
    return hasattr(answer, "is_error") and not answer.is_error


def _state(answer: Any) -> Any:
    return (getattr(answer, "data", None) or {}).get(STATE)


# -- an answer the server gave, or a request it never got: never in doubt ---------------------


def test_a_fault_free_call_passes_unchanged(rig: Rig) -> None:
    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and answer.content[0].text == "added a"
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1


def test_a_lost_session_is_reopened_and_the_request_sent_on_it(rig: Rig) -> None:
    from mcp.shared.exceptions import MCPError

    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto")

    async def body(session: Any) -> Any:
        first = await session.call_tool("add_note", {"text": "a"})
        rig.kill_server()  # the server's sessions die with it
        rig.server()
        try:
            second = await session.call_tool("add_note", {"text": "b"})
        except MCPError as exc:
            second = exc
        return first, second

    first, second = _session(rig.relay_url, body)
    assert _ok(first) and _ok(second)
    assert rig.notes() == ["a", "b"]  # each once: a 404 means the request was not processed
    assert "session_reopened" in rig.events()


def test_without_the_relay_a_lost_session_fails_the_call(rig: Rig) -> None:
    rig.server()
    rig.faults("none")

    async def body(session: Any) -> Any:
        from mcp.shared.exceptions import MCPError

        rig.kill_server()
        rig.server()
        try:
            return await session.call_tool("add_note", {"text": "b"})
        except (MCPError, Exception) as exc:  # noqa: BLE001 -- whatever the client raises
            return exc

    answer = _session(rig.direct_url, body, survive=True)
    assert not _ok(answer)  # the baseline: the client is left with a dead session
    assert rig.notes() == []


def test_a_request_that_could_not_be_sent_is_sent_once_the_server_is_back(rig: Rig) -> None:
    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto", "--connect-retry", "10")

    async def body(session: Any) -> Any:
        rig.procs["faults"].kill()  # nothing listens: the connection cannot be opened
        rig.procs["faults"].wait()

        async def back() -> None:
            await anyio.sleep(1.5)
            rig.faults("none")

        async with anyio.create_task_group() as tg:
            tg.start_soon(back)
            result = await session.call_tool("add_note", {"text": "a"})
        return result

    answer = _session(rig.relay_url, body)
    assert _ok(answer)
    assert rig.notes() == ["a"]


# -- a request that may have executed ------------------------------------------------------------


@pytest.mark.parametrize("mode", ["lost_response", "http_502_after", "cut_stream"])
def test_an_unkeyed_write_that_may_have_executed_is_unknown_never_resent(
    rig: Rig, mode: str
) -> None:
    rig.server()
    rig.faults(mode)
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _state(answer) == "unknown"
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1  # executed once, not resent


@pytest.mark.parametrize("mode", ["kill_before", "http_503_before"])
def test_an_unkeyed_write_cut_before_the_server_is_unknown_too(rig: Rig, mode: str) -> None:
    # The relay cannot tell these from a loss after execution: it never resends them.
    rig.server()
    rig.faults(mode)
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _state(answer) == "unknown"
    assert rig.notes() == [] and rig.calls_reaching_server() == 0


@pytest.mark.parametrize(
    ("mode", "outcome"),
    [
        ("lost_response", "replayed"),
        ("http_502_after", "replayed"),
        ("cut_stream", "replayed"),
        ("kill_before", "executed"),
        ("http_503_before", "executed"),
    ],
)
def test_a_keyed_write_is_resent_with_its_key_and_runs_once(
    rig: Rig, mode: str, outcome: str
) -> None:
    rig.server(keyed=True)
    rig.faults(mode)
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and (answer.meta or {}).get(OUTCOME) == outcome
    assert rig.notes() == ["a"]


def test_a_cut_stream_of_a_resumable_server_is_resumed_without_running_again(rig: Rig) -> None:
    rig.server(resumable=True)
    rig.faults("cut_stream")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and answer.content[0].text == "added a"  # the original answer
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1
    assert "stream_resumed" in rig.events()


def test_a_declared_read_is_requested_again(rig: Rig) -> None:
    effects = rig.tmp / "effects.json"
    effects.write_text(json.dumps({"tools": [{"name": "list_notes", "effect": "read_only"}]}))
    rig.server()
    rig.faults("lost_response", tool="list_notes")
    rig.relay("--retry", "auto", "--effects", str(effects))

    async def body(session: Any) -> Any:
        return await session.call_tool("list_notes", {})

    answer = _session(rig.relay_url, body)
    assert _ok(answer)
    decisions = [e for e in rig.audit() if e["event"] == "retry_decision"]
    assert decisions and decisions[0]["retry"] is True


# -- the baselines: no relay, and a client that blindly resends ----------------------------------


def test_without_the_relay_a_lost_response_reaches_the_client_as_a_transport_failure(
    rig: Rig,
) -> None:
    rig.server()
    rig.faults("lost_response")
    answer = _session(rig.direct_url, _add_note(), survive=True)
    assert not _ok(answer)  # the client cannot tell this apart from "it did not run"
    assert rig.notes() == ["a"]


def test_a_blind_resend_without_a_key_duplicates_the_effect(rig: Rig) -> None:
    rig.server()
    rig.faults("lost_response")
    for _ in range(2):  # the naive client: on any failure, a new session and the call again
        if _ok(_session(rig.direct_url, _add_note(), survive=True)):
            break
    assert rig.notes() == ["a", "a"]  # what the relay's rules exist to prevent


# -- servers that answer 400 for a session they no longer have ------------------------------------


def _relay_module() -> Any:
    import importlib.util

    spec = importlib.util.spec_from_file_location("http_proxy_under_test", RELAY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("body", "lost"),
    [
        # @modelcontextprotocol/server-everything 2026.8.31, verbatim
        (
            b'{"jsonrpc":"2.0","error":{"code":-32000,'
            b'"message":"Bad Request: No valid session ID provided"},"id":4}',
            True,
        ),
        (b'{"jsonrpc":"2.0","error":{"code":-32602,"message":"Invalid params"},"id":4}', False),
        (b'{"jsonrpc":"2.0","result":{},"id":4}', False),
        (b"Bad Request", False),
        (b"", False),
    ],
)
def test_a_400_counts_as_a_lost_session_only_when_it_names_the_session(
    body: bytes, lost: bool
) -> None:
    assert _relay_module().says_session_unknown(body) is lost


# -- a deadline for what can be sent again -------------------------------------------------------


@pytest.mark.parametrize(
    ("mode", "outcome"), [("hang_after", "replayed"), ("hang_before", "executed")]
)
def test_a_keyed_write_without_an_answer_in_time_is_raced_and_runs_once(
    rig: Rig, mode: str, outcome: str
) -> None:
    rig.server(keyed=True)
    rig.faults(mode)
    rig.relay("--retry", "auto", "--call-deadline", "1.5")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and (answer.meta or {}).get(OUTCOME) == outcome
    assert rig.notes() == ["a"]


def test_a_declared_read_without_an_answer_in_time_is_requested_again(rig: Rig) -> None:
    effects = rig.tmp / "effects.json"
    effects.write_text(json.dumps({"tools": [{"name": "list_notes", "effect": "read_only"}]}))
    rig.server()
    rig.faults("hang_after", tool="list_notes")
    rig.relay("--retry", "auto", "--effects", str(effects), "--call-deadline", "1.5")

    async def body(session: Any) -> Any:
        return await session.call_tool("list_notes", {})

    assert _ok(_session(rig.relay_url, body))


def test_an_unkeyed_write_is_never_cut_short_by_the_deadline(rig: Rig) -> None:
    from mcp.shared.exceptions import MCPError

    rig.server()
    rig.faults("hang_after")
    rig.relay("--retry", "auto", "--call-deadline", "0.5")

    async def body(session: Any) -> Any:
        try:
            with anyio.fail_after(4):  # the client's own patience, not the relay's
                return await session.call_tool("add_note", {"text": "a"})
        except (TimeoutError, MCPError) as exc:
            return exc

    answer = _session(rig.relay_url, body)
    assert isinstance(answer, TimeoutError)  # still waiting for the server: no "unknown" invented
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1  # and never resent


# -- calls checked against the server's own schemas ----------------------------------------------


def _call(name: str, arguments: dict[str, Any]) -> Any:
    from mcp.shared.exceptions import MCPError

    async def body(session: Any) -> Any:
        try:
            return await session.call_tool(name, arguments)
        except MCPError as exc:
            return exc

    return body


def test_a_drifted_call_is_repaired_on_a_freshly_listed_schema(rig: Rig) -> None:
    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _call("add_note", {"Text": "a"}))
    assert _ok(answer) and rig.notes() == ["a"]
    calls = [e for e in rig.audit() if e["event"] == "call"]
    assert calls[-1]["proposal"] == "repair" and calls[-1]["listing"] == "confirmed"


def test_a_call_with_no_safe_correction_is_refused_and_never_reaches_the_server(rig: Rig) -> None:
    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _call("add_note", {}))
    assert getattr(answer, "is_error", False) is True
    assert rig.calls_reaching_server() == 0 and rig.notes() == []


def test_in_shadow_mode_a_drifted_call_is_forwarded_as_received(rig: Rig) -> None:
    rig.server()
    rig.faults("none")
    rig.relay("--retry", "auto", "--shadow")
    _session(rig.relay_url, _call("add_note", {"Text": "a"}))
    assert rig.calls_reaching_server() == 1 and rig.notes() == []  # the server rejected it itself
    calls = [e for e in rig.audit() if e["event"] == "call"]
    assert calls[-1]["action"] == "hold" and calls[-1]["proposal"] == "repair"


class _KeepAliveUpstream(socketserver.StreamRequestHandler):
    """Answers every request as an event stream saying ``keep-alive``, and never closes."""

    def handle(self) -> None:
        while True:
            length = 0
            while (line := self.rfile.readline()) not in (b"\r\n", b""):
                if line.lower().startswith(b"content-length:"):
                    length = int(line.split(b":", 1)[1])
            if line == b"":
                return
            self.rfile.read(length)
            event = b'event: message\r\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\r\n\r\n'
            self.wfile.write(
                b"HTTP/1.1 200 OK\r\nconnection: keep-alive\r\n"
                b"content-type: text/event-stream\r\ntransfer-encoding: chunked\r\n\r\n"
                + f"{len(event):x}\r\n".encode()
                + event
                + b"\r\n0\r\n\r\n"
            )
            self.wfile.flush()


def test_the_fault_proxy_ends_each_exchange_even_when_the_server_keeps_it_alive(
    rig: Rig,
) -> None:
    """
    A server that answers ``keep-alive`` whatever the request asked (Express's
    event streams do): the proxy still answers ``Connection: close``, so a
    client never sends its next request down a connection the proxy no longer
    reads. Before, that request waited for the server's own idle timeout.
    """
    import httpx2

    upstream = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _KeepAliveUpstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    try:
        rig.server_port = int(upstream.server_address[1])
        rig.faults("none")
        with httpx2.Client(timeout=3.0) as client:
            for n in range(3):
                answer = client.post(
                    rig.direct_url,
                    json={"jsonrpc": "2.0", "id": n, "method": "ping"},
                    headers={"accept": "application/json, text/event-stream"},
                )
                assert answer.status_code == 200
                assert answer.headers["connection"] == "close"
                assert '"result"' in answer.text
    finally:
        upstream.shutdown()
        upstream.server_close()

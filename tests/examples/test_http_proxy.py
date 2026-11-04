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
import shutil
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


# -- cancellations go to the request they name, and to nothing else ------------------------------


class _HoldingUpstream:
    """
    A minimal MCP server over HTTP that holds every ``tools/call`` open until
    it is cancelled (or a few seconds pass), and records each cancellation as
    (its session, the request id it names). With *sessions* off it is
    stateless: no ``Mcp-Session-Id``, as the 2026-07-28 revision.
    """

    def __init__(self, *, sessions: bool = True) -> None:
        import http.server

        self.sessions = sessions
        self.calls: list[tuple[str | None, str]] = []
        self.cancels: list[tuple[str | None, str]] = []
        self._held: dict[tuple[str | None, str], threading.Event] = {}
        self._lock = threading.Lock()
        self._opened = 0
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *args: Any) -> None:
                pass

            def do_POST(self) -> None:
                message = json.loads(self.rfile.read(int(self.headers["content-length"])))
                session = self.headers.get("mcp-session-id")
                method = message.get("method")
                headers: dict[str, str] = {}
                if method == "initialize":
                    if upstream.sessions:
                        with upstream._lock:
                            upstream._opened += 1
                            headers["mcp-session-id"] = f"up-{upstream._opened}"
                    result: Any = {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "holding", "version": "0"},
                    }
                elif method == "notifications/cancelled":
                    target = (session, str(message["params"]["requestId"]))
                    with upstream._lock:
                        upstream.cancels.append(target)
                        held = upstream._held.get(target)
                    if held is not None:
                        held.set()
                    self._send(202, None, {})
                    return
                elif "id" not in message:
                    self._send(202, None, {})
                    return
                elif method == "tools/list":
                    result = {"tools": []}
                elif method == "tools/call":
                    key = (session, str(message["id"]))
                    released = threading.Event()
                    with upstream._lock:
                        upstream.calls.append(key)
                        upstream._held[key] = released
                    released.wait(4.0)
                    result = {"content": [{"type": "text", "text": "done"}], "isError": False}
                else:
                    result = {}
                self._send(200, {"jsonrpc": "2.0", "id": message["id"], "result": result}, headers)

            def _send(self, status: int, body: Any, headers: dict[str, str]) -> None:
                data = json.dumps(body).encode() if body is not None else b""
                self.send_response(status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                for name, value in headers.items():
                    self.send_header(name, value)
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = int(self.server.server_address[1])
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def wait_for_calls(self, n: int, timeout: float = 10.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.calls) < n:
            assert time.monotonic() < deadline, f"{len(self.calls)} calls reached the server"
            time.sleep(0.02)

    def close(self) -> None:
        with self._lock:
            for held in self._held.values():
                held.set()
        self.server.shutdown()
        self.server.server_close()


class _Client:
    """A raw Streamable HTTP client whose request ids the test chooses."""

    def __init__(self, url: str) -> None:
        import httpx2

        self.url = url
        self.http = httpx2.Client(timeout=15.0)
        self.headers = {"accept": "application/json, text/event-stream"}
        answer = self.post(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "t", "version": "0"},
                },
            }
        )
        if "mcp-session-id" in answer.headers:
            self.headers["mcp-session-id"] = answer.headers["mcp-session-id"]
        self.post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def post(self, message: dict[str, Any]) -> Any:
        return self.http.post(self.url, json=message, headers=self.headers)

    def call_in_background(self, request_id: int, tool: str = "slow") -> threading.Thread:
        message = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": tool, "arguments": {}},
        }
        thread = threading.Thread(target=self.post, args=(message,), daemon=True)
        thread.start()
        return thread

    def cancel(self, request_id: int) -> None:
        self.post(
            {
                "jsonrpc": "2.0",
                "method": "notifications/cancelled",
                "params": {"requestId": request_id},
            }
        )


@pytest.fixture
def holding(request: pytest.FixtureRequest) -> Iterator[_HoldingUpstream]:
    upstream = _HoldingUpstream(sessions=getattr(request, "param", True))
    yield upstream
    upstream.close()


def test_a_cancellation_reaches_only_its_own_clients_request(
    rig: Rig, holding: _HoldingUpstream
) -> None:
    """
    Two clients, each with a call numbered 7 in flight: client A's
    cancellation goes to A's call on A's session -- never to B's call, which
    shares the client-side id.
    """
    rig.proxy_port = holding.port
    rig.relay()
    a, b = _Client(rig.relay_url), _Client(rig.relay_url)
    calls = [a.call_in_background(7)]
    holding.wait_for_calls(1)
    calls.append(b.call_in_background(7))
    holding.wait_for_calls(2)
    a.cancel(7)
    calls[0].join(10)
    a_call = holding.calls[0]
    assert holding.cancels == [a_call]
    assert holding.calls[1][0] != a_call[0]  # B's call is on another session, and still held
    assert calls[1].is_alive()


@pytest.mark.parametrize("holding", [False], indirect=True)
def test_without_sessions_an_ambiguous_cancellation_is_dropped(
    rig: Rig, holding: _HoldingUpstream
) -> None:
    """
    A stateless server, two clients with a call numbered 7 each: the relay
    cannot tell whose cancellation it is, so it sends none rather than risk
    stopping the other client's call. One call alone is cancelled as usual.
    """
    rig.proxy_port = holding.port
    rig.relay()
    a, b = _Client(rig.relay_url), _Client(rig.relay_url)
    first = a.call_in_background(7)
    holding.wait_for_calls(1)
    b.call_in_background(7)
    holding.wait_for_calls(2)
    a.cancel(7)
    time.sleep(0.5)
    assert holding.cancels == []
    assert "cancel_ambiguous" in rig.events()
    holding.close()  # both calls answered: neither is live any more
    first.join(10)


@pytest.mark.parametrize("holding", [False], indirect=True)
def test_without_sessions_a_single_call_is_cancelled(rig: Rig, holding: _HoldingUpstream) -> None:
    rig.proxy_port = holding.port
    rig.relay()
    a = _Client(rig.relay_url)
    call = a.call_in_background(7)
    holding.wait_for_calls(1)
    a.cancel(7)
    call.join(10)
    assert holding.cancels == holding.calls


def test_a_raced_requests_cancellation_reaches_both_attempts(
    rig: Rig, holding: _HoldingUpstream
) -> None:
    """A declared read past its deadline has two attempts open; cancelling it reaches both."""
    effects = rig.tmp / "effects.json"
    effects.write_text(json.dumps({"tools": [{"name": "slow", "effect": "read_only"}]}))
    rig.proxy_port = holding.port
    rig.relay("--retry", "auto", "--call-deadline", "0.5", "--effects", str(effects))
    a = _Client(rig.relay_url)
    call = a.call_in_background(7)
    holding.wait_for_calls(2)  # the original, and the attempt after the deadline
    a.cancel(7)
    call.join(10)
    assert sorted(holding.cancels) == sorted(holding.calls)


# -- a TypeScript server over HTTP, keyed by the preload -----------------------------------------

NODE = shutil.which("node") or "/opt/node22/bin/node"
TS = ROOT / "adapters" / "typescript"
TS_SERVER = TS / "test" / "fixtures" / "http_notes_server.ts"
PRELOAD = TS / "src" / "preload.ts"


def _node_ready() -> bool:
    if not Path(NODE).exists() or not (TS / "node_modules" / "@modelcontextprotocol").exists():
        return False
    out = subprocess.run([NODE, "--version"], capture_output=True, text=True, check=False)
    major, minor = (int(part) for part in out.stdout.strip().lstrip("v").split(".")[:2])
    return (major, minor) >= (22, 18)  # type stripping without a flag


needs_node = pytest.mark.skipif(
    not _node_ready(), reason="needs Node >= 22.18 and adapters/typescript deps"
)


def _ts_server(rig: Rig, *, keyed: bool = True, fault: str | None = None) -> None:
    """The TypeScript SDK's HTTP server (one McpServer per session), keys only from the preload."""
    env = dict(rig.env)
    argv = [NODE, "--no-warnings"]
    if keyed:
        env.update(
            STATEGUARD_IDEMPOTENCY_DB=str(rig.tmp / "keys.db"), STATEGUARD_KEYED_TOOLS="add_note"
        )
    if fault is not None:
        env.update(STATEGUARD_FAULT=fault, STATEGUARD_FAULT_FILE=str(rig.tmp / "crash.fired"))
    if keyed or fault is not None:
        argv += ["--import", str(PRELOAD)]
    argv += [str(TS_SERVER), str(rig.tmp / "notes.txt"), str(rig.server_port)]
    rig._spawn("server", argv, env)
    _wait_for(rig.server_port)


@needs_node
def test_a_typescript_server_over_http_answers_through_the_relay(rig: Rig) -> None:
    _ts_server(rig)
    rig.faults("none")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and (answer.meta or {}).get(OUTCOME) == "executed"
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1


@needs_node
@pytest.mark.parametrize("mode", ["lost_response", "http_502_after", "cut_stream"])
def test_a_typescript_keyed_write_over_http_is_resent_and_runs_once(rig: Rig, mode: str) -> None:
    """
    The preload keys the per-session servers of an HTTP deployment as it keys a
    stdio one: the relay resends the lost call with its key, and the server's
    store answers it ``replayed`` -- the note is written once.
    """
    _ts_server(rig)
    rig.faults(mode)
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _ok(answer) and (answer.meta or {}).get(OUTCOME) == "replayed"
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 2


@needs_node
def test_without_keys_a_typescript_write_over_http_is_unknown_never_resent(rig: Rig) -> None:
    _ts_server(rig, keyed=False)
    rig.faults("lost_response")
    rig.relay("--retry", "auto")
    answer = _session(rig.relay_url, _add_note())
    assert _state(answer) == "unknown"
    assert rig.notes() == ["a"] and rig.calls_reaching_server() == 1


@needs_node
def test_a_lost_typescript_session_is_reopened_and_the_keyed_call_runs_once(rig: Rig) -> None:
    """The server restarts between the client's handshake and its call: every session is gone."""
    _ts_server(rig)
    rig.faults("none")
    rig.relay("--retry", "auto")

    async def body(session: Any) -> Any:
        rig.kill_server()
        _ts_server(rig)
        return await _add_note()(session)

    answer = _session(rig.relay_url, body)
    assert _ok(answer) and (answer.meta or {}).get(OUTCOME) == "executed"
    assert rig.notes() == ["a"]
    assert "session_reopened" in rig.events()


@needs_node
def test_a_typescript_crash_after_the_effect_is_in_doubt_over_http(rig: Rig) -> None:
    """
    The server dies (SIGKILL) after the note is written and before its key is
    recorded. The relay resends the call with its key to the restarted server,
    which answers "in doubt": reported, never run again.
    """
    _ts_server(rig, fault="after_effect")
    rig.proxy_port = rig.server_port  # no fault proxy: a dead server refuses the connection
    rig.relay("--retry", "auto", "--connect-retry", "15")
    restarted = threading.Event()

    def restart() -> None:
        rig.procs["server"].wait(timeout=30)  # the crash
        rig.procs.pop("server")
        _ts_server(rig)  # the fault fires once per fault file
        restarted.set()

    threading.Thread(target=restart, daemon=True).start()
    answer = _session(rig.relay_url, _add_note())
    assert restarted.is_set()
    assert getattr(answer, "code", None) == -32010, answer  # in doubt
    assert rig.notes() == ["a"]  # the effect happened once, and was not run again

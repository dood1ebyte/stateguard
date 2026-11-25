"""
S4.1, schema-repair safety: when the server rejects a call StateGuard repaired, the relay sends
the client's own call instead -- once, and only where that cannot run anything twice:

* the server refused the arguments themselves (JSON-RPC ``-32602``: nothing ran), or
* the operator declared the tool safe to repeat (``read_only`` or ``idempotent``).

Otherwise the server's error is delivered as sent (the repaired call may have run). Every
fallback is in the audit log (``repair_fallback``).

The server here publishes one schema and validates another (its published ``limit`` is an
integer; it accepts only the string the client sends), so the relay's repair -- the string
``"5"`` to the integer ``5`` -- is wrong for it. Synthetic fixtures, not traffic.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import IO, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import RelayMode, Rig, ScriptedServer, ServerCall, ServerTool  # noqa: E402

from stateguard.adapters.mcp import EffectPolicy  # noqa: E402

SEARCH = ServerTool(
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {"query": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["query"],
            "additionalProperties": False,
        },
    }
)


class DisagreeingServer(ScriptedServer):
    """Rejects an integer ``limit`` (its published schema says integer; it wants a string)."""

    rejection: str = "invalid_params"

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        arguments = params.get("arguments") or {}
        if isinstance(arguments.get("limit"), int):
            self.executed.append(ServerCall(-1, "search", arguments, message))  # it was sent
            if self.rejection == "invalid_params":
                error = {"code": -32602, "message": "limit: expected a string"}
                self._write(writer, _json({"jsonrpc": "2.0", "id": identifier, "error": error}))
            else:
                self._reply(writer, identifier, {
                    "content": [{"type": "text", "text": "limit: expected a string"}],
                    "isError": True})  # fmt: skip
            return True
        if isinstance(arguments.get("limit"), str):  # what it really accepts
            self.executed.append(ServerCall(-1, "search", arguments, message))
            self._reply(writer, identifier, {"content": [{"type": "text", "text": "ok"}],
                                             "isError": False})  # fmt: skip
            return True
        return super()._call(identifier, params, message, writer)


def _json(message: dict[str, Any]) -> bytes:
    import json

    return json.dumps(message).encode()


def _run(rejection: str, effects: EffectPolicy | None = None) -> tuple[Rig, Any]:
    server = DisagreeingServer([SEARCH])
    server.rejection = rejection
    rig = Rig(server, RelayMode.AUTO, effects=effects)
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig, rig.call("search", {"query": "x", "limit": "5"})


def _fallbacks(rig: Rig) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == "repair_fallback"]


def test_a_repair_refused_as_invalid_params_falls_back_to_the_clients_call() -> None:
    rig, answer = _run("invalid_params")
    try:
        assert answer is not None and answer["result"]["isError"] is False, answer
        sent = [call.arguments for call in rig.server.executed]
        # The repaired call (refused, nothing ran), then the client's own, which ran once.
        assert sent == [{"query": "x", "limit": 5}, {"query": "x", "limit": "5"}]
        (fallback,) = _fallbacks(rig)
        assert fallback["acted"] is True and "invalid params" in fallback["reason"]
    finally:
        rig.close()


def test_a_repair_answered_with_a_tool_error_is_not_resent_unless_safe_to_repeat() -> None:
    """A tool error may mean the repaired call ran: never resent for an undeclared tool."""
    rig, answer = _run("tool_error")
    try:
        assert answer is not None and answer["result"]["isError"] is True  # delivered as sent
        assert [call.arguments for call in rig.server.executed] == [{"query": "x", "limit": 5}]
        (fallback,) = _fallbacks(rig)
        assert fallback["acted"] is False and "may have run" in fallback["reason"]
    finally:
        rig.close()


@pytest.mark.parametrize("effect", ["read_only", "idempotent"])
def test_a_tool_declared_safe_to_repeat_falls_back_after_a_tool_error(effect: str) -> None:
    rig, answer = _run("tool_error", EffectPolicy({"search": effect}))
    try:
        assert answer is not None and answer["result"]["isError"] is False, answer
        sent = [call.arguments for call in rig.server.executed]
        assert sent == [{"query": "x", "limit": 5}, {"query": "x", "limit": "5"}]
        (fallback,) = _fallbacks(rig)
        assert fallback["acted"] is True and effect in fallback["reason"]
    finally:
        rig.close()


def test_a_declared_non_idempotent_tool_never_falls_back_after_a_tool_error() -> None:
    rig, answer = _run("tool_error", EffectPolicy({"search": "non_idempotent"}))
    try:
        assert answer is not None and answer["result"]["isError"] is True
        assert len(rig.server.executed) == 1
        (fallback,) = _fallbacks(rig)
        assert fallback["acted"] is False
    finally:
        rig.close()


def test_a_call_the_relay_did_not_change_never_falls_back() -> None:
    server = DisagreeingServer([SEARCH])
    rig = Rig(server, RelayMode.AUTO)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        answer = rig.call("search", {"query": "x", "limit": 5})  # valid as published: unchanged
        assert answer is not None and "error" in answer  # the server's own refusal, delivered
        assert len(server.executed) == 1 and _fallbacks(rig) == []
    finally:
        rig.close()


def test_shadow_never_falls_back() -> None:
    server = DisagreeingServer([SEARCH])
    rig = Rig(server, RelayMode.SHADOW)
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        answer = rig.call("search", {"query": "x", "limit": "5"})  # shadow sends it as is
        assert answer is not None and answer["result"]["isError"] is False
        assert [call.arguments for call in server.executed] == [{"query": "x", "limit": "5"}]
        assert _fallbacks(rig) == []
    finally:
        rig.close()


# -- the HTTP relay -------------------------------------------------------------------------------


class _Upstream:
    """A raw Streamable HTTP upstream (JSON answers) that publishes one schema, enforces another."""

    def __init__(self) -> None:
        import http.server
        import json
        import threading

        calls: list[Any] = []
        self.calls = calls
        tool = SEARCH.definition

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_: Any) -> None:
                pass

            def do_POST(self) -> None:  # noqa: N802 -- the http.server name
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                if "id" not in body:
                    self.send_response(202)
                    self.end_headers()
                    return
                method, ident = body.get("method"), body["id"]
                if method == "initialize":
                    result: Any = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "disagreeing", "version": "1"}}  # fmt: skip
                    answer: Any = {"jsonrpc": "2.0", "id": ident, "result": result}
                elif method == "tools/list":
                    answer = {"jsonrpc": "2.0", "id": ident, "result": {"tools": [tool]}}
                else:
                    arguments = body["params"].get("arguments") or {}
                    calls.append(arguments)
                    if isinstance(arguments.get("limit"), int):
                        answer = {"jsonrpc": "2.0", "id": ident,
                                  "error": {"code": -32602, "message": "limit: expected a string"}}  # fmt: skip
                    else:
                        answer = {"jsonrpc": "2.0", "id": ident, "result": {
                            "content": [{"type": "text", "text": "ok"}], "isError": False}}  # fmt: skip
                data = json.dumps(answer).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def _http_call(*relay_flags: str) -> tuple[_Upstream, Any, list[dict[str, Any]]]:
    import json
    import os
    import socket
    import subprocess
    import tempfile
    import time

    httpx2 = pytest.importorskip("httpx2")
    upstream = _Upstream()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    audit = Path(tempfile.mkdtemp()) / "audit.jsonl"
    root = Path(__file__).resolve().parents[2]
    relay = subprocess.Popen(
        [sys.executable, "-c", "from stateguard.cli import main; main()", "relay", "http",
         "--listen", f"127.0.0.1:{port}", "--audit", str(audit), *relay_flags,
         "--upstream", f"http://127.0.0.1:{upstream.port}/mcp"],  # fmt: skip
        env={**os.environ, "PYTHONPATH": str(root / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            with socket.socket() as s:
                if s.connect_ex(("127.0.0.1", port)) == 0:
                    break
            time.sleep(0.05)
        client = httpx2.Client(timeout=15.0)
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {"accept": "application/json, text/event-stream"}
        client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                               "clientInfo": {"name": "t", "version": "0"}}})  # fmt: skip
        client.post(
            url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        answer = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 3,
                             "method": "tools/call", "params": {"name": "search",
                             "arguments": {"query": "x", "limit": "5"}}}).json()  # fmt: skip
        client.close()
    finally:
        relay.terminate()
        relay.wait(timeout=30)
        upstream.close()
    events = [json.loads(line) for line in audit.read_text().splitlines() if line.strip()]
    return upstream, answer, events


def test_the_http_relay_falls_back_when_a_repair_is_refused_as_invalid_params() -> None:
    upstream, answer, events = _http_call()
    assert "result" in answer and answer["result"]["isError"] is False, answer
    assert upstream.calls == [{"query": "x", "limit": 5}, {"query": "x", "limit": "5"}]
    (fallback,) = [e for e in events if e.get("event") == "repair_fallback"]
    assert fallback["acted"] is True


def test_the_http_relay_with_refresh_off_never_repairs_on_an_unconfirmed_listing() -> None:
    upstream, answer, events = _http_call("--no-listing-refresh")
    assert "result" in answer and answer["result"]["isError"] is False, answer
    assert upstream.calls == [{"query": "x", "limit": "5"}]  # as the client sent it
    (decision,) = [e for e in events if e.get("event") == "call"]
    assert decision["proposal"] == "withheld" and decision["listing"] == "unconfirmed"
    assert not [e for e in events if e.get("event") == "repair_fallback"]

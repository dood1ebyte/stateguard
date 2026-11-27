"""
S4.4: the HTTP relay reads an answered event stream to its end in the background, so the
connection to the server is reused.

Before, the relay closed the stream at the answer event, before the server's end of stream.
httpx keeps a connection only once its response was read to the end, so every call answered
over an event stream paid a new TCP connection. The upstream here counts the connections it
accepts. Synthetic fixtures, not traffic.
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


class _Upstream:
    """Streamable HTTP over HTTP/1.1 keep-alive: JSON for the handshake, events for calls."""

    def __init__(self, *, hold_open: float = 0.0) -> None:
        import http.server

        self.connections = 0
        lock = threading.Lock()
        upstream = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            disable_nagle_algorithm = True  # as asyncio servers (uvicorn) and Node set it

            def setup(self) -> None:
                super().setup()
                with lock:
                    upstream.connections += 1

            def log_message(self, *_: Any) -> None:
                pass

            def _send(self, status: int, body: bytes, kind: str, *, close: bool = False) -> None:
                self.send_response(status)
                self.send_header("content-type", kind)
                if close:
                    self.send_header("connection", "close")
                else:
                    self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.wfile.flush()

            def do_POST(self) -> None:  # noqa: N802 -- the http.server name
                body = json.loads(self.rfile.read(int(self.headers.get("content-length", 0))))
                if "id" not in body:
                    self._send(202, b"", "application/json")
                    return
                method, ident = body.get("method"), body["id"]
                if method == "initialize":
                    result: Any = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}},
                                   "serverInfo": {"name": "events", "version": "1"}}  # fmt: skip
                elif method == "tools/list":
                    result = {"tools": [{"name": "echo", "inputSchema": {"type": "object"}}]}
                else:
                    result = {"content": [{"type": "text", "text": "ok"}], "isError": False}
                answer = {"jsonrpc": "2.0", "id": ident, "result": result}
                if method != "tools/call":
                    self._send(200, json.dumps(answer).encode(), "application/json")
                    return
                event = f"event: message\ndata: {json.dumps(answer)}\n\n".encode()
                if not hold_open:
                    self._send(200, event, "text/event-stream")
                    return
                # A server that keeps the stream open after the answer (then closes it).
                self._send(200, event, "text/event-stream", close=True)
                time.sleep(hold_open)
                self.close_connection = True

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _session(upstream: _Upstream, calls: int) -> tuple[list[Any], list[float], list[Any]]:
    httpx2 = pytest.importorskip("httpx2")
    port = _free_port()
    audit = Path(tempfile.mkdtemp()) / "audit.jsonl"
    relay = subprocess.Popen(
        [sys.executable, "-m", "stateguard", "relay", "http", "--listen", f"127.0.0.1:{port}",
         "--audit", str(audit), "--upstream", f"http://127.0.0.1:{upstream.port}/mcp"],
        env={**os.environ, "PYTHONPATH": str(ROOT / "src")},
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    answers, seconds = [], []
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
        client.post(url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"})  # fmt: skip
        client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        for n in range(calls):
            t0 = time.monotonic()
            reply = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 10 + n,
                                "method": "tools/call", "params": {"name": "echo",
                                "arguments": {}}})  # fmt: skip
            seconds.append(time.monotonic() - t0)
            lines = [x for x in reply.text.splitlines() if x.startswith("data: ")]
            answers.append(json.loads(lines[-1][6:]) if lines else reply.json())
        client.close()
        time.sleep(0.3)  # let the last background drain finish
    finally:
        relay.terminate()
        relay.wait(timeout=30)
    events = [json.loads(x) for x in audit.read_text().splitlines() if x.strip()]
    return answers, seconds, events


def test_calls_answered_over_event_streams_reuse_one_connection() -> None:
    upstream = _Upstream()
    try:
        answers, _, events = _session(upstream, 6)
    finally:
        upstream.close()
    assert all(a["result"]["isError"] is False for a in answers), answers
    # The handshake, the listing and six calls, one after another inside the pool's idle
    # timeout: one connection (two allowed, for a pool race at start). Before S4.4: one per
    # call, so 7 or more.
    assert upstream.connections <= 2, upstream.connections
    assert not [e for e in events if e.get("event") == "stream_not_drained"]


def test_a_stream_held_open_after_the_answer_never_delays_the_client() -> None:
    upstream = _Upstream(hold_open=3.0)
    try:
        answers, seconds, events = _session(upstream, 1)
        time.sleep(2.2)  # past the drain bound
    finally:
        upstream.close()
    assert answers[0]["result"]["isError"] is False
    assert seconds[0] < 1.5, seconds  # the answer is relayed at once, not after the drain

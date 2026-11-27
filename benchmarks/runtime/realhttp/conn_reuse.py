"""
S4.4 (TEST-ONLY): connections to the server, and time per call, for calls answered over event
streams through the HTTP relay.

The server is the cooperating notes server (``examples/mcp/cooperating_notes_server.py``,
MCP Python SDK 2.x, Streamable HTTP, which answers ``tools/call`` with an event stream),
unkeyed. A TCP proxy in front of it counts the connections it accepts. The relay is
``stateguard relay http``, run from ``--src`` (a checkout's ``src``), so two versions can be
compared on one machine. The client makes CALLS sequential ``list_notes`` calls on one session.

    conn_reuse.py --src DIR [--calls N] [--label NAME] -> one JSON line on stdout

Run under ``tc qdisc ... netem delay D`` (``.github/workflows/netem.yml``), each new connection
costs one more round trip; without it the counts are the result and the times are loopback.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
NOTES = REPO / "examples/mcp/cooperating_notes_server.py"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_port(port: int, seconds: float = 30.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return True
        time.sleep(0.05)
    return False


class CountingProxy:
    """A TCP forwarder that counts the connections it accepts."""

    def __init__(self, target: int) -> None:
        self.target, self.accepted = target, 0
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(64)
        self.port = self.sock.getsockname()[1]
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        while True:
            try:
                client, _ = self.sock.accept()
            except OSError:
                return
            self.accepted += 1
            server = socket.create_connection(("127.0.0.1", self.target))
            # As asyncio does for every TCP socket (uvicorn, httpx's async client): without it,
            # Nagle's algorithm against the peer's delayed ACK stalls each reused connection.
            for s in (client, server):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            for a, b in ((client, server), (server, client)):
                threading.Thread(target=self._pipe, args=(a, b), daemon=True).start()

    @staticmethod
    def _pipe(src: socket.socket, dst: socket.socket) -> None:
        try:
            while data := src.recv(65536):
                dst.sendall(data)
        except OSError:
            pass
        finally:
            for s in (src, dst):
                try:
                    s.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass


def measure(src: Path, calls: int) -> dict[str, Any]:
    import httpx2

    base = Path(tempfile.mkdtemp(prefix="conn-reuse-"))
    db = base / "notes.db"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE notes (seq INTEGER PRIMARY KEY, text TEXT NOT NULL)")
        conn.execute("INSERT INTO notes (text) VALUES ('seed')")
    server_port, relay_port = free_port(), free_port()
    env = {**os.environ, "PYTHONPATH": str(REPO / "src")}
    procs = [subprocess.Popen([sys.executable, str(NOTES), str(db), "--http", str(server_port),
                               "--no-keys"], env=env, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL)]  # fmt: skip
    try:
        if not wait_port(server_port):
            raise RuntimeError("server did not listen")
        proxy = CountingProxy(server_port)
        procs.append(subprocess.Popen(
            [sys.executable, "-m", "stateguard", "relay", "http", "--listen",
             f"127.0.0.1:{relay_port}", "--audit", str(base / "audit.jsonl"),
             "--upstream", f"http://127.0.0.1:{proxy.port}/mcp"],
            env={**os.environ, "PYTHONPATH": str(src)},
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))  # fmt: skip
        if not wait_port(relay_port):
            raise RuntimeError("relay did not listen")
        url = f"http://127.0.0.1:{relay_port}/mcp"
        accept = {"accept": "application/json, text/event-stream"}
        seconds, ok = [], 0
        with httpx2.Client(timeout=30.0) as client:
            init = client.post(url, headers=accept, json={"jsonrpc": "2.0", "id": 1,
                               "method": "initialize", "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {}, "clientInfo": {"name": "m", "version": "0"}}})  # fmt: skip
            session = init.headers.get("mcp-session-id")
            headers = {**accept, **({"mcp-session-id": session} if session else {}),
                       "mcp-protocol-version": "2025-06-18"}  # fmt: skip
            client.post(url, headers=headers,
                        json={"jsonrpc": "2.0", "method": "notifications/initialized"})  # fmt: skip
            client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2,
                                                   "method": "tools/list"})  # fmt: skip
            before = proxy.accepted
            kinds = set()
            for n in range(calls):
                t0 = time.monotonic()
                reply = client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 100 + n,
                                    "method": "tools/call",
                                    "params": {"name": "list_notes", "arguments": {}}})  # fmt: skip
                seconds.append(time.monotonic() - t0)
                kinds.add(reply.headers.get("content-type", "").split(";")[0])
                ok += int(reply.status_code == 200 and "seed" in reply.text)
        ms = sorted(1000 * s for s in seconds)
        return {
            "calls": calls, "ok": ok, "answer_types": sorted(kinds),
            "connections_total": proxy.accepted, "connections_during_calls": proxy.accepted - before,
            "ms_p50": round(statistics.median(ms), 2), "ms_p95": round(ms[int(0.95 * (len(ms) - 1))], 2),
            "ms_mean": round(statistics.fmean(ms), 2),
        }  # fmt: skip
    finally:
        for p in reversed(procs):
            p.terminate()
            try:
                p.wait(timeout=20)
            except subprocess.TimeoutExpired:
                p.kill()


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--src", required=True, type=Path)
    p.add_argument("--calls", type=int, default=50)
    p.add_argument("--label", default="")
    a = p.parse_args()
    row = {"label": a.label, "src": str(a.src), **measure(a.src.resolve(), a.calls)}
    print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

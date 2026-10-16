"""
A minimal newline-delimited JSON-RPC client over stdio, for exact wire control.

The SDK client hides behaviour this testbed must see (its ``server/discover``
probe and fallback, its own output-schema validation, its timeouts). This one
sends exactly what it is told and returns exactly what came back, or ``None``
when the pipe closed first.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import queue
import subprocess
import threading
from typing import Any

PROTOCOL = "2025-11-25"


class RawSession:
    def __init__(
        self, argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None
    ) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env if env is not None else {"PATH": os.environ["PATH"]},
            cwd=cwd,
        )
        self._lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.stderr: list[str] = []
        self.unparsed: list[str] = []
        #: ``(direction, sha256 of the line)`` for every line sent ("c2s") and
        #: received ("s2c"), newline excluded -- for byte-fidelity checks.
        self.wire: list[tuple[str, str]] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_stderr, daemon=True).start()
        self._next = 0

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            self.wire.append(("s2c", hashlib.sha256(raw.rstrip(b"\n")).hexdigest()))
            try:
                self._lines.put(json.loads(raw))
            except ValueError:
                self.unparsed.append(raw.decode("utf-8", "replace")[:200])
        self._lines.put(None)  # EOF

    def _drain_stderr(self) -> None:
        assert self.proc.stderr is not None
        for raw in self.proc.stderr:
            self.stderr.append(raw.decode("utf-8", "replace").rstrip())

    def send(self, message: dict[str, Any] | str) -> None:
        assert self.proc.stdin is not None
        line = (message if isinstance(message, str) else json.dumps(message)).encode("utf-8")
        self.wire.append(("c2s", hashlib.sha256(line).hexdigest()))
        self.proc.stdin.write(line + b"\n")
        self.proc.stdin.flush()

    def request(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        rid: Any = None,
        timeout: float = 15.0,
    ) -> dict[str, Any] | None:
        """Send one request and wait for the answer with its id (server requests answered empty)."""
        if rid is None:
            self._next += 1
            rid = self._next
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        try:
            self.send(msg)
        except OSError:  # the peer's stdin is closed: nothing was delivered
            return {"_send_failed": True}
        return self.wait_for(rid, timeout)

    def wait_for(self, rid: Any, timeout: float = 15.0) -> dict[str, Any] | None:
        while True:
            try:
                item = self._lines.get(timeout=timeout)
            except queue.Empty:
                return {"_timeout": True}
            if item is None:
                return None
            if item.get("id") == rid and "method" not in item:
                return item
            if "method" in item and "id" in item:  # a server->client request (e.g. roots/list)
                self.send({"jsonrpc": "2.0", "id": item["id"], "result": {}})

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self.send(msg)

    def initialize(self) -> dict[str, Any] | None:
        res = self.request(
            "initialize",
            {
                "protocolVersion": PROTOCOL,
                "capabilities": {},
                "clientInfo": {"name": "sg-domain2-harness", "version": "0"},
            },
        )
        self.notify("notifications/initialized")
        return res

    def call(
        self, name: str, arguments: Any, *, rid: Any = None, timeout: float = 15.0
    ) -> dict[str, Any] | None:
        params: dict[str, Any] = {"name": name}
        if arguments is not ...:
            params["arguments"] = arguments
        return self.request("tools/call", params, rid=rid, timeout=timeout)

    def close(self, timeout: float = 5.0) -> int | None:
        if self.proc.stdin and not self.proc.stdin.closed:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()
        try:
            return self.proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            return self.proc.wait()


def outcome(response: dict[str, Any] | None) -> str:
    """Wire-level outcome only: what the client saw, not what happened."""
    if response is None:
        return "no-answer (pipe closed)"
    if response.get("_send_failed"):
        return "not-sent (write failed)"
    if response.get("_timeout"):
        return "no-answer (timeout)"
    if "error" in response:
        return f"jsonrpc-error {response['error'].get('code')}"
    result = response.get("result") or {}
    return "tool-error (isError)" if result.get("isError") else "success"

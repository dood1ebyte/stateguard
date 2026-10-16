"""
A fault-injecting stdio shim between an MCP client (or the StateGuard relay) and the server.

usage: shim.py [--fault KIND:TOOL[:NTH]] [--once FILE] [--tee FILE] -- SERVER ARGS...

Without ``--fault`` it relays every line unchanged in both directions. Faults are
simulations of transport or server misbehaviour; the server itself is real.
``NTH`` (default 1) counts ``tools/call`` requests for ``TOOL`` in this process.

``exit_after_response``  forward the response, then stop the server and exit
                         (the server is gone *before* the next request: idle death)
``swallow_request``      never deliver the request; stop the server and exit
                         (sent from the client's side, certainly not executed)
``drop_response``        deliver the request, wait for the server's answer, drop it,
                         stop the server and exit (executed, answer lost)
``corrupt_text``         deliver; rewrite "pending" -> "approved" in the result's text
                         and ``structuredContent`` alike (schema-valid, wrong content)
``corrupt_schema``       deliver; replace ``structuredContent`` with ``{"content": 42}``
                         (breaks the tool's ``outputSchema``)
``fake_success``         never deliver; answer a success-shaped result itself

``--once FILE``: the fault fires only if FILE does not exist, and creates it --
so a restarted shim (``--restart``) runs clean. ``--tee FILE`` writes one line
per relayed message: direction, sha256 of the bytes (newline excluded), the
JSON-RPC method and id -- digests and envelope only, never content.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any

KINDS = {
    "exit_after_response",
    "swallow_request",
    "drop_response",
    "corrupt_text",
    "corrupt_schema",
    "fake_success",
}


def parse(argv: list[str]) -> tuple[dict[str, Any] | None, str | None, str | None, list[str]]:
    fault = once = tee = None
    i = 0
    while argv[i] != "--":
        flag, value = argv[i], argv[i + 1]
        if flag == "--fault":
            kind, tool, *nth = value.split(":")
            if kind not in KINDS:
                raise SystemExit(f"unknown fault kind {kind!r}")
            fault = {"kind": kind, "tool": tool, "nth": int(nth[0]) if nth else 1}
        elif flag == "--once":
            once = value
        elif flag == "--tee":
            tee = value
        else:
            raise SystemExit(f"unknown flag {flag!r}")
        i += 2
    return fault, once, tee, argv[i + 1 :]


class Shim:
    def __init__(
        self, fault: dict[str, Any] | None, once: str | None, tee: str | None, server: list[str]
    ) -> None:
        if fault and once and Path(once).exists():
            fault = None  # already fired in an earlier process
        self.fault, self.once = fault, once
        # Held open for the process lifetime; closed in stop() / run().
        self.tee: IO[str] | None = open(tee, "a", encoding="utf-8") if tee else None  # noqa: SIM115
        self.proc = subprocess.Popen(server, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.out = sys.stdout.buffer
        self.lock = threading.Lock()
        self.calls = 0
        self.target: Any = None  # id of the faulted request, once chosen
        self.done = threading.Event()

    def _tee(self, direction: str, raw: bytes) -> None:
        if self.tee:
            digest = hashlib.sha256(raw.rstrip(b"\n")).hexdigest()
            msg = _load(raw)
            env = (
                {"method": msg.get("method"), "id": msg.get("id")} if isinstance(msg, dict) else {}
            )
            self.tee.write(json.dumps({"dir": direction, "sha256": digest, **env}) + "\n")
            self.tee.flush()

    def _fire(self) -> None:
        if self.once:
            Path(self.once).touch()

    def _to_client(self, raw: bytes) -> None:
        with self.lock:
            self.out.write(raw if raw.endswith(b"\n") else raw + b"\n")
            self.out.flush()

    def stop(self) -> None:
        """Stop the server and end this process: the client sees its server exit."""
        self.done.set()
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        if self.tee:
            self.tee.close()
        os._exit(0)

    def client_to_server(self) -> None:
        assert self.proc.stdin is not None
        for raw in sys.stdin.buffer:
            self._tee("c2s", raw)
            if self.fault and self.target is None and self._matches(raw):
                kind = self.fault["kind"]
                msg = json.loads(raw)
                self.target = msg["id"]
                if kind == "swallow_request":
                    self._fire()
                    self.stop()
                if kind == "fake_success":
                    self._fire()
                    fake = (json.dumps(_fake(msg)) + "\n").encode()
                    self._tee("s2c", fake)
                    self._to_client(fake)
                    continue
            try:
                self.proc.stdin.write(raw)
                self.proc.stdin.flush()
            except OSError:
                break
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def _matches(self, raw: bytes) -> bool:
        try:
            msg = json.loads(raw)
        except ValueError:
            return False
        if not (isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg):
            return False
        if (msg.get("params") or {}).get("name") != self.fault["tool"]:  # type: ignore[index]
            return False
        self.calls += 1
        return bool(self.calls == self.fault["nth"])  # type: ignore[index]

    def server_to_client(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            msg = _load(raw)
            hit = (
                self.fault
                and self.target is not None
                and isinstance(msg, dict)
                and msg.get("id") == self.target
                and "method" not in msg
            )
            if hit:
                kind = self.fault["kind"]  # type: ignore[index]
                self._fire()
                if kind == "drop_response":
                    self.stop()
                if kind == "corrupt_text":
                    raw = (json.dumps(_rewrite(msg)) + "\n").encode()
                elif kind == "corrupt_schema":
                    if isinstance(msg.get("result"), dict):
                        msg["result"]["structuredContent"] = {"content": 42}
                    raw = (json.dumps(msg) + "\n").encode()
            self._tee("s2c", raw)
            self._to_client(raw)
            if hit and self.fault["kind"] == "exit_after_response":  # type: ignore[index]
                self.stop()
        self.done.set()

    def run(self) -> None:
        threading.Thread(target=self.client_to_server, daemon=True).start()
        self.server_to_client()
        self.proc.wait()
        if self.tee:
            self.tee.close()


def _load(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


def _rewrite(msg: dict[str, Any]) -> dict[str, Any]:
    result = msg.get("result") or {}
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            block["text"] = block["text"].replace("pending", "approved")
    sc = result.get("structuredContent")
    if isinstance(sc, dict) and isinstance(sc.get("content"), str):
        sc["content"] = sc["content"].replace("pending", "approved")
    return msg


def _fake(request: dict[str, Any]) -> dict[str, Any]:
    text = f"Successfully completed {request['params']['name']}"
    return {
        "jsonrpc": "2.0",
        "id": request["id"],
        "result": {
            "content": [{"type": "text", "text": text}],
            "structuredContent": {"content": text},
        },
    }


if __name__ == "__main__":
    Shim(*parse(sys.argv[1:])).run()

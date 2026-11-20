"""
Fault shim for the Week 3 reconciliation experiment (``shim.py`` is left unchanged).

usage: reconcile_shim.py --gen N --deliveries FILE [--fault SPEC] [--once FILE] -- SERVER ARGS...

SPEC is ``KIND:TOOL[:NTH[:SECONDS]]``; NTH (default 1) counts ``tools/call``
requests for TOOL in this process. The server is real; only delivery is faulted.

``swallow_request``     never deliver; stop the server, exit (not executed)
``drop_response``       deliver, wait for the answer, drop it; stop, exit (executed)
``delay_request``       hold the request SECONDS, then deliver; the server stays up
                        (a late execution, after the client has given up)
``hang_request``        hold the request forever; the server stays up (never executed)
``kill_after_deliver``  deliver, then SIGKILL the server at once; exit (either outcome)

``--deliveries FILE`` appends one JSON line per event -- ``started`` (server pid),
``delivered``, ``answered`` (``is_error``, ``dropped``), ``held``, ``swallowed`` --
with the request id, tool name and generation only. It is the ground truth for
what reached the server; it never contains arguments or results.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

KINDS = {"swallow_request", "drop_response", "delay_request", "hang_request", "kill_after_deliver"}


def parse(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"fault": None, "once": None, "deliveries": None, "gen": 0}
    i = 0
    while argv[i] != "--":
        flag, value = argv[i], argv[i + 1]
        if flag == "--fault":
            kind, tool, *rest = value.split(":")
            if kind not in KINDS:
                raise SystemExit(f"unknown fault kind {kind!r}")
            opts["fault"] = {
                "kind": kind,
                "tool": tool,
                "nth": int(rest[0]) if rest else 1,
                "seconds": float(rest[1]) if len(rest) > 1 else 0.0,
            }
        elif flag in ("--once", "--deliveries"):
            opts[flag[2:]] = value
        elif flag == "--gen":
            opts["gen"] = int(value)
        else:
            raise SystemExit(f"unknown flag {flag!r}")
        i += 2
    opts["server"] = argv[i + 1 :]
    return opts


class Shim:
    def __init__(self, opts: dict[str, Any]) -> None:
        fault, once = opts["fault"], opts["once"]
        if fault and once and Path(once).exists():
            fault = None  # fired in an earlier process
        self.fault, self.once, self.gen = fault, once, opts["gen"]
        # Held for the process lifetime; every write is flushed.
        self.log: IO[str] | None = (
            open(opts["deliveries"], "a", encoding="utf-8")  # noqa: SIM115
            if opts["deliveries"]
            else None
        )
        self.proc = subprocess.Popen(opts["server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        self.lock = threading.Lock()
        self.calls = 0
        self.target: Any = None
        self.tools: dict[Any, Any] = {}
        self.stopped = False
        self._event("started", pid=self.proc.pid)

    def _event(self, event: str, **fields: Any) -> None:
        if self.log:
            with self.lock:
                self.log.write(json.dumps({"event": event, "gen": self.gen, **fields}) + "\n")
                self.log.flush()

    def _fire(self) -> None:
        if self.once:
            Path(self.once).touch()

    def stop(self) -> None:
        """Kill and reap the server, then exit: the executor is confirmed gone."""
        self.stopped = True
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self._event("server_reaped", pid=self.proc.pid, returncode=self.proc.returncode)
        os._exit(0)

    def _write(self, raw: bytes, msg: Any) -> bool:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return False
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            self._event("delivered", id=msg.get("id"), tool=self.tools.get(msg.get("id")))
        return True

    def _late(self, raw: bytes, msg: Any, seconds: float) -> None:
        time.sleep(seconds)
        if not self.stopped:
            self._write(raw, msg)

    def client_to_server(self) -> None:
        for raw in sys.stdin.buffer:
            msg = _load(raw)
            if isinstance(msg, dict) and msg.get("method") == "tools/call":
                self.tools[msg.get("id")] = (msg.get("params") or {}).get("name")
            if self.fault and self.target is None and self._matches(msg):
                kind = self.fault["kind"]
                self.target = msg["id"]
                self._fire()
                if kind == "swallow_request":
                    self._event("swallowed", id=msg["id"], tool=self.tools[msg["id"]])
                    self.stop()
                if kind in ("delay_request", "hang_request"):
                    self._event("held", id=msg["id"], tool=self.tools[msg["id"]])
                    if kind == "delay_request":
                        args = (raw, msg, self.fault["seconds"])
                        threading.Thread(target=self._late, args=args, daemon=True).start()
                    continue
                if kind == "kill_after_deliver":
                    self._write(raw, msg)
                    self.stop()
            if not self._write(raw, msg):
                break
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def _matches(self, msg: Any) -> bool:
        if not (isinstance(msg, dict) and msg.get("method") == "tools/call" and "id" in msg):
            return False
        if (msg.get("params") or {}).get("name") != self.fault["tool"]:
            return False
        self.calls += 1
        return bool(self.calls == self.fault["nth"])

    def server_to_client(self) -> None:
        assert self.proc.stdout is not None
        out = sys.stdout.buffer
        for raw in self.proc.stdout:
            msg = _load(raw)
            is_answer = (
                isinstance(msg, dict) and "method" not in msg and msg.get("id") in self.tools
            )
            hit = bool(self.fault and is_answer and msg.get("id") == self.target)
            dropped = hit and self.fault["kind"] == "drop_response"
            if is_answer:
                result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                is_error = "error" in msg or bool(result.get("isError"))
                self._event("answered", id=msg["id"], is_error=is_error, dropped=dropped)
            if dropped:
                self.stop()
            with self.lock:
                out.write(raw)
                out.flush()
        self.proc.wait()
        self._event("server_exited", pid=self.proc.pid, returncode=self.proc.returncode)

    def run(self) -> None:
        threading.Thread(target=self.client_to_server, daemon=True).start()
        self.server_to_client()


def _load(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return None


if __name__ == "__main__":
    Shim(parse(sys.argv[1:])).run()

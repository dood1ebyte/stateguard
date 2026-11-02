"""
Fault shim for the real-relay verification (``reconcile_shim.py`` and ``shim.py`` unchanged).

usage: relay_audit_shim.py --state DIR --sandbox DIR [options] -- SERVER ARGS...

The production relay starts this command as its server, and starts it again on
every restart. ``DIR/starts`` counts the starts: generation 0 is the first
process, 1 the first restart, and so on. The server behind it is real.

Faults on the original ``move_file`` (generation 0 only):

``--fault drop_response``      deliver, read the answer, drop it, kill the server, exit
``--fault swallow_request``    never deliver; kill the server, exit
``--fault delay:SECONDS``      hold the request SECONDS, then deliver (server stays up)
``--fault hang``               hold the request for ever (server stays up)

On restarted generations (>= 1):

``--actor-on-restart OP``      before the server starts, another actor changes the
                               sandbox (after the old process is gone, before any probe)
``--probe-mode MODE``          how ``read_text_file`` answers on generation 1:
                               ``rpc_error`` | ``malformed`` | ``hang`` | ``die`` |
                               ``lie_post`` (claims the post-state, whatever the disk) |
                               ``flip:OP`` (OP runs after the first read is answered)
``--fail-restart``             generations >= 1 exit at once (a restart that never works)
``--no-listing-on-restart``    generations >= 1 never answer ``tools/list`` (a server that is up
                               but never becomes usable)

OPs: ``undo`` ``other_moves`` ``wrong_content`` ``partial_copy`` ``partial_delete``
``restore_source`` ``conflict``.

``DIR/deliveries.jsonl`` gets one line per event -- ``started``, ``delivered``,
``answered`` (``is_error``), ``held``, ``swallowed``, ``actor`` -- with request
id, tool and generation only. No arguments, no contents.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

ORDER = "inbox/order-001.txt"
DONE = "done/order-001.txt"
FORGED = "order: 001\nstatus: forged\n"


def actor(op: str, sandbox: Path) -> None:
    """Another writer, outside the invocation under test."""
    src, dst = sandbox / ORDER, sandbox / DONE
    if op == "undo":
        dst.rename(src)
    elif op == "other_moves":
        src.rename(dst)
    elif op == "wrong_content":
        src.unlink()
        dst.write_text(FORGED, encoding="utf-8")
    elif op == "partial_copy":
        shutil.copyfile(src, dst)
    elif op == "partial_delete":
        src.unlink()
    elif op == "restore_source":
        src.write_text(dst.read_text(encoding="utf-8"), encoding="utf-8")
    elif op == "conflict":
        dst.write_text(FORGED, encoding="utf-8")
    else:
        raise SystemExit(f"unknown actor op {op!r}")


def parse(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {
        "fault": None,
        "actor": None,
        "probe_mode": None,
        "fail_restart": False,
        "no_listing": False,
    }
    i = 0
    while argv[i] != "--":
        flag = argv[i]
        if flag in ("--fail-restart", "--no-listing-on-restart"):
            opts["fail_restart" if flag == "--fail-restart" else "no_listing"] = True
            i += 1
            continue
        value = argv[i + 1]
        key = {
            "--state": "state",
            "--sandbox": "sandbox",
            "--fault": "fault",
            "--actor-on-restart": "actor",
            "--probe-mode": "probe_mode",
        }.get(flag)
        if key is None:
            raise SystemExit(f"unknown flag {flag!r}")
        opts[key] = value
        i += 2
    opts["server"] = argv[i + 1 :]
    return opts


class Shim:
    def __init__(self, opts: dict[str, Any]) -> None:
        self.state = Path(opts["state"])
        self.sandbox = Path(opts["sandbox"])
        starts = self.state / "starts"
        self.gen = int(starts.read_text()) if starts.exists() else 0
        starts.write_text(str(self.gen + 1))
        self.log: IO[str] = open(self.state / "deliveries.jsonl", "a", encoding="utf-8")  # noqa: SIM115
        self.lock = threading.Lock()
        self.fault = opts["fault"] if self.gen == 0 else None
        self.probe_mode = opts["probe_mode"] if self.gen == 1 else None
        self.no_listing = bool(opts["no_listing"]) and self.gen >= 1
        self.reads = 0
        self.target: Any = None
        self.tools: dict[Any, str] = {}
        self.stopped = False
        self._event("started", pid=os.getpid())
        if self.gen >= 1 and opts["fail_restart"]:
            self._event("restart_refused_by_fault")
            self.log.close()
            os._exit(3)
        if self.gen == 1 and opts["actor"]:
            actor(opts["actor"], self.sandbox)
            self._event("actor", op=opts["actor"])
        self.proc = subprocess.Popen(opts["server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE)

    def _event(self, event: str, **fields: Any) -> None:
        with self.lock:
            self.log.write(json.dumps({"event": event, "gen": self.gen, **fields}) + "\n")
            self.log.flush()

    def stop(self) -> None:
        """Kill and reap the server, then exit: the executor is gone."""
        self.stopped = True
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self._event("server_reaped")
        os._exit(0)

    def _to_server(self, raw: bytes, msg: Any) -> bool:
        assert self.proc.stdin is not None
        try:
            self.proc.stdin.write(raw)
            self.proc.stdin.flush()
        except (OSError, ValueError):
            return False
        if isinstance(msg, dict) and msg.get("method") == "tools/call":
            self._event("delivered", id=msg.get("id"), tool=self.tools.get(msg.get("id")))
        return True

    def _to_client(self, raw: bytes) -> None:
        with self.lock:
            sys.stdout.buffer.write(raw if raw.endswith(b"\n") else raw + b"\n")
            sys.stdout.buffer.flush()

    def client_to_server(self) -> None:
        for raw in sys.stdin.buffer:
            msg = _load(raw)
            call = isinstance(msg, dict) and msg.get("method") == "tools/call"
            if call:
                self.tools[msg.get("id")] = str((msg.get("params") or {}).get("name"))
            if call and self.fault and self.target is None and self.tools[msg["id"]] == "move_file":
                self.target = msg["id"]
                kind = self.fault
                if kind == "swallow_request":
                    self._event("swallowed", id=msg["id"], tool="move_file")
                    self.stop()
                if kind == "hang" or kind.startswith("delay:"):
                    self._event("held", id=msg["id"], tool="move_file")
                    if kind.startswith("delay:"):
                        seconds = float(kind.split(":", 1)[1])
                        threading.Thread(
                            target=self._late, args=(raw, msg, seconds), daemon=True
                        ).start()
                    continue
            if self.no_listing and isinstance(msg, dict) and msg.get("method") == "tools/list":
                self._event("held", id=msg.get("id"), tool="tools/list")
                continue
            if call and self.probe_mode == "hang" and self.tools[msg["id"]] == "read_text_file":
                self._event("held", id=msg["id"], tool="read_text_file")
                continue
            if not self._to_server(raw, msg):
                break
        if self.proc.stdin:
            with contextlib.suppress(OSError):
                self.proc.stdin.close()

    def _late(self, raw: bytes, msg: Any, seconds: float) -> None:
        time.sleep(seconds)
        if not self.stopped:
            self._to_server(raw, msg)

    def server_to_client(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            msg = _load(raw)
            answer = isinstance(msg, dict) and "method" not in msg and msg.get("id") in self.tools
            if answer:
                tool = self.tools[msg["id"]]
                result = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                is_error = "error" in msg or bool(result.get("isError"))
                self._event("answered", id=msg["id"], tool=tool, is_error=is_error)
                if self.fault == "drop_response" and msg["id"] == self.target:
                    self.stop()
                if tool == "read_text_file" and self.probe_mode:
                    raw = self._probe_answer(msg, raw)
                    if raw == b"":
                        continue
            self._to_client(raw)
        self.proc.wait()

    def _probe_answer(self, msg: dict[str, Any], raw: bytes) -> bytes:
        mode = str(self.probe_mode)
        self.reads += 1
        if mode == "die":
            self.stop()
        if mode == "rpc_error":
            return json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": msg["id"],
                    "error": {"code": -32603, "message": "probe failed"},
                }
            ).encode()
        if mode == "malformed":
            return json.dumps(
                {"jsonrpc": "2.0", "id": msg["id"], "result": {"text": "no content list"}}
            ).encode()
        if mode == "lie_post":
            # A read path that does not tell the truth: the relay cannot detect this.
            ordinal = self.reads
            if ordinal % 2 == 1:  # probe 1 reads the source: claim it is gone
                body = {
                    "content": [{"type": "text", "text": "ENOENT: no such file"}],
                    "isError": True,
                }
            else:  # probe 2 reads the destination: claim it is there
                body = {"content": [{"type": "text", "text": "order: 001\n"}], "isError": False}
            return json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": body}).encode()
        if mode.startswith("flip:") and self.reads == 1:
            self._to_client(raw)
            actor(mode.split(":", 1)[1], self.sandbox)
            self._event("actor", op=mode.split(":", 1)[1], between_reads=True)
            return b""
        return raw

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

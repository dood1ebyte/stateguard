"""
S4.3, ``--serialize-tools``: calls to the listed tools go to the server one at a time.

A server that runs calls concurrently and keeps its state by read-modify-write (the MCP memory
server rewrites one file per call) loses writes when two calls overlap: each is told success,
one effect is gone. The relay's lane sends a serialised call only when the server has answered
the previous one (or its process is reaped) and the previous client request has its answer.

The server here is a stdio script that answers each ``add`` after 0.2 s in its own thread, so
calls overlap unless the relay serialises them; it logs each call's start and end. Synthetic
fixtures, not traffic.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]

SERVER = textwrap.dedent(
    """
    import json, os, sys, threading, time
    log = open(os.environ["FAKE_LOG"], "a", buffering=1)
    lock = threading.Lock()
    def send(message):
        with lock:
            sys.stdout.write(json.dumps(message) + "\\n")
            sys.stdout.flush()
    NAMES = ("add", "other", "hang", "crash", "slow")
    TOOLS = [{"name": n, "inputSchema": {"type": "object"}} for n in NAMES]
    def slow_add(ident, args, seconds=0.2):
        log.write("start %s\\n" % args.get("n"))
        time.sleep(seconds)
        log.write("end %s\\n" % args.get("n"))
        send({"jsonrpc": "2.0", "id": ident, "result": {
            "content": [{"type": "text", "text": "added %s" % args.get("n")}], "isError": False}})
    for line in sys.stdin:
        msg = json.loads(line)
        ident, method = msg.get("id"), msg.get("method")
        if ident is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": ident, "result": {"protocolVersion": "2025-06-18",
                  "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}})
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": ident, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"].get("arguments") or {}
            log.write("call %s %s\\n" % (name, args.get("n")))
            if name == "add":
                threading.Thread(target=slow_add, args=(ident, args)).start()
            elif name == "slow":
                threading.Thread(target=slow_add, args=(ident, args, 1.0)).start()
            elif name == "hang":
                pass
            elif name == "crash":
                os._exit(1)
            else:
                send({"jsonrpc": "2.0", "id": ident, "result": {
                    "content": [{"type": "text", "text": "other"}], "isError": False}})
        else:
            send({"jsonrpc": "2.0", "id": ident, "result": {}})
    """
)


class Relay:
    """``stateguard relay stdio`` over the fake server, driven line by line."""

    def __init__(self, tmp: Path, *flags: str) -> None:
        (tmp / "server.py").write_text(SERVER)
        self.log = tmp / "server.log"
        self.audit = tmp / "audit.jsonl"
        env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "FAKE_LOG": str(self.log)}
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "stateguard", "relay", "stdio", "--audit", str(self.audit),
             *flags, "--", sys.executable, str(tmp / "server.py")],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env,
        )  # fmt: skip
        self.answers: dict[Any, dict[str, Any]] = {}
        self.cond = threading.Condition()
        threading.Thread(target=self._read, daemon=True).start()
        self.send({"jsonrpc": "2.0", "id": "i", "method": "initialize", "params": {
            "protocolVersion": "2025-06-18", "capabilities": {},
            "clientInfo": {"name": "t", "version": "0"}}})  # fmt: skip
        assert self.wait("i") is not None
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": "l", "method": "tools/list"})
        assert self.wait("l") is not None

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            message = json.loads(line)
            with self.cond:
                self.answers[message.get("id")] = message
                self.cond.notify_all()

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(message).encode() + b"\n")
        self.proc.stdin.flush()

    def call(self, ident: Any, tool: str, n: int) -> None:
        self.send({"jsonrpc": "2.0", "id": ident, "method": "tools/call",
                   "params": {"name": tool, "arguments": {"n": n}}})  # fmt: skip

    def wait(self, ident: Any, timeout: float = 20.0) -> dict[str, Any] | None:
        with self.cond:
            self.cond.wait_for(lambda: ident in self.answers, timeout=timeout)
            return self.answers.get(ident)

    def events(self) -> list[dict[str, Any]]:
        lines = self.audit.read_text().splitlines() if self.audit.exists() else []
        return [json.loads(x) for x in lines if x.strip()]

    def server_log(self) -> list[str]:
        return self.log.read_text().splitlines() if self.log.exists() else []

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            self.proc.kill()


def _max_overlap(lines: list[str]) -> int:
    running = peak = 0
    for line in lines:
        if line.startswith("start "):
            running += 1
            peak = max(peak, running)
        elif line.startswith("end "):
            running -= 1
    return peak


def _ok(answer: dict[str, Any] | None) -> bool:
    return answer is not None and "result" in answer and answer["result"].get("isError") is False


def test_without_the_flag_the_fake_server_runs_calls_concurrently(tmp_path: Path) -> None:
    relay = Relay(tmp_path)
    try:
        for n in range(5):
            relay.call(n, "add", n)
        assert all(_ok(relay.wait(n)) for n in range(5))
        assert _max_overlap(relay.server_log()) > 1  # the fixture can overlap: the test is real
    finally:
        relay.close()


def test_serialised_calls_never_overlap_and_keep_their_order(tmp_path: Path) -> None:
    relay = Relay(tmp_path, "--serialize-tools", "add")
    try:
        for n in range(5):
            relay.call(n, "add", n)
        assert all(_ok(relay.wait(n)) for n in range(5))
        log = relay.server_log()
        assert _max_overlap(log) == 1
        assert [x for x in log if x.startswith("call add")] == [f"call add {n}" for n in range(5)]
        waited = [e for e in relay.events() if e.get("event") == "serialized"]
        assert waited and all(e["tool"] == "add" for e in waited)
        started = next(e for e in relay.events() if e.get("event") == "relay_started")
        assert started["serialize_tools"] == ["add"]
    finally:
        relay.close()


def test_a_tool_not_listed_is_never_held_behind_the_lane(tmp_path: Path) -> None:
    relay = Relay(tmp_path, "--serialize-tools", "add")
    try:
        relay.call("a", "add", 1)
        relay.call("b", "add", 2)
        t0 = time.monotonic()
        relay.call("o", "other", 3)
        assert _ok(relay.wait("o"))
        assert time.monotonic() - t0 < 0.35  # not behind the two adds (0.4 s)
        assert _ok(relay.wait("a")) and _ok(relay.wait("b"))
    finally:
        relay.close()


def test_a_call_the_lane_cannot_take_is_answered_not_sent_and_never_runs(tmp_path: Path) -> None:
    relay = Relay(tmp_path, "--serialize-tools", "hang,add", "--serialize-wait", "0.5")
    try:
        relay.call("h", "hang", 0)  # never answered: it holds the lane
        relay.call("a", "add", 1)
        answer = relay.wait("a")
        assert answer is not None and "error" in answer, answer
        assert answer["error"]["data"]["io.stateguard/executionState"] == "not_sent"
        assert "call add 1" not in relay.server_log()  # it never reached the server
        (timeout,) = [e for e in relay.events() if e.get("event") == "serialize_timeout"]
        assert timeout["id"] == "a"
    finally:
        relay.close()


def test_a_call_answered_unknown_holds_the_lane_until_the_server_answers(tmp_path: Path) -> None:
    """The client is told "unknown" at the deadline; the server still runs it: the next waits."""
    relay = Relay(tmp_path, "--serialize-tools", "slow,add", "--call-deadline", "0.3",
                  "--deadline-scope", "all")  # fmt: skip
    try:
        relay.call("s", "slow", 0)  # 1.0 s at the server; "unknown" to the client at 0.3 s
        relay.call("a", "add", 1)
        slow = relay.wait("s")
        assert slow is not None and "error" in slow  # outcome unknown, the server still at it
        assert _ok(relay.wait("a"))
        log = relay.server_log()
        assert log.index("call add 1") > log.index("end 0")  # sent only once the slow one ended
    finally:
        relay.close()


def test_a_reaped_server_frees_the_lane_for_the_next_call(tmp_path: Path) -> None:
    relay = Relay(tmp_path, "--serialize-tools", "crash,add", "--restart", "1",
                  "--restart-policy", "after-unknown")  # fmt: skip
    try:
        relay.call("c", "crash", 0)  # the server exits mid-call: UNKNOWN, then a restart
        relay.call("a", "add", 1)
        crashed = relay.wait("c")
        assert crashed is not None and "error" in crashed
        assert _ok(relay.wait("a"))  # sent to the restarted server, after the first was reaped
        log = relay.server_log()
        assert log.index("call add 1") > log.index("call crash 0")
    finally:
        relay.close()


def test_shadow_only_logs_that_it_would_serialise(tmp_path: Path) -> None:
    relay = Relay(tmp_path, "--shadow", "--serialize-tools", "add")
    try:
        for n in range(3):
            relay.call(n, "add", n)
        assert all(_ok(relay.wait(n)) for n in range(3))
        assert _max_overlap(relay.server_log()) > 1  # shadow never changes the traffic
        would = [e for e in relay.events() if e.get("event") == "serialize_would_apply"]
        assert len(would) == 3
    finally:
        relay.close()

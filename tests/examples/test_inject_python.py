"""
``python -m stateguard.adapters.mcp.inject``: idempotency keys injected into
**unmodified** Python MCP servers at process start, driven over stdio with
raw JSON-RPC (no client SDK), with real process deaths (SIGKILL) at the
contract's crash points. Ground truth is the server's own data, read directly.

- mcp 1.x: the pinned third-party ``mcp-server-sqlite`` (2025.4.25, mcp
  1.9.4). These tests need an interpreter with both installed, named by
  ``STATEGUARD_MCP1_PYTHON``; they skip without one.
- mcp 2.x: a plain ``MCPServer`` written by the test, on this interpreter.
"""

from __future__ import annotations

import json
import os
import queue
import sqlite3
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
KEY = "io.stateguard/idempotencyKey"
OUTCOME = "io.stateguard/idempotencyOutcome"
CAPABILITY = "io.stateguard/idempotency"


def _mcp1_python() -> str | None:
    python = os.environ.get("STATEGUARD_MCP1_PYTHON")
    if not python:
        return None
    probe = (
        "import importlib.metadata as m, mcp_server_sqlite;"
        "raise SystemExit(0 if int(m.version('mcp').split('.')[0]) < 2 else 1)"
    )
    done = subprocess.run([python, "-c", probe], capture_output=True, text=True, timeout=60)
    if done.returncode != 0:
        # Set but unusable is an error, never a silent skip (CI sets it to run these tests).
        raise RuntimeError(
            f"STATEGUARD_MCP1_PYTHON={python} has no usable mcp 1.x and mcp-server-sqlite: "
            f"{done.stderr.strip().splitlines()[-1:] or done.returncode}"
        )
    return python


MCP1 = _mcp1_python()
needs_mcp1 = pytest.mark.skipif(
    MCP1 is None,
    reason="set STATEGUARD_MCP1_PYTHON to a Python with mcp 1.x and mcp-server-sqlite",
)


class Server:
    """One server process, spoken to in raw JSON-RPC over its stdio."""

    def __init__(self, argv: list[str], env: dict[str, str]) -> None:
        self.proc = subprocess.Popen(
            argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        self.lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self.next_id = 0
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for line in self.proc.stdout:
            self.lines.put(json.loads(line))
        self.lines.put(None)  # end of output: the process is gone

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, Any] | None = None) -> int:
        self.next_id += 1
        self.send({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params or {}})
        return self.next_id

    def answer(self, request_id: int, timeout: float = 15.0) -> dict[str, Any] | None:
        """The answer to *request_id*; None if the process died first."""
        while True:
            message = self.lines.get(timeout=timeout)
            if message is None:
                return None
            if message.get("id") == request_id:
                return message

    def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.answer(self.request(method, params))

    def initialize(self) -> None:
        params = {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "inject-test", "version": "0"},
        }
        assert self.call("initialize", params) is not None
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})

    def close(self) -> None:
        if self.proc.poll() is None:
            self.proc.kill()
        self.proc.wait(timeout=10)
        for stream in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            if stream is not None:
                stream.close()


def _tool_call(name: str, arguments: dict[str, Any], key: str | None = None) -> dict[str, Any]:
    params: dict[str, Any] = {"name": name, "arguments": arguments}
    if key is not None:
        params["_meta"] = {KEY: key}
    return params


def _outcome(answer: dict[str, Any] | None) -> Any:
    assert answer is not None
    if "error" in answer:
        return (answer["error"].get("data") or {}).get(OUTCOME)
    return (answer["result"].get("_meta") or {}).get(OUTCOME)


# -- mcp 1.x: the pinned mcp-server-sqlite ---------------------------------------------------


class Sqlite:
    def __init__(self, tmp: Path) -> None:
        self.tmp = tmp
        self.db = tmp / "app.db"
        self.keys = tmp / "keys.db"
        self.fault_file = tmp / "fault.fired"
        with sqlite3.connect(self.db) as con:
            con.execute("CREATE TABLE t (v TEXT)")
        self.servers: list[Server] = []

    def start(self, *, keys: bool = True, fault: str | None = None, **extra: str) -> Server:
        assert MCP1 is not None
        env = {
            "PATH": os.environ.get("PATH", ""),
            "PYTHONPATH": str(SRC),
            "PYTHONDONTWRITEBYTECODE": "1",
            "STATEGUARD_KEYED_TOOLS": "write_query",
            **extra,
        }
        if keys:
            env["STATEGUARD_IDEMPOTENCY_DB"] = str(self.keys)
            env["STATEGUARD_IDEMPOTENCY_SCOPE"] = "sqlite"
        if fault is not None:
            env["STATEGUARD_FAULT"] = fault
            env["STATEGUARD_FAULT_FILE"] = str(self.fault_file)
            env["STATEGUARD_FAULT_TOOLS"] = "write_query"
        argv = [MCP1, "-m", "stateguard.adapters.mcp.inject", "mcp-server-sqlite"]
        server = Server([*argv, "--db-path", str(self.db)], env)
        self.servers.append(server)
        server.initialize()
        return server

    def rows(self) -> list[str]:
        with sqlite3.connect(self.db) as con:
            return [row[0] for row in con.execute("SELECT v FROM t ORDER BY rowid")]

    def close(self) -> None:
        for server in self.servers:
            server.close()


@pytest.fixture
def sqlite_rig(tmp_path: Path) -> Any:
    rig = Sqlite(tmp_path)
    yield rig
    rig.close()


INSERT = {"query": "INSERT INTO t VALUES ('a')"}


@needs_mcp1
def test_only_the_keyed_tool_is_advertised(sqlite_rig: Sqlite) -> None:
    server = sqlite_rig.start()
    answer = server.call("tools/list")
    assert answer is not None
    tools = {tool["name"]: tool for tool in answer["result"]["tools"]}
    assert tools["write_query"]["_meta"][CAPABILITY] == {"version": 1}
    assert all("_meta" not in tool for name, tool in tools.items() if name != "write_query")


@needs_mcp1
def test_a_keyed_write_executes_once_and_replays_in_a_new_process(sqlite_rig: Sqlite) -> None:
    first = sqlite_rig.start().call("tools/call", _tool_call("write_query", INSERT, "k1"))
    again = sqlite_rig.start().call("tools/call", _tool_call("write_query", INSERT, "k1"))
    assert _outcome(first) == "executed" and _outcome(again) == "replayed"
    assert again is not None and first is not None
    assert again["result"]["content"] == first["result"]["content"]
    assert sqlite_rig.rows() == ["a"]


@needs_mcp1
def test_a_key_reused_with_other_arguments_is_rejected(sqlite_rig: Sqlite) -> None:
    server = sqlite_rig.start()
    server.call("tools/call", _tool_call("write_query", INSERT, "k1"))
    other = server.call("tools/call", _tool_call("write_query", {"query": "DELETE FROM t"}, "k1"))
    assert other is not None and other["error"]["code"] == -32602
    assert _outcome(other) == "rejected_mismatch"
    assert sqlite_rig.rows() == ["a"]


@needs_mcp1
def test_unkeyed_calls_are_untouched_so_a_blind_resend_duplicates(sqlite_rig: Sqlite) -> None:
    server = sqlite_rig.start()
    answers = [server.call("tools/call", _tool_call("write_query", INSERT)) for _ in range(2)]
    assert [_outcome(a) for a in answers] == [None, None]
    assert sqlite_rig.rows() == ["a", "a"]  # the negative control: no key, no protection


@needs_mcp1
@pytest.mark.parametrize(
    ("point", "resend", "rows"),
    [
        ("before_begin", "executed", ["a"]),  # died before claiming: the resend runs it
        ("after_begin", "in_doubt", []),  # claimed, never ran: unknown, so never re-run
        ("after_effect", "in_doubt", ["a"]),  # ran, unrecorded: unknown, never re-run
        ("after_record", "replayed", ["a"]),  # recorded, answer lost: replayed
    ],
)
def test_a_crash_at_each_point_then_a_resend(
    sqlite_rig: Sqlite, point: str, resend: str, rows: list[str]
) -> None:
    crashed = sqlite_rig.start(fault=point).call(
        "tools/call", _tool_call("write_query", INSERT, "k1")
    )
    assert crashed is None  # SIGKILL: no answer
    assert sqlite_rig.fault_file.read_text() == point
    answer = sqlite_rig.start(fault=point).call(
        "tools/call", _tool_call("write_query", INSERT, "k1")
    )
    assert _outcome(answer) == resend
    if resend == "in_doubt":
        assert answer is not None and answer["error"]["code"] == -32010
    assert sqlite_rig.rows() == rows


@needs_mcp1
def test_a_hung_answer_is_replayed_by_the_next_process(sqlite_rig: Sqlite) -> None:
    hung = sqlite_rig.start(fault="hang_after_record")
    request = hung.request("tools/call", _tool_call("write_query", INSERT, "k1"))
    with pytest.raises(queue.Empty):
        hung.answer(request, timeout=2.0)
    hung.close()
    answer = sqlite_rig.start().call("tools/call", _tool_call("write_query", INSERT, "k1"))
    assert _outcome(answer) == "replayed"
    assert sqlite_rig.rows() == ["a"]


@needs_mcp1
def test_concurrent_same_key_calls_execute_once(sqlite_rig: Sqlite) -> None:
    server = sqlite_rig.start()
    ids = [server.request("tools/call", _tool_call("write_query", INSERT, "k1")) for _ in range(2)]
    outcomes = sorted(_outcome(server.answer(i)) for i in ids)
    assert outcomes == ["executed", "replayed"]
    assert sqlite_rig.rows() == ["a"]


@needs_mcp1
def test_a_failed_store_executes_nothing(sqlite_rig: Sqlite) -> None:
    server = sqlite_rig.start()
    for suffix in ("", "-wal", "-shm"):
        path = Path(f"{sqlite_rig.keys}{suffix}")
        if path.exists():
            path.write_bytes(b"not a database" * 100)
    answer = server.call("tools/call", _tool_call("write_query", INSERT, "k1"))
    assert answer is not None and answer["error"]["code"] == -32013
    assert _outcome(answer) == "rejected_store"
    assert sqlite_rig.rows() == []


@needs_mcp1
def test_without_a_usable_store_the_server_does_not_start(sqlite_rig: Sqlite) -> None:
    assert MCP1 is not None
    env = {
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
        "STATEGUARD_IDEMPOTENCY_DB": str(sqlite_rig.tmp / "missing-dir" / "keys.db"),
        "STATEGUARD_KEYED_TOOLS": "write_query",
    }
    argv = [MCP1, "-m", "stateguard.adapters.mcp.inject", "mcp-server-sqlite"]
    done = subprocess.run(
        [*argv, "--db-path", str(sqlite_rig.db)],
        input=b"",
        capture_output=True,
        env=env,
        timeout=60,
    )
    assert done.returncode != 0 and done.stdout == b""  # fails closed: never serves unprotected
    assert b"StoreUnavailable" in done.stderr


@needs_mcp1
def test_fault_only_mode_crashes_the_unkeyed_server_at_the_same_points(sqlite_rig: Sqlite) -> None:
    crashed = sqlite_rig.start(keys=False, fault="after_effect")
    assert crashed.call("tools/call", _tool_call("write_query", INSERT)) is None
    resent = sqlite_rig.start(keys=False, fault="after_effect")
    answer = resent.call("tools/call", _tool_call("write_query", INSERT))
    assert _outcome(answer) is None and answer is not None and "result" in answer
    assert sqlite_rig.rows() == ["a", "a"]  # the arm without keys: the blind resend duplicates


# -- mcp 2.x: a plain MCPServer, on this interpreter ------------------------------------------

PLAIN_SERVER = """
import os, sys
from mcp.server.mcpserver import MCPServer

notes = sys.argv[1]
app = MCPServer("plain")

@app.tool()
def add_note(text: str) -> str:
    with open(notes, "a") as out:
        out.write(text + "\\n")
        out.flush()
        os.fsync(out.fileno())
    return f"added {text}"

@app.tool()
def tag(text: str) -> str:
    # Idempotent: a set of tags, one per line, never twice.
    path = notes + ".tags"
    tags = open(path).read().split() if os.path.exists(path) else []
    if text not in tags:
        with open(path, "a") as out:
            out.write(text + "\\n")
            out.flush()
            os.fsync(out.fileno())
    return f"tagged {text}"

app.run()
"""


def _mcp2_server(
    tmp: Path, fault: str | None = None, extra: dict[str, str] | None = None
) -> Server:
    pytest.importorskip("mcp.server.mcpserver")
    script = tmp / "plain_server.py"
    script.write_text(PLAIN_SERVER)
    env = {
        **os.environ,
        "PYTHONPATH": str(SRC),
        "PYTHONDONTWRITEBYTECODE": "1",
        "STATEGUARD_IDEMPOTENCY_DB": str(tmp / "keys.db"),
        "STATEGUARD_KEYED_TOOLS": "add_note",
    }
    if fault is not None:
        env.update(STATEGUARD_FAULT=fault, STATEGUARD_FAULT_FILE=str(tmp / "fault.fired"))
    env.update(extra or {})
    argv = [sys.executable, "-m", "stateguard.adapters.mcp.inject", str(script), str(tmp / "n.txt")]
    server = Server(argv, env)
    server.initialize()
    return server


def _notes(tmp: Path) -> list[str]:
    path = tmp / "n.txt"
    return path.read_text().split("\n")[:-1] if path.exists() else []


def test_an_unmodified_mcp2_server_is_keyed_by_injection(tmp_path: Path) -> None:
    servers = [_mcp2_server(tmp_path)]
    try:
        listed = servers[0].call("tools/list")
        assert listed is not None
        tools = {tool["name"]: tool for tool in listed["result"]["tools"]}
        assert tools["add_note"]["_meta"][CAPABILITY] == {"version": 1}
        first = servers[0].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1"))
        servers.append(_mcp2_server(tmp_path))
        again = servers[1].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1"))
        assert [_outcome(first), _outcome(again)] == ["executed", "replayed"]
        assert _notes(tmp_path) == ["a"]
    finally:
        for server in servers:
            server.close()


def test_an_mcp2_crash_after_the_effect_is_in_doubt_never_rerun(tmp_path: Path) -> None:
    servers = [_mcp2_server(tmp_path, fault="after_effect")]
    try:
        assert servers[0].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1")) is None
        servers.append(_mcp2_server(tmp_path, fault="after_effect"))
        answer = servers[1].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1"))
        assert _outcome(answer) == "in_doubt"
        assert _notes(tmp_path) == ["a"]
    finally:
        for server in servers:
            server.close()


# -- an operator-declared idempotent tool: an abandoned attempt runs again --------------------------

IDEMPOTENT = {
    "STATEGUARD_KEYED_TOOLS": "add_note,tag",
    "STATEGUARD_IDEMPOTENT_TOOLS": "tag",
    "STATEGUARD_IDEMPOTENCY_EXCLUSIVE": "1",
    "STATEGUARD_FAULT_TOOLS": "add_note,tag",
}


def _tags(tmp: Path) -> list[str]:
    path = tmp / "n.txt.tags"
    return path.read_text().split() if path.exists() else []


@pytest.mark.parametrize("point", ["after_begin", "after_effect"])
def test_an_abandoned_call_to_a_declared_idempotent_tool_runs_again(
    tmp_path: Path, point: str
) -> None:
    """The crashed process's key is abandoned (exclusive store); the next process runs the call
    again instead of answering in doubt, and the tag exists once."""
    servers = [_mcp2_server(tmp_path, fault=point, extra=IDEMPOTENT)]
    try:
        assert servers[0].call("tools/call", _tool_call("tag", {"text": "x"}, "k1")) is None
        servers[0].close()  # its process is dead: the kernel drops its store lock
        servers.append(_mcp2_server(tmp_path, extra=IDEMPOTENT))
        answer = servers[1].call("tools/call", _tool_call("tag", {"text": "x"}, "k1"))
        assert _outcome(answer) == "executed", answer
        again = servers[1].call("tools/call", _tool_call("tag", {"text": "x"}, "k1"))
        assert _outcome(again) == "replayed"
        assert _tags(tmp_path) == ["x"]
    finally:
        for server in servers:
            server.close()


def test_an_undeclared_tool_stays_in_doubt_under_the_same_store(tmp_path: Path) -> None:
    servers = [_mcp2_server(tmp_path, fault="after_effect", extra=IDEMPOTENT)]
    try:
        assert servers[0].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1")) is None
        servers[0].close()
        servers.append(_mcp2_server(tmp_path, extra=IDEMPOTENT))
        answer = servers[1].call("tools/call", _tool_call("add_note", {"text": "a"}, "k1"))
        assert _outcome(answer) == "in_doubt"
        assert _notes(tmp_path) == ["a"]  # never re-run
    finally:
        for server in servers:
            server.close()


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ({"STATEGUARD_IDEMPOTENT_TOOLS": "add_note"}, "needs STATEGUARD_IDEMPOTENCY_EXCLUSIVE=1"),
        (
            {"STATEGUARD_IDEMPOTENT_TOOLS": "other", "STATEGUARD_IDEMPOTENCY_EXCLUSIVE": "1"},
            "must be keyed tools: other",
        ),
    ],
)
def test_an_idempotent_declaration_that_could_not_act_is_refused(
    extra: dict[str, str], message: str, tmp_path: Path
) -> None:
    from stateguard.adapters.mcp.inject import _attacher

    env = {
        "STATEGUARD_IDEMPOTENCY_DB": str(tmp_path / "keys.db"),
        "STATEGUARD_KEYED_TOOLS": "add_note",
        **extra,
    }
    with pytest.raises(SystemExit, match=message):
        _attacher(env)

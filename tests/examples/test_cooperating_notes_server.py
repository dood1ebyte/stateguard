"""
The cooperating notes server (``examples/mcp/cooperating_notes_server.py``, Phase 2 S3.1): a keyed
``add_note`` whose note row and key record commit in one SQLite transaction. Its crash window is
closed: a crash at any point before the commit leaves nothing, and the relay's keyed resend
executes once; after the commit, the resend replays. Over stdio and Streamable HTTP.

The replay rule (PHASE2_PLAN.md, clarification A): a late original attempt racing the resend is
fenced off by the transaction itself -- the database's write lock and the key's primary key --
so it can never commit a second note. The non-cooperating baseline (``--no-keys``) stays
UNKNOWN, never success.

TEST-ONLY crash faults (SIGKILL at a point); synthetic fixtures, not traffic.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from stateguard.adapters.mcp.idempotency import IdempotencyStore, Outcome, fingerprint  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
SERVER = ROOT / "examples" / "mcp" / "cooperating_notes_server.py"
#: Phase 2 S3.3: its TypeScript twin (the TypeScript SDK and this repository's adapter).
TS_SERVER = ROOT / "adapters" / "typescript" / "examples" / "cooperating_notes_server.ts"
NODE = shutil.which("node")
_TS_READY = (
    NODE is not None
    and (ROOT / "adapters/typescript/node_modules/@modelcontextprotocol/sdk").exists()
)
#: The server command (before its DB argument), per language.
LANGUAGES = [
    pytest.param([sys.executable, str(SERVER)], id="python"),
    pytest.param(
        [NODE or "node", "--no-warnings", str(TS_SERVER)],
        id="typescript",
        marks=pytest.mark.skipif(not _TS_READY, reason="node or the TypeScript SDK not installed"),
    ),
]
CLI = [sys.executable, "-c", "from stateguard.cli import main; main()"]
POINTS = ("after_begin", "after_effect", "after_record")
OUTCOME = "io.stateguard/idempotencyOutcome"
STATE = "io.stateguard/executionState"
ENV = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONDONTWRITEBYTECODE": "1"}


def _count(db: Path, text: str) -> int:
    if not db.exists():
        return 0
    with sqlite3.connect(db) as connection:
        return int(
            connection.execute("SELECT COUNT(*) FROM notes WHERE text = ?", (text,)).fetchone()[0]
        )


def _effects(tmp: Path) -> Path:
    path = tmp / "effects.json"
    path.write_text(json.dumps({"tools": [{"name": "add_note", "effect": "non_idempotent"},
                                          {"name": "list_notes", "effect": "read_only"}]}))  # fmt: skip
    return path


def _crash_env(tmp: Path, point: str) -> dict[str, str]:
    return {**ENV, "STATEGUARD_FAULT": point, "STATEGUARD_FAULT_FILE": str(tmp / "crash.fired")}


# -- stdio --------------------------------------------------------------------------------------


def _stdio_call(
    tmp: Path, point: str, *server_flags: str, server: list[str] | None = None
) -> dict[str, Any]:
    db = tmp / "notes.db"
    relay = [
        *CLI, "relay", "stdio", "--retry", "auto", "--restart", "2",
        "--restart-policy", "after-unknown", "--effects", str(_effects(tmp)),
        "--audit", str(tmp / "audit.jsonl"),
        "--", *(server or [sys.executable, str(SERVER)]), str(db), *server_flags,
    ]  # fmt: skip
    process = subprocess.Popen(
        relay, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        env=_crash_env(tmp, point),
    )  # fmt: skip
    assert process.stdin is not None and process.stdout is not None

    def request(ident: int, method: str, params: dict[str, Any]) -> Any:
        assert process.stdin is not None and process.stdout is not None
        line = {"jsonrpc": "2.0", "id": ident, "method": method, "params": params}
        process.stdin.write(json.dumps(line).encode() + b"\n")
        process.stdin.flush()
        while True:
            raw = process.stdout.readline()
            if not raw:
                return None
            answer = json.loads(raw)
            if answer.get("id") == ident:
                return answer

    try:
        request(0, "initialize", {"protocolVersion": "2025-11-25", "capabilities": {},
                                  "clientInfo": {"name": "t", "version": "0"}})  # fmt: skip
        process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
        process.stdin.flush()
        request(1, "tools/list", {})
        answer = request(2, "tools/call", {"name": "add_note", "arguments": {"text": "m-1"}})
    finally:
        process.stdin.close()
        process.wait(timeout=60)
    return {"answer": answer, "count": _count(db, "m-1"), "fired": (tmp / "crash.fired").exists()}


@pytest.mark.parametrize("server", LANGUAGES)
@pytest.mark.parametrize("point", POINTS)
def test_a_keyed_call_survives_a_crash_at_any_point_over_stdio(
    tmp_path: Path, point: str, server: list[str]
) -> None:
    out = _stdio_call(tmp_path, point, server=server)
    assert out["fired"], "the crash point was never reached"
    answer = out["answer"]
    assert answer is not None and "result" in answer, answer
    assert not answer["result"].get("isError"), answer
    # Before the commit the transaction rolled back, so the resend executed; after it, replayed.
    expected = "replayed" if point == "after_record" else "executed"
    assert answer["result"]["_meta"][OUTCOME] == expected, answer
    assert out["count"] == 1  # exactly once, never a duplicate


@pytest.mark.parametrize("server", LANGUAGES)
@pytest.mark.parametrize("point", ["after_begin", "after_effect"])
def test_without_cooperation_the_same_crash_stays_unknown(
    tmp_path: Path, point: str, server: list[str]
) -> None:
    """The baseline (--no-keys): the relay cannot resend a non-idempotent call, so UNKNOWN."""
    out = _stdio_call(tmp_path, point, "--no-keys", server=server)
    assert out["fired"]
    answer = out["answer"]
    assert answer is not None and "error" in answer, answer  # never success
    assert answer["error"]["data"][STATE] == "unknown"
    assert out["count"] == 1  # it did execute: claiming failure would be false


# -- Streamable HTTP ------------------------------------------------------------------------------


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def _listening(port: int, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise TimeoutError(port)


@pytest.mark.parametrize("server", LANGUAGES)
@pytest.mark.parametrize("point", POINTS)
def test_a_keyed_call_survives_a_crash_at_any_point_over_http(
    tmp_path: Path, point: str, server: list[str]
) -> None:
    httpx2 = pytest.importorskip("httpx2")
    db = tmp_path / "notes.db"
    server_port, relay_port = _port(), _port()
    server_cmd = [*server, str(db), "--http", str(server_port), "--json"]
    env = _crash_env(tmp_path, point)
    servers = [subprocess.Popen(server_cmd, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)]  # fmt: skip
    _listening(server_port)
    stop = threading.Event()

    def supervise() -> None:  # restart the server once when it dies, on the same port
        while not stop.is_set():
            if servers[-1].poll() is not None and len(servers) == 1:
                servers.append(subprocess.Popen(server_cmd, env=env, stdout=subprocess.DEVNULL,
                                                stderr=subprocess.DEVNULL))  # fmt: skip
            time.sleep(0.02)

    threading.Thread(target=supervise, daemon=True).start()
    relay = subprocess.Popen(
        [*CLI, "relay", "http", "--retry", "auto", "--effects", str(_effects(tmp_path)),
         "--call-deadline", "0", "--listen", f"127.0.0.1:{relay_port}",
         "--audit", str(tmp_path / "audit.jsonl"),
         "--upstream", f"http://127.0.0.1:{server_port}/mcp"],  # fmt: skip
        env=ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )  # fmt: skip
    try:
        _listening(relay_port)
        http = httpx2.Client(timeout=30.0)
        url = f"http://127.0.0.1:{relay_port}/mcp"
        headers = {"accept": "application/json, text/event-stream"}
        init = http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                    "clientInfo": {"name": "t", "version": "0"}}})  # fmt: skip
        if "mcp-session-id" in init.headers:
            headers["mcp-session-id"] = init.headers["mcp-session-id"]
        http.post(
            url, headers=headers, json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        http.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        answer = http.post(url, headers=headers, json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "add_note", "arguments": {"text": "m-1"}}}).json()  # fmt: skip
        http.close()
    finally:
        stop.set()
        relay.terminate()
        relay.wait(timeout=30)
        for server in servers:
            if server.poll() is None:
                server.kill()
                server.wait(timeout=10)
    assert (tmp_path / "crash.fired").exists()
    assert "result" in answer and not answer["result"].get("isError"), answer
    expected = "replayed" if point == "after_record" else "executed"
    assert answer["result"]["_meta"][OUTCOME] == expected, answer
    assert _count(db, "m-1") == 1


# -- the replay rule: a late original attempt is fenced off ------------------------------------


def _store(tmp: Path, timeout: float = 5.0) -> IdempotencyStore:
    store = IdempotencyStore(tmp / "notes.db", scope="notes", timeout_seconds=timeout)
    with store.connect() as connection:
        connection.execute("CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY, text TEXT)")
    return store


def _insert(text: str, hold: threading.Event | None = None, entered: threading.Event | None = None,
            fail: bool = False) -> Any:  # fmt: skip
    def effect(connection: sqlite3.Connection) -> Any:
        connection.execute("INSERT INTO notes (text) VALUES (?)", (text,))
        if entered is not None:
            entered.set()
        if hold is not None:
            hold.wait(10)
        if fail:
            raise RuntimeError("the original attempt failed inside its transaction")
        return {"content": [{"type": "text", "text": f"added {text}"}]}

    return effect


@pytest.mark.parametrize("original", ["commits", "rolls_back"])
def test_a_late_original_attempt_is_fenced_off_by_the_transaction(
    tmp_path: Path, original: str
) -> None:
    """
    The original attempt is inside its transaction (its note written, not committed) when the
    resend arrives. The resend waits on the write lock. If the original commits, the resend
    finds the record and replays; if the original rolls back (it failed, or its process died),
    the resend executes. Either way: one note.
    """
    store = _store(tmp_path)
    key, fp = "k-1", fingerprint("add_note", {"text": "m"})
    hold, entered = threading.Event(), threading.Event()
    results: dict[str, Any] = {}

    def first() -> None:
        results["original"] = store.transact(
            key, fp, _insert("m", hold, entered, fail=original == "rolls_back"), tool="add_note"
        )

    thread = threading.Thread(target=first)
    thread.start()
    assert entered.wait(10)
    resend: dict[str, Any] = {}
    racer = threading.Thread(
        target=lambda: resend.update(
            r=IdempotencyStore(tmp_path / "notes.db", scope="notes").transact(
                key, fp, _insert("m"), tool="add_note"
            )
        )  # fmt: skip
    )
    racer.start()
    time.sleep(0.3)
    assert racer.is_alive(), "the resend did not wait for the original's transaction"
    hold.set()
    thread.join(10)
    racer.join(10)
    if original == "commits":
        assert results["original"].outcome is Outcome.EXECUTED
        assert resend["r"].outcome is Outcome.REPLAYED
    else:
        assert results["original"].outcome is Outcome.ROLLED_BACK
        assert resend["r"].outcome is Outcome.EXECUTED
    assert _count(tmp_path / "notes.db", "m") == 1


def test_a_resend_that_cannot_wait_executes_nothing(tmp_path: Path) -> None:
    """The original holds the lock past the resend's timeout: the resend fails closed."""
    store = _store(tmp_path)
    key, fp = "k-2", fingerprint("add_note", {"text": "m"})
    hold, entered = threading.Event(), threading.Event()
    thread = threading.Thread(
        target=lambda: store.transact(key, fp, _insert("m", hold, entered), tool="add_note")
    )
    thread.start()
    assert entered.wait(10)
    impatient = IdempotencyStore(tmp_path / "notes.db", scope="notes", timeout_seconds=0.2)
    assert (
        impatient.transact(key, fp, _insert("m"), tool="add_note").outcome is Outcome.REJECTED_STORE
    )
    hold.set()
    thread.join(10)
    assert _count(tmp_path / "notes.db", "m") == 1
    # And a resend after the original committed replays it.
    assert impatient.transact(key, fp, _insert("m"), tool="add_note").outcome is Outcome.REPLAYED
    assert _count(tmp_path / "notes.db", "m") == 1


ORIGINAL = """
import sqlite3, sys, time
sys.path.insert(0, sys.argv[2])
from stateguard.adapters.mcp.idempotency import IdempotencyStore, fingerprint
store = IdempotencyStore(sys.argv[1], scope="notes")
def effect(db):
    db.execute("INSERT INTO notes (text) VALUES ('m')")
    print("inside", flush=True)
    time.sleep(60)  # killed here: inside the transaction, its note written, not committed
store.transact("k-3", fingerprint("add_note", {"text": "m"}), effect, tool="add_note")
"""


def test_an_original_process_killed_inside_its_transaction_leaves_nothing(tmp_path: Path) -> None:
    """A real process: its note is written, uncommitted, when it is killed; the resend executes."""
    _store(tmp_path)
    original = subprocess.Popen(
        [sys.executable, "-c", ORIGINAL, str(tmp_path / "notes.db"), str(ROOT / "src")],
        stdout=subprocess.PIPE, text=True,
    )  # fmt: skip
    assert original.stdout is not None and original.stdout.readline().strip() == "inside"
    resend: dict[str, Any] = {}
    racer = threading.Thread(
        target=lambda: resend.update(
            r=IdempotencyStore(tmp_path / "notes.db", scope="notes").transact(
                "k-3", fingerprint("add_note", {"text": "m"}), _insert("m"), tool="add_note"
            )
        )  # fmt: skip
    )
    racer.start()
    time.sleep(0.3)
    assert racer.is_alive()  # waiting on the original's write lock
    original.send_signal(signal.SIGKILL)
    original.wait(10)
    racer.join(10)
    assert resend["r"].outcome is Outcome.EXECUTED
    assert _count(tmp_path / "notes.db", "m") == 1

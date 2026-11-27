"""
End to end: the real stdio relay (a process) in front of the SDK-based reference
server (``examples/mcp/keyed_reference_server.py``, a process), which dies at a
chosen point. Ground truth is the server's own effects, read directly: rows in
its orders table, lines in its ledger file.

Also: an unmodified official MCP SDK client against the server and against the
relay -- the contract changes nothing for a client that does not use it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp")

from stateguard.adapters.mcp.idempotency import (  # noqa: E402
    IDEMPOTENCY_KEY,
    IDEMPOTENCY_OUTCOME,
    BeginStatus,
    IdempotencyStore,
    fingerprint,
)

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]
RELAY = ROOT / "examples" / "mcp" / "stdio_proxy.py"
SERVER = ROOT / "examples" / "mcp" / "keyed_reference_server.py"
INIT = {
    "protocolVersion": "2025-11-25",
    "capabilities": {},
    "clientInfo": {"name": "t", "version": "0"},
}


class Client:
    """A JSON-lines client on a subprocess's stdio; collects every message."""

    def __init__(self, argv: list[str]) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
        )
        self.seen: list[Any] = []
        self._eof = threading.Event()
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                message = json.loads(raw)
            except ValueError:
                message = {"_unparsed": raw.decode("utf-8", "replace")}
            self.seen.append(message)
        self._eof.set()

    def send(self, message: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def wait(self, identifier: Any, timeout: float = 20.0) -> Any:
        """The first answer to *identifier* (kept: other answers are never consumed)."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for message in list(self.seen):
                if (
                    isinstance(message, dict)
                    and message.get("id") == identifier
                    and ("method" not in message)
                ):
                    return message
            if self._eof.is_set():
                return "eof"
            time.sleep(0.02)
        return "timeout"

    def start(self) -> None:
        self.send({"jsonrpc": "2.0", "id": "init", "method": "initialize", "params": INIT})
        assert isinstance(self.wait("init"), dict)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        self.send({"jsonrpc": "2.0", "id": "list", "method": "tools/list", "params": {}})
        assert isinstance(self.wait("list"), dict)
        time.sleep(0.3)  # the relay's own listing

    def call(
        self,
        identifier: Any,
        name: str,
        arguments: dict[str, Any],
        key: str | None = None,
        timeout: float = 30.0,
    ) -> Any:
        params: dict[str, Any] = {"name": name, "arguments": arguments}
        if key is not None:
            params["_meta"] = {IDEMPOTENCY_KEY: key}
        self.send({"jsonrpc": "2.0", "id": identifier, "method": "tools/call", "params": params})
        return self.wait(identifier, timeout)

    def answers(self, identifier: Any) -> list[Any]:
        return [
            m
            for m in self.seen
            if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
        ]

    def close(self) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.close()
        try:
            self.proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()


def _server_argv(tmp: Path, *extra: str) -> list[str]:
    return [
        sys.executable,
        str(SERVER),
        "--db",
        str(tmp / "orders.db"),
        "--ledger",
        str(tmp / "ledger.txt"),
        "--fault-file",
        str(tmp / "fault.fired"),
        *extra,
    ]


def _relay(tmp: Path, *server_extra: str, relay_extra: tuple[str, ...] = ()) -> Client:
    argv = [
        sys.executable,
        str(RELAY),
        "--audit",
        str(tmp / "audit.jsonl"),
        "--retry",
        "auto",
        "--restart",
        "2",
        *relay_extra,
        "--",
        *_server_argv(tmp, *server_extra),
    ]
    client = Client(argv)
    client.start()
    return client


def _orders(tmp: Path, item: str) -> int:
    with sqlite3.connect(tmp / "orders.db") as db:
        return int(db.execute("SELECT COUNT(*) FROM orders WHERE item = ?", (item,)).fetchone()[0])


def _ledger(tmp: Path) -> list[str]:
    """The ledger's complete lines, each as its entry (a keyed line is ``entry<TAB>key``)."""
    path = tmp / "ledger.txt"
    lines = path.read_text().split("\n")[:-1] if path.exists() else []  # a torn tail is no entry
    return [line.split("\t")[0] for line in lines]


def _ok(answer: Any) -> bool:
    return isinstance(answer, dict) and "result" in answer and not answer["result"].get("isError")


def _unknown(answer: Any) -> bool:
    data = answer.get("error", {}).get("data", {}) if isinstance(answer, dict) else {}
    return data.get("io.stateguard/executionState") == "unknown"


def _outcome(answer: dict[str, Any]) -> str | None:
    return answer.get("result", {}).get("_meta", {}).get(IDEMPOTENCY_OUTCOME)


def _audit(tmp: Path, event: str) -> list[dict[str, Any]]:
    path = tmp / "audit.jsonl"
    lines = path.read_text().splitlines() if path.exists() else []
    return [e for e in map(json.loads, lines) if e.get("event") == event]


# -- crash points through the relay ------------------------------------------------------

#: point -> client succeeds? (create_order is transactional: every crash recovers)
CREATE = ["before_begin", "after_begin", "mid_effect", "after_effect", "after_record"]
#: --no-recover: point -> (client succeeds?, complete ledger lines on disk)
APPEND_IN_DOUBT = {
    "before_begin": (True, 1),
    "after_begin": (False, 0),  # claimed, never acted: in doubt -> unknown
    "mid_effect": (False, 0),  # half a line: in doubt -> unknown
    "after_effect": (False, 1),  # the two-phase crash window: acted, unknown, never re-run
    "after_record": (True, 1),  # replayed from the record
}
#: recovery (the default): point -> the resend's outcome; one complete line, always
APPEND_RECOVERED = {
    "before_begin": "executed",  # nothing claimed: the resend executes
    "after_begin": "executed",  # abandoned, no line for the key: executed now, once
    "mid_effect": "executed",  # the torn half line is cut at startup, then as above
    "after_effect": "recovered",  # the key's own line is there: recorded from it
    "after_record": "replayed",
}


@pytest.mark.parametrize("point", CREATE)
def test_transactional_writes_recover_from_every_crash_point(tmp_path: Path, point: str) -> None:
    client = _relay(tmp_path, "--fault", point)
    try:
        answer = client.call(1, "create_order", {"item": "widget", "qty": 2})
        assert _ok(answer), answer
        assert len(client.answers(1)) == 1
        expected = "replayed" if point == "after_record" else "executed"
        assert _outcome(answer) == expected
    finally:
        client.close()
    assert (tmp_path / "fault.fired").exists()  # the server really died
    assert _orders(tmp_path, "widget") == 1  # ground truth: exactly one order


@pytest.mark.parametrize("point", list(APPEND_IN_DOUBT))
def test_two_phase_writes_without_recovery_stay_unknown_never_duplicate(
    tmp_path: Path, point: str
) -> None:
    client = _relay(tmp_path, "--no-recover", "--fault", point)
    try:
        answer = client.call(1, "append_ledger", {"entry": "pay-7"})
        succeeds, lines = APPEND_IN_DOUBT[point]
        assert _ok(answer) is succeeds, answer
        if not succeeds:
            assert _unknown(answer)
        assert len(client.answers(1)) == 1
    finally:
        client.close()
    assert (tmp_path / "fault.fired").exists()
    assert _ledger(tmp_path).count("pay-7") == lines  # never two


@pytest.mark.parametrize("point", list(APPEND_RECOVERED))
def test_two_phase_writes_with_recovery_succeed_exactly_once(tmp_path: Path, point: str) -> None:
    client = _relay(tmp_path, "--fault", point)
    try:
        answer = client.call(1, "append_ledger", {"entry": "pay-7"})
        assert _ok(answer), answer
        assert _outcome(answer) == APPEND_RECOVERED[point]
        assert len(client.answers(1)) == 1
    finally:
        client.close()
    assert (tmp_path / "fault.fired").exists()
    text = (tmp_path / "ledger.txt").read_text()
    assert text.endswith("\n")  # no torn line left behind
    assert _ledger(tmp_path).count("pay-7") == 1
    assert _ledger(tmp_path) == ["pay-7"]


def test_recovery_never_runs_while_the_attempt_may_still_act(tmp_path: Path) -> None:
    """A live server holds the store: a second one does not start, so it cannot adopt."""
    first = _direct(tmp_path)
    try:
        assert _ok(first.call(1, "append_ledger", {"entry": "live"}, key="k-live"))
        second = subprocess.run(
            _server_argv(tmp_path),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
            check=False,
        )
        assert second.returncode != 0
        assert b"exclusively" in second.stderr
    finally:
        first.close()
    assert _ledger(tmp_path).count("live") == 1


def test_recovery_needs_the_key_on_the_effect_not_a_lookalike(tmp_path: Path) -> None:
    """The same entry written by another request (another key) is not this key's effect."""
    client = _direct(tmp_path)
    try:
        assert _ok(client.call(1, "append_ledger", {"entry": "same"}, key="other"))
    finally:
        client.close()
    store = IdempotencyStore(tmp_path / "orders.db", scope="keyed-reference")
    # An abandoned attempt of "mine" whose effect never happened.
    begun = store.begin("mine", fingerprint("append_ledger", {"entry": "same"}))
    assert begun.status is BeginStatus.EXECUTE
    store.mark_in_doubt("mine", "simulated: cut off before acting")
    client = _direct(tmp_path)
    try:
        answer = client.call(2, "append_ledger", {"entry": "same"}, key="mine")
        assert _ok(answer) and _outcome(answer) == "executed", answer
    finally:
        client.close()
    assert _ledger(tmp_path).count("same") == 2  # one per key: the lookalike did not count


def test_a_lost_answer_on_a_live_server_is_replayed_after_a_deadline(tmp_path: Path) -> None:
    client = _relay(
        tmp_path,
        "--fault",
        "hang_after_record",
        relay_extra=("--call-deadline", "0.5", "--deadline-scope", "recoverable"),
    )
    try:
        answer = client.call(1, "append_ledger", {"entry": "pay-8"})
        assert _ok(answer) and _outcome(answer) == "replayed", answer
    finally:
        client.close()
    assert _ledger(tmp_path).count("pay-8") == 1


def test_a_server_that_does_not_advertise_gets_no_keys_and_no_resend(tmp_path: Path) -> None:
    client = _relay(
        tmp_path,
        "--no-advertise",
        "--fault",
        "after_effect",
        relay_extra=("--restart-policy", "after-unknown"),
    )
    try:
        answer = client.call(1, "append_ledger", {"entry": "pay-9"})
        assert _unknown(answer)
    finally:
        client.close()
    assert _ledger(tmp_path).count("pay-9") == 1  # ran once, never resent
    decisions = _audit(tmp_path, "retry_decision")
    assert decisions and not any(d.get("keyed") for d in decisions)


def test_a_read_and_a_keyed_write_lost_together_both_recover(tmp_path: Path) -> None:
    client = _relay(
        tmp_path,
        "--fault",
        "after_record",
        "--effect-delay",
        "0.3",
        relay_extra=("--trust-read-only-hints",),
    )
    try:
        client.send(
            {
                "jsonrpc": "2.0",
                "id": "w",
                "method": "tools/call",
                "params": {"name": "append_ledger", "arguments": {"entry": "pay-10"}},
            }
        )
        client.send(
            {
                "jsonrpc": "2.0",
                "id": "r",
                "method": "tools/call",
                "params": {"name": "read_ledger", "arguments": {}},
            }
        )
        write, read = client.wait("w"), client.wait("r")
        assert _ok(write) and _ok(read), (write, read)
        assert len(client.answers("w")) == 1 and len(client.answers("r")) == 1
    finally:
        client.close()
    assert _ledger(tmp_path).count("pay-10") == 1


def test_repeated_unknowns_never_execute_until_the_operator_decides(tmp_path: Path) -> None:
    client = _relay(tmp_path, "--no-recover", "--fault", "after_begin")
    try:
        first = client.call(1, "append_ledger", {"entry": "pay-11"}, key="client-key")
        assert _unknown(first)
        for attempt in (2, 3):  # the client tries again with its own key
            again = client.call(attempt, "append_ledger", {"entry": "pay-11"}, key="client-key")
            data = again.get("error", {}).get("data", {})
            assert data.get(IDEMPOTENCY_OUTCOME) == "in_doubt", again
        assert _ledger(tmp_path).count("pay-11") == 0
        # The operator checked: it never ran. Only then may it execute.
        assert IdempotencyStore(tmp_path / "orders.db", scope="keyed-reference").release(
            "client-key"
        )
        final = client.call(4, "append_ledger", {"entry": "pay-11"}, key="client-key")
        assert _ok(final) and _outcome(final) == "executed"
    finally:
        client.close()
    assert _ledger(tmp_path).count("pay-11") == 1


# -- the server alone ------------------------------------------------------------------------


def _direct(tmp: Path, *extra: str) -> Client:
    client = Client(_server_argv(tmp, *extra))
    client.start()
    return client


@pytest.mark.parametrize(
    "tool, arguments",
    [
        ("append_ledger", {"entry": "dup"}),
        ("create_order", {"item": "dup", "qty": 1}),
    ],
)
def test_concurrent_duplicates_with_one_key_execute_once(
    tmp_path: Path, tool: str, arguments: dict[str, Any]
) -> None:
    client = _direct(tmp_path, "--effect-delay", "0.4")
    try:
        for identifier in ("a", "b", "c"):
            client.send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "tools/call",
                    "params": {
                        "name": tool,
                        "arguments": arguments,
                        "_meta": {IDEMPOTENCY_KEY: "same"},
                    },
                }
            )
        answers = [client.wait(i) for i in ("a", "b", "c")]
        assert all(_ok(a) for a in answers), answers
        assert sorted(_outcome(a) or "" for a in answers) == ["executed", "replayed", "replayed"]
    finally:
        client.close()
    if tool == "append_ledger":
        assert _ledger(tmp_path).count("dup") == 1
    else:
        assert _orders(tmp_path, "dup") == 1


def test_a_reused_key_with_other_arguments_is_rejected(tmp_path: Path) -> None:
    client = _direct(tmp_path)
    try:
        assert _ok(client.call(1, "create_order", {"item": "a", "qty": 1}, key="k"))
        rejected = client.call(2, "create_order", {"item": "b", "qty": 1}, key="k")
        assert rejected["error"]["data"][IDEMPOTENCY_OUTCOME] == "rejected_mismatch"
    finally:
        client.close()
    assert _orders(tmp_path, "a") == 1 and _orders(tmp_path, "b") == 0


def test_a_restarted_server_replays_its_persisted_result(tmp_path: Path) -> None:
    first = _direct(tmp_path)
    try:
        executed = first.call(1, "create_order", {"item": "p", "qty": 3}, key="persist")
    finally:
        first.close()
    second = _direct(tmp_path)
    try:
        replayed = second.call(1, "create_order", {"item": "p", "qty": 3}, key="persist")
    finally:
        second.close()
    assert _outcome(executed) == "executed" and _outcome(replayed) == "replayed"
    assert replayed["result"]["content"] == executed["result"]["content"]
    assert _orders(tmp_path, "p") == 1


def test_a_corrupted_store_executes_nothing(tmp_path: Path) -> None:
    client = _direct(tmp_path)
    try:
        for suffix in ("", "-wal", "-shm"):
            path = tmp_path / f"orders.db{suffix}"
            if path.exists():
                path.write_bytes(b"\x00garbage" * 512)
        answer = client.call(1, "append_ledger", {"entry": "pay-12"}, key="k")
        assert answer["error"]["data"][IDEMPOTENCY_OUTCOME] == "rejected_store"
        plain = client.call(2, "read_ledger", {})  # an unkeyed call still works
        assert _ok(plain)
    finally:
        client.close()
    assert _ledger(tmp_path).count("pay-12") == 0


# -- compatibility with an unmodified official SDK client ----------------------------------


def _sdk_session(command: list[str], body: Any) -> Any:
    import anyio
    from mcp.client.session import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    async def run() -> Any:
        params = StdioServerParameters(command=command[0], args=command[1:], env=dict(os.environ))
        async with (
            stdio_client(params, errlog=open(os.devnull, "w")) as (read, write),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            return await body(session)

    return anyio.run(run)


@pytest.mark.parametrize("through_relay", [False, True])
def test_an_unmodified_sdk_client_works_with_and_without_keys(
    tmp_path: Path, through_relay: bool
) -> None:
    command = _server_argv(tmp_path)
    if through_relay:
        command = [
            sys.executable,
            str(RELAY),
            "--retry",
            "auto",
            "--audit",
            str(tmp_path / "audit.jsonl"),
            "--",
            *command,
        ]

    async def body(session: Any) -> Any:
        tools = {t.name: t for t in (await session.list_tools()).tools}
        plain = await session.call_tool("create_order", {"item": "sdk", "qty": 1})
        keyed = [
            await session.call_tool(
                "create_order", {"item": "sdk-k", "qty": 1}, meta={IDEMPOTENCY_KEY: "sdk-key"}
            )
            for _ in range(2)
        ]
        return tools, plain, keyed

    tools, plain, keyed = _sdk_session(command, body)
    assert (tools["create_order"].meta or {}).get("io.stateguard/idempotency") == {"version": 1}
    assert (
        tools["get_order"].meta is None
        or "io.stateguard/idempotency" not in tools["get_order"].meta
    )
    assert not plain.is_error
    assert [(k.meta or {}).get(IDEMPOTENCY_OUTCOME) for k in keyed] == ["executed", "replayed"]
    assert _orders(tmp_path, "sdk") == 1
    assert _orders(tmp_path, "sdk-k") == 1
    if through_relay:
        assert _orders(tmp_path, "sdk") == 1  # the relay's own key changed nothing either

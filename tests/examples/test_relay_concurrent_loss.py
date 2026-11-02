"""
Two mutations genuinely in flight when the connection is lost.

The third-party validation (THIRD_PARTY_MCP_RECOVERY_VALIDATION.md, S2) could
not force this: its concurrent read always resolved before the loss. Here the
scripted server makes it deterministic. It processes calls one at a time, so
the server dies on the *second* mutating call only after it has received the
first and left it unanswered. At the moment of loss both requests are
therefore unresolved by construction, not by timing.

Ground truth is the server's shared dict and its executed-call ledger, read by
the test directly and never through StateGuard. Every scenario checks the
safety invariants that matter at loss:

* neither mutation is resent, to the dead server or to any restarted one;
* each client request gets exactly one answer, and that answer is an unknown
  outcome (never success, never "not sent");
* each call is reconciled against its *own* arguments, so the verdict for one
  call never borrows the other call's state;
* the verdict never asserts execution or attribution.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import IO, Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import RelayMode, Rig, ServerAction, ServerCall  # noqa: E402

from stateguard.adapters.mcp import ExecutionState, Postcondition  # noqa: E402

from .test_relay_reconciliation import (  # noqa: E402
    PEEK,
    READ,
    TOOLS,
    FsServer,
    _move_entry,
    _policies,
    _result,
)

pytestmark = pytest.mark.integration


class HoldingFsServer(FsServer):
    """FsServer that also honours DROP_RESPONSE: execute, keep serving, never answer."""

    def _call(
        self, identifier: Any, params: dict[str, Any], message: dict[str, Any], writer: IO[bytes]
    ) -> bool:
        name, arguments = params.get("name"), params.get("arguments") or {}
        if name in ("read", "peek"):
            return super()._call(identifier, params, message, writer)
        with self._lock:
            call = ServerCall(self._calls, str(name), arguments, message)
            self._calls += 1
        action = self.faults(call)
        if action is ServerAction.CLOSE_BEFORE_EXECUTE:
            self.dead = True
            return False
        if action is ServerAction.HANG_BEFORE_EXECUTE:
            return True  # received, never executed, never answered
        result = self._execute(str(name), arguments)
        self.executed.append(call)  # the side effect happened
        if action is ServerAction.CLOSE_AFTER_EXECUTE:
            self.dead = True
            return False
        if action is ServerAction.DROP_RESPONSE:
            return True
        self._reply(writer, identifier, result)
        return True


class Sequence:
    """The n-th mutating call (across every server process) gets ``actions[n]``."""

    def __init__(self, *actions: ServerAction) -> None:
        self.actions, self.seen = list(actions), 0

    def __call__(self, call: ServerCall) -> ServerAction:
        if call.name in ("read", "peek"):
            return ServerAction.NORMAL
        index, self.seen = self.seen, self.seen + 1
        return self.actions[index] if index < len(self.actions) else ServerAction.NORMAL


def _write_entry() -> dict[str, Any]:
    return {
        "name": "write",
        "effect": "non_idempotent",
        "reconcile": {
            "timeout_seconds": 2.0,
            "probes": [
                {
                    "tool": "read",
                    "arguments": {"path": "$path"},
                    "expect": {"text_equals_argument": "$content"},
                    "absent_marker": "ENOENT",
                }
            ],
        },
    }


def _rig(faults: Sequence, fs: dict[str, str], *, reconcile: bool = True) -> Rig:
    def factory() -> HoldingFsServer:
        return HoldingFsServer(TOOLS, faults=faults, fs=fs)

    effects, policy = _policies(READ, PEEK, _move_entry(), _write_entry())
    rig = Rig(
        HoldingFsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        server_factory=factory,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy if reconcile else None,
        retry="off",
        attest_termination=True,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _send_calls(rig: Rig, *calls: tuple[str, dict[str, Any]]) -> list[int]:
    """Send every call without waiting: they are all in flight together."""
    ids = []
    for name, arguments in calls:
        rig._next_id += 1
        identifier = rig._next_id
        rig.sent_ids.append(identifier)
        ids.append(identifier)
        rig.send(
            {
                "jsonrpc": "2.0",
                "id": identifier,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments, "_meta": {}},
            }
        )
    return ids


def _answers(rig: Rig, ids: list[int], timeout: float = 10.0) -> dict[int, list[dict[str, Any]]]:
    """Every message the client received for each id, once each id has one answer."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        got = {
            i: [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == i]
            for i in ids
        }
        if all(got.values()):
            time.sleep(0.3)  # long enough for a second, duplicate answer to show up
            return {
                i: [m for m in rig.client_received if isinstance(m, dict) and m.get("id") == i]
                for i in ids
            }
        time.sleep(0.02)
    raise AssertionError(f"not every call was answered: {got}")


def _data(message: dict[str, Any]) -> dict[str, Any]:
    assert "error" in message and "result" not in message, message
    return dict(message["error"].get("data", {}))


def _mutations_received(rig: Rig) -> dict[Any, int]:
    """How many times each client request id reached any server as a tools/call."""
    counts: dict[Any, int] = {}
    for line in rig.all_received:
        message = json.loads(line)
        if message.get("method") == "tools/call" and message["params"]["name"] not in (
            "read",
            "peek",
        ):
            counts[message["id"]] = counts.get(message["id"], 0) + 1
    return counts


def _assert_safe(rig: Rig, ids: list[int], answers: dict[int, list[dict[str, Any]]]) -> None:
    received = _mutations_received(rig)
    for identifier in ids:
        assert received.get(identifier, 0) == 1, f"{identifier} reached a server {received}"
        assert len(answers[identifier]) == 1, f"{identifier} answered {answers[identifier]}"
        data = _data(answers[identifier][0])
        assert data["io.stateguard/executionState"] == "unknown"
        assert data.get("io.stateguard/attribution", "unknown") == "unknown"
        inv = rig.relay.ledger.get(identifier)
        assert inv is not None and inv.state is ExecutionState.UNKNOWN
        assert ExecutionState.COMPLETED not in inv.history
    for server in rig.servers[1:]:
        for line in server.received:
            message = json.loads(line)
            if message.get("method") == "tools/call":
                assert message["params"]["name"] in ("read", "peek"), message
                assert str(message["id"]).startswith("stateguard-reconcile:")
    verdicts = [e for e in rig.audit if e.get("event") == "reconcile_verdict"]
    assert not any(v.get("resent") for v in verdicts)


MOVE_A = ("move", {"source": "a.txt", "destination": "b.txt"})
MOVE_C = ("move", {"source": "c.txt", "destination": "d.txt"})


def _fs() -> dict[str, str]:
    return {"a.txt": "order 001", "c.txt": "order 002"}


@pytest.mark.parametrize(
    "first, second, expected_fs, verdicts",
    [
        # both ran, neither answered
        (
            ServerAction.DROP_RESPONSE,
            ServerAction.CLOSE_AFTER_EXECUTE,
            {"b.txt": "order 001", "d.txt": "order 002"},
            ("observed_satisfied", "observed_satisfied"),
        ),
        # first ran, second was received but never ran
        (
            ServerAction.DROP_RESPONSE,
            ServerAction.CLOSE_BEFORE_EXECUTE,
            {"b.txt": "order 001", "c.txt": "order 002"},
            ("observed_satisfied", "observed_not_satisfied"),
        ),
        # first was received but never ran, second ran
        (
            ServerAction.HANG_BEFORE_EXECUTE,
            ServerAction.CLOSE_AFTER_EXECUTE,
            {"a.txt": "order 001", "d.txt": "order 002"},
            ("observed_not_satisfied", "observed_satisfied"),
        ),
    ],
)
def test_two_mutations_in_flight_at_loss_are_each_reconciled_and_never_resent(
    first: ServerAction,
    second: ServerAction,
    expected_fs: dict[str, str],
    verdicts: tuple[str, str],
) -> None:
    fs = _fs()
    rig = _rig(Sequence(first, second), fs)
    try:
        ids = _send_calls(rig, MOVE_A, MOVE_C)
        answers = _answers(rig, ids)
        assert fs == expected_fs  # ground truth, independent of StateGuard
        _assert_safe(rig, ids, answers)
        got = tuple(_data(answers[i][0])["io.stateguard/postcondition"] for i in ids)
        assert got == verdicts
        for identifier, verdict in zip(ids, verdicts, strict=True):
            inv = rig.relay.ledger.get(identifier)
            assert inv is not None and inv.postcondition is Postcondition(verdict)
    finally:
        rig.close()


def test_two_in_flight_without_reconciliation_are_both_unknown_and_never_resent() -> None:
    fs = _fs()
    rig = _rig(
        Sequence(ServerAction.DROP_RESPONSE, ServerAction.CLOSE_AFTER_EXECUTE), fs, reconcile=False
    )
    try:
        ids = _send_calls(rig, MOVE_A, MOVE_C)
        answers = _answers(rig, ids)
        assert fs == {"b.txt": "order 001", "d.txt": "order 002"}
        _assert_safe(rig, ids, answers)
        for identifier in ids:
            assert "io.stateguard/postcondition" not in _data(answers[identifier][0])
    finally:
        rig.close()


def test_conflicting_writes_in_flight_never_borrow_each_others_state() -> None:
    """
    Two writes to the same path, both executed, neither answered. Only the
    later value is on disk. The earlier write's own predicate (its own
    content) must read as not satisfied even though its call ran: the probe
    observes state, not execution, and must not credit it with the other
    call's state.
    """
    fs = {"x.txt": "old"}
    rig = _rig(Sequence(ServerAction.DROP_RESPONSE, ServerAction.CLOSE_AFTER_EXECUTE), fs)
    try:
        ids = _send_calls(
            rig,
            ("write", {"path": "x.txt", "content": "A"}),
            ("write", {"path": "x.txt", "content": "B"}),
        )
        answers = _answers(rig, ids)
        assert fs == {"x.txt": "B"}
        assert [c.arguments["content"] for c in rig.all_executed] == ["A", "B"]
        _assert_safe(rig, ids, answers)
        got = [_data(answers[i][0])["io.stateguard/postcondition"] for i in ids]
        assert got == ["observed_not_satisfied", "observed_satisfied"]
        for identifier in ids:
            assert _data(answers[identifier][0])["io.stateguard/attribution"] == "unknown"
    finally:
        rig.close()


def test_a_read_in_flight_with_a_mutation_is_answered_and_the_mutation_never_resent() -> None:
    fs = _fs()
    # The read is answered before the mutation; the mutation's response is lost.
    rig = _rig(Sequence(ServerAction.CLOSE_AFTER_EXECUTE), fs)
    try:
        ids = _send_calls(rig, ("read", {"path": "c.txt"}), MOVE_A)
        answers = _answers(rig, ids)
        assert "result" in answers[ids[0]][0] and len(answers[ids[0]]) == 1
        assert _data(answers[ids[1]][0])["io.stateguard/postcondition"] == "observed_satisfied"
        assert _mutations_received(rig) == {ids[1]: 1}
        assert fs == {"b.txt": "order 001", "c.txt": "order 002"}
    finally:
        rig.close()


def test_results_are_identical_across_repeated_runs() -> None:
    """Deterministic by construction: the same faults give the same verdicts."""
    seen = set()
    for _ in range(5):
        fs = _fs()
        rig = _rig(Sequence(ServerAction.DROP_RESPONSE, ServerAction.CLOSE_AFTER_EXECUTE), fs)
        try:
            ids = _send_calls(rig, MOVE_A, MOVE_C)
            answers = _answers(rig, ids)
            _assert_safe(rig, ids, answers)
            seen.add(tuple(_data(answers[i][0])["io.stateguard/postcondition"] for i in ids))
        finally:
            rig.close()
    assert seen == {("observed_satisfied", "observed_satisfied")}


_ = _result  # imported for parity with the reconciliation tests' server

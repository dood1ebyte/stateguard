"""
Reconciliation lifecycle and routing under stress (Week 4A), in-process.

Every client answer is counted at end of stream (the client side reads until
the relay closes it), and an answer from a real result is told apart from an
unknown-outcome answer. Ordering is forced with events and hooks, never with
sleeps; every wait is bounded.
"""

from __future__ import annotations

import contextlib
import json
import queue
import threading
import time
from collections.abc import Callable
from typing import IO, Any

import pytest

from tests.examples.test_relay_reconciliation import (
    PEEK,
    READ,
    TOOLS,
    First,
    FsServer,
    World,
    _data,
    _events,
    _move,
    _move_entry,
    _never_resent,
    _policies,
)

# On sys.path via the module above.
from runtime.harness import (  # noqa: E402
    META,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
)

from stateguard.adapters.mcp import TerminationEvidence  # noqa: E402

pytestmark = pytest.mark.integration


def _answers(rig: Rig, identifier: Any) -> list[dict[str, Any]]:
    return [
        m
        for m in rig.client_received
        if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
    ]


def _answers_at_eof(rig: Rig, identifier: Any) -> list[dict[str, Any]]:
    rig.close()  # joins the client-side reader: everything the relay wrote was read
    assert rig.client_eof.is_set(), "the client never saw end of stream"
    return _answers(rig, identifier)


def _await_answers(rig: Rig, ids: list[Any], timeout: float) -> dict[Any, dict[str, Any]]:
    """The first answer to each of *ids*, from everything the client received (bounded)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = {i: a[0] for i in ids if (a := _answers(rig, i))}
        if len(found) == len(ids):
            return found
        with contextlib.suppress(queue.Empty):
            rig._responses.get(timeout=0.05)  # block until the client reads something
    return {i: a[0] for i in ids if (a := _answers(rig, i))}


def _is_unknown_answer(message: dict[str, Any]) -> bool:
    error = message.get("error")
    return (
        isinstance(error, dict)
        and error.get("code") == -32000
        and error.get("data", {}).get("io.stateguard/executionState") == "unknown"
    )


def _is_real_result(message: dict[str, Any]) -> bool:
    return "result" in message and "error" not in message


def _call(identifier: Any, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": name, "arguments": arguments, "_meta": META},
    }


class _EvidenceRig(Rig):
    """A rig whose restarts report exactly the evidence and identity a test gives."""

    def __init__(
        self,
        *args: Any,
        evidence: Callable[[int], Any],
        identity: Callable[[int], Any] = Rig.executor_name,
        gate: threading.Event | None = None,
        **kwargs: Any,
    ) -> None:
        self._evidence_of, self._identity_of, self._gate = evidence, identity, gate
        self.executor_name = identity  # type: ignore[method-assign,assignment]  # generation 0 too
        #: Set when a restart has begun (before the gate, if any).
        self.restart_begun = threading.Event()
        super().__init__(*args, **kwargs)

    def _restart(self) -> Any:
        self.restart_begun.set()
        if self._gate is not None:
            assert self._gate.wait(30), "the test never released the restart"
        replaced = len(self.servers) - 1
        made = super()._restart()  # attest_termination off: it reports nothing itself
        return self._relay_module.Restarted(
            made.stdin,
            made.stdout,
            self._evidence_of(replaced),
            self._identity_of(len(self.servers) - 1),
        )


def _rig(
    *,
    evidence: Callable[[int], Any] | None = None,
    identity: Callable[[int], Any] = Rig.executor_name,
    gate: threading.Event | None = None,
    faults: Any = None,
    entries: tuple[dict[str, Any], ...] = (),
    fs: dict[str, str] | None = None,
    restarted: Callable[[dict[str, str]], ScriptedServer] | None = None,
) -> World:
    fs = fs if fs is not None else {"a.txt": "order 001"}
    faults = faults or First("move", ServerAction.CLOSE_AFTER_EXECUTE)

    def factory() -> ScriptedServer:
        if restarted is not None:
            return restarted(fs)
        return FsServer(TOOLS, faults=faults, fs=fs)

    def scope_ended(replaced: int) -> TerminationEvidence:
        return TerminationEvidence.scope_ended(
            "test scope: confirmed empty", executor=Rig.executor_name(replaced)
        )

    effects, policy = _policies(*entries)
    rig = _EvidenceRig(
        FsServer(TOOLS, faults=faults, fs=fs),
        RelayMode.AUTO,
        evidence=evidence or scope_ended,
        identity=identity,
        gate=gate,
        server_factory=factory,
        max_restarts=2,
        restart_policy="after-unknown",
        effects=effects,
        reconcile=policy,
        retry="off",
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return World(rig, fs)


def _evidence_rig(world: World) -> _EvidenceRig:
    assert isinstance(world.rig, _EvidenceRig)
    return world.rig


def _budgeted(max_seconds: float, timeout: float, probes: int = 2) -> tuple[dict[str, Any], ...]:
    entry = _move_entry(timeout_seconds=timeout)
    base = entry["reconcile"]["probes"]
    entry["reconcile"]["probes"] = (base * probes)[:probes]
    entry["reconcile"]["max_seconds"] = max_seconds
    return (READ, PEEK, entry)


# ---------------------------------------------------------------------------
# Evidence that must never allow a probe
# ---------------------------------------------------------------------------


def test_an_unconfirmed_scope_is_undetermined_and_probes_nothing() -> None:
    world = _rig(evidence=lambda _: TerminationEvidence.scope_unconfirmed("helper stopped"))
    try:
        response = _move(world, timeout=15)
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert data["io.stateguard/treatAsSuccess"] is False
        (reason,) = data["io.stateguard/reconciliation"]["reasons"]
        assert "could not be confirmed empty" in reason and "nothing was probed" in reason
        assert not _events(world.rig, "reconcile_probe")
        (evidence,) = _events(world.rig, "executor_termination")
        assert evidence["kind"] == "scope_unconfirmed" and evidence["applies"] is False
        _never_resent(world)
        assert len(_answers_at_eof(world.rig, world.rig.sent_ids[-1])) == 1
    finally:
        world.rig.close()


@pytest.mark.parametrize("bad", ["has space", "", "x" * 121, "-leading-dash", 42])
def test_a_malformed_executor_identity_never_attests(bad: Any) -> None:
    """The restarted generation's identity is malformed, so it can never be attested later."""
    world = _rig(identity=lambda index: bad if index == 0 else Rig.executor_name(index))
    # The evidence names what generation 0 would have been called: never enough.
    try:
        data = _data(_move(world, timeout=15))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert not _events(world.rig, "reconcile_probe")
        rejected = _events(world.rig, "executor_identity_rejected")
        assert rejected and rejected[0]["generation"] == 0
    finally:
        world.rig.close()


def test_malformed_termination_evidence_is_a_failed_restart_and_probes_nothing() -> None:
    world = _rig(evidence=lambda _: {"kind": "scope_ended", "executor": "rig-executor-0"})
    try:
        data = _data(_move(world, timeout=20))
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert not _events(world.rig, "reconcile_probe")
        failed = _events(world.rig, "restart_failed")
        assert failed and all("TerminationEvidence" in e["error"] for e in failed)
        assert not _events(world.rig, "executor_termination")
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# Bounded waits: one overall budget
# ---------------------------------------------------------------------------


def test_a_termination_that_never_comes_is_answered_within_the_budget() -> None:
    """A scope that never ends: the restart is stuck; the answer still comes in budget."""
    gate = threading.Event()
    world = _rig(gate=gate, entries=_budgeted(max_seconds=1.5, timeout=0.5))
    try:
        world.rig.send(_call("T-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        started = time.monotonic()
        response = world.rig.wait_for("T-1", timeout=10)
        elapsed = time.monotonic() - started
        assert response is not None and _is_unknown_answer(response)
        assert _data(response)["io.stateguard/postcondition"] == "undetermined"
        assert elapsed < 1.5 + 1.0, f"held {elapsed:.2f}s against a 1.5s budget"
        assert _evidence_rig(world).restart_begun.is_set()
        assert not _events(world.rig, "reconcile_probe")
    finally:
        gate.set()
    assert len(_answers_at_eof(world.rig, "T-1")) == 1


class _SilentListServer(FsServer):
    """A restarted server that never answers tools/list."""

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        message = json.loads(line)
        if isinstance(message, dict) and message.get("method") == "tools/list":
            self.received.append(b"")  # noted, never answered
            return True
        return super()._handle(line, writer)


def test_a_probe_server_that_never_lists_is_answered_within_the_budget() -> None:
    world = _rig(
        entries=_budgeted(max_seconds=2.0, timeout=0.5),
        restarted=lambda fs: _SilentListServer(TOOLS, fs=fs),
    )
    try:
        world.rig.send(_call("T-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        started = time.monotonic()
        response = world.rig.wait_for("T-1", timeout=15)
        elapsed = time.monotonic() - started
        assert response is not None
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert (
            "no restarted server became available"
            in data["io.stateguard/reconciliation"]["reasons"][0]
        )
        assert elapsed < 2.0 + 1.0, f"held {elapsed:.2f}s against a 2s budget"
        assert not _events(world.rig, "reconcile_probe")
    finally:
        world.rig.close()


class _HangingReads(FsServer):
    def _read(self, identifier: Any, arguments: dict[str, Any], writer: IO[bytes]) -> bool:
        self.reads += 1
        return True  # never answers a read


def test_the_budget_stops_probing_and_the_verdict_is_incomplete() -> None:
    """Each probe within its timeout, but together over budget: the rest are never sent."""
    world = _rig(
        entries=_budgeted(max_seconds=1.5, timeout=0.5, probes=3),
        restarted=lambda fs: _HangingReads(TOOLS, fs=fs),
    )
    relay = world.rig.relay
    deadlines: list[float] = []
    begin, send_probe = relay._begin_reconciliation, relay._send_probe

    def remember_deadline(*args: Any, **kwargs: Any) -> Any:
        result = begin(*args, **kwargs)
        with relay._reconcile_lock:
            deadlines.extend(p["deadline"] for p in relay._pending_reconcile.values())
        return result

    def first_probe_waits_out_the_budget(*args: Any, **kwargs: Any) -> Any:
        if len(deadlines) == 1:  # only the first probe: it waits for all that is left
            args = (*args[:3], max(0.0, deadlines.pop() - time.monotonic()))
        return send_probe(*args, **kwargs)

    relay._begin_reconciliation = remember_deadline
    relay._send_probe = first_probe_waits_out_the_budget
    try:
        world.rig.send(_call("T-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        started = time.monotonic()
        response = world.rig.wait_for("T-1", timeout=10)
        elapsed = time.monotonic() - started
        data = _data(response)
        assert data["io.stateguard/postcondition"] == "undetermined"
        assert _events(world.rig, "reconcile_budget_exhausted")
        assert len(_events(world.rig, "reconcile_probe")) == 1  # the others were never sent
        assert elapsed < 1.5 + 1.0
    finally:
        world.rig.close()


# ---------------------------------------------------------------------------
# Concurrency and shutdown
# ---------------------------------------------------------------------------


class _TwoMoves:
    """Generation 0 only: the move of a.txt hangs (never executed), the move of c.txt
    executes and the server dies before answering. Both are then unknown."""

    def __init__(self) -> None:
        self.fired: set[str] = set()

    def __call__(self, call: ServerCall) -> ServerAction:
        source = (call.arguments or {}).get("source")
        if call.name != "move" or source in self.fired:
            return ServerAction.NORMAL
        self.fired.add(str(source))
        return (
            ServerAction.HANG_BEFORE_EXECUTE
            if source == "a.txt"
            else ServerAction.CLOSE_AFTER_EXECUTE
        )


def test_simultaneous_reconciliations_are_each_answered_once_with_their_own_verdict() -> None:
    world = _rig(faults=_TwoMoves(), fs={"a.txt": "order 001", "c.txt": "order 002"})
    rig = world.rig
    try:
        rig.send(_call("M-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        rig.send(_call("M-2", "move", {"source": "c.txt", "destination": "d.txt"}))
        answers = _await_answers(rig, ["M-1", "M-2"], timeout=20)
        first, second = answers.get("M-1"), answers.get("M-2")
        assert first is not None and second is not None
        # Ground truth: only the second executed.
        assert world.fs == {"a.txt": "order 001", "d.txt": "order 002"}
        assert _data(first)["io.stateguard/postcondition"] == "observed_not_satisfied"
        assert _data(second)["io.stateguard/postcondition"] == "observed_satisfied"
        for data in (_data(first), _data(second)):
            assert data["io.stateguard/executionState"] == "unknown"
            assert data["io.stateguard/treatAsSuccess"] is False
        assert len(_events(rig, "reconcile_pending")) == 2
        assert len(_events(rig, "reconcile_answered")) == 2
        probe_ids = [e["id"] for e in _events(rig, "reconcile_probe")]
        assert len(probe_ids) == 4
        # A real result after the restart is a real result, not an unknown answer.
        read = rig.call("read", {"path": "d.txt"})
        assert read is not None and _is_real_result(read)
        moves = [c for c in rig.all_executed if c.name == "move"]
        assert len(moves) == 1  # neither original was resent
        assert len(_answers_at_eof(rig, "M-1")) == 1
        assert len(_answers_at_eof(rig, "M-2")) == 1
    finally:
        rig.close()


def test_shutdown_while_termination_is_being_confirmed_answers_once() -> None:
    """The client leaves while the restart is still confirming the old scope."""
    gate = threading.Event()
    world = _rig(gate=gate, entries=_budgeted(max_seconds=1.5, timeout=0.5))
    rig = world.rig
    try:
        rig.send(_call("T-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        assert _evidence_rig(world).restart_begun.wait(10)
        rig._client_w.close()  # the client is gone; the restart is still stuck
        response = rig.wait_for("T-1", timeout=10)
        assert response is not None and _is_unknown_answer(response)
        assert _data(response)["io.stateguard/postcondition"] == "undetermined"
    finally:
        gate.set()
    answers = _answers_at_eof(rig, "T-1")
    assert len(answers) == 1 and _is_unknown_answer(answers[0])
    assert len(_events(rig, "reconcile_answered")) == 1


def test_shutdown_during_a_probe_answers_once() -> None:
    world = _rig(restarted=lambda fs: _HangingReads(TOOLS, fs=fs))
    rig = world.rig
    relay = rig.relay
    probing = threading.Event()
    send_probe = relay._send_probe

    def announce(*args: Any, **kwargs: Any) -> Any:
        probing.set()
        return send_probe(*args, **kwargs)

    relay._send_probe = announce
    try:
        rig.send(_call("T-1", "move", {"source": "a.txt", "destination": "b.txt"}))
        assert probing.wait(10), "no probe was sent"
        started = time.monotonic()
        answers = _answers_at_eof(rig, "T-1")  # closes the client: the relay stops
        assert time.monotonic() - started < 5
        assert len(answers) == 1 and _is_unknown_answer(answers[0])
        assert answers[0]["error"]["data"]["io.stateguard/postcondition"] == "undetermined"
        assert len(_events(rig, "reconcile_answered")) == 1
        assert not _events(rig, "reconcile_answer_unconfirmed")
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Routing: a client id colliding with an outstanding relay-owned request
# ---------------------------------------------------------------------------


class _HoldsListings(FsServer):
    """Answers relay listings normally until ``hold`` is set; then keeps the next one."""

    hold: bool = False
    held: list[Any] | None = None
    held_event: threading.Event | None = None

    def _handle(self, line: bytes, writer: IO[bytes]) -> bool:
        message = json.loads(line)
        if (
            self.hold
            and isinstance(message, dict)
            and message.get("method") == "tools/list"
            and str(message.get("id", "")).startswith("stateguard-relay:")
        ):
            self.received.append(line)
            assert self.held is not None and self.held_event is not None
            self.held.append(message["id"])
            self.held_event.set()
            return True
        return super()._handle(line, writer)


def test_a_client_id_colliding_with_an_outstanding_relay_listing_gets_one_answer() -> None:
    """
    The documented exposure (contract §8): while a relay listing is pending, a
    client request reusing its id loses the first answer to the relay. The client
    still gets at most one answer -- the next one with that id -- never two.
    """
    server = _HoldsListings(TOOLS, fs={"a.txt": "x"})
    server.held, server.held_event = [], threading.Event()
    effects, _ = _policies(READ, PEEK, {"name": "move", "effect": "non_idempotent"})
    rig = Rig(server, RelayMode.AUTO, effects=effects, retry="off")
    try:
        rig.request("server/discover")
        assert rig.wait_synced()
        server.hold = True
        server._write(
            server._writer,
            json.dumps({"jsonrpc": "2.0", "method": "notifications/tools/list_changed"}).encode(),
        )
        assert server.held_event.wait(5), "the relay never asked for a new listing"
        (held,) = server.held
        rig.send({"jsonrpc": "2.0", "id": held, "method": "ping", "params": {"_meta": META}})
        # The server answers the client's ping (an error: unknown method), then the
        # listing it held; both carry the same id.
        assert rig.wait_for(held, timeout=1.0) is None  # the first went to the relay
        listing = [t.definition for t in server.tools]
        server._reply(server._writer, held, {"resultType": "complete", "tools": listing})
        response = rig.wait_for(held, timeout=5.0)
        assert response is not None
        assert _events(rig, "response_id_collision")
        assert len(_answers_at_eof(rig, held)) == 1
    finally:
        rig.close()

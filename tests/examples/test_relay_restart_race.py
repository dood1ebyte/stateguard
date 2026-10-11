"""
T14: a ``tools/call`` that arrives while the relay restarts its server.

The invariant: no call is forwarded until the restarted server's link is up
and its own tool listing has been synced -- the call waits, or is answered
"not sent", and is never decided against the dead server's schemas or
forwarded unvalidated because a restart was under way.

"During the restart" is made a fact, not a timing guess: the server factory
blocks until the test releases it, and each test checks the call is held at
the relay's gate (a link waiter, nothing forwarded, nothing decided) before
releasing.
"""

from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    META,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp import ExecutionState, Observed  # noqa: E402

pytestmark = pytest.mark.integration

ROOT = Path(__file__).resolve().parents[2]


def _search(limit_type: str = "integer") -> dict[str, Any]:
    return {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": limit_type},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }


def _search_users() -> dict[str, Any]:
    corpus = json.loads((ROOT / "tests/adapters/mcp/corpus/drift_corpus.json").read_text())
    return dict(corpus["tools"]["search_users"])


TOOLS = [_search(), _search_users()]


def _until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Wait for an observable state (not a sleep standing in for one)."""
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("state not reached")
        time.sleep(0.002)


class GatedFactory:
    """A restart factory that blocks until released; then fails or builds servers."""

    def __init__(self, make: Callable[[int], ScriptedServer], *, fail: bool = False) -> None:
        self.make = make
        self.fail = fail
        self.entered = threading.Event()
        self.release = threading.Event()
        self.count = itertools.count()

    def __call__(self) -> ScriptedServer:
        n = next(self.count)
        if n == 0:
            self.entered.set()
            assert self.release.wait(10), "test never released the restart"
        if self.fail:
            raise OSError("server cannot be started")
        return self.make(n)


def _rig(
    factory: GatedFactory,
    *,
    first: ScriptedServer | None = None,
    max_restarts: int = 3,
    policy: str = "idle-only",
) -> Rig:
    rig = Rig(
        first or ScriptedServer([ServerTool(t) for t in TOOLS]),
        RelayMode.AUTO,
        listing_wait=2.0,
        server_factory=factory,
        max_restarts=max_restarts,
        restart_policy=policy,
    )
    rig.request("server/discover")
    assert rig.wait_synced()
    return rig


def _send_call(rig: Rig, identifier: Any, name: str, arguments: Any) -> None:
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments, "_meta": dict(META)},
        }
    )


def _answer(rig: Rig, identifier: Any, timeout: float = 5.0) -> dict[str, Any]:
    _until(
        lambda: any(isinstance(m, dict) and m.get("id") == identifier for m in rig.client_received),
        timeout,
    )
    return next(m for m in rig.client_received if isinstance(m, dict) and m.get("id") == identifier)


def _calls_received(server: ScriptedServer) -> list[dict[str, Any]]:
    return [
        m for m in (json.loads(line) for line in server.received) if m.get("method") == "tools/call"
    ]


def _held_at_gate(rig: Rig, ids: list[Any]) -> None:
    """Every id is with the relay, waiting at the gate; none decided or forwarded."""
    _until(lambda: all(rig.relay.ledger.get(i) is not None for i in ids))
    _until(lambda: rig.relay._link_waiters >= 1)
    for identifier in ids:
        assert rig.relay.ledger.get(identifier).state is ExecutionState.PENDING
    assert not [c for server in rig.servers for c in _calls_received(server) if c["id"] in ids]
    assert not [e for e in rig.audit if e.get("event") == "call" and e.get("id") in ids]


def _restart_in_progress(rig: Rig, factory: GatedFactory) -> None:
    rig.kill_server()
    assert factory.entered.wait(5)
    assert rig.relay._link == "restarting"


def _call_events(rig: Rig, identifier: Any) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == "call" and e.get("id") == identifier]


# ---------------------------------------------------------------------------
# A, C: the call waits, then is decided on the new server's listing
# ---------------------------------------------------------------------------


def test_a_a_call_during_restart_waits_then_is_decided_and_sent_once() -> None:
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "a1", "search", {"query": "x"})
        _held_at_gate(rig, ["a1"])
        factory.release.set()
        answer = _answer(rig, "a1")
        assert answer["result"]["isError"] is False
        sent = [c for server in rig.servers for c in _calls_received(server)]
        assert [c["id"] for c in sent] == ["a1"]
        assert _calls_received(rig.servers[1]) == sent  # the new server, exactly once
        names = [e.get("event") for e in rig.audit]
        restarted = names.index("server_restarted")
        synced_after = names.index("tools_synced", restarted)
        decided = rig.audit.index(_call_events(rig, "a1")[0])
        assert restarted < synced_after < decided  # decided on the new listing
        assert any(e.get("event") == "call_gated" and e.get("id") == "a1" for e in rig.audit)
    finally:
        rig.close()


def test_c_repair_still_happens_after_the_restart() -> None:
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "c1", "search", {"query": "x", "limit": "5"})
        _held_at_gate(rig, ["c1"])
        factory.release.set()
        assert _answer(rig, "c1")["result"]["isError"] is False
        assert [c["params"]["arguments"] for c in _calls_received(rig.servers[1])] == [
            {"query": "x", "limit": 5}
        ]
        (event,) = _call_events(rig, "c1")
        assert event["action"] == "forward" and event["listing"] != "unconfirmed"
    finally:
        rig.close()


def test_c_the_decision_uses_the_new_server_s_schema_not_the_dead_one_s() -> None:
    """Old server: limit is a string (the call is valid as sent). New server:
    limit is an integer. Decided on the new listing, the call is repaired."""
    old = ScriptedServer([ServerTool(_search("string"))])
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(_search("integer"))]))
    rig = _rig(factory, first=old)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "c2", "search", {"query": "x", "limit": "5"})
        _held_at_gate(rig, ["c2"])
        factory.release.set()
        assert _answer(rig, "c2")["result"]["isError"] is False
        assert _calls_received(rig.servers[1])[0]["params"]["arguments"]["limit"] == 5
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# B, D: the call is never forwarded unrepaired
# ---------------------------------------------------------------------------


def test_b_restart_fails_the_call_is_not_sent_and_never_forwarded() -> None:
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]), fail=True)
    rig = _rig(factory, max_restarts=1)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "b1", "search", {"query": "x", "limit": "5"})
        _held_at_gate(rig, ["b1"])
        factory.release.set()
        answer = _answer(rig, "b1")
        assert answer["error"]["data"]["io.stateguard/executionState"] == "not_sent"
        assert not any(_calls_received(server) for server in rig.servers)
        assert rig.relay.ledger.get("b1").state is ExecutionState.ANSWERED_LOCALLY
        assert _call_events(rig, "b1") == []  # never decided against the dead server
    finally:
        rig.close()


def test_d_an_ambiguous_call_during_restart_stays_escalated() -> None:
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "d1", "search_users", {"user": "ada"})
        _held_at_gate(rig, ["d1"])
        factory.release.set()
        answer = _answer(rig, "d1")
        assert answer["result"]["isError"] is True  # answered by the relay
        assert rig.relay.ledger.get("d1").state is ExecutionState.ANSWERED_LOCALLY
        assert not any(_calls_received(server) for server in rig.servers)
        (event,) = _call_events(rig, "d1")
        assert event["action"] in ("escalate", "refuse")
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# E, F, G
# ---------------------------------------------------------------------------


def test_e_an_unknown_invocation_is_never_replayed_across_the_restart() -> None:
    def dies_after_first(call: ServerCall) -> ServerAction:
        return ServerAction.CLOSE_AFTER_EXECUTE if call.index == 0 else ServerAction.NORMAL

    first = ScriptedServer([ServerTool(t) for t in TOOLS], faults=dies_after_first)
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory, first=first, policy="after-unknown")
    try:
        _send_call(rig, "e1", "search", {"query": "first"})
        unknown = _answer(rig, "e1")
        assert unknown["error"]["data"]["io.stateguard/executionState"] == "unknown"
        assert factory.entered.wait(5)
        _send_call(rig, "e2", "search", {"query": "second", "limit": "3"})
        _held_at_gate(rig, ["e2"])
        factory.release.set()
        assert _answer(rig, "e2")["result"]["isError"] is False
        assert [c["id"] for c in _calls_received(rig.servers[1])] == ["e2"]
        assert [c.message["id"] for c in rig.all_executed] == ["e1", "e2"]
        assert rig.relay.ledger.get("e1").state is ExecutionState.UNKNOWN
        assert len([m for m in rig.client_received if m.get("id") == "e1"]) == 1
    finally:
        rig.close()


def test_f_several_calls_during_restart_each_go_once_in_order() -> None:
    factory = GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory)
    try:
        _restart_in_progress(rig, factory)
        ids = ["f1", "f2", "f3"]
        for n, identifier in enumerate(ids):
            _send_call(rig, identifier, "search", {"query": identifier, "limit": str(n)})
        _held_at_gate(rig, ids[:1])  # the client pump holds them in arrival order
        factory.release.set()
        for identifier in ids:
            assert _answer(rig, identifier)["result"]["isError"] is False
        sent = _calls_received(rig.servers[1])
        assert [c["id"] for c in sent] == ids
        assert [c["params"]["arguments"]["limit"] for c in sent] == [0, 1, 2]
        assert len(rig.all_executed) == 3
        for identifier in ids:
            assert len([m for m in rig.client_received if m.get("id") == identifier]) == 1
    finally:
        rig.close()


class _DiesOnListing(ScriptedServer):
    """A restarted server that dies before it lists its tools."""

    def _handle(self, line: bytes, writer: Any) -> bool:
        if json.loads(line).get("method") == "tools/list":
            self.dead = True
            return False
        return super()._handle(line, writer)


def test_g_the_server_dies_again_before_the_gated_call_is_sent() -> None:
    def make(n: int) -> ScriptedServer:
        if n == 0:
            return _DiesOnListing([ServerTool(t) for t in TOOLS])
        return ScriptedServer([ServerTool(t) for t in TOOLS])

    factory = GatedFactory(make)
    rig = _rig(factory)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "g1", "search", {"query": "x", "limit": "5"})
        _held_at_gate(rig, ["g1"])
        factory.release.set()
        assert _answer(rig, "g1")["result"]["isError"] is False
        assert _calls_received(rig.servers[1]) == []  # never to the server that died
        assert [c["params"]["arguments"] for c in _calls_received(rig.servers[2])] == [
            {"query": "x", "limit": 5}
        ]
        assert not [e for e in rig.audit if e.get("action") == "unvalidated"]
    finally:
        rig.close()


def test_g_the_server_dies_again_and_no_restart_is_left() -> None:
    factory = GatedFactory(lambda n: _DiesOnListing([ServerTool(t) for t in TOOLS]))
    rig = _rig(factory, max_restarts=1)
    try:
        _restart_in_progress(rig, factory)
        _send_call(rig, "g2", "search", {"query": "x", "limit": "5"})
        _held_at_gate(rig, ["g2"])
        factory.release.set()
        answer = _answer(rig, "g2")
        assert answer["error"]["data"]["io.stateguard/executionState"] == "not_sent"
        assert not any(_calls_received(server) for server in rig.servers)
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# The generation binding itself
# ---------------------------------------------------------------------------


def test_a_decision_is_never_written_to_another_generation() -> None:
    rig = _rig(GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS])))
    try:
        relay = rig.relay
        relay.ledger.received("x1", "tools/call", "search")
        payload = json.dumps({"jsonrpc": "2.0", "id": "x1", "method": "tools/call"}).encode()
        assert relay._send_decided("x1", payload, relay.generation + 1) is False
        assert relay.ledger.get("x1").state is ExecutionState.PENDING  # not SENT
        assert not _calls_received(rig.server)
    finally:
        rig.close()


def test_a_retry_is_never_sent_to_another_generation() -> None:
    rig = _rig(GatedFactory(lambda n: ScriptedServer([ServerTool(t) for t in TOOLS])))
    try:
        relay = rig.relay
        relay._retry_mode = "auto"
        relay._calls["r1"] = (b"{}", "search", relay.generation - 1)
        assert relay._consider_retry("r1", Observed.TOOL_ERROR) is False
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# The race T13 found, repeated
# ---------------------------------------------------------------------------


def _dies_once_factory() -> Callable[[], ScriptedServer]:
    """Servers sharing one fault plan: the first call of the session dies after executing."""
    order = itertools.count()
    lock = threading.Lock()

    def plan(call: ServerCall) -> ServerAction:
        with lock:
            n = next(order)
        return ServerAction.CLOSE_AFTER_EXECUTE if n == 0 else ServerAction.NORMAL

    def make() -> ScriptedServer:
        return ScriptedServer([ServerTool(t) for t in TOOLS], faults=plan)

    return make


def test_the_t13_race_never_lets_an_unrepaired_call_through() -> None:
    """The exact T13 ordering, unsynchronised, repeated: the client sends its
    next (repairable) call the moment it is told the previous one's outcome is
    unknown, while the relay is restarting the server (after-unknown)."""
    bypasses = 0
    for _ in range(15):
        make = _dies_once_factory()

        rig = Rig(
            make(),
            RelayMode.AUTO,
            listing_wait=2.0,
            server_factory=make,
            max_restarts=3,
            restart_policy="after-unknown",
        )
        try:
            rig.request("server/discover")
            rig.wait_synced()
            rig.call("search", {"query": "first"}, timeout=3)
            answer = rig.call("search", {"query": "x", "limit": "5"}, timeout=5)
            sent = [c["params"]["arguments"] for s in rig.servers for c in _calls_received(s)]
            if {"query": "x", "limit": "5"} in sent:
                bypasses += 1
            assert answer is not None and answer["result"]["isError"] is False
            assert not [e for e in rig.audit if e.get("action") == "unvalidated"]
        finally:
            rig.close()
    assert bypasses == 0

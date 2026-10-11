"""
Safe server restart (T12): restart only when it cannot cause a second execution.

Every test checks the servers' own execution ledgers (``Rig.all_executed``,
``Rig.all_received``) across *every* server process, old and new, because
the failure a restart can cause -- a request sent twice, or run by two
processes -- only shows there.

Invariants:
* a request sent before the server died is UNKNOWN, answered so, and never
  sent again -- not to the restarted server either;
* a request that was never sent is either sent once (after a restart) or
  answered "not sent"; it is never lost;
* restarts are bounded and logged;
* a restarted server's tools are listed again before they are trusted;
* an initialize-era session is re-initialized, and a version change refused.
"""

from __future__ import annotations

import json
import sys
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

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

from stateguard.adapters.mcp import ExecutionState  # noqa: E402

pytestmark = pytest.mark.integration


def _tool(param: str = "q") -> ServerTool:
    return ServerTool(
        {
            "name": "search",
            "inputSchema": {
                "type": "object",
                "properties": {param: {"type": "string"}},
                "required": [param],
                "additionalProperties": False,
            },
        }
    )


class Plan:
    """Server actions by global call order, across every server process."""

    def __init__(self, actions: dict[int, ServerAction] | None = None) -> None:
        self.actions = dict(actions or {})
        self._n = 0
        self._lock = threading.Lock()

    def __call__(self, call: ServerCall) -> ServerAction:
        with self._lock:
            n, self._n = self._n, self._n + 1
        return self.actions.get(n, ServerAction.NORMAL)


def _rig(
    *,
    plan: Plan | None = None,
    tools_after: list[ServerTool] | None = None,
    max_restarts: int = 3,
    policy: str = "idle-only",
    factory: Any = None,
    mode: RelayMode = RelayMode.AUTO,
    require_initialize: bool = False,
) -> Rig:
    plan = plan or Plan()

    def default_factory() -> ScriptedServer:
        return ScriptedServer(
            tools_after or [_tool()], faults=plan, require_initialize=require_initialize
        )

    rig = Rig(
        ScriptedServer([_tool()], faults=plan, require_initialize=require_initialize),
        mode,
        server_factory=factory or default_factory,
        max_restarts=max_restarts,
        restart_policy=policy,
    )
    return rig


def _discover(rig: Rig) -> None:
    rig.request("server/discover")
    assert rig.wait_synced()


def _events(rig: Rig, name: str) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == name]


def _wait_event(rig: Rig, name: str, count: int = 1, timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and len(_events(rig, name)) < count:
        time.sleep(0.01)
    assert len(_events(rig, name)) >= count, f"no {name} event: {rig.audit}"


def _stops(rig: Rig, timeout: float = 4.0) -> None:
    assert rig.relay._stopped.wait(timeout), "relay did not stop"


def _ok(response: dict[str, Any] | None) -> bool:
    return response is not None and response.get("result", {}).get("isError") is False


def _state(response: dict[str, Any] | None) -> str | None:
    data = (response or {}).get("error", {}).get("data", {})
    return data.get("io.stateguard/executionState")


def _call_ids_received(rig: Rig) -> Counter[Any]:
    ids: Counter[Any] = Counter()
    for line in rig.all_received:
        message = json.loads(line)
        if message.get("method") == "tools/call":
            ids[message["id"]] += 1
    return ids


def _no_request_sent_twice(rig: Rig) -> None:
    assert all(count == 1 for count in _call_ids_received(rig).values()), _call_ids_received(rig)


# ---------------------------------------------------------------------------
# The scenarios
# ---------------------------------------------------------------------------


def test_idle_death_restarts_and_the_next_call_succeeds() -> None:
    rig = _rig()
    try:
        _discover(rig)
        rig.kill_server()
        _wait_event(rig, "server_restarted")
        assert _ok(rig.call("search", {"q": "x"}))
        assert len(rig.servers) == 2
        assert rig.servers[0].executed == [] and len(rig.servers[1].executed) == 1
        (lost,) = _events(rig, "server_lost")
        assert lost["in_flight_unknown"] == 0
        _no_request_sent_twice(rig)
    finally:
        rig.close()


def test_a_request_arriving_during_the_restart_waits_and_is_sent_once() -> None:
    """Failure before dispatch: the request was never written, so sending it is not a replay."""
    started = threading.Event()

    def slow_factory() -> ScriptedServer:
        started.set()
        time.sleep(0.3)
        return ScriptedServer([_tool()])

    rig = _rig(factory=slow_factory)
    try:
        _discover(rig)
        rig.kill_server()
        assert started.wait(2)
        response = rig.call("search", {"q": "x"}, timeout=3.0)  # arrives mid-restart
        assert _ok(response)
        assert rig.servers[0].executed == [] and len(rig.servers[1].executed) == 1
        invocation = rig.relay.ledger.get(rig.sent_ids[-1])
        assert invocation.history == [
            ExecutionState.PENDING,
            ExecutionState.SENT,
            ExecutionState.COMPLETED,
        ]
        _no_request_sent_twice(rig)
    finally:
        rig.close()


def test_death_after_dispatch_is_unknown_and_blocks_restart_by_default() -> None:
    rig = _rig(plan=Plan({0: ServerAction.CLOSE_AFTER_EXECUTE}))
    try:
        _discover(rig)
        response = rig.call("search", {"q": "x"}, timeout=2.0)
        assert _state(response) == "unknown"
        _wait_event(rig, "restart_refused")
        _stops(rig)  # gone for good: the relay stops, as without StateGuard
        assert len(rig.servers) == 1  # idle-only: not restarted
        assert len(rig.all_executed) == 1  # it ran once; nothing ran twice
        assert rig.relay.ledger.get(rig.sent_ids[-1]).state is ExecutionState.UNKNOWN
        _no_request_sent_twice(rig)
    finally:
        rig.close()


@pytest.mark.parametrize(
    "action", [ServerAction.CLOSE_AFTER_EXECUTE, ServerAction.CLOSE_BEFORE_EXECUTE]
)
def test_after_unknown_policy_restarts_but_never_replays(action: ServerAction) -> None:
    rig = _rig(plan=Plan({0: action}), policy="after-unknown")
    try:
        _discover(rig)
        first = rig.call("search", {"q": "x"}, timeout=2.0)
        first_id = rig.sent_ids[-1]
        assert _state(first) == "unknown"
        _wait_event(rig, "server_restarted")
        assert _ok(rig.call("search", {"q": "y"}))
        # The in-flight request went to the old server only, and stays UNKNOWN.
        assert _call_ids_received(rig)[first_id] == 1
        assert first_id not in [json.loads(x).get("id") for x in rig.servers[1].received]
        inv = rig.relay.ledger.get(first_id)
        assert inv.state is ExecutionState.UNKNOWN and ExecutionState.COMPLETED not in inv.history
        expected = 1 if action is ServerAction.CLOSE_AFTER_EXECUTE else 0
        assert len(rig.servers[0].executed) == expected
        _no_request_sent_twice(rig)
    finally:
        rig.close()


def test_several_requests_in_flight_are_all_unknown_and_none_replayed() -> None:
    plan = Plan({0: ServerAction.HANG_BEFORE_EXECUTE, 1: ServerAction.HANG_BEFORE_EXECUTE})
    rig = _rig(plan=plan, policy="after-unknown")
    try:
        _discover(rig)
        ids = [9001, 9002]
        for identifier in ids:
            rig.send(
                {
                    "jsonrpc": "2.0",
                    "id": identifier,
                    "method": "tools/call",
                    "params": {"name": "search", "arguments": {"q": "x"}, "_meta": META},
                }
            )
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(_call_ids_received(rig)) < 2:
            time.sleep(0.01)
        rig.kill_server()
        answers = [rig.wait_for(i, timeout=2.0) for i in ids]
        assert [_state(a) for a in answers] == ["unknown", "unknown"]
        _wait_event(rig, "server_restarted")
        (lost,) = _events(rig, "server_lost")
        assert lost["in_flight_unknown"] == 2
        assert all(_call_ids_received(rig)[i] == 1 for i in ids)
        assert rig.servers[1].received == [] or all(
            json.loads(x).get("id") not in ids for x in rig.servers[1].received
        )
        assert rig.all_executed == []
    finally:
        rig.close()


def test_repeated_restart_failure_is_bounded_and_observable() -> None:
    attempts: list[int] = []

    def broken() -> ScriptedServer:
        attempts.append(1)
        time.sleep(0.15)
        raise OSError("cannot start server")

    rig = _rig(factory=broken, max_restarts=3)
    try:
        _discover(rig)
        rig.kill_server()
        time.sleep(0.05)
        response = rig.call("search", {"q": "x"}, timeout=3.0)  # arrives mid-attempts
        assert _state(response) == "not_sent"  # answered: never sent, never ran
        _wait_event(rig, "restart_exhausted")
        _stops(rig)
        assert len(attempts) == 3 and len(_events(rig, "restart_failed")) == 3
        assert rig.all_executed == []
        assert rig.relay.ledger.get(rig.sent_ids[-1]).state is ExecutionState.ANSWERED_LOCALLY
    finally:
        rig.close()


def test_a_server_that_dies_on_start_uses_up_the_budget_then_stops() -> None:
    def dies_at_once() -> ScriptedServer:
        server = ScriptedServer([_tool()])
        threading.Timer(0.05, server.kill).start()
        return server

    rig = _rig(factory=dies_at_once, max_restarts=2)
    try:
        _discover(rig)
        rig.kill_server()
        _wait_event(rig, "restart_exhausted", timeout=5.0)
        _stops(rig)
        assert len(_events(rig, "server_restarted")) == 2
        assert len(rig.servers) == 3
    finally:
        rig.close()


def test_a_changed_listing_after_restart_replaces_the_old_registry() -> None:
    rig = _rig(tools_after=[_tool("query")])
    try:
        _discover(rig)
        assert "q" in rig.relay.registry.get("search").input_schema["properties"]
        rig.kill_server()
        _wait_event(rig, "server_restarted")
        _wait_event(rig, "tools_synced", count=2)
        assert "query" in rig.relay.registry.get("search").input_schema["properties"]
        response = rig.call("search", {"query": "x"})  # valid only for the new schema
        assert _ok(response)
        assert rig.servers[1].executed[0].arguments == {"query": "x"}
    finally:
        rig.close()


def _initialize(rig: Rig, version: str = "2025-06-18") -> None:
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": "init",
            "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "t"}},
        }
    )
    assert rig.wait_for("init", timeout=2.0) is not None
    rig.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    assert rig.wait_synced()


def _plain_call(rig: Rig, identifier: Any) -> dict[str, Any] | None:
    rig.send(
        {
            "jsonrpc": "2.0",
            "id": identifier,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"q": "x"}},
        }
    )
    return rig.wait_for(identifier, timeout=3.0)


def test_an_initialize_session_is_reinitialized_after_restart() -> None:
    rig = _rig(require_initialize=True)
    try:
        _initialize(rig)
        assert _ok(_plain_call(rig, "c1"))
        rig.kill_server()
        _wait_event(rig, "reinitialized")
        assert _ok(_plain_call(rig, "c2"))
        methods = [json.loads(x).get("method") for x in rig.servers[1].received]
        assert methods[:2] == ["initialize", "notifications/initialized"]
    finally:
        rig.close()


def test_a_restarted_server_on_another_protocol_version_is_refused() -> None:
    class OtherVersion(ScriptedServer):
        def _handle(self, line: bytes, writer: Any) -> bool:
            message = json.loads(line)
            if message.get("method") == "initialize":
                message["params"]["protocolVersion"] = "1999-01-01"
                line = json.dumps(message).encode()
            return super()._handle(line, writer)

    rig = _rig(require_initialize=True, factory=lambda: OtherVersion([_tool()]), max_restarts=2)
    try:
        _initialize(rig)
        rig.kill_server()
        _wait_event(rig, "restart_exhausted", timeout=5.0)
        _stops(rig)
        assert len(_events(rig, "reinit_failed")) == 2  # each incompatible server refused
        assert rig.all_executed == []
    finally:
        rig.close()


def test_a_request_written_to_a_server_that_just_died_is_unknown_not_lost() -> None:
    """
    The relay learns of a death from EOF, so a request can be written in the
    window between the death and the EOF. It may have been read: UNKNOWN,
    answered so -- and under idle-only that counts as in flight.
    """
    rig = _rig()
    try:
        _discover(rig)
        rig.server.dead = True  # dead, but stdout not yet closed: EOF comes later
        rig.send(
            {
                "jsonrpc": "2.0",
                "id": 77,
                "method": "tools/call",
                "params": {"name": "search", "arguments": {"q": "x"}, "_meta": META},
            }
        )
        time.sleep(0.1)
        rig.kill_server()  # now the EOF
        answer = rig.wait_for(77, timeout=2.0)
        assert _state(answer) == "unknown"
        inv = rig.relay.ledger.get(77)
        assert inv.state is ExecutionState.UNKNOWN and ExecutionState.SENT in inv.history
        _wait_event(rig, "restart_refused")
        assert rig.all_executed == []
    finally:
        rig.close()


def test_shadow_never_restarts() -> None:
    rig = _rig(mode=RelayMode.SHADOW)
    try:
        _discover(rig)
        rig.kill_server()
        _wait_event(rig, "restart_would_attempt")
        assert len(rig.servers) == 1
    finally:
        rig.close()


def test_restart_off_by_default() -> None:
    rig = Rig(ScriptedServer([_tool()]), RelayMode.AUTO)
    try:
        _discover(rig)
        rig.kill_server()
        _wait_event(rig, "server_lost")
        assert _events(rig, "server_restarted") == []
    finally:
        rig.close()


# ---------------------------------------------------------------------------
# Property: random kill / call / restart-failure sequences
# ---------------------------------------------------------------------------

STEPS = st.lists(
    st.sampled_from(["call", "call_dies_after", "call_dies_before", "kill_idle"]),
    min_size=1,
    max_size=5,
)


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
@given(
    steps=STEPS,
    policy=st.sampled_from(["idle-only", "after-unknown"]),
    max_restarts=st.integers(0, 2),
    factory_fails=st.booleans(),
)
def test_property_no_replay_no_duplicate_no_lost_request(
    steps: list[str], policy: str, max_restarts: int, factory_fails: bool
) -> None:
    plan = Plan()
    calls_made = 0

    def factory() -> ScriptedServer:
        if factory_fails:
            raise OSError("cannot start")
        return ScriptedServer([_tool()], faults=plan)

    rig = Rig(
        ScriptedServer([_tool()], faults=plan),
        RelayMode.AUTO,
        server_factory=factory,
        max_restarts=max_restarts,
        restart_policy=policy,
    )
    answers: dict[Any, dict[str, Any] | None] = {}
    try:
        rig.request("server/discover", timeout=2.0)
        rig.wait_synced(timeout=2.0)
        for step in steps:
            if rig.relay._stopped.is_set():
                break  # gone for good: the client would see its server exit
            if step == "kill_idle":
                rig.kill_server()
                time.sleep(0.05)
                continue
            if step != "call":
                plan.actions[calls_made] = (
                    ServerAction.CLOSE_AFTER_EXECUTE
                    if step == "call_dies_after"
                    else ServerAction.CLOSE_BEFORE_EXECUTE
                )
            response = rig.call("search", {"q": "x"}, timeout=3.0)
            if response is None and rig.relay._stopped.wait(2.0):
                break  # sent to a relay that was stopping: the client sees its server exit
            answers[rig.sent_ids[-1]] = response
            calls_made = sum(
                1 for line in rig.all_received if json.loads(line).get("method") == "tools/call"
            )

        received = _call_ids_received(rig)
        # no request is ever sent twice, to any server
        assert all(n == 1 for n in received.values()), received
        # nothing executes twice
        executed_ids = Counter(c.message["id"] for c in rig.all_executed)
        assert all(n == 1 for n in executed_ids.values())
        # no request is lost: every call got exactly one answer
        for identifier, answer in answers.items():
            assert answer is not None, (identifier, rig.audit)
            state = _state(answer)
            if state == "not_sent":
                assert identifier not in received  # truly never sent
            if state == "unknown":
                # Only what the relay wrote can be unknown. (A server that died
                # before reading it is still unknown: the relay cannot tell.)
                inv = rig.relay.ledger.get(identifier)
                assert inv.state is ExecutionState.UNKNOWN
                assert ExecutionState.SENT in inv.history
        # restarts are bounded
        assert len(_events(rig, "server_restarted")) <= max_restarts
        # never restarted with a request in flight under idle-only
        if policy == "idle-only":
            for lost in _events(rig, "server_lost"):
                if lost["in_flight_unknown"]:
                    assert lost["restarts_used"] == len(_events(rig, "server_restarted"))
    finally:
        rig.close()

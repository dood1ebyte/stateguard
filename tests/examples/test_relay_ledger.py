"""
The relay's execution-state ledger, end to end.

* auto drops a second response for an answered request; shadow delivers it
  and logs what auto would do;
* when the server dies with calls in flight, auto answers each with an
  explicit "outcome unknown" error -- the call may have executed -- and
  shadow changes nothing;
* with a call deadline, an unanswered call gets the same answer, and the
  server's late answer is dropped, never delivered as a second response;
* a call refused by StateGuard is recorded as answered locally, never sent.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    DELAY_SECONDS,
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp import ExecutionState  # noqa: E402

pytestmark = pytest.mark.integration

SEARCH = ServerTool(
    {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "maximum": 10},
            },
            "required": ["query"],
        },
    }
)


def _rig(action: ServerAction, mode: RelayMode = RelayMode.AUTO, **kwargs: Any) -> Rig:
    def plan(call: ServerCall) -> ServerAction:
        return action if call.index == 0 else ServerAction.NORMAL

    rig = Rig(ScriptedServer([SEARCH], faults=plan), mode, **kwargs)
    rig.request("server/discover")
    rig.wait_synced()
    return rig


def _answers(rig: Rig, identifier: Any) -> list[dict[str, Any]]:
    time.sleep(0.1)
    return [
        m
        for m in rig.client_received
        if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
    ]


def _unknown(message: dict[str, Any]) -> bool:
    data = message.get("error", {}).get("data", {})
    return data.get("io.stateguard/executionState") == "unknown"


def test_auto_drops_a_duplicate_response() -> None:
    rig = _rig(ServerAction.DUPLICATE_RESPONSE)
    try:
        rig.call("search", {"query": "x"})
        (answer,) = _answers(rig, rig.sent_ids[-1])
        assert answer["result"]["isError"] is False
        assert any(e.get("event") == "response_dropped" for e in rig.audit)
    finally:
        rig.close()


def test_shadow_delivers_the_duplicate_and_logs_it() -> None:
    rig = _rig(ServerAction.DUPLICATE_RESPONSE, RelayMode.SHADOW)
    try:
        rig.call("search", {"query": "x"})
        assert len(_answers(rig, rig.sent_ids[-1])) == 2
        assert any(e.get("event") == "response_would_drop" for e in rig.audit)
    finally:
        rig.close()


@pytest.mark.parametrize(
    "action", [ServerAction.CLOSE_BEFORE_EXECUTE, ServerAction.CLOSE_AFTER_EXECUTE]
)
def test_server_death_is_answered_as_unknown_never_as_failure(action: ServerAction) -> None:
    rig = _rig(action)
    try:
        response = rig.call("search", {"query": "x"}, timeout=2.0)
        assert response is not None and _unknown(response)
        assert "may or may not have been executed" in response["error"]["message"]
        invocation = rig.relay.ledger.get(rig.sent_ids[-1])
        assert invocation.state is ExecutionState.UNKNOWN
        assert invocation.unknown_reason == "transport_closed"
    finally:
        rig.close()


def test_shadow_does_not_answer_on_the_server_s_behalf() -> None:
    rig = _rig(ServerAction.CLOSE_AFTER_EXECUTE, RelayMode.SHADOW)
    try:
        assert rig.call("search", {"query": "x"}, timeout=1.0) is None
        assert any(
            e.get("event") == "outcome_unknown" and e["surfaced"] is False for e in rig.audit
        )
    finally:
        rig.close()


def test_a_deadline_answers_unknown_and_the_late_answer_is_dropped() -> None:
    rig = _rig(ServerAction.DELAYED_RESPONSE, call_deadline=0.15)
    try:
        response = rig.call("search", {"query": "x"}, timeout=2.0)
        assert response is not None and _unknown(response)
        time.sleep(DELAY_SECONDS)
        (only,) = _answers(rig, rig.sent_ids[-1])  # the late success is not delivered
        assert _unknown(only)
        invocation = rig.relay.ledger.get(rig.sent_ids[-1])
        assert invocation.late_response and invocation.late_is_error is False
        assert len(rig.server.executed) == 1  # it did run; the ledger knows, the client was told
    finally:
        rig.close()


def test_without_a_deadline_a_slow_call_simply_completes() -> None:
    rig = _rig(ServerAction.DELAYED_RESPONSE)
    try:
        response = rig.call("search", {"query": "x"}, timeout=2.0)
        assert response is not None and response["result"]["isError"] is False
    finally:
        rig.close()


def test_a_refused_call_is_answered_locally_and_never_sent() -> None:
    rig = _rig(ServerAction.NORMAL)
    try:
        response = rig.call("search", {"query": "x", "limit": 99})
        assert response is not None and response["result"]["isError"] is True
        invocation = rig.relay.ledger.get(rig.sent_ids[-1])
        assert invocation.state is ExecutionState.ANSWERED_LOCALLY
        assert rig.server.executed == []
    finally:
        rig.close()

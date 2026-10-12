"""
The relay's retry: only what the operator declared safe to repeat.

Each test checks the server's execution ledger, not just what the client
saw, because the failure retry can cause -- running a call twice -- happens
on the server.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)

from stateguard.adapters.mcp import EffectPolicy  # noqa: E402

pytestmark = pytest.mark.integration


def _tool(name: str, annotations: dict[str, Any] | None = None) -> ServerTool:
    definition: dict[str, Any] = {
        "name": name,
        "inputSchema": {
            "type": "object",
            "properties": {"q": {"type": "string"}},
            "required": ["q"],
        },
    }
    if annotations:
        definition["annotations"] = annotations
    return ServerTool(definition)


TOOLS = [
    _tool("search"),
    _tool("pay"),
    _tool("hinted", {"readOnlyHint": True, "idempotentHint": True}),
]
EFFECTS = EffectPolicy({"search": "read_only", "pay": "non_idempotent"})


def _rig(
    plan: dict[int, ServerAction],
    *,
    retry: str = "auto",
    mode: RelayMode = RelayMode.AUTO,
    deadline: float | None = None,
) -> Rig:
    def faults(call: ServerCall) -> ServerAction:
        return plan.get(call.index, ServerAction.NORMAL)

    rig = Rig(
        ScriptedServer(TOOLS, faults=faults),
        mode,
        effects=EFFECTS,
        retry=retry,
        call_deadline=deadline,
    )
    rig.request("server/discover")
    rig.wait_synced()
    return rig


def _decisions(rig: Rig) -> list[dict[str, Any]]:
    return [e for e in rig.audit if e.get("event") == "retry_decision"]


def _ok(response: dict[str, Any] | None) -> bool:
    return response is not None and response.get("result", {}).get("isError") is False


def test_a_declared_read_only_tool_is_retried_after_a_transient_error() -> None:
    rig = _rig({0: ServerAction.TRANSIENT_ERROR})
    try:
        response = rig.call("search", {"q": "x"})
        assert _ok(response) and response["id"] == rig.sent_ids[-1]  # the client's own id
        assert len(rig.server.executed) == 1
        (decision,) = _decisions(rig)
        assert decision["decision"] == "apply" and decision["acted"] is True
    finally:
        rig.close()


def test_a_hang_on_a_read_only_tool_is_retried_after_the_deadline() -> None:
    rig = _rig({0: ServerAction.HANG_BEFORE_EXECUTE}, deadline=0.2)
    try:
        assert _ok(rig.call("search", {"q": "x"}, timeout=2.0))
        assert len(rig.server.executed) == 1
    finally:
        rig.close()


def test_a_non_idempotent_tool_is_never_retried_after_it_may_have_run() -> None:
    rig = _rig({0: ServerAction.DROP_RESPONSE}, deadline=0.2)
    try:
        response = rig.call("pay", {"q": "x"}, timeout=2.0)
        assert response is not None and "error" in response  # outcome unknown, surfaced
        time.sleep(0.3)
        assert len(rig.server.executed) == 1  # ran once, never again
        (decision,) = _decisions(rig)
        assert decision["decision"] == "ambiguous" and decision["acted"] is False
    finally:
        rig.close()


def test_annotations_alone_never_justify_a_retry() -> None:
    rig = _rig({0: ServerAction.DROP_RESPONSE}, deadline=0.2)
    try:
        response = rig.call("hinted", {"q": "x"}, timeout=2.0)
        assert response is not None and "error" in response
        time.sleep(0.3)
        assert len(rig.server.executed) == 1
        (decision,) = _decisions(rig)
        assert decision["acted"] is False
        assert decision["effect"]["declared"] is False
        assert decision["effect"]["hints"] == {"readOnlyHint": True, "idempotentHint": True}
    finally:
        rig.close()


def test_an_undeclared_tool_error_is_delivered_as_is() -> None:
    rig = _rig({0: ServerAction.TRANSIENT_ERROR})
    try:
        response = rig.call("hinted", {"q": "x"})
        assert response is not None and response["result"]["isError"] is True
        assert len(rig.server.received) and _decisions(rig)[0]["decision"] == "reject"
    finally:
        rig.close()


def test_the_attempt_budget_holds() -> None:
    rig = _rig({0: ServerAction.TRANSIENT_ERROR, 1: ServerAction.TRANSIENT_ERROR})
    try:
        response = rig.call("search", {"q": "x"})
        assert response is not None and response["result"]["isError"] is True
        calls = [line for line in rig.server.received if b'"tools/call"' in line]
        assert len(calls) == 2  # the original and one retry
    finally:
        rig.close()


def test_an_auth_error_is_never_retried() -> None:
    rig = _rig({0: ServerAction.AUTH_EXPIRED})
    try:
        response = rig.call("search", {"q": "x"})
        assert response is not None and "error" in response
        calls = [line for line in rig.server.received if b'"tools/call"' in line]
        assert len(calls) == 1
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("retry", "mode"), [("shadow", RelayMode.AUTO), ("auto", RelayMode.SHADOW)]
)
def test_shadow_logs_the_decision_and_retries_nothing(retry: str, mode: RelayMode) -> None:
    rig = _rig({0: ServerAction.TRANSIENT_ERROR}, retry=retry, mode=mode)
    try:
        response = rig.call("search", {"q": "x"})
        assert response is not None and response["result"]["isError"] is True
        (decision,) = _decisions(rig)
        assert decision["decision"] == "apply" and decision["acted"] is False
        calls = [line for line in rig.server.received if b'"tools/call"' in line]
        assert len(calls) == 1
    finally:
        rig.close()


def test_retry_off_records_nothing() -> None:
    rig = _rig({0: ServerAction.TRANSIENT_ERROR}, retry="off")
    try:
        rig.call("search", {"q": "x"})
        assert _decisions(rig) == []
    finally:
        rig.close()

"""
"Did StateGuard change this call?" is a JSON question, not a Python one.

Found while investigating capture 1a (F3): every payload-change decision used
Python ``==``, which says ``2 == 2.0`` and ``True == 1``. A repair coercing
``2.0`` to ``2`` therefore compared "unchanged" and the *original* value --
the one the server rejects -- was forwarded; shadow mode reported nothing to
review. It was masked whenever an injected default made the payloads differ.

``same_json`` decides by JSON type; these tests pin it and every place that
asks: ``forward_message`` (the wire), ``outcome_for`` (shadow HOLD vs FORWARD,
the preview, the reason), and the shadow forwarding invariant.
"""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.mcp import MCPAction, repair_tool_call
from stateguard.adapters.mcp.jsonrpc import ToolCallRequest, forward_message
from stateguard.adapters.mcp.payload import same_json
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import RepairMode

# ---------------------------------------------------------------------------
# same_json
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b",
    [
        (2, 2.0),
        (2.0, 2),
        (True, 1),
        (1, True),
        (False, 0),
        (0, None),
        ("2", 2),
        ({"x": 2}, {"x": 2.0}),
        ({"x": [1, 2]}, {"x": [1, 2.0]}),
        ({"x": {"y": True}}, {"x": {"y": 1}}),
        ([1, 2], [2, 1]),  # array order matters
        ({"x": 1}, {"x": 1, "y": None}),  # a key with null is not an absent key
        ({}, []),
    ],
)
def test_different_json_is_never_the_same(a: Any, b: Any) -> None:
    assert not same_json(a, b)
    assert not same_json(b, a)


@pytest.mark.parametrize(
    "a,b",
    [
        (2, 2),
        (2.5, 2.5),
        (True, True),
        (None, None),
        ("x", "x"),
        ({"a": 1, "b": {"c": [1, 2.0, None]}}, {"b": {"c": [1, 2.0, None]}, "a": 1}),  # key order
        ([{"k": "v"}], ({"k": "v"},)),  # a tuple and a list are the same JSON array
        ({}, {}),
    ],
)
def test_the_same_json_is_the_same(a: Any, b: Any) -> None:
    assert same_json(a, b)


def test_it_is_not_bounded_by_the_recursion_limit() -> None:
    deep_a: Any = 1
    deep_b: Any = 1
    for _ in range(50_000):
        deep_a, deep_b = {"n": deep_a}, {"n": deep_b}
    assert same_json(deep_a, deep_b)


# ---------------------------------------------------------------------------
# The repair path: 2.0 -> 2 is a change, everywhere it is asked
# ---------------------------------------------------------------------------

TOOL = {
    "name": "list_issues",
    "inputSchema": {
        "type": "object",
        "properties": {
            "page": {"type": "integer", "minimum": 1},
            "per_page": {"type": "integer", "default": 30},
        },
        "required": ["page"],
    },
}
#: Every optional supplied: nothing but the coercion can make the payloads differ.
SENT = {"page": 2.0, "per_page": 30}


def _message(arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {"name": "list_issues", "arguments": arguments},
    }


def test_auto_forwards_the_coerced_integer_not_the_original_float() -> None:
    outcome = repair_tool_call(ContractGuard.with_mcp(), TOOL, SENT)
    assert outcome.result.status is RepairStatus.SUCCESS
    assert outcome.action is MCPAction.FORWARD
    wire = forward_message(ToolCallRequest.parse(_message(SENT)), outcome)
    assert wire is not None
    page = wire["params"]["arguments"]["page"]
    assert page == 2 and type(page) is int  # the bug sent 2.0
    assert '"page": 2,' in json.dumps(wire["params"]["arguments"])
    assert "Coerced" in outcome.reason or "repaired" in outcome.reason.lower()


def test_shadow_holds_the_call_and_previews_the_integer() -> None:
    guard = ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW))
    outcome = repair_tool_call(guard, TOOL, SENT)
    assert outcome.action is MCPAction.HOLD  # the bug said FORWARD: "nothing to review"
    assert outcome.arguments == SENT and type(outcome.arguments["page"]) is float  # as received
    assert outcome.preview is not None and type(outcome.preview["page"]) is int
    wire = forward_message(ToolCallRequest.parse(_message(SENT)), outcome)
    assert wire == _message(SENT)  # shadow sends what it received, the float


def test_an_unchanged_call_still_goes_out_byte_for_byte() -> None:
    sent = {"per_page": 30, "page": 2}  # valid; key order differs from the schema's
    outcome = repair_tool_call(ContractGuard.with_mcp(), TOOL, sent)
    assert outcome.result.status is RepairStatus.ALREADY_VALID
    request = ToolCallRequest.parse(_message(sent))
    assert forward_message(request, outcome) == request.message


def test_true_to_one_is_a_change_on_the_wire() -> None:
    """No current strategy turns True into 1 (the engine refuses both ways);
    the boundary must still see it as a change if one ever does."""
    sent = {"page": True, "per_page": 30}
    base = repair_tool_call(ContractGuard.with_mcp(), TOOL, {"page": 1, "per_page": 30})
    outcome = dataclasses.replace(base, arguments={"page": 1, "per_page": 30})
    wire = forward_message(ToolCallRequest.parse(_message(sent)), outcome)
    assert wire is not None and type(wire["params"]["arguments"]["page"]) is int

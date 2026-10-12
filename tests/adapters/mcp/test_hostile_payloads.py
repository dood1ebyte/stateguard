"""
Hostile payloads: nesting that json.loads accepts and recursion does not.

Found in adversarial review: arguments nested ~900 levels deep -- well within
what json.loads produces -- made ToolCallRequest.parse and
ToolRegistry.process_call raise RecursionError from a deep copy, which
would have killed a relay's pump thread. Every layer now bounds nesting
iteratively before anything recursive runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.adapters.mcp import MalformedMessageError, MCPAction, ToolCallRequest, ToolRegistry
from stateguard.core.errors.results import RepairStatus
from stateguard.core.paths import MAX_PAYLOAD_DEPTH, nests_deeper_than

TOOL = {"name": "t", "inputSchema": {"type": "object", "properties": {"a": {"type": "object"}}}}


def _deep(depth: int) -> Any:
    value: Any = 0
    for _ in range(depth):
        value = {"x": value}
    return value


@pytest.mark.parametrize("depth", [MAX_PAYLOAD_DEPTH + 1, 900, 5000, 50_000])
def test_a_deep_call_is_refused_not_raised(depth: int) -> None:
    registry = ToolRegistry()
    registry.register(TOOL)
    outcome = registry.process_call("t", {"a": _deep(depth)})
    assert outcome.action is MCPAction.REFUSE
    assert outcome.result.status is RepairStatus.FAILED
    assert "nests deeper than" in outcome.result.remaining_violations[0].message
    assert "<root>" in outcome.reason
    outcome.explain()
    outcome.to_dict()


def test_a_reasonable_depth_still_works() -> None:
    registry = ToolRegistry()
    registry.register(TOOL)
    assert (
        registry.process_call("t", {"a": _deep(MAX_PAYLOAD_DEPTH - 5)}).action is MCPAction.FORWARD
    )


def test_validate_is_bounded_too() -> None:
    assert not ContractGuard.with_mcp().validate(TOOL, {"a": _deep(5000)}).is_valid


def test_shadow_still_forwards_it() -> None:
    from stateguard import GuardConfig
    from stateguard.core.models.config import RepairMode

    registry = ToolRegistry(ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW)))
    registry.register(TOOL)
    payload = {"a": _deep(900)}
    outcome = registry.process_call("t", payload)
    assert outcome.action is MCPAction.HOLD and outcome.arguments is not None
    assert outcome.auto_action is MCPAction.REFUSE


def test_parse_refuses_rather_than_overflowing() -> None:
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "t", "arguments": _deep(900)},
    }
    with pytest.raises(MalformedMessageError, match="nest deeper"):
        ToolCallRequest.parse(message)


def test_the_depth_check_is_iterative() -> None:
    assert nests_deeper_than(_deep(200_000), 100)
    assert not nests_deeper_than(_deep(10), 100)
    assert nests_deeper_than([[[[]]]], 3) and not nests_deeper_than([[[]]], 3)

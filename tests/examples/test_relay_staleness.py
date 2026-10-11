"""
The relay never acts on a stale listing.

Under protocol 2026-07-28 ``tools/list_changed`` rides only on
``subscriptions/listen`` streams (MCP SDK 2.3.0,
``mcp/server/mcpserver/context.py``), so a relay that does not hold one is not
told when a schema changes. Each scenario here swaps the server's listing
*without* a notification, then sends a call that is correct for the new
schema. With the refresh, every call is forwarded unchanged and executed
once. Without it (``refresh_before_intervening=False``, the old behaviour),
the relay refuses or rewrites calls that are valid.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerTool,
)

pytestmark = pytest.mark.integration


def _tool(properties: dict[str, Any], required: list[str], closed: bool = True) -> ServerTool:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "required": required}
    if closed:
        schema["additionalProperties"] = False
    return ServerTool({"name": "search", "inputSchema": schema})


S = {"type": "string"}
#: (name, v1 tool, v2 tool, a call valid under v2)
SCENARIOS = [
    ("param renamed", _tool({"query": S}, ["query"]), _tool({"q": S}, ["q"]), {"q": "x"}),
    (
        "required param added",
        _tool({"query": S}, ["query"]),
        _tool({"query": S, "repo": S}, ["query", "repo"]),
        {"query": "x", "repo": "a/b"},
    ),
    (
        "enum member added",
        _tool({"query": S, "state": {"type": "string", "enum": ["open", "closed"]}}, ["query"]),
        _tool(
            {"query": S, "state": {"type": "string", "enum": ["open", "closed", "all"]}},
            ["query"],
        ),
        {"query": "x", "state": "all"},
    ),
    (
        "type widened",
        _tool({"query": S, "page": {"type": "integer"}}, ["query"]),
        _tool({"query": S, "page": {"type": "string"}}, ["query"]),
        {"query": "x", "page": "next"},
    ),
    (
        "bound loosened",
        _tool({"query": S, "limit": {"type": "integer", "maximum": 50}}, ["query"]),
        _tool({"query": S, "limit": {"type": "integer", "maximum": 500}}, ["query"]),
        {"query": "x", "limit": 200},
    ),
]


def _run(v1: ServerTool, v2: ServerTool, arguments: Any, *, refresh: bool) -> tuple[Rig, Any]:
    server = ScriptedServer([v1])
    rig = Rig(server, RelayMode.AUTO, refresh_before_intervening=refresh, listing_wait=3.0)
    rig.request("server/discover")
    rig.call("search", {"query": "warm"})  # the relay has listed v1
    server.executed.clear()
    server.set_tools([v2])  # no notification: the relay is not told
    return rig, rig.call("search", arguments)


def _own_listings(server: ScriptedServer) -> int:
    return sum(
        1
        for line in server.received
        if str(json.loads(line).get("id", "")).startswith("stateguard-relay:")
    )


@pytest.mark.parametrize(
    ("name", "v1", "v2", "arguments"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
def test_a_call_valid_for_the_new_schema_is_forwarded_unchanged(
    name: str, v1: ServerTool, v2: ServerTool, arguments: Any
) -> None:
    rig, response = _run(v1, v2, arguments, refresh=True)
    try:
        assert response is not None and response["result"]["isError"] is False, response
        (executed,) = rig.server.executed
        assert executed.arguments == arguments
        decision = [e for e in rig.audit if e.get("event") == "call"][-1]
        assert decision["action"] == "forward"
        assert decision["listing"] == "confirmed"
    finally:
        rig.close()


@pytest.mark.parametrize(
    ("name", "v1", "v2", "arguments"), SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
def test_without_the_refresh_the_stale_listing_blocks_a_valid_call(
    name: str, v1: ServerTool, v2: ServerTool, arguments: Any
) -> None:
    """The defect the refresh fixes, kept as a regression witness."""
    rig, response = _run(v1, v2, arguments, refresh=False)
    try:
        assert rig.server.executed == [] or rig.server.executed[0].arguments != arguments
    finally:
        rig.close()


def test_a_call_that_passes_unchanged_costs_no_extra_round_trip() -> None:
    v1 = _tool({"query": S}, ["query"])
    server = ScriptedServer([v1])
    rig = Rig(server, RelayMode.AUTO)
    try:
        rig.request("server/discover")
        rig.call("search", {"query": "a"})
        before = _own_listings(server)
        for i in range(5):
            rig.call("search", {"query": str(i)})
        assert _own_listings(server) == before
    finally:
        rig.close()


def test_drift_against_the_new_schema_is_still_repaired_after_the_refresh() -> None:
    v1, v2 = _tool({"query": S}, ["query"]), _tool({"q": S, "limit": {"type": "integer"}}, ["q"])
    server = ScriptedServer([v1])
    rig = Rig(server, RelayMode.AUTO)
    try:
        rig.request("server/discover")
        server.set_tools([v2])
        response = rig.call("search", {"q": "x", "limit": "3"})
        assert response is not None and response["result"]["isError"] is False
        assert server.executed[-1].arguments == {"q": "x", "limit": 3}
    finally:
        rig.close()


def test_if_the_listing_cannot_be_confirmed_the_call_is_forwarded_unchanged() -> None:
    v1 = _tool({"query": S}, ["query"])
    server = ScriptedServer([v1])
    rig = Rig(server, RelayMode.AUTO, listing_wait=1.0)
    try:
        rig.request("server/discover")
        rig.call("search", {"query": "warm"})
        original = server._handle

        def failing_list(line: bytes, writer: Any) -> bool:
            message = json.loads(line)
            if message.get("method") == "tools/list":
                writer.write(
                    json.dumps(
                        {
                            "jsonrpc": "2.0",
                            "id": message["id"],
                            "error": {"code": -1, "message": "x"},
                        }
                    ).encode()
                    + b"\n"
                )
                writer.flush()
                return True
            return original(line, writer)

        server._handle = failing_list  # type: ignore[method-assign]
        response = rig.call("search", {"qury": "x"})
        assert response is not None
        assert server.received and json.loads(server.received[-1])["params"]["arguments"] == {
            "qury": "x"
        }
        decision = [e for e in rig.audit if e.get("event") == "call"][-1]
        assert decision["listing"] == "unconfirmed" and decision["action"] == "unvalidated"
    finally:
        rig.close()


def test_shadow_records_the_decision_on_a_confirmed_listing() -> None:
    v1, v2, arguments = SCENARIOS[0][1], SCENARIOS[0][2], SCENARIOS[0][3]
    server = ScriptedServer([v1])
    rig = Rig(server, RelayMode.SHADOW)
    try:
        rig.request("server/discover")
        server.set_tools([v2])
        rig.call("search", arguments)
        decision = [e for e in rig.audit if e.get("event") == "call"][-1]
        assert decision["auto_action"] == "forward" and decision["listing"] == "confirmed"
    finally:
        rig.close()

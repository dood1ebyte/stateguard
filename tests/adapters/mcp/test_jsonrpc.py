"""
The JSON-RPC boundary: what is forwarded is the client's message, with at
most ``params.arguments`` changed -- ``id``, ``_meta`` and every other field
preserved -- and what is returned for an unforwarded call is a well-formed
response to the right ``id``.
"""

from __future__ import annotations

import copy
import json
from typing import Any

import pytest

from stateguard import ContractGuard, GuardConfig
from stateguard.adapters.mcp import MalformedMessageError, MCPAction, ToolRegistry
from stateguard.adapters.mcp.jsonrpc import (
    INVALID_PARAMS,
    TOOLS_LIST_CHANGED,
    ToolCallRequest,
    ToolListAccumulator,
    error_response,
    forward_message,
    is_notification,
    is_request,
    is_response,
    refusal_response,
)
from stateguard.core.models.config import RepairMode

TOOL: dict[str, Any] = {
    "name": "get_forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "maximum": 14},
        },
        "required": ["location", "days"],
    },
}


def _call(arguments: Any = None, *, id: Any = 7, **params: Any) -> dict[str, Any]:
    body: dict[str, Any] = {"name": "get_forecast", **params}
    if arguments is not None:
        body["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": id, "method": "tools/call", "params": body}


def _registry(mode: RepairMode = RepairMode.AUTO) -> ToolRegistry:
    registry = ToolRegistry(ContractGuard.with_mcp(config=GuardConfig(mode=mode)))
    registry.register(TOOL)
    return registry


def _decide(message: dict[str, Any], mode: RepairMode = RepairMode.AUTO) -> Any:
    request = ToolCallRequest.parse(message)
    outcome = _registry(mode).process_call(request.name, request.arguments)
    return request, outcome


class TestParse:
    def test_a_well_formed_call(self) -> None:
        request = ToolCallRequest.parse(
            _call({"location": "x", "days": 1}, _meta={"progressToken": "p1"})
        )
        assert (request.id, request.name, request.arguments) == (
            7,
            "get_forecast",
            {"location": "x", "days": 1},
        )
        assert request.meta == {"progressToken": "p1"}

    def test_absent_arguments_stay_absent(self) -> None:
        assert ToolCallRequest.parse(_call()).arguments is None

    @pytest.mark.parametrize(
        "message",
        [
            {
                "jsonrpc": "2.0",
                "method": "tools/call",
                "params": {"name": "t"},
            },  # no id: a notification
            {"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            {"jsonrpc": "1.0", "id": 1, "method": "tools/call", "params": {"name": "t"}},
            {"jsonrpc": "2.0", "id": True, "method": "tools/call", "params": {"name": "t"}},
            {"jsonrpc": "2.0", "id": None, "method": "tools/call", "params": {"name": "t"}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call"},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": ""}},
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": []},
            "tools/call",
        ],
    )
    def test_malformed_messages_are_refused(self, message: Any) -> None:
        with pytest.raises(MalformedMessageError):
            ToolCallRequest.parse(message)

    def test_the_original_is_copied(self) -> None:
        message = _call({"location": "x", "days": 1})
        request = ToolCallRequest.parse(message)
        message["params"]["arguments"]["days"] = 99
        assert request.arguments == {"location": "x", "days": 1}


class TestForward:
    def test_a_repaired_call_changes_only_arguments(self) -> None:
        message = _call({"loc": "Mumbai", "days": "5"}, _meta={"progressToken": "p1"}, extra="kept")
        request, outcome = _decide(message)
        forwarded = forward_message(request, outcome)
        assert forwarded is not None
        assert forwarded["params"]["arguments"] == {"location": "Mumbai", "days": 5}
        expected = copy.deepcopy(message)
        expected["params"]["arguments"] = {"location": "Mumbai", "days": 5}
        assert forwarded == expected

    def test_an_unchanged_call_is_forwarded_byte_for_byte(self) -> None:
        message = _call({"location": "Mumbai", "days": 5}, id="req-1")
        request, outcome = _decide(message)
        forwarded = forward_message(request, outcome)
        assert json.dumps(forwarded) == json.dumps(message)

    @pytest.mark.parametrize("arguments", [{"loc": "Mumbai", "days": "5"}, {"days": 99}, None])
    def test_shadow_forwards_exactly_what_was_received(self, arguments: Any) -> None:
        """Including a call auto mode would refuse, and absent arguments."""
        message = _call(arguments, _meta={"progressToken": 3})
        request, outcome = _decide(message, RepairMode.SHADOW)
        assert outcome.should_forward
        assert forward_message(request, outcome) == message

    def test_an_unforwarded_call_has_no_message(self) -> None:
        request, outcome = _decide(_call({"location": "x", "days": 99}))
        assert outcome.action is MCPAction.REFUSE
        assert forward_message(request, outcome) is None


class TestRefusal:
    def test_a_refusal_is_a_tool_error_to_the_same_id(self) -> None:
        request, outcome = _decide(_call({"location": "x", "days": 99}, id="abc"))
        response = refusal_response(request, outcome)
        assert response["id"] == "abc" and response["jsonrpc"] == "2.0"
        result = response["result"]
        assert result["isError"] is True
        (block,) = result["content"]
        assert block["type"] == "text"
        assert "do not match the tool's input schema" in block["text"]
        assert "days" in block["text"]
        assert is_response(response)

    def test_an_escalation_says_it_could_be_corrected(self) -> None:
        tool = {
            "name": "lookup",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "account_id": {"type": "string"},
                    "account_name": {"type": "string"},
                },
                "required": ["account_id", "account_name"],
            },
        }
        registry = ToolRegistry()
        registry.register(tool)
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "lookup", "arguments": {"account": "SECRET-9"}},
        }
        request = ToolCallRequest.parse(message)
        outcome = registry.process_call(request.name, request.arguments)
        assert outcome.action is MCPAction.ESCALATE
        text = refusal_response(request, outcome)["result"]["content"][0]["text"]
        assert "possible correction" in text
        assert "SECRET-9" not in text  # value-free

    def test_refusing_a_forwarded_call_is_an_error(self) -> None:
        request, outcome = _decide(_call({"location": "x", "days": 1}))
        with pytest.raises(ValueError):
            refusal_response(request, outcome)

    def test_error_response(self) -> None:
        assert error_response(4, INVALID_PARAMS, "bad", {"field": "days"}) == {
            "jsonrpc": "2.0",
            "id": 4,
            "error": {"code": -32602, "message": "bad", "data": {"field": "days"}},
        }


class TestClassification:
    def test_kinds(self) -> None:
        assert is_request(_call(), "tools/call") and not is_request(_call(), "tools/list")
        assert is_notification({"jsonrpc": "2.0", "method": TOOLS_LIST_CHANGED}, TOOLS_LIST_CHANGED)
        assert is_response({"jsonrpc": "2.0", "id": 1, "result": {}})
        assert not is_response({"jsonrpc": "2.0", "id": 1, "method": "x"})


def _list_request(id: Any, cursor: str | None = None) -> dict[str, Any]:
    params = {} if cursor is None else {"cursor": cursor}
    return {"jsonrpc": "2.0", "id": id, "method": "tools/list", "params": params}


def _list_page(id: Any, names: list[str], next_cursor: str | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "tools": [{"name": n, "inputSchema": {"type": "object"}} for n in names]
    }
    if next_cursor:
        result["nextCursor"] = next_cursor
    return {"jsonrpc": "2.0", "id": id, "result": result}


class TestToolListAccumulator:
    def test_a_single_page(self) -> None:
        acc = ToolListAccumulator()
        acc.observe_request(_list_request(1))
        complete = acc.observe_response(_list_page(1, ["a", "b"]))
        assert [t["name"] for t in complete or []] == ["a", "b"]

    def test_pages_are_collected_until_the_last(self) -> None:
        acc = ToolListAccumulator()
        acc.observe_request(_list_request(1))
        assert acc.observe_response(_list_page(1, ["a"], "c1")) is None
        acc.observe_request(_list_request(2, "c1"))
        assert acc.observe_response(_list_page(2, ["b"], "c2")) is None
        acc.observe_request(_list_request(3, "c2"))
        complete = acc.observe_response(_list_page(3, ["c"]))
        assert [t["name"] for t in complete or []] == ["a", "b", "c"]

    def test_a_new_listing_supersedes_an_unfinished_one(self) -> None:
        acc = ToolListAccumulator()
        acc.observe_request(_list_request(1))
        acc.observe_response(_list_page(1, ["old"], "c1"))
        acc.observe_request(_list_request(2))  # client restarted the listing
        acc.observe_request(_list_request(3, "c1"))  # a late page of the old one
        assert acc.observe_response(_list_page(2, ["new"], "n1")) is None
        assert acc.observe_response(_list_page(3, ["stale"])) is None
        acc.observe_request(_list_request(4, "n1"))
        complete = acc.observe_response(_list_page(4, ["new2"]))
        assert [t["name"] for t in complete or []] == ["new", "new2"]

    def test_errors_and_strangers_are_ignored(self) -> None:
        acc = ToolListAccumulator()
        assert acc.observe_response(_list_page(9, ["x"])) is None  # never requested
        acc.observe_request(_list_request(1))
        assert (
            acc.observe_response(
                {"jsonrpc": "2.0", "id": 1, "error": {"code": -1, "message": "no"}}
            )
            is None
        )
        acc.observe_request(_call())  # not a listing
        acc.observe_request(_list_request(2))
        assert acc.observe_response(_list_page(2, [])) == []

    def test_into_a_registry(self) -> None:
        acc = ToolListAccumulator()
        registry = ToolRegistry()
        acc.observe_request(_list_request(1))
        acc.observe_response(_list_page(1, ["a"], "c"))
        acc.observe_request(_list_request(2, "c"))
        listing = acc.observe_response(_list_page(2, ["b"]))
        assert listing is not None
        report = registry.sync(listing)
        assert [c.key.name for c in report.added] == ["a", "b"]


class TestProtocolShapedResults:
    """
    Found by driving a real MCP 2.x client through the relay: under the
    stateless 2026-07-28 protocol, a synthesized result without
    ``resultType`` is rejected by the client.
    """

    def _refusal(self, meta: dict[str, Any] | None) -> dict[str, Any]:
        params: dict[str, Any] = {"location": "x", "days": 99}
        message = _call(params) if meta is None else _call(params, _meta=meta)
        request, outcome = _decide(message)
        return refusal_response(request, outcome)["result"]

    def test_a_versioned_modern_request_gets_result_type(self) -> None:
        result = self._refusal({"io.modelcontextprotocol/protocolVersion": "2026-07-28"})
        assert result["resultType"] == "complete"

    @pytest.mark.parametrize(
        "meta",
        [None, {"progressToken": 1}, {"io.modelcontextprotocol/protocolVersion": "2025-11-25"}],
    )
    def test_older_sessions_never_see_it(self, meta: dict[str, Any] | None) -> None:
        assert "resultType" not in self._refusal(meta)

    def test_tool_error_response(self) -> None:
        from stateguard.adapters.mcp.jsonrpc import protocol_version, tool_error_response

        request = ToolCallRequest.parse(
            _call({}, _meta={"io.modelcontextprotocol/protocolVersion": "2026-07-28"})
        )
        assert protocol_version(request) == "2026-07-28"
        assert tool_error_response(request, "no") == {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "content": [{"type": "text", "text": "no"}],
                "isError": True,
                "resultType": "complete",
            },
        }

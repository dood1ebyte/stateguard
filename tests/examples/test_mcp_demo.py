"""
The demo has to actually run.

``MCP_ADAPTER_PLAN.md`` Phase 3's exit criterion is that the demo runs from a
clean checkout, and a demo that only worked on the day it was written is
worse than no demo -- it is a claim in the README that quietly stops being
true. So the whole path is exercised here: a real server subprocess, real
MCP over stdio, the real proxy.

This is also the only test that touches the MCP SDK at all. Everything in
``tests/adapters/mcp/`` runs without it, which is the zero-dependency
guarantee ``tests/isolation/`` enforces; the SDK is needed to *run a server*,
not to read one's schema.

Skipped when the ``mcp`` extra is not installed.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="needs the 'mcp' extra: pip install 'sguard[mcp]'")

import anyio  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

from stateguard import ContractGuard  # noqa: E402
from stateguard.adapters.mcp import MCPAction  # noqa: E402
from stateguard.core.models.config import GuardConfig, RepairMode  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "mcp"
sys.path.insert(0, str(EXAMPLES))

from proxy import RepairingClient  # noqa: E402

pytestmark = pytest.mark.integration


def _server() -> StdioServerParameters:
    return StdioServerParameters(
        command=sys.executable,
        args=[str(EXAMPLES / "server.py")],
        env={**os.environ, "STATEGUARD_DEMO_QUIET": "1"},
    )


def _text(response: Any) -> str:
    return " ".join(block.text for block in response.content if getattr(block, "text", None))


async def _through_proxy(
    calls: list[tuple[str, dict[str, Any]]],
    guard: ContractGuard | None = None,
) -> list[Any]:
    async with Client(_server()) as client:
        proxy = RepairingClient(client, guard=guard)
        await proxy.load_tools()
        return [await proxy.call_tool(name, args) for name, args in calls]


class TestTheServerRejectsWhatItShould:
    """
    The premise. If the server accepted these, the demo would be proving
    nothing -- so it is asserted rather than assumed.
    """

    def test_drifted_arguments_are_rejected_without_the_proxy(self) -> None:
        async def run() -> Any:
            async with Client(_server()) as client:
                return await client.call_tool("get_forecast", {"loc": "Mumbai", "days": "5"})

        response = anyio.run(run)
        assert response.is_error is True
        assert "location" in _text(response)


class TestTheDemoCallsSucceed:
    def test_renamed_parameter_and_stringified_number(self) -> None:
        (call,) = anyio.run(_through_proxy, [("get_forecast", {"loc": "Mumbai", "days": "5"})])
        assert call.outcome.action is MCPAction.FORWARD
        assert call.sent == {"location": "Mumbai", "days": 5, "unit": "celsius"}
        assert call.response.is_error is False
        assert "Mumbai" in _text(call.response)

    def test_enum_in_the_wrong_case(self) -> None:
        (call,) = anyio.run(
            _through_proxy,
            [("get_forecast", {"location": "Delhi", "days": 3, "unit": "Celsius"})],
        )
        assert call.sent is not None
        assert call.sent["unit"] == "celsius"
        assert call.response.is_error is False

    def test_abbreviated_parameter_and_omitted_optional(self) -> None:
        (call,) = anyio.run(_through_proxy, [("search_places", {"q": "Bengaluru"})])
        assert call.sent == {"query": "Bengaluru", "limit": 5}
        assert call.response.is_error is False


class TestTheRefusalHolds:
    """
    The call the demo exists to *not* repair. A repair layer that always
    finds an answer is one you cannot trust with the answers it finds.
    """

    def test_out_of_range_is_refused_and_never_sent(self) -> None:
        (call,) = anyio.run(_through_proxy, [("search_places", {"query": "Chennai", "limit": 999})])
        assert call.outcome.action is MCPAction.REFUSE
        assert call.sent is None
        assert call.response is None
        assert call.called is False

    def test_the_refusal_names_the_field(self) -> None:
        (call,) = anyio.run(_through_proxy, [("search_places", {"query": "Chennai", "limit": 999})])
        assert "'limit'" in call.outcome.reason


class TestShadowMode:
    def test_the_server_receives_the_untouched_arguments(self) -> None:
        """
        Shadow's entire promise. If the proxy applied the repair here it
        would be doing exactly what the caller asked it not to do -- and the
        server's rejection is the proof that nothing was changed.
        """
        arguments = {"loc": "Mumbai", "days": "5"}
        (call,) = anyio.run(
            _through_proxy,
            [("get_forecast", arguments)],
            ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW)),
        )
        assert call.outcome.action is MCPAction.HOLD
        assert call.sent == arguments
        assert call.response.is_error is True

    def test_the_preview_is_what_auto_would_have_sent(self) -> None:
        """A shadow diff you cannot trust to match tells you nothing."""
        arguments = {"loc": "Mumbai", "days": "5"}
        (shadow,) = anyio.run(
            _through_proxy,
            [("get_forecast", arguments)],
            ContractGuard.with_mcp(config=GuardConfig(mode=RepairMode.SHADOW)),
        )
        (auto,) = anyio.run(_through_proxy, [("get_forecast", arguments)])
        assert shadow.outcome.preview == auto.sent


class TestTheProxyItself:
    def test_an_unknown_tool_is_a_clear_error(self) -> None:
        # Caught inside the coroutine rather than around ``anyio.run``:
        # anyio wraps anything escaping the client's task group in an
        # ExceptionGroup, so ``pytest.raises(KeyError)`` outside would not
        # match and would say nothing useful about why.
        async def run() -> str:
            async with Client(_server()) as client:
                proxy = RepairingClient(client)
                await proxy.load_tools()
                try:
                    await proxy.call_tool("nope", {})
                except KeyError as exc:
                    return str(exc)
                return ""

        message = anyio.run(run)
        assert "Unknown tool 'nope'" in message
        assert "get_forecast" in message

    def test_tools_load_lazily_if_not_loaded_explicitly(self) -> None:
        async def run() -> Any:
            async with Client(_server()) as client:
                proxy = RepairingClient(client)
                return await proxy.call_tool("get_forecast", {"loc": "Pune", "days": 2})

        call = anyio.run(run)
        assert call.response.is_error is False

    def test_the_sdk_generated_schema_extracts_without_refusal(self) -> None:
        """
        The corpus test in miniature. A schema this adapter cannot read is
        refused loudly by design -- which is right, and also means a real
        SDK-generated schema failing to extract would be a silent demo
        break. It carries 'title' on every property, which is exactly the
        kind of keyword a stricter subset would have rejected.
        """

        async def run() -> dict[str, Any]:
            async with Client(_server()) as client:
                proxy = RepairingClient(client)
                return await proxy.load_tools()

        tools = anyio.run(run)
        guard = ContractGuard.with_mcp()
        for name, tool in tools.items():
            assert "title" in str(tool["inputSchema"]), f"{name}: premise changed"
            contract = guard._adapter.extract_contract(tool)
            assert contract.fields, f"{name}: extracted no fields"

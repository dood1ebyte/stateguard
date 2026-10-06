"""
The stdio relay, end to end: a real MCP client, the relay, a real server.

The client is the MCP SDK's own ``Client``, pointed at ``stdio_proxy.py``
exactly as an agent host would point it at a server command. Everything the
relay claims is checked from the client's side of the wire: listings pass
through, drifted calls are repaired, refused and ambiguous calls are
answered by the relay and never reach the server, valid results are
byte-identical to a direct connection, shadow mode changes nothing, and the
audit log records each decision without argument values.

Skipped when the ``mcp`` extra is not installed.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("mcp", reason="needs the 'mcp' extra: pip install 'sguard[mcp]'")

import anyio  # noqa: E402
from mcp import Client, StdioServerParameters  # noqa: E402

EXAMPLES = Path(__file__).resolve().parents[2] / "examples" / "mcp"
pytestmark = pytest.mark.integration


def _params(*relay_args: str, direct: bool = False) -> StdioServerParameters:
    server = [sys.executable, str(EXAMPLES / "server.py")]
    args = server[1:] if direct else [str(EXAMPLES / "stdio_proxy.py"), *relay_args, "--", *server]
    return StdioServerParameters(
        command=sys.executable,
        args=args,
        env={**os.environ, "STATEGUARD_DEMO_QUIET": "1"},
    )


def _text(result: Any) -> str:
    return " ".join(block.text for block in result.content if getattr(block, "text", None))


def _session(
    params: StdioServerParameters, calls: list[tuple[str, Any]]
) -> tuple[list[str], list[Any]]:
    async def go() -> tuple[list[str], list[Any]]:
        async with Client(params) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            return names, [await client.call_tool(name, args) for name, args in calls]

    return anyio.run(go)


def _audit(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def test_through_the_relay(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    names, results = _session(
        _params("--audit", str(audit)),
        [
            ("get_forecast", {"loc": "Mumbai", "days": "5"}),  # repaired
            ("search_places", {"query": "Chennai", "limit": 999}),  # refused by the relay
            ("compare_cities", {"city": "Delhi"}),  # escalated by the relay
            ("get_forecast", {"location": "Pune", "days": 2}),  # valid as sent
        ],
    )
    assert names == ["get_forecast", "search_places", "compare_cities"]
    repaired, refused, escalated, valid = results

    assert not repaired.is_error
    assert _text(repaired) == "Mumbai: 5-day forecast, temperatures in degrees C."

    assert refused.is_error
    assert "do not match the tool's input schema" in _text(refused)
    assert "limit" in _text(refused)

    assert escalated.is_error
    assert "possible correction" in _text(escalated)

    assert not valid.is_error

    calls = [e for e in _audit(audit) if e["event"] == "call"]
    assert [c["action"] for c in calls] == ["forward", "refuse", "escalate", "forward"]
    assert calls[0]["decisions"][0]["path"] == "location"
    assert "Mumbai" not in audit.read_text()  # decisions, not values
    synced = [e for e in _audit(audit) if e["event"] == "tools_synced"]
    assert synced and synced[0]["summary"]["added"] == 3


def test_results_are_byte_identical_to_a_direct_connection() -> None:
    call = [("search_places", {"query": "Goa", "limit": 3})]
    _, (direct,) = _session(_params(direct=True), call)
    _, (relayed,) = _session(_params(), call)
    assert relayed.model_dump() == direct.model_dump()


def test_shadow_changes_nothing_on_the_wire(tmp_path: Path) -> None:
    audit = tmp_path / "audit.jsonl"
    calls = [
        ("get_forecast", {"loc": "Mumbai", "days": "5"}),
        ("search_places", {"query": "x", "limit": 999}),
    ]
    _, shadowed = _session(_params("--shadow", "--audit", str(audit)), calls)
    _, direct = _session(_params(direct=True), calls)
    # The server saw exactly what it would have seen without the relay.
    assert [r.is_error for r in shadowed] == [r.is_error for r in direct] == [True, True]
    decisions = [e for e in _audit(audit) if e["event"] == "call"]
    assert [d["action"] for d in decisions] == ["hold", "hold"]
    assert [d["auto_action"] for d in decisions] == ["forward", "refuse"]

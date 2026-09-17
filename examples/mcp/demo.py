"""
Before / after: the same drifted tool calls, with and without StateGuard.

Story: the server updated ``get_forecast``'s signature. The agent is still
calling the old shape -- and doing what models do, which is to send a number
as a string and get an enum's capitalisation wrong.

Run it::

    python examples/mcp/demo.py

It launches ``server.py`` as a real subprocess and speaks MCP over stdio, so
what you see is the actual protocol, not a simulation of it.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

import anyio
from mcp import Client, StdioServerParameters
from mcp.types import CallToolResult

sys.path.insert(0, str(Path(__file__).parent))

from proxy import RepairingClient  # noqa: E402

SERVER = StdioServerParameters(
    command=sys.executable,
    args=[str(Path(__file__).parent / "server.py")],
    # Keeps the server's stderr out of the transcript -- see server.py. The
    # rejections are still real; they are just not interleaved into stdout in
    # whatever order the OS happens to flush two pipes.
    env={**os.environ, "STATEGUARD_DEMO_QUIET": "1"},
)

#: What the agent sends. Every one of these is a real drift shape.
CALLS: list[tuple[str, str, dict[str, Any]]] = [
    (
        "renamed parameter + stringified number",
        "get_forecast",
        {"loc": "Mumbai", "days": "5"},
    ),
    (
        "enum in the wrong case",
        "get_forecast",
        {"location": "Delhi", "days": 3, "unit": "Celsius"},
    ),
    (
        "abbreviated parameter, optional omitted",
        "search_places",
        {"q": "Bengaluru"},
    ),
    (
        "out of range -- not repairable, and should not be",
        "search_places",
        {"query": "Chennai", "limit": 999},
    ),
]

RULE = "=" * 78


def summarise(response: CallToolResult | None) -> str:
    if response is None:
        return "(not sent)"
    text = " ".join(block.text for block in response.content if getattr(block, "text", None))
    prefix = "SERVER ERROR" if response.is_error else "OK"
    return f"{prefix}: {text.splitlines()[0][:96]}"


async def without_stateguard() -> None:
    print(f"\n{RULE}\nWITHOUT StateGuard -- the agent calls the server directly\n{RULE}")
    async with Client(SERVER) as client:
        for label, tool, arguments in CALLS:
            print(f"\n{label}\n  sent: {arguments}")
            response = await client.call_tool(tool, arguments)
            print(f"  {summarise(response)}")


async def with_stateguard() -> None:
    print(f"\n{RULE}\nWITH StateGuard -- the same calls through the repairing proxy\n{RULE}")
    async with Client(SERVER) as client:
        proxy = RepairingClient(client, on_event=print)
        await proxy.load_tools()
        for label, tool, arguments in CALLS:
            print(f"\n{label}\n  sent by agent: {arguments}")
            call = await proxy.call_tool(tool, arguments)
            if call.sent is not None and call.sent != arguments:
                print(f"  sent to server: {call.sent}")
            print(f"  {summarise(call.response)}")


async def main() -> None:
    await without_stateguard()
    await with_stateguard()
    print(
        f"\n{RULE}\n"
        "Three calls that the server rejected now succeed. The fourth is still\n"
        "refused -- 'limit: 999' violates the schema's maximum and there is no\n"
        "honest repair for it, so StateGuard does not invent one.\n"
        f"{RULE}"
    )


if __name__ == "__main__":
    anyio.run(main)

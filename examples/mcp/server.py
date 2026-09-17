"""
A small MCP server, standing in for one whose signature has moved on.

Nothing here knows about StateGuard. That is the point: the server is an
ordinary MCP server, the agent is an ordinary MCP client, and the repair
happens in between (``proxy.py``).

The tools are chosen to produce the three drift shapes that actually occur
in tool calls, rather than one contrived one:

* ``get_forecast`` -- a renamed parameter (``loc`` for ``location``), a
  stringified number (``"5"`` for ``5``), an enum in the wrong case
  (``"Celsius"`` for ``"celsius"``), and an optional with a declared default
  the model omits.
* ``search_places`` -- a bounded integer, so an out-of-range value is
  refused rather than repaired. A demo that only ever succeeds is not
  showing you anything.

Run it directly to serve over stdio::

    python examples/mcp/server.py

Requires the MCP SDK: ``pip install 'sguard[mcp]'``.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Literal

from mcp.server.mcpserver import MCPServer
from pydantic import Field

# The SDK logs every rejected tool call to stderr. That is right for a real
# server and wrong for a demo transcript: the subprocess's stderr interleaves
# with the parent's stdout in whatever order the OS flushes them, so the
# rejections show up detached from the calls that caused them. ``demo.py``
# sets this to keep the transcript readable; running the server yourself does
# not, so you still see them.
if os.environ.get("STATEGUARD_DEMO_QUIET"):
    logging.getLogger("mcp").setLevel(logging.CRITICAL)

server = MCPServer(name="weather-demo")


@server.tool()
def get_forecast(
    location: str,
    days: Annotated[int, Field(ge=1, le=14)],
    unit: Literal["celsius", "fahrenheit"] = "celsius",
) -> str:
    """Get a weather forecast for a location."""
    degrees = "C" if unit == "celsius" else "F"
    return f"{location}: {days}-day forecast, temperatures in degrees {degrees}."


@server.tool()
def search_places(
    query: str,
    limit: Annotated[int, Field(ge=1, le=10)] = 5,
) -> str:
    """Search for places by name."""
    return f"Found up to {limit} places matching {query!r}."


if __name__ == "__main__":
    server.run()

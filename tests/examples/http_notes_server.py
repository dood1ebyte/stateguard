"""
A plain ``MCPServer`` (mcp 2.x) over Streamable HTTP, for the HTTP relay tests:
it knows nothing of StateGuard. ``add_note`` appends one line to a notes file
(one fsync'd write); ``list_notes`` reads it.

    python tests/examples/http_notes_server.py NOTES PORT [--resumable] [--json] [--slow S]
        [--slow-read S]

``--resumable`` keeps the server's events in memory (an ``EventStore``), so a
client can resume a cut response stream with ``Last-Event-ID``. ``--slow S`` makes
``add_note`` wait S seconds before it writes (TEST-ONLY), so a call can be in flight when
the server is killed; ``--slow-read S`` makes ``list_notes`` wait S seconds (a slow read).
"""

from __future__ import annotations

import itertools
import os
import sys
from typing import Any

import anyio

from mcp.server.mcpserver import MCPServer
from mcp.server.streamable_http import EventCallback, EventMessage, EventStore
from mcp_types import ToolAnnotations


class MemoryEvents(EventStore):
    """Every event, in order, for as long as the process lives."""

    def __init__(self) -> None:
        self.events: list[tuple[str, str, Any]] = []
        self.ids = itertools.count(1)

    async def store_event(self, stream_id: str, message: Any) -> str:
        event_id = str(next(self.ids))
        self.events.append((event_id, stream_id, message))
        return event_id

    async def replay_events_after(self, last_event_id: str, send_callback: EventCallback) -> Any:
        stream = next((s for e, s, _ in self.events if e == last_event_id), None)
        if stream is None:
            return None
        after = False
        for event_id, stream_id, message in self.events:
            if after and stream_id == stream and message is not None:
                await send_callback(EventMessage(message, event_id))
            after = after or event_id == last_event_id
        return stream


def main() -> None:
    notes, port = sys.argv[1], int(sys.argv[2])
    slow = float(sys.argv[sys.argv.index("--slow") + 1]) if "--slow" in sys.argv else 0.0
    slow_read = (
        float(sys.argv[sys.argv.index("--slow-read") + 1]) if "--slow-read" in sys.argv else 0.0
    )
    app = MCPServer("notes")

    @app.tool()
    async def add_note(text: str) -> str:
        if slow:
            await anyio.sleep(slow)
        with open(notes, "a") as out:  # noqa: ASYNC230 -- one short fsync'd append
            out.write(text + "\n")
            out.flush()
            os.fsync(out.fileno())
        return f"added {text}"

    @app.tool(annotations=ToolAnnotations(readOnlyHint=True))
    async def list_notes() -> str:
        if slow_read:
            await anyio.sleep(slow_read)
        return open(notes).read() if os.path.exists(notes) else ""  # noqa: ASYNC230

    app.run(
        transport="streamable-http",
        host="127.0.0.1",
        port=port,
        json_response="--json" in sys.argv,
        event_store=MemoryEvents() if "--resumable" in sys.argv else None,
    )


if __name__ == "__main__":
    main()

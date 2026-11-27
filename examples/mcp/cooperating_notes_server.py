"""
A **cooperating** MCP server (``mcp`` 2.x ``MCPServer``): its effect and its idempotency key's
record commit in **one SQLite transaction**, so a keyed call's crash window is closed -- for this
effect, and only because the server cooperates.

    python examples/mcp/cooperating_notes_server.py DB [--http PORT [--json]] [--no-keys]

The notes live in the key store's own database (``DB``, table ``notes``), next to
``stateguard_keys``. The tools are the notes server's (``tests/examples/http_notes_server.py``), so
the same clients and drivers run against both:

``add_note(text)``   mutating. Keyed, it is **transactional**
                     (``KeyedTools.transactional``): the note row and the key's record are
                     written in one ``BEGIN IMMEDIATE`` transaction on ``DB``:

                     - a crash anywhere before the commit leaves neither (SQLite rolls the
                       uncommitted transaction back), and a resend with the key executes, once;
                     - after the commit both exist, and a resend replays the recorded result;
                     - a late or concurrent duplicate of the key waits on the database's write
                       lock and then finds the record: it cannot commit a second note (the
                       key is the record's primary key, and the record commits with the note).

                     This is the replay rule's first case (PHASE2_PLAN.md, clarification A):
                     the execution state is authoritative and the original attempt is fenced
                     off by the transaction itself. Without a key it is an ordinary insert.
``list_notes()``     read-only: the notes, one per line, in order.

``--no-keys`` serves the same tools with no keys at all: the non-cooperating baseline.
Stdio by default; ``--http PORT`` serves Streamable HTTP on 127.0.0.1 (``--json`` for JSON
responses instead of event streams).

TEST-ONLY fault injection (labelled faults, never in production), the injector's variables:

    STATEGUARD_FAULT=before_begin|after_begin|after_effect|after_record|hang_after_record
    STATEGUARD_FAULT_FILE=<path>   the fault fires once per file (whatever the process generation)
    STATEGUARD_FAULT_TOOLS=<list>  tools it applies to (default: add_note)

The process kills itself (SIGKILL, as a crash would) at the point. Keyed:

``before_begin``   before the transaction begins
``after_begin``    inside it: the key looked up and absent, before the note is inserted
``after_effect``   inside it: the note inserted, before the key's record and the commit
``after_record``   committed, before the answer is sent

Unkeyed (``--no-keys``, or a call without a key): ``before_begin`` before the insert and every
``after_*`` point after it (the insert committed), as the injector's fault-only mode does.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import signal
import sqlite3
import sys
from typing import Any

import anyio
import mcp_types as types
from mcp.server.mcpserver import MCPServer

from stateguard.adapters.mcp.idempotency import IdempotencyStore
from stateguard.adapters.mcp.idempotency_sdk import KeyedTools
from stateguard.adapters.mcp.inject import Faults

TOOL = "add_note"


def _added(text: str) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=f"added {text}")])


def build(db: str, *, keys: bool = True, environ: dict[str, str] | None = None) -> MCPServer:
    faults = Faults(dict(os.environ if environ is None else environ), [TOOL])
    store = IdempotencyStore(db, scope="notes")
    with contextlib.closing(store.connect()) as connection:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS notes (seq INTEGER PRIMARY KEY, text TEXT NOT NULL)"
        )

    def crash(point: str) -> None:
        """A synchronous crash point (inside the transaction, in a worker thread)."""
        if faults.fires(point, TOOL):
            os.kill(os.getpid(), signal.SIGKILL)  # no cleanup, no commit, no answer

    def insert_note(connection: sqlite3.Connection, arguments: dict[str, Any]) -> Any:
        """The keyed effect, inside the transaction that also records the key."""
        text = arguments.get("text")
        if not isinstance(text, str) or not text:
            raise ValueError("a note is non-empty text")
        crash("after_begin")
        connection.execute("INSERT INTO notes (text) VALUES (?)", (text,))
        crash("after_effect")
        return _added(text)

    middleware = []
    if keys:
        keyed = KeyedTools(store, on_point=faults.on_point).transactional(TOOL, insert_note)
        middleware.append(keyed.middleware())
    app = MCPServer("cooperating-notes", middleware=middleware)

    @app.tool(structured_output=False)
    async def add_note(text: str) -> types.CallToolResult:
        """Add a note (keyed: in one transaction with its key's record)."""
        if not text:
            raise ValueError("a note is non-empty text")
        await faults.at("before_begin", TOOL)

        def insert() -> None:
            with contextlib.closing(store.connect()) as connection:
                connection.execute("INSERT INTO notes (text) VALUES (?)", (text,))

        await anyio.to_thread.run_sync(insert)
        for point in ("after_begin", "after_effect", "after_record"):
            await faults.at(point, TOOL)
        await faults.at("hang_after_record", TOOL)
        return _added(text)

    @app.tool(annotations=types.ToolAnnotations(read_only_hint=True), structured_output=False)
    async def list_notes() -> str:
        """The notes, one per line, in order."""

        def read() -> str:
            with contextlib.closing(store.connect()) as connection:
                rows = connection.execute("SELECT text FROM notes ORDER BY seq").fetchall()
            return "\n".join(row[0] for row in rows)

        return await anyio.to_thread.run_sync(read)

    return app


def count(db: str, text: str) -> int:
    """How many notes say *text* (the ground truth a test or driver reads)."""
    if not os.path.exists(db):
        return 0
    with contextlib.closing(sqlite3.connect(db)) as connection:
        try:
            return int(
                connection.execute("SELECT COUNT(*) FROM notes WHERE text = ?", (text,)).fetchone()[
                    0
                ]
            )
        except sqlite3.OperationalError:
            return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0] if __doc__ else None)
    parser.add_argument("db", help="the SQLite database (notes and keys)")
    parser.add_argument("--http", type=int, metavar="PORT", help="Streamable HTTP on 127.0.0.1")
    parser.add_argument("--json", action="store_true", help="JSON responses (HTTP)")
    parser.add_argument("--no-keys", action="store_true", help="serve without keys (baseline)")
    args = parser.parse_args(argv)
    app = build(args.db, keys=not args.no_keys)
    if args.http:
        app.run(
            transport="streamable-http", host="127.0.0.1", port=args.http, json_response=args.json
        )
    else:
        app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

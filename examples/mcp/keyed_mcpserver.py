"""
Adopting StateGuard idempotency keys in an ``MCPServer`` (the Python MCP SDK's
high-level server, formerly FastMCP): one middleware and a recovery check.

    python examples/mcp/keyed_mcpserver.py --db notes.db --notes notes.txt
    python examples/mcp/keyed_mcpserver.py --db notes.db --notes notes.txt --http 8765

The adoption, in full:

    store = IdempotencyStore(db, scope="notes", exclusive=True)
    keyed = KeyedTools(store).two_phase("add_note", recover=note_has_key)
    app = MCPServer("notes", middleware=[keyed.middleware()])

The tools:

``add_note(text)``   mutating, two-phase. It appends ``text<TAB>key`` to a notes
                     file in one fsync'd write. The key comes from ``current_key()``,
                     so the recovery check is exact. Before serving, a torn last
                     line (an interrupted append) is cut; this is safe because the
                     exclusive store proves no other process is appending.
``list_notes()``     read-only (``readOnlyHint``).

The middleware sits below the transport, so the contract is the same on stdio
and on Streamable HTTP (``--http PORT``).

Test-only fault injection (``--fault POINT --fault-file F``): the process exits
abruptly (``os._exit``) once per *F*, on the first ``add_note`` call that reaches
POINT:

``after_begin``    the key is claimed, before the note is written
``after_effect``   the note is written, before its key's record
``after_record``   the record is committed, before the answer is sent
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

import mcp_types as types
from mcp.server.context import CallNext, HandlerResult, ServerRequestContext
from mcp.server.mcpserver import MCPServer

from stateguard.adapters.mcp.idempotency import IdempotencyStore
from stateguard.adapters.mcp.idempotency_sdk import KeyedTools, Recovery, current_key

POINTS = ("after_begin", "after_effect", "after_record")


def build(args: argparse.Namespace) -> MCPServer:
    notes = Path(args.notes)
    flag = Path(args.fault_file) if args.fault_file else None

    def crash(point: str) -> None:
        if args.fault == point and flag is not None and not flag.exists():
            flag.write_text(point)
            os._exit(137)

    store = IdempotencyStore(args.db, scope="notes", exclusive=True)
    if notes.exists():  # holding the lock: no other process is appending
        data = notes.read_bytes()
        keep = data.rfind(b"\n") + 1
        if keep != len(data):
            with open(notes, "r+b") as out:
                out.truncate(keep)
                os.fsync(out.fileno())

    def added(text: str) -> types.CallToolResult:
        return types.CallToolResult(content=[types.TextContent(type="text", text=f"added {text}")])

    def note_has_key(key: str, arguments: dict[str, Any]) -> Recovery:
        lines = (notes.read_text() if notes.exists() else "").split("\n")
        if lines[-1]:
            return Recovery.unknown("the notes file ends in a torn line")
        if f"{arguments.get('text')}\t{key}" in lines[:-1]:
            return Recovery.found(added(str(arguments.get("text"))))
        return Recovery.absent()

    keyed = KeyedTools(store).two_phase("add_note", recover=note_has_key)

    async def crash_after_record(
        ctx: ServerRequestContext[Any, Any], call_next: CallNext
    ) -> HandlerResult:
        result = await call_next(ctx)
        params = ctx.params if isinstance(ctx.params, dict) else {}
        if ctx.method == "tools/call" and params.get("name") == "add_note":
            crash("after_record")
        return result

    app = MCPServer("keyed-notes", middleware=[crash_after_record, keyed.middleware()])

    @app.tool(structured_output=False)
    def add_note(text: str) -> types.CallToolResult:
        """Append a note."""
        if not text or "\n" in text or "\t" in text:
            raise ValueError("a note is one line without tabs")
        crash("after_begin")
        key = current_key()
        line = f"{text}\t{key}\n" if key else f"{text}\n"
        with open(notes, "ab") as out:
            out.write(line.encode())
            out.flush()
            os.fsync(out.fileno())
        crash("after_effect")
        return added(text)

    @app.tool(annotations=types.ToolAnnotations(read_only_hint=True), structured_output=False)
    def list_notes() -> str:
        """The notes, one per line."""
        text = notes.read_text() if notes.exists() else ""
        return "\n".join(line.split("\t", 1)[0] for line in text.splitlines())

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--notes", required=True)
    parser.add_argument("--http", type=int, metavar="PORT", help="Streamable HTTP on 127.0.0.1")
    parser.add_argument("--fault", choices=POINTS)
    parser.add_argument("--fault-file")
    args = parser.parse_args(argv)
    app = build(args)
    if args.http:
        app.run(transport="streamable-http", host="127.0.0.1", port=args.http)
    else:
        app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())

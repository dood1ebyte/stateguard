"""
A reference MCP server that honours StateGuard idempotency keys, built on the
official Python MCP SDK (``mcp`` 2.x, low-level ``Server``) with
``stateguard.adapters.mcp.idempotency_sdk``.

    python examples/mcp/keyed_reference_server.py --db orders.db --ledger ledger.txt

Tools:

``create_order(item, qty)``   mutating, **transactional**: the order row and its
                              key's record commit in one SQLite transaction
                              (the store's database), so a crash before the
                              commit leaves neither.
``append_ledger(entry)``      mutating, **two-phase**: appends a line to a plain
                              file (fsync'd) -- an effect outside any
                              transaction, so the contract's crash window
                              applies, and is closed by recovery (below).
``get_order(order_id)``       read-only.
``read_ledger()``             read-only.

Every call without a key behaves as an ordinary MCP tool call.

**Recovery** (the default; ``--no-recover`` turns it off, and a crash between
claiming a key and recording its result then leaves the key in doubt). The
contract's two conditions, met here:

1. *The attempt is over.* The store is exclusive (``flock``, held for the
   process lifetime), so a key another process started is abandoned once
   this process holds the lock. A second live server on the same store does
   not start.
2. *The check is about this key.* A keyed append writes ``entry<TAB>key`` in
   one ``write`` -- the line carries its key. Before serving, the server cuts
   a torn last line (no newline: an append a crash interrupted) -- it was
   never an entry, and nothing else can be writing. The check for a key is
   then exact: a complete line ``entry<TAB>key`` means it happened (answered
   ``recovered``); none means it did not (executed now, once).

Ledger lines are ``entry`` (unkeyed) or ``entry<TAB>key`` (keyed);
``read_ledger`` returns the entries.

Test-only fault injection (``--fault POINT --fault-file F``): the process
exits abruptly (``os._exit``, as a kill would) at POINT on the first keyed
call that reaches it, once per *F*, whatever the process generation:

``before_begin``   before the key is claimed
``after_begin``    the key is claimed, before the effect
``mid_effect``     part of the effect written (ledger: half a line)
``after_effect``   the effect is done, before the key's record
``after_record``   the record is committed, before the answer is written
``hang_after_record``  the record is committed; never answer (stay alive)

``--effect-delay S`` sleeps inside each mutating effect (for concurrency tests).
``--no-advertise`` lists no tool as keyed (a server that does not support keys).
``--no-recover`` keeps abandoned keys in doubt (the contract without recovery).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

import anyio
import mcp_types as types
from mcp.server.lowlevel import Server
from mcp.server.stdio import stdio_server

from stateguard.adapters.mcp.idempotency import IDEMPOTENCY_KEY, IdempotencyStore
from stateguard.adapters.mcp.idempotency_sdk import KeyedTools, NotExecuted, Recovery

POINTS = (
    "before_begin",
    "after_begin",
    "mid_effect",
    "after_effect",
    "after_record",
    "hang_after_record",
)


class Faults:
    def __init__(self, point: str | None, flag: str | None) -> None:
        self.point, self.flag = point, Path(flag) if flag else None

    def hit(self, point: str) -> bool:
        """True (once per flag file) when *point* is the configured fault."""
        if self.point != point or self.flag is None or self.flag.exists():
            return False
        self.flag.write_text(point)
        return True

    def crash(self, point: str) -> None:
        if self.hit(point):
            sys.stdout.flush()
            os._exit(137)


def _text(payload: Any) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=json.dumps(payload))])


def _schema(**properties: Any) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


READ_ONLY = types.ToolAnnotations(read_only_hint=True)


def cut_torn_tail(ledger: Path) -> int:
    """Drop a last line with no newline (an interrupted append). Bytes removed."""
    if not ledger.exists():
        return 0
    data = ledger.read_bytes()
    keep = data.rfind(b"\n") + 1
    if keep == len(data):
        return 0
    with open(ledger, "r+b") as out:
        out.truncate(keep)
        out.flush()
        os.fsync(out.fileno())
    return len(data) - keep


def ledger_entry(line: str) -> str:
    return line.split("\t", 1)[0]


def build(args: argparse.Namespace) -> Server:
    recovering = not args.no_recover
    store = IdempotencyStore(args.db, scope=args.scope, exclusive=recovering)
    faults = Faults(args.fault, args.fault_file)
    ledger = Path(args.ledger)
    if recovering:
        # Holding the lock: no other process can be appending.
        cut = cut_torn_tail(ledger)
        if cut:
            print(f"keyed-reference: cut a torn ledger line ({cut} bytes)", file=sys.stderr)
    with contextlib.closing(store.connect()) as db:
        db.execute("CREATE TABLE IF NOT EXISTS orders (id INTEGER PRIMARY KEY, item TEXT, qty INT)")

    def insert_order(db: sqlite3.Connection, arguments: dict[str, Any]) -> types.CallToolResult:
        faults.crash("after_begin")
        if not isinstance(arguments.get("qty"), int) or arguments["qty"] <= 0:
            raise ValueError("qty must be a positive integer")
        cursor = db.execute(
            "INSERT INTO orders (item, qty) VALUES (?, ?)", (arguments["item"], arguments["qty"])
        )
        if args.effect_delay:
            time.sleep(args.effect_delay)
        faults.crash("mid_effect")
        faults.crash("after_effect")
        return _text({"order_id": cursor.lastrowid, "item": arguments["item"]})

    async def list_tools(ctx: Any, params: Any) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name="create_order",
                    description="Create an order.",
                    input_schema=_schema(item={"type": "string"}, qty={"type": "integer"}),
                ),
                types.Tool(
                    name="append_ledger",
                    description="Append an entry to the ledger.",
                    input_schema=_schema(entry={"type": "string"}),
                ),
                types.Tool(
                    name="get_order",
                    description="Read an order.",
                    input_schema=_schema(item={"type": "string"}),
                    annotations=READ_ONLY,
                ),
                types.Tool(
                    name="read_ledger",
                    description="Read the ledger.",
                    input_schema=_schema(),
                    annotations=READ_ONLY,
                ),
            ]
        )

    async def call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        arguments = dict(params.arguments or {})
        if params.name == "create_order":  # reached without a key: an ordinary call
            with contextlib.closing(store.connect()) as db:
                return insert_order(db, arguments)
        if params.name == "append_ledger":
            entry = arguments.get("entry")
            if not isinstance(entry, str) or not entry or "\n" in entry or "\t" in entry:
                raise NotExecuted("entry must be one non-empty line without tabs")
            faults.crash("after_begin")
            key = (params.meta or {}).get(IDEMPOTENCY_KEY)
            marked = f"{entry}\t{key}" if isinstance(key, str) and key else entry
            line = (marked + "\n").encode()
            with open(ledger, "ab") as out:
                if args.effect_delay:
                    await anyio.sleep(args.effect_delay)
                if faults.hit("mid_effect"):
                    out.write(line[: len(line) // 2])
                    out.flush()
                    os.fsync(out.fileno())
                    os._exit(137)
                out.write(line)
                out.flush()
                os.fsync(out.fileno())
            faults.crash("after_effect")
            return _text({"appended": entry})
        if params.name == "get_order":
            with contextlib.closing(store.connect()) as db:
                rows = db.execute(
                    "SELECT id, qty FROM orders WHERE item = ?", (arguments.get("item"),)
                ).fetchall()
            return _text({"orders": rows})
        if params.name == "read_ledger":
            text = ledger.read_text() if ledger.exists() else ""
            return _text({"lines": [ledger_entry(line) for line in text.splitlines()]})
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=f"Unknown tool: {params.name}")],
            is_error=True,
        )

    def ledger_has_key(key: str, arguments: dict[str, Any]) -> Recovery:
        # Exact: this key's own line. A crash's torn line was cut at startup; one
        # now (a failed write in this process) means an append can't be ruled out.
        lines = (ledger.read_text() if ledger.exists() else "").split("\n")
        if lines[-1]:
            return Recovery.unknown("the ledger ends in a torn line")
        if f"{arguments.get('entry')}\t{key}" in lines[:-1]:
            return Recovery.found(_text({"appended": arguments.get("entry")}))
        return Recovery.absent()

    keyed = KeyedTools(store)
    if not args.no_advertise:
        keyed.two_phase("append_ledger", recover=ledger_has_key if recovering else None)
        keyed.transactional("create_order", insert_order)
    keyed_call = keyed.wrap_call(call_tool)

    mutating = {"create_order", "append_ledger"}

    async def on_call_tool(ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name in mutating:
            faults.crash("before_begin")
        result: types.CallToolResult = await keyed_call(ctx, params)
        if params.name in mutating:
            faults.crash("after_record")
            if faults.hit("hang_after_record"):
                await anyio.sleep_forever()
        return result

    return Server(
        "keyed-reference", on_list_tools=keyed.wrap_list(list_tools), on_call_tool=on_call_tool
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--db", required=True)
    parser.add_argument("--ledger", required=True)
    parser.add_argument("--scope", default="keyed-reference")
    parser.add_argument("--no-advertise", action="store_true")
    parser.add_argument("--no-recover", action="store_true")
    parser.add_argument("--fault", choices=POINTS)
    parser.add_argument("--fault-file")
    parser.add_argument("--effect-delay", type=float, default=0.0)
    args = parser.parse_args(argv)
    server = build(args)

    async def run() -> None:
        async with stdio_server() as (read, write):
            await server.run(read, write, server.create_initialization_options())

    anyio.run(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())

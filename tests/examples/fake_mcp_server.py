"""
A scripted MCP server over stdio, for driving the relay byte by byte.

No SDK: newline-delimited JSON-RPC, written by hand, so the tests control
every byte -- including deliberately odd spacing in results, which the
relay must pass through untouched. Every message received is appended to the
log file named in argv[1], so tests can see what reached the server.

Tools (paginated two per page):
  search(query: str, limit: int <= 10)  -- 'flip' tightens limit to <= 5 and
                                            adds a required 'index', then
                                            announces tools/list_changed
  broken(...)                           -- uses allOf: StateGuard cannot read it
  flip()                                -- triggers the change above
"""

import json
import sys
from typing import Any

LOG = open(sys.argv[1], "a", encoding="utf-8")  # noqa: SIM115 -- open for the process lifetime
STATE = {"version": 1}


def tools() -> list[dict[str, Any]]:
    search: dict[str, Any] = {
        "name": "search",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "maximum": 10},
            },
            "required": ["query"],
        },
    }
    if STATE["version"] == 2:
        search["inputSchema"]["properties"]["limit"]["maximum"] = 5
        search["inputSchema"]["properties"]["index"] = {"type": "string"}
        search["inputSchema"]["required"].append("index")
    return [
        search,
        {"name": "broken", "inputSchema": {"type": "object", "allOf": [{"required": ["a"]}]}},
        {"name": "flip", "inputSchema": {"type": "object"}},
    ]


def send(raw: str) -> None:
    sys.stdout.write(raw + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    LOG.write(line)
    LOG.flush()
    message = json.loads(line)
    method, ident = message.get("method"), message.get("id")
    if ident is None:
        continue
    if method == "tools/list":
        cursor = (message.get("params") or {}).get("cursor")
        listing = tools()
        page = listing[2:] if cursor == "p2" else listing[:2]
        result = {"tools": page}
        if cursor != "p2":
            result["nextCursor"] = "p2"
        send(json.dumps({"jsonrpc": "2.0", "id": ident, "result": result}))
    elif method == "tools/call":
        name = message["params"]["name"]
        arguments = message["params"].get("arguments")
        if name == "flip":
            STATE["version"] = 2
            send('{"jsonrpc":"2.0","method":"notifications/tools/list_changed"}')
        # Odd but valid spacing: the relay must not re-serialise results.
        body = json.dumps({"name": name, "arguments": arguments}, sort_keys=True)
        ident_json, body_json = json.dumps(ident), json.dumps(body)
        send(
            f'{{"jsonrpc": "2.0",   "id": {ident_json}, '
            f'"result": {{"content": [{{"type": "text", "text": {body_json}}}]}}}}'
        )
    else:
        send(json.dumps({"jsonrpc": "2.0", "id": ident, "result": {}}))

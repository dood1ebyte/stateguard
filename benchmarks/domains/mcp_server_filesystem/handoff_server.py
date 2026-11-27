"""
A test MCP server whose effect happens in an external system (contract-v2 batch).

usage: handoff_server.py serve  --state DIR --sandbox DIR
       handoff_server.py worker --state DIR

``serve``: a minimal stdio MCP server (``read_text_file``, ``move_file``). On
generation 0 a ``move_file`` is *handed off*: the server writes a job file to
``DIR/spool`` and exits without answering. It never moves anything itself.

``worker``: started by the benchmark, **outside** the server's executor scope
(for example outside its PID namespace). It waits for ``DIR/go``, then
performs the queued move and writes ``DIR/worker_done``. It stands for any
external system -- a queue consumer, a remote API -- that keeps working after
every process of the server has ended. No termination evidence covers it.

Logs (``DIR/events.jsonl``): ``started``, ``queued``, ``worker_moved``; no contents.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

TOOLS = [
    {
        "name": "read_text_file",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "move_file",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["source", "destination"],
        },
    },
]


def event(state: Path, name: str, **fields: Any) -> None:
    with open(state / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": name, **fields}) + "\n")


def serve(state: Path) -> None:
    starts = state / "starts"
    gen = int(starts.read_text()) if starts.exists() else 0
    starts.write_text(str(gen + 1))
    event(state, "started", gen=gen)
    out = sys.stdout

    def send(msg: dict[str, Any]) -> None:
        out.write(json.dumps(msg) + "\n")
        out.flush()

    def text(rid: Any, body: str, error: bool = False) -> None:
        content = [{"type": "text", "text": body}]
        send({"jsonrpc": "2.0", "id": rid, "result": {"content": content, "isError": error}})

    for raw in sys.stdin:
        msg = json.loads(raw)
        rid, method = msg.get("id"), msg.get("method")
        if rid is None:
            continue
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": msg["params"].get("protocolVersion", "2025-11-25"),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "handoff-server", "version": "0"}}})  # fmt: skip
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            name, args = msg["params"]["name"], msg["params"].get("arguments", {})
            if name == "read_text_file":
                p = Path(args["path"])
                if p.is_file():
                    text(rid, p.read_text(encoding="utf-8"))
                else:
                    text(rid, f"ENOENT: no such file or directory, open '{p}'", True)
            elif name == "move_file" and gen == 0:
                spool = state / "spool"
                spool.mkdir(exist_ok=True)
                (spool / "job.json").write_text(json.dumps(args), encoding="utf-8")
                event(state, "queued", gen=gen)
                os._exit(0)  # handed off; no answer
            elif name == "move_file":
                os.rename(args["source"], args["destination"])
                text(rid, "moved")


def worker(state: Path) -> None:
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        job = state / "spool" / "job.json"
        if (state / "go").exists() and job.exists():
            args = json.loads(job.read_text(encoding="utf-8"))
            if Path(args["source"]).exists():
                os.rename(args["source"], args["destination"])
                event(state, "worker_moved")
            (state / "worker_done").touch()
            return
        time.sleep(0.02)  # a file-spool consumer: polling is how it learns of work


if __name__ == "__main__":
    opts = dict(zip(sys.argv[2::2], sys.argv[3::2], strict=False))
    if sys.argv[1] == "serve":
        serve(Path(opts["--state"]))
    else:
        worker(Path(opts["--state"]))

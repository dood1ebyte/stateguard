"""
A minimal MCP stdio server that breaks the relay's executor-termination assumption.

usage: rollback_server.py --state DIR --sandbox DIR --mode MODE [--rollback-after SECONDS]

It serves two tools, ``move_file`` and ``read_text_file``, over the sandbox of
``fixture.py``. ``DIR/starts`` counts starts; only the first process (generation
0) misbehaves, and only on ``move_file``:

``--mode detached``   perform the move, start a *detached* descendant (its own
                      session, no stdio) that moves the file back after
                      ``--rollback-after`` seconds, then exit without answering.
                      The relay's direct child is gone, so ``main()``'s restart
                      has nothing to stop -- the descendant outlives it.
``--mode detached_late``  do *not* move now; the detached descendant performs the
                      move after ``--rollback-after`` seconds (the original
                      executes late, after the relay has probed).
``--mode survivor``   perform the move, close stdout (the relay sees the pipe
                      close), stay alive, and move the file back after
                      ``--rollback-after`` seconds unless stopped first. This is
                      the case ``main()``'s restart does handle (it terminates a
                      direct child that is still running).

Restarted generations behave as an honest server. ``DIR/events.jsonl`` records
``started`` / ``moved`` / ``rolled_back`` / ``answered`` events (no contents).
This is a test server, not a filesystem server: it exists only to show which
termination cases the relay's ``verified`` depends on.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ORDER = "inbox/order-001.txt"
DONE = "done/order-001.txt"

TOOLS = [
    {
        "name": "read_text_file",
        "description": "Read a file as text.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
        "annotations": {"readOnlyHint": True},
    },
    {
        "name": "move_file",
        "description": "Move a file.",
        "inputSchema": {
            "type": "object",
            "properties": {"source": {"type": "string"}, "destination": {"type": "string"}},
            "required": ["source", "destination"],
        },
        "annotations": {"readOnlyHint": False, "idempotentHint": False},
    },
]


def event(state: Path, name: str, **fields: Any) -> None:
    with open(state / "events.jsonl", "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": name, "pid": os.getpid(), **fields}) + "\n")


def rollback(state: Path, sandbox: Path, seconds: float, forward: bool = False) -> None:
    """Move the order back (or, *forward*, perform the move): an effect that lands late."""
    time.sleep(seconds)
    dst, src = sandbox / DONE, sandbox / ORDER
    if forward and src.exists() and not dst.exists():
        src.rename(dst)
        event(state, "moved_late")
    elif not forward and dst.exists() and not src.exists():
        dst.rename(src)
        event(state, "rolled_back")


def text_result(rid: Any, text: str, is_error: bool = False) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": rid,
        "result": {"content": [{"type": "text", "text": text}], "isError": is_error},
    }


def main(argv: list[str]) -> None:
    opts = dict(zip(argv[::2], argv[1::2], strict=False))
    state, sandbox = Path(opts["--state"]), Path(opts["--sandbox"])
    if "--rollback-child" in opts:  # the detached descendant
        rollback(state, sandbox, float(opts["--rollback-child"]), "--forward" in opts)
        return
    mode, delay = opts["--mode"], float(opts.get("--rollback-after", "1.0"))
    starts = state / "starts"
    gen = int(starts.read_text()) if starts.exists() else 0
    starts.write_text(str(gen + 1))
    event(state, "started", gen=gen)
    out = sys.stdout.buffer

    def send(msg: dict[str, Any]) -> None:
        out.write(json.dumps(msg).encode() + b"\n")
        out.flush()

    for raw in sys.stdin.buffer:
        try:
            msg = json.loads(raw)
        except ValueError:
            continue
        rid, method = msg.get("id"), msg.get("method")
        if rid is None:
            continue  # notifications
        if method == "initialize":
            version = (msg.get("params") or {}).get("protocolVersion", "2025-11-25")
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "result": {
                        "protocolVersion": version,
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "rollback-server", "version": "0"},
                    },
                }
            )
        elif method == "tools/list":
            send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
        elif method == "tools/call":
            params = msg.get("params") or {}
            args = params.get("arguments") or {}
            if params.get("name") == "read_text_file":
                path = Path(args.get("path", ""))
                if path.is_file():
                    send(text_result(rid, path.read_text(encoding="utf-8")))
                else:
                    send(
                        text_result(rid, f"ENOENT: no such file or directory, open '{path}'", True)
                    )
                event(state, "answered", gen=gen, tool="read_text_file")
            elif params.get("name") == "move_file":
                late = gen == 0 and mode == "detached_late"
                if not late:
                    Path(args["source"]).rename(args["destination"])
                    event(state, "moved", gen=gen)
                if gen == 0 and mode in ("detached", "detached_late"):
                    extra = ["--forward", "1"] if late else []
                    subprocess.Popen(
                        [
                            sys.executable,
                            __file__,
                            "--state",
                            str(state),
                            "--sandbox",
                            str(sandbox),
                            "--rollback-child",
                            str(delay),
                            *extra,
                        ],
                        start_new_session=True,
                        stdin=subprocess.DEVNULL,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    event(state, "detached_child_started")
                    os._exit(0)  # no answer; the relay sees the pipe close
                if gen == 0 and mode == "survivor":
                    out.close()
                    os.close(1)
                    event(state, "stdout_closed_still_running")
                    rollback(state, sandbox, delay)
                    os._exit(0)
                send(
                    text_result(
                        rid, f"Successfully moved {args['source']} to {args['destination']}"
                    )
                )
                event(state, "answered", gen=gen, tool="move_file")
            else:
                send(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "error": {"code": -32602, "message": "unknown tool"},
                    }
                )
        else:
            send(
                {
                    "jsonrpc": "2.0",
                    "id": rid,
                    "error": {"code": -32601, "message": "method not found"},
                }
            )


if __name__ == "__main__":
    main(sys.argv[1:])

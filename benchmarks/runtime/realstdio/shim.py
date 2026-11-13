"""
TEST-ONLY fault shim for an MCP server over stdio. It runs the server as its child and passes
newline-delimited JSON-RPC both ways; on the first ``tools/call`` of ``--tool`` (once per
``--fault-file``, across restarts) it injects ``--mode``:

  none        pass everything
  kill_before the call is never forwarded; the shim and its server exit (not executed)
  lost_exit   the call runs; its answer is dropped and the shim and server exit (executed,
              answer lost)
  hang        the call runs; its answer is dropped and the shim stays silent (executed, no
              answer)
  transient   the shim answers a JSON-RPC error itself; the call is never forwarded
  slow        the call runs; its answer reaches the client --slow seconds late (a slow answer)

Adversarial modes (the failure taxonomy's J, H, C and B; the call always runs):

  duplicate     its answer is sent twice (J: a duplicate response)
  garbage       its answer is replaced by a line that is not JSON (H: result integrity)
  truncated     its answer is cut off half-way (H: not valid JSON)
  nonconformant its answer says "jsonrpc": "1.0" (C: protocol conformance)
  stale_listing the first two tools/list answers show --tool with its first input property
                capitalised -- a schema the server no longer serves (B: a stale listing). With
                a relay those are its own listing and the client's; later listings are true.

``--log`` gets ``call <tool>`` for each call that reached the server, and ``fault <mode>``.

    shim.py --mode M --tool T --fault-file F [--log L] -- SERVER ARGV...
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import IO, Any


def _log(path: Path | None, line: str) -> None:
    if path is not None:
        with path.open("a") as out:
            out.write(line + "\n")


def main() -> int:
    split = sys.argv.index("--")
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", default="none")
    parser.add_argument("--tool", default="")
    parser.add_argument("--fault-file")
    parser.add_argument("--log")
    parser.add_argument("--slow", type=float, default=5.0)
    args = parser.parse_args(sys.argv[1:split])
    server_argv = sys.argv[split + 1 :]
    log = Path(args.log) if args.log else None
    flag = Path(args.fault_file) if args.fault_file else None
    child = subprocess.Popen(server_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    out_lock = threading.Lock()
    watched: set[str] = set()
    stale: set[str] = set()  # ids of tools/list requests answered with the stale schema
    listings = [0]
    # Once per fault file: a restarted shim (the file exists) serves true listings.
    stale_armed = args.mode == "stale_listing" and flag is not None and not flag.exists()

    def write(stream: IO[bytes], line: bytes) -> None:
        with out_lock:
            stream.write(line if line.endswith(b"\n") else line + b"\n")
            stream.flush()

    def die() -> None:
        child.kill()
        os._exit(1)

    def fires(message: Any) -> bool:
        if args.mode == "none" or flag is None or flag.exists():
            return False
        params = message.get("params") if isinstance(message, dict) else None
        if not (
            isinstance(params, dict)
            and message.get("method") == "tools/call"
            and params.get("name") == args.tool
        ):
            return False
        flag.write_text(args.mode)
        _log(log, f"fault {args.mode}")
        return True

    def from_server() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            key = json.dumps(message.get("id")) if isinstance(message, dict) else None
            if key is not None and key in stale and "method" not in message:
                stale.discard(key)
                write(sys.stdout.buffer, json.dumps(_stale(message, args.tool)).encode())
                continue
            if key is not None and key in watched and "method" not in message:
                watched.discard(key)
                if args.mode == "lost_exit":
                    die()
                if args.mode == "slow":
                    threading.Timer(args.slow, write, (sys.stdout.buffer, line)).start()
                if args.mode == "duplicate":
                    write(sys.stdout.buffer, line)
                    write(sys.stdout.buffer, line)
                if args.mode == "garbage":
                    write(sys.stdout.buffer, b"<<TEST-ONLY: an answer that is not JSON>>")
                if args.mode == "truncated":
                    write(sys.stdout.buffer, line.rstrip(b"\n")[: max(1, len(line) // 2)])
                if args.mode == "nonconformant":
                    write(sys.stdout.buffer, json.dumps({**message, "jsonrpc": "1.0"}).encode())
                continue  # hang: the answer is dropped, and nothing more is said about it
            write(sys.stdout.buffer, line)
        os._exit(child.wait())

    threading.Thread(target=from_server, daemon=True).start()
    for line in sys.stdin.buffer:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        is_call = isinstance(message, dict) and message.get("method") == "tools/call"
        name = message.get("params", {}).get("name") if is_call else None
        if stale_armed and isinstance(message, dict) and message.get("method") == "tools/list":
            listings[0] += 1
            if listings[0] <= 2 and flag is not None:
                stale.add(json.dumps(message.get("id")))
                flag.write_text("stale_listing")
                _log(log, "fault stale_listing")
        if is_call and fires(message):
            if args.mode == "kill_before":
                die()
            if args.mode == "transient":
                error = {"code": -32603, "message": "transient failure (TEST-ONLY fault)"}
                write(
                    sys.stdout.buffer,
                    json.dumps(
                        {"jsonrpc": "2.0", "id": message.get("id"), "error": error}
                    ).encode(),
                )
                continue
            watched.add(json.dumps(message.get("id")))
        if is_call:
            _log(log, f"call {name}")
        write(child.stdin, line)
    child.stdin.close()
    return child.wait()


def _stale(message: dict[str, Any], tool: str) -> dict[str, Any]:
    """The listing with *tool*'s first input property capitalised (a schema since changed)."""
    result = message.get("result")
    tools = result.get("tools") if isinstance(result, dict) else None
    for entry in tools if isinstance(tools, list) else []:
        schema = entry.get("inputSchema") if isinstance(entry, dict) else None
        properties = schema.get("properties") if isinstance(schema, dict) else None
        if entry.get("name") != tool or not isinstance(properties, dict) or not properties:
            continue
        first = next(iter(properties))
        renamed = first[:1].upper() + first[1:]
        schema["properties"] = {
            (renamed if name == first else name): value for name, value in properties.items()
        }
        if isinstance(schema.get("required"), list):
            schema["required"] = [renamed if r == first else r for r in schema["required"]]
    return message


if __name__ == "__main__":
    sys.exit(main())

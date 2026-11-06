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
    args = parser.parse_args(sys.argv[1:split])
    server_argv = sys.argv[split + 1 :]
    log = Path(args.log) if args.log else None
    flag = Path(args.fault_file) if args.fault_file else None
    child = subprocess.Popen(server_argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    out_lock = threading.Lock()
    watched: set[str] = set()

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
            if key is not None and key in watched and "method" not in message:
                watched.discard(key)
                if args.mode == "lost_exit":
                    die()
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
        if is_call and fires(message):
            if args.mode == "kill_before":
                die()
            if args.mode == "transient":
                error = {"code": -32603, "message": "transient failure (TEST-ONLY fault)"}
                write(sys.stdout.buffer, json.dumps(
                    {"jsonrpc": "2.0", "id": message.get("id"), "error": error}
                ).encode())
                continue
            watched.add(json.dumps(message.get("id")))
        if is_call:
            _log(log, f"call {name}")
        write(child.stdin, line)
    child.stdin.close()
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())

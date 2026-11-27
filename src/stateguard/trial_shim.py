"""
TEST-ONLY fault shim for ``stateguard trial --fault`` (labelled faults, never in production).

    python -m stateguard.trial_shim --mode kill_before|lost_answer --tool TOOL --flag FILE \\
        -- SERVER COMMAND...

It runs the server as its child and passes newline-delimited JSON-RPC both ways. On the first
``tools/call`` of TOOL (once per FLAG file, across restarts) it injects MODE:

- ``kill_before``: the call is never forwarded; the shim and the server exit;
- ``lost_answer``: the call is forwarded and runs; its answer is dropped; the shim and the
  server exit.

``stateguard trial`` uses it only against a sandbox copy of the server's data.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any


def _fires(flag: Path, mode: str) -> bool:
    if flag.exists():
        return False
    flag.write_text(mode)
    return True


def _is_call(message: Any, tool: str) -> bool:
    return (
        isinstance(message, dict)
        and message.get("method") == "tools/call"
        and isinstance(message.get("params"), dict)
        and message["params"].get("name") == tool
    )


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    split = argv.index("--")
    parser = argparse.ArgumentParser(prog="stateguard.trial_shim")
    parser.add_argument("--mode", choices=["kill_before", "lost_answer"], required=True)
    parser.add_argument("--tool", required=True)
    parser.add_argument("--flag", required=True)
    args = parser.parse_args(argv[:split])
    flag = Path(args.flag)
    child = subprocess.Popen(argv[split + 1 :], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert child.stdin is not None and child.stdout is not None
    lost: set[Any] = set()  # ids whose answer is dropped (lost_answer)
    done = threading.Event()

    def from_server() -> None:
        assert child.stdout is not None
        for line in child.stdout:
            try:
                message = json.loads(line)
            except ValueError:
                message = None
            if isinstance(message, dict) and message.get("id") in lost and "method" not in message:
                child.kill()
                os._exit(0)  # the answer is lost with the process
            sys.stdout.buffer.write(line)
            sys.stdout.buffer.flush()
        done.set()

    threading.Thread(target=from_server, daemon=True).start()
    for line in sys.stdin.buffer:
        try:
            message = json.loads(line)
        except ValueError:
            message = None
        if _is_call(message, args.tool) and _fires(flag, args.mode):
            if args.mode == "kill_before":
                child.kill()
                os._exit(0)
            lost.add(message["id"])
        child.stdin.write(line)
        child.stdin.flush()
    child.stdin.close()
    done.wait(timeout=30)
    return child.wait()


if __name__ == "__main__":
    sys.exit(main())

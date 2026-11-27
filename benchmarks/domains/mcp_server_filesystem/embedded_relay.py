"""
Reference embedding of ``StdioRelay`` with an explicit executor scope (benchmark only).

usage: embedded_relay.py --scope direct-child|pid-namespace [--audit FILE]
                         [--effects FILE] [--restart N] [--call-deadline S]
                         [--shadow] -- SERVER ARGS...

``examples/mcp/stdio_proxy.py main()`` refuses ``reconcile`` blocks, because a
reaped direct child does not show that detached descendants stopped. This
script is what an *embedder* must do instead: give each server generation an
executor identity, and return ``Restarted(..., termination, executor)`` from its
restart factory. It is a test and benchmark harness, not a supported product
configuration; it exists to measure the reconciliation contract honestly.

Scopes:

``direct-child``   what ``main()`` can see. The old process is stopped and
                   reaped; the evidence is ``direct_child_reaped`` -- which does
                   **not** establish termination, so nothing is ever probed.
``pid-namespace``  Linux only, needs permission to create a PID namespace. The
                   server runs as init of a new PID namespace
                   (``unshare --pid --fork --kill-child``). When init exits the
                   kernel kills every other process in that namespace,
                   including descendants that called ``setsid``. On restart
                   the embedder stops init (if still running), then checks
                   ``/proc`` until no process is left in that namespace (bounded:
                   ``EMPTY_WAIT_SECONDS``). Only then does it assert
                   ``scope_ended`` naming the old executor. This covers
                   processes; it does **not** cover external systems the server
                   handed work to.

                   Liveness note: ``unshare`` keeps the server's stdout pipe
                   open, so a server that closes stdout but keeps running is
                   only seen as lost when it exits (the audit's T3 case).
                   That delays reconciliation; it does not weaken it.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import IO, Any

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "benchmarks"))

from runtime.harness import load_relay_module  # noqa: E402

from stateguard import ContractGuard, GuardConfig  # noqa: E402
from stateguard.adapters.mcp import (  # noqa: E402
    EffectPolicy,
    ReconcilePolicy,
    TerminationEvidence,
)
from stateguard.core.models.config import RepairMode  # noqa: E402

#: How long the embedder waits for the kernel to empty a PID namespace.
EMPTY_WAIT_SECONDS = 5.0
#: How long it waits for ``unshare`` to start the namespace's init.
INIT_WAIT_SECONDS = 5.0


def _children(pid: int) -> list[int]:
    out = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        with contextlib.suppress(OSError):
            fields = (entry / "stat").read_text().rsplit(")", 1)[1].split()
            if int(fields[1]) == pid:
                out.append(int(entry.name))
    return out


def _members(namespace: str) -> list[int]:
    """Processes whose PID namespace is *namespace* (zombies cannot be read and cannot act)."""
    out = []
    for entry in Path("/proc").iterdir():
        if entry.name.isdigit():
            with contextlib.suppress(OSError):
                if os.readlink(entry / "ns" / "pid") == namespace:
                    out.append(int(entry.name))
    return out


class Executor:
    """One server generation: its process, its identity and how to end it."""

    _counter = itertools.count()

    def __init__(self, scope: str, command: list[str]) -> None:
        self.scope = scope
        argv = ["unshare", "--pid", "--fork", "--kill-child", "--", *command]
        self.proc = subprocess.Popen(
            argv if scope == "pid-namespace" else command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        self.namespace: str | None = None
        n = next(self._counter)
        if scope == "pid-namespace":
            deadline = time.monotonic() + INIT_WAIT_SECONDS
            while self.namespace is None and time.monotonic() < deadline:
                for child in _children(self.proc.pid):
                    with contextlib.suppress(OSError):
                        self.namespace = os.readlink(f"/proc/{child}/ns/pid")
                if self.namespace is None:
                    if self.proc.poll() is not None:
                        break
                    time.sleep(0.01)  # polling /proc: there is no event for "init started"
        # Unique per generation even if the kernel reuses a namespace inode.
        self.identity: str | None = (
            f"pidns-{self.namespace.strip('pid:[]')}-{self.proc.pid}-{n}"
            if self.namespace
            else (f"child-{self.proc.pid}-{n}" if scope == "direct-child" else None)
        )

    @property
    def pipes(self) -> tuple[IO[bytes], IO[bytes]]:
        assert self.proc.stdin is not None and self.proc.stdout is not None
        return self.proc.stdin, self.proc.stdout

    def end(self) -> TerminationEvidence:
        """Stop this generation and report exactly what was established."""
        if self.proc.poll() is None:
            if self.scope == "pid-namespace":
                # unshare --kill-child: init gets SIGKILL, then the whole namespace does.
                self.proc.kill()
            else:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
        self.proc.wait()
        if self.scope != "pid-namespace" or self.namespace is None or self.identity is None:
            return TerminationEvidence.direct_child_reaped(self.proc.returncode)
        deadline = time.monotonic() + EMPTY_WAIT_SECONDS
        while _members(self.namespace):
            if time.monotonic() > deadline:
                return TerminationEvidence.direct_child_reaped(self.proc.returncode)
            time.sleep(0.01)  # the kernel empties the namespace asynchronously
        return TerminationEvidence.scope_ended(
            f"linux pid namespace {self.namespace} empty after its init exited",
            executor=self.identity,
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["direct-child", "pid-namespace"], required=True)
    parser.add_argument("--audit")
    parser.add_argument("--effects")
    parser.add_argument("--restart", type=int, default=2)
    parser.add_argument("--call-deadline", type=float)
    parser.add_argument("--shadow", action="store_true")
    parser.add_argument("server", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.server[1:] if args.server[:1] == ["--"] else args.server
    relay_module = load_relay_module()
    effects = EffectPolicy.from_file(args.effects) if args.effects else None
    reconcile = (
        ReconcilePolicy.from_file(args.effects, effects) if args.effects and effects else None
    )
    audit: Any = open(args.audit, "a", encoding="utf-8") if args.audit else sys.stderr  # noqa: SIM115
    executors = [Executor(args.scope, command)]

    def restart() -> Any:
        evidence = executors[-1].end()
        new = Executor(args.scope, command)
        executors.append(new)
        return relay_module.Restarted(*new.pipes, evidence, new.identity)

    mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
    relay = relay_module.StdioRelay(
        ContractGuard.with_mcp(config=GuardConfig(mode=mode)),
        sys.stdin.buffer,
        sys.stdout.buffer,
        *executors[0].pipes,
        audit,
        call_deadline=args.call_deadline,
        effects=effects,
        retry="off",
        restart=restart,
        max_restarts=args.restart,
        restart_policy="after-unknown",
        reconcile=reconcile,
        executor=executors[0].identity,
    )
    relay._log("relay_started", mode=mode.value, embedded_scope=args.scope)
    threading.Thread(target=relay.pump_client, daemon=True).start()
    threading.Thread(target=relay.run_deadlines, daemon=True).start()
    relay.pump_server()
    for executor in executors:
        if executor.proc.poll() is None:
            executor.end()
    return executors[-1].proc.returncode or 0


if __name__ == "__main__":
    code = main()
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)

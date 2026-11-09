"""
Per-call overhead of a running stdio relay (TEST-ONLY harness). P1 measured whole sessions,
relay start included; here one session makes many calls, so the relay's start is paid once and
each call's latency is its own.

For each round, the arms run one after another (raw, sg, sg_keyed), each as one SDK stdio
session: initialize, list tools, then CALLS calls alternating the read and the write. No fault
is injected; the shim passes everything (mode none), so every arm has the same path but the
relay. Configuration is ``driver.py``'s (servers, effects, relay flags, keys).

Rows (stdout, JSONL): one per call: server, arm, round, i, op, latency_s, ok. A session that
fails is one row with ``"session_error"``; it is never dropped.

usage: per_call.py SERVER ROUNDS CALLS [arms]
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

sys.path.insert(0, str(Path(__file__).resolve().parent))
from driver import PY, READ_TOOL, RELAY, RUNS, SHIM, WRITE_TOOL, setup  # noqa: E402

ARMS = ["raw", "sg", "sg_keyed"]


def _command(server: str, arm: str, base: Path, cmd: list[str]) -> list[str]:
    command = [
        PY,
        SHIM,
        "--mode",
        "none",
        "--tool",
        WRITE_TOOL[server],
        "--fault-file",
        str(base / "fault_flag"),
        "--",
        *cmd,
    ]
    if arm.startswith("sg"):
        effects = base / "effects.json"
        effects.write_text(
            json.dumps(
                {
                    "tools": [
                        {"name": WRITE_TOOL[server], "effect": "non_idempotent"},
                        {"name": READ_TOOL[server], "effect": "read_only"},
                    ]
                }
            )
        )
        command = [
            PY,
            RELAY,
            "--retry",
            "auto",
            "--restart",
            "2",
            "--restart-policy",
            "after-unknown",
            "--effects",
            str(effects),
            "--trust-read-only-hints",
            "--call-deadline",
            "2",
            "--audit",
            str(base / "audit.jsonl"),
            "--",
            *command,
        ]
    return command


def _write_args(server: str, marker: str) -> dict:
    if server == "sqlite":
        return {"query": f"INSERT INTO notes VALUES ('{marker}')"}
    return {"observations": [{"entityName": "seed", "contents": [marker]}]}


async def _session(
    command: list[str],
    env: dict[str, str],
    server: str,
    read: tuple,
    calls: int,
    rows_out: list[dict],
    meta: dict,
) -> None:
    params = StdioServerParameters(command=command[0], args=command[1:], env=env)
    async with stdio_client(params) as (r, w), ClientSession(r, w) as session:
        with anyio.fail_after(30):
            await session.initialize()
            await session.list_tools()
        for i in range(calls):
            op = "read" if i % 2 == 0 else "write"
            name, args = (
                (read[0], read[1])
                if op == "read"
                else (WRITE_TOOL[server], _write_args(server, f"{meta['round']}-{meta['arm']}-{i}"))
            )
            t0 = time.perf_counter()
            with anyio.fail_after(20):
                result = await session.call_tool(name, args)
            latency = time.perf_counter() - t0
            rows_out.append(
                {
                    **meta,
                    "i": i,
                    "op": op,
                    "latency_s": round(latency, 5),
                    "ok": not result.is_error,
                }
            )


def run(server: str, arm: str, rnd: int, calls: int) -> list[dict]:
    base = RUNS / f"percall-{server}-{arm}-{rnd}-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    cmd, env, read, _write, _marker = setup(server, base, rnd, arm, "read", "none")
    meta = {"server": server, "arm": arm, "round": rnd}
    rows: list[dict] = []
    try:
        anyio.run(_session, _command(server, arm, base, cmd), env, server, read, calls, rows, meta)
    except BaseException as exc:  # a failed session is a row, never dropped
        if isinstance(exc, KeyboardInterrupt):
            raise
        rows.append(
            {
                **meta,
                "session_error": f"{type(exc).__name__}: {str(exc)[:200]}",
                "calls_done": len(rows),
            }
        )
    shutil.rmtree(base, ignore_errors=True)
    return rows


def main() -> None:
    server, rounds, calls = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
    arms = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else ARMS
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    for rnd in range(offset, offset + rounds):
        for arm in arms:
            for row in run(server, arm, rnd, calls):
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

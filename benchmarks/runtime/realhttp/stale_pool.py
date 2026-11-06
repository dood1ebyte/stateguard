"""
TEST-ONLY experiment: the stale pooled connection race between the HTTP relay and a server.

One client session (the official Python SDK) makes N ``add_note`` calls through the relay,
each after an idle gap drawn from [--gap-lo, --gap-hi] seconds, near the server's own
keep-alive timeout (uvicorn: 5 s). Between the relay and the server, ``delay_proxy.py`` adds a
one-way delay, as a network path does. If the relay reuses a pooled connection the server is
closing, the request is lost unread, and the relay cannot tell that from a crash after
execution.

    stale_pool.py ARM N [--pool-idle S] [--keyed] [--delay-ms D] [--seed K] -> JSONL rows

Each row is one call, scored against ground truth (how often its note is in the server's file)
and the relay's own ``invocation`` record. Code under test: ``STATEGUARD_CODE`` (default: this
checkout).
"""

import argparse
import json
import os
import random
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

import anyio
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[2]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
STATE = "io.stateguard/executionState"
OUTCOME = "io.stateguard/idempotencyOutcome"


def _port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait(port: int) -> None:
    deadline = time.monotonic() + 20
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.05)
    raise RuntimeError(f"nothing listening on {port}")


async def _calls(url: str, texts: list[str], gaps: list[float], wait: float) -> list[dict]:
    seen = []
    async with streamable_http_client(url) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        await session.list_tools()
        for text, gap in zip(texts, gaps, strict=True):
            await anyio.sleep(gap)
            t0 = time.monotonic()
            try:
                with anyio.fail_after(wait):
                    result = await session.call_tool("add_note", {"text": text})
                answer = {
                    "status": "result",
                    "is_error": bool(result.is_error),
                    "outcome": (result.meta or {}).get(OUTCOME),
                }
            except MCPError as exc:
                data = exc.data if isinstance(exc.data, dict) else {}
                answer = {"status": "mcp_error", "code": exc.code, "state": data.get(STATE)}
            except TimeoutError:
                answer = {"status": "timeout"}
            answer["latency_s"] = round(time.monotonic() - t0, 3)
            seen.append(answer)
    return seen


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("arm")
    p.add_argument("n", type=int)
    p.add_argument("--pool-idle", type=float, default=None, help="relay --pool-idle-timeout")
    p.add_argument("--keyed", action="store_true")
    p.add_argument("--delay-ms", type=float, default=40.0)
    p.add_argument("--gap-lo", type=float, default=4.8)
    p.add_argument("--gap-hi", type=float, default=5.1)
    p.add_argument("--seed", type=int, default=1)
    p.add_argument("--wait", type=float, default=15.0)
    a = p.parse_args()
    rng = random.Random(a.seed)
    base = Path(tempfile.mkdtemp(prefix=f"stale-{a.arm}-"))
    notes = base / "notes.txt"
    notes.write_text("")
    env = {**os.environ, "PYTHONPATH": str(CODE / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    server_port, proxy_port, relay_port = _port(), _port(), _port()
    argv = [str(CODE / "tests/examples/http_notes_server.py"), str(notes), str(server_port)]
    if a.keyed:
        env.update(
            STATEGUARD_IDEMPOTENCY_DB=str(base / "keys.db"),
            STATEGUARD_IDEMPOTENCY_SCOPE="notes",
            STATEGUARD_KEYED_TOOLS="add_note",
        )
        argv = ["-m", "stateguard.adapters.mcp.inject", *argv]
    effects = base / "effects.json"
    effects.write_text(json.dumps({"tools": [{"name": "add_note", "effect": "non_idempotent"}]}))
    pool = ["--pool-idle-timeout", str(a.pool_idle)] if a.pool_idle is not None else []
    procs = []
    try:
        procs.append(
            subprocess.Popen(
                [PY, *argv], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
        )
        _wait(server_port)
        procs.append(
            subprocess.Popen(
                [
                    PY,
                    str(CODE / "benchmarks/runtime/delay_proxy.py"),
                    "--listen",
                    f"127.0.0.1:{proxy_port}",
                    "--upstream",
                    f"127.0.0.1:{server_port}",
                    "--delay-ms",
                    str(a.delay_ms),
                    "--log",
                    str(base / "accepts.log"),
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        _wait(proxy_port)
        procs.append(
            subprocess.Popen(
                [
                    PY,
                    str(CODE / "examples/mcp/http_proxy.py"),
                    "--listen",
                    f"127.0.0.1:{relay_port}",
                    "--retry",
                    "auto",
                    "--effects",
                    str(effects),
                    *pool,
                    "--audit",
                    str(base / "audit.jsonl"),
                    "--",
                    f"http://127.0.0.1:{proxy_port}/mcp",
                ],
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        )
        _wait(relay_port)
        texts = [f"s{i}-{uuid.uuid4().hex[:8]}" for i in range(a.n)]
        gaps = [rng.uniform(a.gap_lo, a.gap_hi) for _ in range(a.n)]
        seen = anyio.run(_calls, f"http://127.0.0.1:{relay_port}/mcp", texts, gaps, a.wait)
        time.sleep(0.5)
    finally:
        for proc in reversed(procs):
            proc.kill()
            proc.wait(timeout=10)
    written = notes.read_text().split("\n")
    audit = [json.loads(line) for line in (base / "audit.jsonl").read_text().splitlines() if line]
    records = [
        e for e in audit if e.get("event") == "invocation" and e.get("method") == "tools/call"
    ]
    log = base / "accepts.log"
    accepts = log.read_text().count("accept") if log.exists() else 0
    for i, (text, gap, answer) in enumerate(zip(texts, gaps, seen, strict=True)):
        count = written.count(text)
        told_ok = answer["status"] == "result" and not answer["is_error"]
        record = records[i] if i < len(records) else None
        print(
            json.dumps(
                {
                    "arm": a.arm,
                    "i": i,
                    "gap_s": round(gap, 3),
                    "pool_idle": a.pool_idle,
                    "keyed": a.keyed,
                    "delay_ms": a.delay_ms,
                    "count": count,
                    "told_ok": told_ok,
                    "success": told_ok and count == 1,
                    "duplicate": count > 1,
                    "false_success": told_ok and count == 0,
                    "answer": answer,
                    "relay_faults": record["faults"] if record else None,
                    "relay_resolution": record["resolution"] if record else None,
                    "relay_attempts": record["attempts"] if record else None,
                }
            ),
            flush=True,
        )
    print(
        json.dumps(
            {"arm": a.arm, "summary": True, "proxy_connections": accepts, "calls": len(seen)}
        ),
        flush=True,
    )
    shutil.rmtree(base, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

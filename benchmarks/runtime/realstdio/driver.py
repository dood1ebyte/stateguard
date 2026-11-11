"""
Strict per-invocation evaluation of StateGuard over stdio, on pinned real servers (TEST-ONLY).

One row is one logical invocation: one client session (the official Python SDK's stdio client),
one ``tools/call``, at most one injected fault. Rows have the realhttp schema, so
``benchmarks/runtime/realhttp/summarize.py`` scores them.

Servers (reported separately):
  sqlite   mcp-server-sqlite 2025.4.25 (Python, mcp 1.9.4) in MCP1_PYTHON's environment;
           keys via ``python -m stateguard.adapters.mcp.inject``.
           read: read_query (SELECT); write: write_query (INSERT), ground truth = rows.
  memory   @modelcontextprotocol/server-memory 2026.8.31 (TypeScript, in MEMORY_JS's tree);
           keys via ``node --import adapters/typescript/src/preload.ts``.
           read: search_nodes; write: add_observations, ground truth = the memory file.
           The tool drops an observation it already has, so a resend cannot duplicate there.

Arms:
  raw        client -> shim -> server
  naive      raw, plus a blind resend on a new session after any failure (no key)
  sg         client -> stdio relay (--retry auto, --restart 2 after-unknown, the write declared
             non_idempotent and the read read_only, --call-deadline 2) -> shim -> server
  sg_keyed   sg, and the server keyed (injector / preload)
  sg_keyed_idempotent  sg_keyed, the write also declared idempotent to the keyed server, with an
             exclusive store (STATEGUARD_IDEMPOTENT_TOOLS): memory only -- add_observations
             drops an observation it already has; sqlite's INSERT is not idempotent

Faults (TEST-ONLY): transport faults from ``shim.py`` (kill_before, lost_exit, hang, transient)
and crash points inside the server (STATEGUARD_FAULT: after_begin, after_effect, after_record;
without keys the same points wrap the unmodified handler).

usage: driver.py SERVER N [cells] [arms] [workers] -> JSONL rows on stdout
"""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from stateguard.adapters.mcp.incidence import read_records

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[2]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
MCP1 = os.environ.get("MCP1_PYTHON", "/tmp/mcp1/bin/python")
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
MEMORY_JS = os.environ.get(
    "MEMORY_JS",
    "/opt/sg-servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js",
)
RELAY = str(CODE / "examples/mcp/stdio_proxy.py")
SHIM = str(HERE / "shim.py")
PRELOAD = str(CODE / "adapters/typescript/src/preload.ts")
CLIENT = str(HERE / "client.py")
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/realstdio-runs"))
FAILING = Path(os.environ["FAILING_DIR"]) if os.environ.get("FAILING_DIR") else None
OUTCOME = "io.stateguard/idempotencyOutcome"
STATE = "io.stateguard/executionState"
TRANSPORT = ["kill_before", "lost_exit", "hang", "transient"]
CRASH = ["after_begin", "after_effect", "after_record"]
CELLS = {
    s: ["read/none", "write/none"]
    + [f"{op}/{m}" for op in ("read", "write") for m in TRANSPORT]
    + [f"write/{m}" for m in CRASH]
    for s in ("sqlite", "memory")
}
ARMS = {s: ["raw", "naive", "sg", "sg_keyed"] for s in ("sqlite", "memory")}
WRITE_TOOL = {"sqlite": "write_query", "memory": "add_observations"}
READ_TOOL = {"sqlite": "read_query", "memory": "search_nodes"}
CLIENT_WAIT = 8.0


def setup(server: str, base: Path, t: int, arm: str, op: str, mode: str) -> tuple:
    env = {**os.environ, "PYTHONPATH": str(CODE / "src"), "PYTHONDONTWRITEBYTECODE": "1"}
    marker = f"w-{t}-{uuid.uuid4().hex[:6]}"
    keyed = arm in ("sg_keyed", "sg_keyed_idempotent")
    if keyed:
        env.update(STATEGUARD_IDEMPOTENCY_DB=str(base / "keys.db"),
                   STATEGUARD_IDEMPOTENCY_SCOPE=server, STATEGUARD_KEYED_TOOLS=WRITE_TOOL[server])
    if arm == "sg_keyed_idempotent":
        if server != "memory":
            raise ValueError("sg_keyed_idempotent: only memory's write is idempotent")
        env.update(STATEGUARD_IDEMPOTENCY_EXCLUSIVE="1", STATEGUARD_IDEMPOTENT_TOOLS=WRITE_TOOL[server])
    if mode in CRASH:
        env.update(STATEGUARD_FAULT=mode, STATEGUARD_FAULT_FILE=str(base / "crash.fired"),
                   STATEGUARD_FAULT_TOOLS=WRITE_TOOL[server])
    if server == "sqlite":
        db = base / "app.db"
        with sqlite3.connect(db) as conn:
            conn.execute("CREATE TABLE notes (text TEXT)")
            conn.execute("INSERT INTO notes VALUES (?)", (f"seed-{t}",))
        inner = ["mcp-server-sqlite", "--db-path", str(db)]
        if keyed or mode in CRASH:
            cmd = [MCP1, "-m", "stateguard.adapters.mcp.inject", *inner]
        else:
            cmd = [str(Path(MCP1).parent / "mcp-server-sqlite"), "--db-path", str(db)]
        read = ("read_query", {"query": "SELECT text FROM notes"}, f"seed-{t}")
        write = ("write_query", {"query": f"INSERT INTO notes VALUES ('{marker}')"})
    else:
        memory = base / "memory.jsonl"
        memory.write_text(json.dumps({"type": "entity", "name": "seed", "entityType": "test",
                                      "observations": [f"seed-{t}"]}) + "\n")
        env["MEMORY_FILE_PATH"] = str(memory)
        cmd = [NODE, "--no-warnings"]
        if keyed or mode in CRASH:
            cmd += ["--import", PRELOAD]
        cmd.append(MEMORY_JS)
        read = ("search_nodes", {"query": "seed"}, f"seed-{t}")
        write = ("add_observations",
                 {"observations": [{"entityName": "seed", "contents": [marker]}]})
    return cmd, env, read, write, marker


def truth_count(server: str, base: Path, marker: str) -> int:
    if server == "sqlite":
        with sqlite3.connect(base / "app.db") as conn:
            return conn.execute("SELECT COUNT(*) FROM notes WHERE text = ?", (marker,)).fetchone()[0]
    text = (base / "memory.jsonl").read_text() if (base / "memory.jsonl").exists() else ""
    return text.count(json.dumps(marker))


def run(server: str, cell: str, arm: str, t: int) -> dict:
    op, mode = cell.split("/")
    base = RUNS / f"{server}-{op}-{mode}-{arm}-{t}-{uuid.uuid4().hex[:10]}"
    base.mkdir(parents=True)
    cmd, env, read, write, marker = setup(server, base, t, arm, op, mode)
    name, args = (read[0], read[1]) if op == "read" else write
    shim_mode = mode if mode in TRANSPORT else "none"
    command = [PY, SHIM, "--mode", shim_mode, "--tool", name, "--fault-file",
               str(base / "fault_flag"), "--log", str(base / "shim.log"), "--", *cmd]
    if arm.startswith("sg"):
        effects = base / "effects.json"
        # As an operator declares them: the stdio relay re-requests only a declared read (a
        # server's readOnlyHint is never a declaration), and never resends the write.
        effects.write_text(json.dumps({"tools": [
            {"name": WRITE_TOOL[server], "effect": "non_idempotent"},
            {"name": READ_TOOL[server], "effect": "read_only"},
        ]}))
        command = [PY, RELAY, "--retry", "auto", "--restart", "2", "--restart-policy",
                   "after-unknown", "--effects", str(effects), "--trust-read-only-hints",
                   "--call-deadline", "2", "--audit", str(base / "audit.jsonl"), "--", *command]
    client = [PY, CLIENT, json.dumps(command), name, json.dumps(args), "--wait", str(CLIENT_WAIT)]
    if arm == "naive":
        client.append("--naive")
    t0 = time.monotonic()
    with open(base / "client.err", "wb") as err:
        done = subprocess.run(client, capture_output=False, stdout=subprocess.PIPE, stderr=err,
                              env=env, timeout=120, check=False)
    latency = time.monotonic() - t0
    seen = json.loads(done.stdout.decode().strip().splitlines()[-1])
    final = seen["final"]
    told_ok = final["status"] == "result" and not final["is_error"]
    data = final.get("data") or {}
    unknown = data.get(STATE) == "unknown"
    kinds = []
    if final["status"] in ("timeout", "crash"):
        kinds.append("no_answer_" + final["status"])
    elif unknown:
        kinds.append("surfaced_unknown")
    elif data.get(STATE) == "not_sent":
        kinds.append("surfaced_not_sent")
    elif not told_ok:
        kinds.append("error_answer")
    if op == "read":
        right = told_ok and read[2] in final.get("text", "")
        if told_ok and not right:
            kinds.append("wrong_content")
        success, count = bool(right), None
    else:
        count = truth_count(server, base, marker)
        if count > 1:
            kinds.append("duplicate_effect")
        if count == 0 and told_ok:
            kinds.append("false_success")
        if count >= 1 and not told_ok and not unknown and final["status"] != "timeout" and not (
            final["status"] == "mcp_error" and final.get("code") in (-32010, -32011)
        ):
            kinds.append("false_failure")
        if count == 0:
            kinds.append("not_executed")
        success = count == 1 and told_ok
    audit = []
    if (base / "audit.jsonl").exists():
        audit = [json.loads(x) for x in (base / "audit.jsonl").read_text().splitlines() if x.strip()]
    shim_log = (base / "shim.log").read_text().splitlines() if (base / "shim.log").exists() else []
    # Finished records, and an ``unfinished`` one for each request the relay never finished.
    records = [e for e in (read_records([base / "audit.jsonl"]) if audit else [])
               if e.get("method") == "tools/call"]
    row = {
        "server": server, "op": op, "tool": name, "mode": mode,
        "family": "crash" if mode in CRASH else "transport", "arm": arm, "trial": t,
        "run": base.name, "success": success, "kinds": kinds, "latency_s": round(latency, 3),
        "count": count, "told_ok": told_ok,
        "server_outcome": (final.get("meta") or {}).get(OUTCOME) if final["status"] == "result" else None,
        "first_status": seen["first"]["status"], "client_retried": seen["retried"],
        "fault_fired": (base / "fault_flag").exists() or (base / "crash.fired").exists(),
        "server_calls_of_target": sum(1 for x in shim_log if x == f"call {name}"),
        "relay_events": sorted({e["event"] for e in audit}),
        "relay_invocation": records[-1] if records else None,
        "final": final,
    }
    if not success and FAILING is not None:
        FAILING.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, FAILING / base.name)
    shutil.rmtree(base, ignore_errors=True)
    return row


def main() -> None:
    server, n = sys.argv[1], int(sys.argv[2])
    cells = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else CELLS[server]
    arms = sys.argv[4].split(",") if len(sys.argv) > 4 and sys.argv[4] else ARMS[server]
    workers = int(sys.argv[5]) if len(sys.argv) > 5 else 4
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    jobs = [(cell, arm, t) for t in range(offset, offset + n) for cell in cells for arm in arms]
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, server, *job): job for job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                row = fut.result()
            except Exception as exc:  # a driver error is a recorded row, never dropped
                cell, arm, t = futures[fut]
                op, mode = cell.split("/")
                row = {"server": server, "op": op, "mode": mode, "arm": arm, "trial": t,
                       "family": "crash" if mode in CRASH else "transport",
                       "success": False, "kinds": ["driver_error"],
                       "error": f"{type(exc).__name__}: {exc}"}
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

"""
SER1 (TEST-ONLY): concurrent writes to @modelcontextprotocol/server-memory on one session, with
and without the stdio relay's ``--serialize-tools``. Plan:
``benchmarks/evidence/realstdio/SER1/PLAN.md``.

    concurrent_writes.py N [arms] [calls] -> one JSON row per trial on stdout

Each trial: a fresh memory file (one entity), one client session (``multi_client.py``), CALLS
``add_observations`` calls in flight at once, each adding its own observation; ground truth is
each observation's presence in the file afterwards.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
CODE = Path(os.environ.get("STATEGUARD_CODE", str(HERE.parents[2]))).resolve()
PY = os.environ.get("STATEGUARD_PYTHON", sys.executable)
NODE = os.environ.get("NODE") or shutil.which("node") or "/opt/node22/bin/node"
MEMORY_JS = os.environ.get(
    "MEMORY_JS",
    "/opt/sg-servers/memory/node_modules/@modelcontextprotocol/server-memory/dist/index.js",
)
CLIENT = str(HERE / "multi_client.py")
RUNS = Path(os.environ.get("RUNS_DIR", tempfile.gettempdir() + "/ser-runs"))
ARMS = ["raw", "sg", "sg_serial"]


def run(arm: str, t: int, calls: int) -> dict:
    base = RUNS / f"memory-{arm}-{t}-{uuid.uuid4().hex[:8]}"
    base.mkdir(parents=True)
    memory = base / "memory.jsonl"
    seed = {"type": "entity", "name": "seed", "entityType": "test", "observations": [f"s-{t}"]}
    memory.write_text(json.dumps(seed) + "\n")
    server = [NODE, "--no-warnings", MEMORY_JS]
    command = server
    if arm != "raw":
        serial = ["--serialize-tools", "add_observations"] if arm == "sg_serial" else []
        command = [PY, "-m", "stateguard", "relay", "stdio", "--retry", "auto", *serial,
                   "--audit", str(base / "audit.jsonl"), "--", *server]  # fmt: skip
    markers = [f"o-{t}-{i}-{uuid.uuid4().hex[:6]}" for i in range(calls)]
    arguments = [{"observations": [{"entityName": "seed", "contents": [m]}]} for m in markers]
    env = {**os.environ, "MEMORY_FILE_PATH": str(memory), "PYTHONPATH": str(CODE / "src")}
    t0 = time.monotonic()
    done = subprocess.run(
        [PY, CLIENT, json.dumps(command), "add_observations", json.dumps(arguments),
         "--concurrency", str(calls), "--wait", "30"],
        capture_output=True, text=True, env=env, timeout=300, check=False,
    )  # fmt: skip
    seconds = round(time.monotonic() - t0, 3)
    seen = json.loads(done.stdout.strip().splitlines()[-1])
    text = memory.read_text()
    told = [c["final"].get("status") == "result" and not c["final"].get("is_error")
            for c in seen["calls"]]  # fmt: skip
    present = [text.count(json.dumps(m)) for m in markers]
    row = {
        "arm": arm, "trial": t, "calls": calls, "seconds": seconds,
        "told_ok": sum(told), "present": sum(1 for p in present if p),
        "lost": sum(1 for ok, p in zip(told, present, strict=True) if ok and not p),
        "duplicates": sum(1 for p in present if p > 1),
        "not_ok": sum(1 for ok in told if not ok), "sessions": seen.get("sessions"),
    }  # fmt: skip
    if arm != "raw" and (base / "audit.jsonl").exists():
        events = [json.loads(x) for x in (base / "audit.jsonl").read_text().splitlines() if x]
        row["serialized_waits"] = sum(1 for e in events if e.get("event") == "serialized")
        row["serialize_timeouts"] = sum(1 for e in events if e.get("event") == "serialize_timeout")
    shutil.rmtree(base, ignore_errors=True)
    return row


def summarize(rows_path: str, out: str | None) -> None:
    rows = [json.loads(x) for x in Path(rows_path).read_text().splitlines() if x.strip()]
    summary = {}
    for arm in ARMS:
        mine = [r for r in rows if r.get("arm") == arm]
        if not mine:
            continue
        keys = ("calls", "told_ok", "present", "lost", "duplicates", "not_ok")
        summary[arm] = {k: sum(r.get(k, 0) for r in mine) for k in keys}
        summary[arm]["trials"] = len(mine)
        summary[arm]["trials_with_loss"] = sum(1 for r in mine if r.get("lost"))
        secs = sorted(r["seconds"] for r in mine)
        summary[arm]["seconds_median"] = secs[len(secs) // 2]
        summary[arm]["driver_errors"] = sum(1 for r in mine if r.get("error"))
    if out:
        Path(out).write_text(json.dumps(summary, indent=1) + "\n")
    for arm, s in summary.items():
        print(f"{arm:10s} trials {s['trials']}  calls {s['calls']}  told_ok {s['told_ok']}  "
              f"present {s['present']}  LOST {s['lost']} (trials with loss "
              f"{s['trials_with_loss']})  duplicates {s['duplicates']}  not_ok {s['not_ok']}  "
              f"median {s['seconds_median']} s")  # fmt: skip


def main() -> None:
    if sys.argv[1].endswith(".jsonl"):  # run_batch.py's scorer call: ROWS --json OUT
        summarize(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else None)
        return
    n = int(sys.argv[1])
    arms = sys.argv[2].split(",") if len(sys.argv) > 2 and sys.argv[2] else ARMS
    calls = int(sys.argv[3]) if len(sys.argv) > 3 else 16
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    for t in range(offset, offset + n):
        for arm in arms:
            try:
                row = run(arm, t, calls)
            except Exception as exc:  # a driver error is a recorded row, never dropped
                row = {"arm": arm, "trial": t, "error": f"{type(exc).__name__}: {exc}"[:400]}
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

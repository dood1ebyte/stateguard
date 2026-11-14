"""
R2: repeated faults on stdio, with calls in flight together (TEST-ONLY harness). Plan:
``benchmarks/evidence/realstdio/R2/PLAN.md``.

One trial: one client session (``multi_client.py``) making CALLS writes, each with its own marker,
up to CONCURRENCY in flight at once (memory: 1, see below). The server runs behind the shim in
``repeat`` mode: every EVERY-th write that reaches the shim -- a relay's resends included, the
count kept across restarts -- meets the next mode of CYCLE. StateGuard arms put the stdio relay
in front of the shim (as ``driver.py``'s arms, with --restart RESTARTS).

Every row is one logical invocation (one call the client made), scored by ``driver.score`` by
its marker's ground truth (``driver.truth_count``). A call lost with a broken session is a
failure, never dropped.

memory runs one call at a time: server-memory's add_observations loads the memory file,
changes it and saves it with no lock, so concurrent calls can lose each other's writes with no
fault at all (the server's own behaviour, not StateGuard's).

usage: repeated.py SERVER N [arms] [workers] -> JSONL rows on stdout
  TRIAL_OFFSET=K numbers trials K..K+N-1
"""

import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from collections import Counter

from driver import FAILING, OUTCOME, PY, READ_TOOL, RELAY, RUNS, SHIM, WRITE_TOOL, score, setup, truth_count

from stateguard.adapters.mcp.incidence import read_records

HERE = os.path.dirname(os.path.abspath(__file__))
CLIENT = os.path.join(HERE, "multi_client.py")
CALLS = 12
CONCURRENCY = {"sqlite": 4, "filesystem": 4, "memory": 1}
EVERY = 3
CYCLE = ["kill_before", "lost_exit", "hang", "transient", "duplicate", "garbage", "truncated",
         "nonconformant"]
ARMS = ["raw", "naive", "sg", "sg_keyed"]
RESTARTS = "10"
CLIENT_WAIT = 8.0
ANSWERED = ("answered", "recovered")


def _calls(server, base, t):
    """The CALLS write arguments, each with its own marker."""
    out = []
    for c in range(CALLS):
        marker = f"w-{t}-{c}-{uuid.uuid4().hex[:6]}"
        if server == "sqlite":
            args = {"query": f"INSERT INTO notes VALUES ('{marker}')"}
        elif server == "filesystem":
            source = base / "root" / f"src-{t}-{c}.txt"
            source.write_text(f"{marker}\n")
            args = {"source": str(source), "destination": str(base / "root" / f"dst-{marker}.txt")}
        else:
            args = {"observations": [{"entityName": "seed", "contents": [marker]}]}
        out.append((marker, args))
    return out


def run(server, arm, t):
    base = RUNS / f"r2-{server}-{arm}-{t}-{uuid.uuid4().hex[:10]}"
    base.mkdir(parents=True)
    cmd, env, _, _, _ = setup(server, base, t, arm, "write", "none")
    tool = WRITE_TOOL[server]
    calls = _calls(server, base, t)
    command = [PY, SHIM, "--mode", "repeat", "--every", str(EVERY), "--cycle", ",".join(CYCLE),
               "--tool", tool, "--fault-file", str(base / "faults.txt"), "--log",
               str(base / "shim.log"), "--", *cmd]
    if arm.startswith("sg"):
        effects = base / "effects.json"
        effects.write_text(json.dumps({"tools": [
            {"name": tool, "effect": "non_idempotent"},
            {"name": READ_TOOL[server], "effect": "read_only"},
        ]}))
        command = [PY, RELAY, "--retry", "auto", "--restart", RESTARTS, "--restart-policy",
                   "after-unknown", "--effects", str(effects), "--trust-read-only-hints",
                   "--call-deadline", "2", "--audit", str(base / "audit.jsonl"), "--", *command]
    client = [PY, CLIENT, json.dumps(command), tool, json.dumps([a for _, a in calls]),
              "--wait", str(CLIENT_WAIT), "--concurrency", str(CONCURRENCY[server])]
    if arm == "naive":
        client.append("--naive")
    t0 = time.monotonic()
    with open(base / "client.err", "wb") as err:
        done = subprocess.run(client, stdout=subprocess.PIPE, stderr=err, env=env, timeout=600,
                              check=False)
    wall = time.monotonic() - t0
    seen = json.loads(done.stdout.decode().strip().splitlines()[-1])
    hits: dict[str, list[str]] = {}
    fault_file = base / "faults.txt"
    fault_lines = fault_file.read_text().splitlines() if fault_file.exists() else []
    by_args = {json.dumps(a, sort_keys=True): m for m, a in calls}
    for line in fault_lines:
        mode, arguments = line.split(" ", 1)
        hits.setdefault(by_args.get(arguments, "?"), []).append(mode)
    audit = base / "audit.jsonl"
    records = [r for r in (read_records([audit]) if audit.exists() else [])
               if r.get("method") == "tools/call"]
    resolutions = Counter(r.get("resolution") for r in records)
    rows = []
    for c, entry in enumerate(seen["calls"]):
        marker = calls[c][0]
        final = entry["final"]
        count = truth_count(server, base, marker)
        success, told_ok, kinds = score(final, count)
        rows.append({
            "server": server, "op": "write", "tool": tool, "mode": "repeated",
            "family": "concurrent", "arm": arm, "trial": t, "call": c, "run": base.name,
            "success": success, "kinds": kinds, "count": count, "told_ok": told_ok,
            "server_outcome": (final.get("meta") or {}).get(OUTCOME)
            if final["status"] == "result" else None,
            "first_status": entry["first"]["status"], "client_retried": entry["retried"],
            "faults_hit": hits.get(marker, []), "final": final,
        })
    trial = {
        "calls": len(rows), "successes": sum(r["success"] for r in rows),
        "faults_injected": len(fault_lines), "client_sessions": seen["sessions"],
        "relay_records": len(records), "relay_answered": sum(resolutions[k] for k in ANSWERED),
        "relay_resolutions": dict(resolutions), "wall_s": round(wall, 3),
        "seed_intact": True,
    }
    for r in rows:
        r["trial_totals"] = trial
    if any(not r["success"] for r in rows) and FAILING is not None:
        FAILING.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base, FAILING / base.name)
    shutil.rmtree(base, ignore_errors=True)
    return rows


def main():
    server, n = sys.argv[1], int(sys.argv[2])
    arms = sys.argv[3].split(",") if len(sys.argv) > 3 and sys.argv[3] else ARMS
    workers = int(sys.argv[4]) if len(sys.argv) > 4 else 2
    offset = int(os.environ.get("TRIAL_OFFSET", "0"))
    jobs = [(arm, t) for t in range(offset, offset + n) for arm in arms]
    with concurrent.futures.ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(run, server, *job): job for job in jobs}
        for fut in concurrent.futures.as_completed(futures):
            try:
                rows = fut.result()
            except Exception as exc:  # a driver error: every call of the trial is a failed row
                arm, t = futures[fut]
                rows = [{"server": server, "op": "write", "mode": "repeated",
                         "family": "concurrent", "arm": arm, "trial": t, "call": c,
                         "success": False, "kinds": ["driver_error"],
                         "error": f"{type(exc).__name__}: {exc}"} for c in range(CALLS)]
            for row in rows:
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

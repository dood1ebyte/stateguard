"""
R1: concurrency under repeated faults (TEST-ONLY harness). Plan:
``benchmarks/evidence/realhttp/R1/PLAN.md``.

One trial: one notes server (Python ``notes`` or TypeScript ``tsnotes``), one fault proxy in
``repeat`` mode in front of it, and (StateGuard arms) one HTTP relay in front of that. SESSIONS
client processes (``multi_client.py``) start together; each makes CALLS ``add_note`` calls, one
after another, on its own session, every note text unique. The proxy faults every EVERY-th
``add_note`` request that reaches it, from any session, a relay's resends included, cycling
through CYCLE. So a resend can itself be faulted, and the sessions' calls and faults interleave.

Every row is one logical invocation (one call one client made), scored as ``driver.score``
scores a write: by its note's count in the notes file, the ground truth outside StateGuard. A
call the client could not make because its session broke is a failure, never dropped.

Arms: raw (no relay), naive (raw plus one blind resend per failed call, no key), sg (relay,
--retry auto, add_note declared non-idempotent), sg_keyed (sg, and the server keyed by the
injector / preload). No deadline flag: the relay's explicit 0, as the matrix arms (no hang
modes are in the cycle).

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

from driver import (
    FAILING,
    FAULTS,
    HERE,
    OUTCOME,
    PY,
    RELAY,
    RUNS,
    Procs,
    free_port,
    score,
    setup,
    wait_port,
)

from stateguard.adapters.mcp.incidence import read_records

CLIENT = str(HERE / "multi_client.py")
SESSIONS = 4
CALLS = 6
EVERY = 3
CYCLE = ["kill_before", "http_503_before", "lost_response", "http_502_after", "cut_stream",
         "rpc_internal_error"]
ARMS = ["raw", "naive", "sg", "sg_keyed"]
CLIENT_WAIT = 8.0
ANSWERED = ("answered", "recovered")  # as summarize.py: the relay told the client "it ran"


def run(server, arm, t):
    uid = uuid.uuid4().hex[:10]
    base = RUNS / f"r1-{server}-{arm}-{t}-{uid}"
    base.mkdir(parents=True)
    procs = Procs(base)
    try:
        port, server_cmd, env, _, _ = setup(server, base, t, arm, "write")
        procs.start("server", server_cmd, env)
        if not wait_port(port):
            raise RuntimeError("server did not listen")
        proxy_port = free_port()
        procs.start("faults", [PY, FAULTS, "--listen", f"127.0.0.1:{proxy_port}", "--upstream",
                               f"127.0.0.1:{port}", "--mode", "repeat", "--every", str(EVERY),
                               "--cycle", ",".join(CYCLE), "--tool", "add_note",
                               "--fault-file", str(base / "faults.txt"),
                               "--log", str(base / "proxy.log")], env)
        if not wait_port(proxy_port):
            raise RuntimeError("fault proxy did not listen")
        url = f"http://127.0.0.1:{proxy_port}/mcp"
        if arm.startswith("sg"):
            effects = base / "effects.json"
            effects.write_text(json.dumps({"tools": [{"name": "add_note",
                                                      "effect": "non_idempotent"}]}))
            relay_port = free_port()
            procs.start("relay", [*RELAY, "--listen", f"127.0.0.1:{relay_port}", "--retry",
                                  "auto", "--trust-read-only-hints", "--effects", str(effects),
                                  "--call-deadline", "0", "--audit", str(base / "audit.jsonl"),
                                  "--upstream", f"http://127.0.0.1:{proxy_port}/mcp"], env)
            if not wait_port(relay_port):
                raise RuntimeError("relay did not listen")
            url = f"http://127.0.0.1:{relay_port}/mcp"
        texts = [[f"w-{t}-{s}-{c}" for c in range(CALLS)] for s in range(SESSIONS)]
        clients = []
        t0 = time.monotonic()
        for s in range(SESSIONS):
            argv = [PY, CLIENT, url, "add_note", json.dumps(texts[s]), "--wait", str(CLIENT_WAIT)]
            if arm == "naive":
                argv.append("--naive")
            clients.append(subprocess.Popen(argv, stdout=subprocess.PIPE,
                                            stderr=open(base / f"client{s}.err", "wb"), env=env))
        seen = []
        for proc in clients:
            out, _ = proc.communicate(timeout=300)
            seen.append(json.loads(out.decode().strip().splitlines()[-1]))
        wall = time.monotonic() - t0
    finally:
        procs.stop_all()
    notes = (base / "notes.txt").read_text().split("\n")
    counts = Counter(notes)
    hits: dict[str, list[str]] = {}
    fault_file = base / "faults.txt"
    fault_lines = fault_file.read_text().splitlines() if fault_file.exists() else []
    for line in fault_lines:
        mode, arguments = line.split(" ", 1)
        hits.setdefault(json.loads(arguments).get("text"), []).append(mode)
    audit = base / "audit.jsonl"
    records = [r for r in (read_records([audit]) if audit.exists() else [])
               if r.get("method") == "tools/call"]
    resolutions = Counter(r.get("resolution") for r in records)
    rows = []
    for s in range(SESSIONS):
        for c, entry in enumerate(seen[s]["calls"]):
            text = texts[s][c]
            final = entry["final"]
            success, told_ok, kinds = score(final, counts[text])
            rows.append({
                "server": server, "op": "write", "tool": "add_note", "mode": "repeated",
                "family": "concurrent", "arm": arm, "trial": t, "session": s, "call": c,
                "run": base.name, "success": success, "kinds": kinds, "count": counts[text],
                "told_ok": told_ok,
                "server_outcome": (final.get("meta") or {}).get(OUTCOME)
                if final["status"] == "result" else None,
                "first_status": entry["first"]["status"], "client_retried": entry["retried"],
                "faults_hit": hits.get(text, []), "final": final,
            })
    # The trial's totals, on every row of it (the relay's records are not matched per call:
    # they carry the client's JSON-RPC id and session, not the note).
    trial = {
        "calls": len(rows), "successes": sum(r["success"] for r in rows),
        "faults_injected": len(fault_lines), "client_sessions": sum(x["sessions"] for x in seen),
        "relay_records": len(records), "relay_answered": sum(resolutions[k] for k in ANSWERED),
        "relay_resolutions": dict(resolutions), "wall_s": round(wall, 3),
        "seed_intact": counts[f"seed-{t}"] == 1,
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
                         "family": "concurrent", "arm": arm, "trial": t, "session": s, "call": c,
                         "success": False, "kinds": ["driver_error"],
                         "error": f"{type(exc).__name__}: {exc}"}
                        for s in range(SESSIONS) for c in range(CALLS)]
            for row in rows:
                print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()

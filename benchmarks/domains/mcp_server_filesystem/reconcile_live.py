"""
Live check of the relay's reconciliation path against the real filesystem server.

usage: reconcile_live.py SERVER_BIN WORKSPACE OUT_JSON [--reps N]

Runs the production relay (``examples/mcp/stdio_proxy.py``, auto mode,
``--restart 2 --restart-policy after-unknown``) in front of
``reconcile_shim.py`` and the pinned server, then sends one ``move_file``
whose answer the shim loses. Ground truth comes from the sandbox on disk and
the shim's delivery ledger -- never from the relay's answer or its audit log.

Scenarios (one fault each):

``L1``  executed, answer lost (``drop_response``)       expect: postcondition verified
``L2``  never executed (``swallow_request``)            expect: not_verified
``L3``  executes late (``delay_request`` 3 s, deadline 1 s)  expect: undetermined, no probe
``L4``  shadow mode, executed, answer lost              expect: no probe, nothing restarted

Every scenario also checks: the original reached a server at most once, no
restarted server received anything but read-only probes, the execution state
reported is ``unknown``, and the sentinel outside the sandbox is unchanged.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession, outcome  # noqa: E402

RELAY = HERE.parents[2] / "examples" / "mcp" / "stdio_proxy.py"
SHIM = HERE / "reconcile_shim.py"
PY = sys.executable

EFFECTS = {
    "tools": [
        {"name": "read_text_file", "effect": "read_only"},
        {
            "name": "move_file",
            "effect": "non_idempotent",
            "reconcile": {
                "timeout_seconds": 5,
                "probes": [
                    {
                        "tool": "read_text_file",
                        "arguments": {"path": "$source"},
                        "expect": "absent",
                        "absent_marker": "ENOENT",
                    },
                    {
                        "tool": "read_text_file",
                        "arguments": {"path": "$destination"},
                        "expect": "present",
                        "absent_marker": "ENOENT",
                    },
                ],
            },
        },
    ]
}

SCENARIOS = [
    {"id": "L1", "fault": "drop_response:move_file", "expect": "verified", "executed": True},
    {"id": "L2", "fault": "swallow_request:move_file", "expect": "not_verified", "executed": False},
    {
        "id": "L3",
        "fault": "delay_request:move_file:1:3.0",
        "deadline": 1.0,
        "expect": "undetermined",
        "executed": True,
        "late_wait": 3.5,
    },
    {
        "id": "L4",
        "fault": "drop_response:move_file",
        "shadow": True,
        "expect": None,
        "executed": True,
    },
]


def run(server: str, ws: Path, sc: dict[str, Any], rep: int) -> dict[str, Any]:
    tag = f"{sc['id']}-{rep}"
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    assert fixture.snapshot(sb) == fixture.checkpoints()[2]
    effects = ws / "effects.json"
    effects.write_text(json.dumps(EFFECTS), encoding="utf-8")
    audit, deliveries = ws / f"audit-{tag}.jsonl", ws / f"deliveries-{tag}.jsonl"
    for f in (audit, deliveries, ws / f"once-{tag}"):
        f.unlink(missing_ok=True)
    relay = [PY, str(RELAY), "--audit", str(audit), "--effects", str(effects), "--retry", "off"]
    if sc.get("shadow"):
        relay.append("--shadow")
    else:
        relay += ["--restart", "2", "--restart-policy", "after-unknown"]
    if sc.get("deadline"):
        relay += ["--call-deadline", str(sc["deadline"])]
    shim = [
        PY,
        str(SHIM),
        "--gen",
        "0",
        "--deliveries",
        str(deliveries),
        "--fault",
        sc["fault"],
        "--once",
        str(ws / f"once-{tag}"),
    ]
    cmd = [*relay, "--", *shim, "--", server, str(sb)]
    session = RawSession(cmd, env={"PATH": os.environ["PATH"], "HOME": str(ws)}, cwd=str(ws))
    session.initialize()
    tool, args = workflow.arguments("S3", sb)
    t0 = time.monotonic()
    response = session.call(tool, args, rid="T-1", timeout=30.0)
    elapsed = round(time.monotonic() - t0, 3)
    if sc.get("late_wait"):
        time.sleep(max(0.0, t0 + sc["late_wait"] - time.monotonic()))
    session.close()
    final = fixture.snapshot(sb)
    events = (
        [json.loads(x) for x in deliveries.read_text().splitlines()] if deliveries.exists() else []
    )
    audit_events = [json.loads(x) for x in audit.read_text().splitlines()] if audit.exists() else []
    starts = [e for e in events if e["event"] == "started"]
    # Which shim process each delivery belongs to: events between its "started" lines.
    process = -1
    per_process: list[list[Any]] = []
    for e in events:
        if e["event"] == "started":
            process += 1
            per_process.append([])
        elif e["event"] == "delivered":
            per_process[process].append(e.get("tool"))
    data = (response or {}).get("error", {}).get("data", {}) if isinstance(response, dict) else {}
    original_deliveries = sum(
        1 for e in events if e["event"] == "delivered" and e.get("id") == "T-1"
    )
    later_tools = sorted({t for proc in per_process[1:] for t in proc})
    checks = {
        "expected_postcondition": data.get("io.stateguard/postcondition") == sc["expect"]
        if sc["expect"]
        else "io.stateguard/postcondition" not in data,
        "execution_state_unknown": data.get("io.stateguard/executionState") == "unknown"
        if not sc.get("shadow")
        else response is None,
        "attribution_unknown_if_verified": sc["expect"] != "verified"
        or data.get("io.stateguard/attribution") == "unknown",
        "ground_truth_executed": (final == fixture.checkpoints()[3]) is sc["executed"],
        "original_delivered_at_most_once": original_deliveries <= 1,
        "restarted_servers_only_probed": set(later_tools) <= {"read_text_file"},
        "no_probe_unless_expected": (
            not any(e["event"] == "reconcile_probe" for e in audit_events)
            if sc["expect"] in (None, "undetermined")
            else True
        ),
        "sentinel_unchanged": final.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
    }
    return {
        "scenario": sc["id"],
        "rep": rep,
        "wire": outcome(response),
        "postcondition": data.get("io.stateguard/postcondition"),
        "execution_state": data.get("io.stateguard/executionState"),
        "attribution": data.get("io.stateguard/attribution"),
        "reasons": (data.get("io.stateguard/reconciliation") or {}).get("reasons"),
        "server_processes": len(starts),
        "later_process_tools": later_tools,
        "final_checkpoint": fixture.matching_checkpoint(final),
        "answer_latency_s": elapsed,
        "audit_events": sorted({e["event"] for e in audit_events if "reconcile" in e["event"]}),
        "checks": checks,
        "ok": all(checks.values()),
    }


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    reps = int(sys.argv[5]) if sys.argv[4:5] == ["--reps"] else 3
    ws.mkdir(parents=True, exist_ok=True)
    results = [run(server, ws, sc, rep) for sc in SCENARIOS for rep in range(reps)]
    fixture.reset(ws / "sandbox")
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    for r in results:
        failed = [k for k, v in r["checks"].items() if not v]
        print(
            f"{r['scenario']} r{r['rep']} {'ok ' if r['ok'] else 'FAIL'} wire={r['wire']:26} "
            f"postcondition={r['postcondition']!s:13} cp={r['final_checkpoint']} "
            f"procs={r['server_processes']} later={r['later_process_tools']} "
            f"t={r['answer_latency_s']}s {failed or ''}"
        )
    print("runs:", len(results), "failed:", sum(not r["ok"] for r in results))


if __name__ == "__main__":
    main()

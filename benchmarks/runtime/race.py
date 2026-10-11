"""
Regression measurement for the mid-restart race (TASKMAP, T14).

    python benchmarks/runtime/race.py [--runs 20] [--json OUT]

T13's replay found that a ``tools/call`` arriving while the relay restarts
its server -- right after the client was told "outcome unknown" for the call
the server died on -- could be decided against the dead server's registry,
fail to refresh its listing on the dead pipe (``tools_list_unsent``), and be
forwarded **unvalidated** to the new server: StateGuard's repair skipped
because a restart was in progress.

This records one session that provokes exactly that (repairable calls right
after two after-unknown restarts), then replays it ``--runs`` times with the
client sending as soon as it has its previous answer -- no waiting for the
restart to settle -- and counts, per run and in total:

* restart-gated calls (the relay held them until the new server was listed),
* repaired / forwarded / refused-or-escalated calls,
* UNKNOWN and not-sent answers, duplicate executions,
* **unsafe bypasses** (``replay.unsafe_bypasses``): the T13 path.

Fault injection on a synthetic workload. Not real-world evidence.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from stateguard.adapters.mcp.recording import read_recording  # noqa: E402

from .harness import RelayMode, ServerAction  # noqa: E402
from .record_corpus import BASIC, Call, Session, record  # noqa: E402
from .replay import ArmRun, derive, run_arm, unsafe_bypasses  # noqa: E402

RUNS = 20


def race_session() -> Session:
    act = ServerAction
    return Session(
        "R_restart_race",
        "repairable calls right after after-unknown restarts",
        BASIC,
        [
            Call("write_note", {"text": "a"}, {"text": "a"}, act.CLOSE_AFTER_EXECUTE, "I"),
            Call(
                "search_issues",
                {"query": "x", "limit": "5"},
                {"query": "x", "limit": 5},
                category="A",
                note="repairable: number sent as a string",
            ),
            Call(
                "search_issues",
                {"query": "y", "state": "Open"},
                {"query": "y", "state": "open"},
                category="A",
                note="repairable: enum case",
            ),
            Call("write_note", {"text": "b"}, {"text": "b"}, act.CLOSE_AFTER_EXECUTE, "I"),
            Call(
                "search_issues",
                {"query": "z", "limit": "7"},
                {"query": "z", "limit": 7},
                category="A",
            ),
        ],
        mode=RelayMode.AUTO,
        max_restarts=3,
        restart_policy="after-unknown",
    )


def _repaired(audit: list[dict[str, Any]]) -> int:
    return sum(
        1
        for e in audit
        if e.get("event") == "call"
        and e.get("action") == "forward"
        and any(d.get("op") for d in e.get("decisions") or [])
    )


def summarise_run(run: ArmRun) -> dict[str, Any]:
    calls = [s for s in run.steps if s.method == "tools/call"]
    return {
        "calls": len(calls),
        "restart_gated": sum(1 for e in run.audit if e.get("event") == "call_gated"),
        "regated": sum(1 for e in run.audit if e.get("event") == "call_regated"),
        "repaired": _repaired(run.audit),
        "forwarded": run.tools_call_sent,
        "refused_or_escalated": sum(
            1
            for e in run.audit
            if e.get("event") == "call" and e.get("action") in ("refuse", "escalate")
        ),
        "unvalidated": sum(
            1 for e in run.audit if e.get("event") == "call" and e.get("action") == "unvalidated"
        ),
        "unknown": sum(1 for s in calls if s.answer == "unknown"),
        "not_sent": sum(1 for s in calls if s.answer == "not_sent"),
        "duplicate_executions": sum(max(0, s.executions - 1) for s in calls),
        "labelled_failures": sum(1 for s in calls if s.ok is False),
        "restarts": run.restarts,
        "unsafe_bypasses": unsafe_bypasses(run.audit),
    }


def measure(runs: int = RUNS) -> dict[str, Any]:
    with tempfile.TemporaryDirectory() as tmp:
        env = derive(read_recording(record(race_session(), Path(tmp))))
    per_run = [summarise_run(run_arm(env, "as_recorded", race_provoking=True)) for _ in range(runs)]
    totals = {key: sum(r[key] for r in per_run) for key in per_run[0]}
    return {
        "provenance": "fault injection on a synthetic workload -- not real-world evidence",
        "runs": runs,
        "totals": totals,
        "runs_with_a_bypass": sum(1 for r in per_run if r["unsafe_bypasses"]),
        "per_run": per_run,
    }


def render(summary: dict[str, Any]) -> str:
    t = summary["totals"]
    return "\n".join(
        [
            "StateGuard mid-restart race regression (T14)",
            f"CORPUS: {summary['provenance']}",
            f"runs {summary['runs']}  calls {t['calls']}  restarts {t['restarts']}",
            f"restart-gated {t['restart_gated']}  re-gated {t['regated']}  "
            f"repaired {t['repaired']}  forwarded {t['forwarded']}  "
            f"refused/escalated {t['refused_or_escalated']}  unvalidated {t['unvalidated']}",
            f"UNKNOWN {t['unknown']}  not sent {t['not_sent']}  "
            f"duplicate executions {t['duplicate_executions']}  "
            f"labelled failures {t['labelled_failures']}",
            f"UNSAFE BYPASSES {t['unsafe_bypasses']} "
            f"(runs with one: {summary['runs_with_a_bypass']}/{summary['runs']})",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument("--runs", type=int, default=RUNS)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    summary = measure(args.runs)
    print(render(summary))
    if args.json:
        args.json.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

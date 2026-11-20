"""
Summarise R1 rows (``repeated.py``; TEST-ONLY): per server and arm, the residual (failed and
unresolved, as ``phase1_table.py`` counts them), unsafe outcomes, the faults met, and the
per-trial agreement checks of the relay's records against ground truth.

usage: repeated_summary.py ROWS.jsonl [--json OUT.json]
"""

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from phase1_table import UNSAFE, outcome, wilson  # noqa: E402


def summarize(rows):
    out = {"n_rows": len(rows), "driver_errors": sum("driver_error" in r["kinds"] for r in rows)}
    groups = defaultdict(list)
    for r in rows:
        groups[(r["server"], r["arm"])].append(r)
    cells = {}
    for (server, arm), rs in sorted(groups.items()):
        outcomes = Counter(outcome(r) for r in rs)
        failed = len(rs) - outcomes["success"]
        lo, hi = wilson(failed, len(rs))
        trials = {}
        for r in rs:
            if "trial_totals" in r:
                trials[r["trial"]] = r["trial_totals"]
        disagree = [
            t for t, x in sorted(trials.items())
            if arm.startswith("sg") and (x["relay_records"] != x["calls"]
                                         or x["relay_answered"] != x["successes"])
        ]
        hit = [r for r in rs if r.get("faults_hit")]
        cells[f"{server}/{arm}"] = {
            "eligible": len(rs),
            "failed": failed,
            "unresolved": outcomes["unresolved"],
            "failed_resolved": outcomes["failed_resolved"],
            "rfr": round(failed / len(rs), 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "unsafe": {u: sum(u in r["kinds"] for r in rs) for u in UNSAFE},
            "calls_faulted": len(hit),
            "calls_faulted_twice_or_more": sum(len(r["faults_hit"]) >= 2 for r in hit),
            "failed_with_one_fault": sum(
                not r["success"] and len(r.get("faults_hit") or []) == 1 for r in rs
            ),
            "failed_with_two_or_more": sum(
                not r["success"] and len(r.get("faults_hit") or []) >= 2 for r in rs
            ),
            "failed_unfaulted": sum(not r["success"] and not r.get("faults_hit") for r in rs),
            "faults_injected": sum(x["faults_injected"] for x in trials.values()),
            "trials": len(trials),
            "trials_disagreeing": disagree,
            "seed_intact": all(x["seed_intact"] for x in trials.values()),
            "wall_s_max": max((x["wall_s"] for x in trials.values()), default=None),
        }
    out["cells"] = cells
    return out


def main(argv):
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2:]
    rows = [json.loads(line) for line in Path(argv[0]).read_text().splitlines() if line.strip()]
    result = summarize(rows)
    print(f"{result['n_rows']} rows, {result['driver_errors']} driver errors")
    for name, c in result["cells"].items():
        unsafe = sum(c["unsafe"].values())
        print(f"{name}: {c['failed']}/{c['eligible']} ({c['rfr']:.1%}, CI {c['ci95'][0]:.1%}-"
              f"{c['ci95'][1]:.1%}); unresolved {c['unresolved']}, failed resolved "
              f"{c['failed_resolved']}; unsafe {unsafe} {c['unsafe']}; faulted calls "
              f"{c['calls_faulted']} ({c['calls_faulted_twice_or_more']} twice+); failed with "
              f"1 fault {c['failed_with_one_fault']}, 2+ {c['failed_with_two_or_more']}, none "
              f"{c['failed_unfaulted']}; disagreeing trials {c['trials_disagreeing']}")
    if out_json:
        Path(out_json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

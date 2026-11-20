"""
Score stale_pool.py rows per arm: residual failure rate (Wilson 95%), the relay's recorded
faults, unsafe outcomes, and the upstream connections opened.

usage: stale_pool_summary.py ROWS.jsonl [--json OUT.json]
"""

import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, __import__("os").path.dirname(__file__))
from summarize import latency as _latency  # noqa: E402
from summarize import wilson  # noqa: E402


def main(argv: list[str]) -> None:
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    rows = [
        json.loads(line) for p in argv for line in Path(p).read_text().splitlines() if line.strip()
    ]
    calls = [r for r in rows if not r.get("summary")]
    connections = {r["arm"]: r["proxy_connections"] for r in rows if r.get("summary")}
    result = {}
    for arm in sorted({r["arm"] for r in calls}):
        a = [r for r in calls if r["arm"] == arm]
        fail = sum(not r["success"] for r in a)
        lo, hi = wilson(fail, len(a))
        result[arm] = {
            "n": len(a),
            "failed": fail,
            "rate": round(fail / len(a), 4),
            "ci95": [round(lo, 4), round(hi, 4)],
            "relay_faults": dict(Counter(f for r in a for f in (r["relay_faults"] or []))),
            "relay_resolutions": dict(Counter(str(r["relay_resolution"]) for r in a)),
            "answers": dict(
                Counter(
                    r["answer"]["status"]
                    + ":"
                    + str(r["answer"].get("state") or r["answer"].get("outcome"))
                    for r in a
                )
            ),
            "duplicates": sum(r["duplicate"] for r in a),
            "false_success": sum(r["false_success"] for r in a),
            "not_executed": sum(r["count"] == 0 for r in a),
            "upstream_connections": connections.get(arm),
            "call_latency_s": _latency([r["answer"] for r in a]),
        }
        e = result[arm]
        print(
            f"{arm}: failed {fail}/{len(a)} ({e['rate']:.1%}, ci {lo:.1%}-{hi:.1%})  "
            f"faults {e['relay_faults']}  resolutions {e['relay_resolutions']}  "
            f"dup {e['duplicates']}  false_success {e['false_success']}  "
            f"not_executed {e['not_executed']}  connections {e['upstream_connections']}  "
            f"call latency {e['call_latency_s']}"
        )
    if out_json:
        with open(out_json, "w") as fh:
            json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])

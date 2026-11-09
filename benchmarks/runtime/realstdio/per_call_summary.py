"""
Score per_call.py rows: per server, op and arm, the per-call latency, and its difference from
raw on the same (round, call index, op), with a normal 95% interval. The first two calls of a
session (one read, one write) are reported apart as ``first`` (warm-up: imports, the key store);
``warm`` is the rest. Failed calls and failed sessions are counted, never dropped.

usage: per_call_summary.py ROWS.jsonl [...] [--json OUT.json]
"""

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def _stats(values: list[float]) -> dict | None:
    if not values:
        return None
    values = sorted(values)
    p95 = values[min(len(values) - 1, int(math.ceil(0.95 * len(values))) - 1)]
    return {
        "n": len(values),
        "mean_ms": round(statistics.fmean(values) * 1000, 2),
        "median_ms": round(statistics.median(values) * 1000, 2),
        "p95_ms": round(p95 * 1000, 2),
    }


def _diff(values: list[float]) -> dict | None:
    if not values:
        return None
    mean = statistics.fmean(values)
    se = statistics.stdev(values) / math.sqrt(len(values)) if len(values) > 1 else float("nan")
    return {
        "pairs": len(values),
        "mean_ms": round(mean * 1000, 2),
        "ci95_ms": [round((mean - 1.96 * se) * 1000, 2), round((mean + 1.96 * se) * 1000, 2)],
    }


def main(argv: list[str]) -> None:
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    rows = [
        json.loads(line) for p in argv for line in Path(p).read_text().splitlines() if line.strip()
    ]
    errors = [r for r in rows if "session_error" in r]
    calls = [r for r in rows if "session_error" not in r]
    raw = {(r["server"], r["round"], r["i"], r["op"]): r for r in calls if r["arm"] == "raw"}
    groups = defaultdict(list)
    for r in calls:
        phase = "first" if r["i"] < 2 else "warm"
        groups[(r["server"], r["op"], phase, r["arm"])].append(r)
    result = {
        "calls": len(calls),
        "session_errors": len(errors),
        "failed_calls": sum(not r["ok"] for r in calls),
        "cells": {},
    }
    print(
        f"{len(calls)} calls, {len(errors)} session errors, {result['failed_calls']} failed calls"
    )
    for key in sorted(groups):
        server, op, phase, arm = key
        rs = groups[key]
        entry = {"latency": _stats([r["latency_s"] for r in rs])}
        if arm != "raw":
            pairs = [
                r["latency_s"] - raw[k]["latency_s"]
                for r in rs
                if (k := (server, r["round"], r["i"], op)) in raw
            ]
            entry["vs_raw"] = _diff(pairs)
        result["cells"]["/".join(key)] = entry
        print(
            f"{server:<7} {op:<6} {phase:<6} {arm:<9} {entry['latency']}  "
            f"{'vs raw ' + str(entry.get('vs_raw')) if arm != 'raw' else ''}"
        )
    if out_json:
        Path(out_json).write_text(json.dumps(result, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])

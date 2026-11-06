"""
Paired overhead against raw (TEST-ONLY harness): for each server, cell and trial, an arm's
session latency minus raw's on the same trial. Rows from ``driver.py`` (realhttp or realstdio).

Only fault-free cells are paired, and only trials where both rows succeeded; the others are
counted, not dropped silently. The interval is the mean difference +/- 1.96 standard errors.

usage: paired_overhead.py ROWS.jsonl [...] [--json OUT.json]
"""

import json
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def main(argv: list[str]) -> None:
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    rows = [
        json.loads(line) for p in argv for line in Path(p).read_text().splitlines() if line.strip()
    ]
    by_key: dict[tuple, dict[str, dict]] = defaultdict(dict)
    for r in rows:
        if r.get("mode") != "none":
            continue
        by_key[(r["server"], r["op"], r["trial"])][r["arm"]] = r
    groups: dict[tuple, list[float]] = defaultdict(list)
    unpaired: dict[tuple, int] = defaultdict(int)
    raw_lat: dict[tuple, list[float]] = defaultdict(list)
    for (server, op, _t), arms in by_key.items():
        raw = arms.get("raw")
        for arm, r in arms.items():
            if arm == "raw":
                if r.get("success") and "latency_s" in r:
                    raw_lat[(server, op)].append(r["latency_s"])
                continue
            if raw and raw.get("success") and r.get("success"):
                groups[(server, op, arm)].append(r["latency_s"] - raw["latency_s"])
            else:
                unpaired[(server, op, arm)] += 1
    result = {}
    for key in sorted(set(groups) | set(unpaired)):
        d = sorted(groups.get(key, []))
        server, op, arm = key
        entry: dict = {"pairs": len(d), "unpaired": unpaired.get(key, 0)}
        if d:
            mean = statistics.fmean(d)
            se = statistics.stdev(d) / math.sqrt(len(d)) if len(d) > 1 else float("nan")
            entry.update(
                mean_s=round(mean, 3),
                ci95_s=[round(mean - 1.96 * se, 3), round(mean + 1.96 * se, 3)],
                median_s=round(statistics.median(d), 3),
                raw_mean_s=round(statistics.fmean(raw_lat[(server, op)]), 3)
                if raw_lat[(server, op)]
                else None,
            )
        result[f"{server}/{op}/{arm}"] = entry
        print(f"{server:<10} {op:<6} {arm:<18} " + "  ".join(f"{k} {v}" for k, v in entry.items()))
    if out_json:
        with open(out_json, "w") as fh:
            json.dump(result, fh, indent=1)


if __name__ == "__main__":
    main(sys.argv[1:])

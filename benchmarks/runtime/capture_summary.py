"""
Summarize C1 rows (``capture_workloads.py``): per server and arm, never pooled across arms, and
the shadow/auto pairs (same server, seed and workload). TEST-ONLY harness.

    capture_summary.py rows.jsonl [--json OUT]
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


def wilson(k: int, n: int, z: float = 1.959964) -> list[float]:
    if n == 0:
        return [0.0, 1.0]
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return [round(max(0.0, centre - half), 4), round(min(1.0, centre + half), 4)]


def main(argv: list[str]) -> int:
    rows = [json.loads(line) for line in Path(argv[0]).read_text().splitlines() if line.strip()]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        groups[(row["server"], row["arm"])].append(row)
    out: dict[str, Any] = {"rows": len(rows), "label": "real server, scripted workload",
                           "groups": {}, "pairs": []}  # fmt: skip
    for (server, arm), items in sorted(groups.items()):
        valid = [r for r in items if not r.get("invalid")]
        calls = sum(r["report"]["calls"]["n"] for r in valid)
        def k(name: str) -> int:
            return sum(r["report"]["calls"][name]["k"] for r in valid)
        entry = {
            "sessions": len(items),
            "invalid_sessions": [r.get("invalid") for r in items if r.get("invalid")],
            "client_tool_calls": sum(r["client"]["tool_calls"] for r in valid),
            "relay_tool_calls": sum(r["report"]["denominators"]["client_tool_calls"] for r in valid),
            "records": sum(r["report"]["denominators"]["tool_call_records"] for r in valid),
            "answered": sum(r["report"]["denominators"]["tool_calls_answered_to_client"] for r in valid),
            "unfinished": sum(r["report"]["denominators"]["unfinished"] for r in valid),
            "checks_hold": all(r["report"]["denominators"]["checks_hold"] for r in valid),
            "client_equals_relay": all(r["client_calls_equal_relay_calls"] for r in valid),
            "client_tool_errors_equal_report": all(r["client_tool_errors_equal_report"] for r in valid),
            "rebuilt_equal": all(r["rebuilt_equal"] for r in valid),
            "calls": calls,
            **{name: {"k": k(name), "ci95": wilson(k(name), calls)}
               for name in ("succeeded", "tool_error", "failed", "unknown", "not_successful")},
            "retries_made": sum(r["report"]["retries"]["calls_resent"] for r in valid),
            "would_retry": sum(r["report"]["retries"]["would_retry"] or 0 for r in valid),
            "latency_p50_ms_median_of_sessions": sorted(r["report"]["latency_ms"]["p50"] for r in valid)[len(valid) // 2] if valid else None,
            "latency_p95_ms_max_of_sessions": max((r["report"]["latency_ms"]["p95"] for r in valid), default=None),
            "overhead_p50_ms_median_of_sessions": sorted(r["report"]["overhead_ms"]["p50"] for r in valid)[len(valid) // 2] if valid else None,
            "overhead_p95_ms_max_of_sessions": max((r["report"]["overhead_ms"]["p95"] for r in valid), default=None),
            "overhead_calls": sum(r["report"]["overhead_ms"]["n"] for r in valid),
            "client_noise_lines": sum(r.get("client_noise_lines", 0) for r in valid),
        }  # fmt: skip
        out["groups"][f"{server}/{arm}"] = entry
    by_pair: dict[tuple[str, int], dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in rows:
        by_pair[(row["server"], row["seed"])][row["arm"]] = row
    for (server, seed), arms in sorted(by_pair.items()):
        if {"shadow", "auto"} - set(arms):
            out["pairs"].append({"server": server, "seed": seed, "paired": False, "why": "an arm is missing"})
            continue
        s, a = arms["shadow"], arms["auto"]
        if s.get("invalid") or a.get("invalid"):
            out["pairs"].append({"server": server, "seed": seed, "paired": False, "why": "an arm is invalid"})
            continue
        names = ("succeeded", "tool_error", "failed", "unknown")
        counts = {n: [s["report"]["calls"][n]["k"], a["report"]["calls"][n]["k"]] for n in names}
        out["pairs"].append({
            "server": server, "seed": seed, "paired": True,
            "same_workload": s["client"]["by_tool"] == a["client"]["by_tool"],
            "shadow_auto": counts, "equal": all(v[0] == v[1] for v in counts.values()),
        })  # fmt: skip
    total = sum(g["calls"] for g in out["groups"].values())
    out["total_tool_calls"] = total
    out["servers"] = sorted({s for s, _ in groups})
    text = [f"C1  real server, scripted workload   rows {len(rows)}   tool calls {total}", ""]
    for name, g in out["groups"].items():
        text.append(
            f"{name:20} sessions {g['sessions']} invalid {len(g['invalid_sessions'])}  "
            f"calls client {g['client_tool_calls']} relay {g['relay_tool_calls']} records "
            f"{g['records']} answered {g['answered']} unfinished {g['unfinished']}  checks "
            f"{'hold' if g['checks_hold'] else 'FAIL'}  rebuilt {'equal' if g['rebuilt_equal'] else 'DIFF'}"
        )
        text.append(
            f"{'':20} succeeded {g['succeeded']['k']} tool_error {g['tool_error']['k']} failed "
            f"{g['failed']['k']} unknown {g['unknown']['k']} (unknown ci {g['unknown']['ci95']})  "
            f"retries {g['retries_made']} would_retry {g['would_retry']}  latency p50 "
            f"{g['latency_p50_ms_median_of_sessions']} p95max {g['latency_p95_ms_max_of_sessions']}"
            f"  overhead p50 {g['overhead_p50_ms_median_of_sessions']} p95max "
            f"{g['overhead_p95_ms_max_of_sessions']} (n {g['overhead_calls']})"
        )
    text.append("")
    for pair in out["pairs"]:
        text.append(f"pair {pair['server']} seed {pair['seed']}: " + (
            f"paired, same workload {pair['same_workload']}, equal {pair['equal']} {json.dumps(pair['shadow_auto'])}"
            if pair["paired"] else f"NOT paired: {pair['why']}"))
    print("\n".join(text))
    if "--json" in argv:
        Path(argv[argv.index("--json") + 1]).write_text(json.dumps(out, indent=1) + "\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

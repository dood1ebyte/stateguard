"""
The Phase 1 residual-failure table, generated from recorded evidence (TEST-ONLY harness).

Definitions are in ``docs/runtime/PHASE1_METRICS.md``; this file is their only implementation
for the Phase 1 tables. Every row of every ``rows_*.jsonl`` in each batch directory counts:
nothing is excluded, and a driver error is a failure.

* One row is one eligible logical invocation. Each row has exactly one outcome:
  ``success``, ``unresolved`` (the client cannot tell whether the call ran) or
  ``failed_resolved`` (failed, and the client was told so correctly or wrongly).
* RFR = (failed_resolved + unresolved) / eligible, with a Wilson 95% interval, per suite,
  server, arm and fault family. Families and servers are never pooled.
* Unsafe outcomes, StateGuard-induced failures (Z) and recoveries are counted against raw on
  the same (op, mode, trial).
* ``--by-category`` adds the failure taxonomy (``TASKMAP.md``, A-K and Z) per injected fault.

usage: phase1_table.py BATCH_DIR [...] [--by-category] [--json OUT.json]
  e.g. phase1_table.py benchmarks/evidence/realhttp/B2 benchmarks/evidence/realstdio/S4
"""

from __future__ import annotations

import json
import math
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

#: The client cannot tell whether the call ran.
UNRESOLVED_KINDS = frozenset({"surfaced_unknown", "no_answer_timeout", "no_answer_crash"})
#: A keyed server's own "in doubt" (-32010) and "still running" (-32011) answers.
UNRESOLVED_CODES = frozenset({-32010, -32011})
UNSAFE = ("duplicate_effect", "false_success", "false_failure", "wrong_content")
ORDER = ["raw", "naive", "sg", "sg_keyed", "sg_resumable", "sg_deadline", "sg_keyed_deadline"]
FAMILIES = ["fault_free", "transport", "session", "crash", "slow", "drift"]

#: Injected fault -> failure taxonomy category (TASKMAP.md) and whether it ran before the fault.
CATEGORY = {
    "kill_before": ("E", "transport lost before the request reached the server; not executed"),
    "http_503_before": ("F", "transient 503 before forwarding; not executed"),
    "transient": ("F", "transient JSON-RPC -32603 before forwarding; not executed"),
    "lost_response": ("I", "executed; the answer lost with the connection"),
    "lost_exit": ("I", "executed; the answer lost and the server process exited"),
    "http_502_after": ("I", "executed; a 502 in place of the answer"),
    "cut_stream": ("I", "executed; the event stream cut before the answer"),
    "hang_after": ("I", "executed; no answer ever (a hang)"),
    "hang": ("I", "executed; no answer ever (a hang)"),
    "after_begin": ("I", "server crash after the key was claimed, before the effect"),
    "after_effect": ("I", "server crash after the effect, before the key was recorded"),
    "after_record": ("I", "server crash after the key was recorded, before the answer"),
    "session_loss": ("D", "the server restarted between handshake and call; session unknown"),
    "drift": ("A", "the argument's field renamed by the caller (schema drift)"),
    "slow": ("F", "no fault: the tool is slow; a deadline must not turn it into a failure"),
}


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def outcome(row: dict[str, Any]) -> str:
    if row.get("success"):
        return "success"
    final = row.get("final") or {}
    kinds = set(row.get("kinds") or ())
    if kinds & UNRESOLVED_KINDS or final.get("code") in UNRESOLVED_CODES:
        return "unresolved"
    return "failed_resolved"


def family(row: dict[str, Any]) -> str:
    if row.get("op") == "drift":
        return "drift"
    if row.get("mode") == "none":
        return "fault_free"
    return str(row.get("family") or "transport")


def load(batch: Path) -> list[dict[str, Any]]:
    suite = batch.parent.name
    rows = []
    for path in sorted(batch.glob("rows*.jsonl")):
        for line in path.read_text().splitlines():
            if line.strip():
                row = json.loads(line)
                row["suite"] = suite
                row.setdefault("kinds", [])
                rows.append(row)
    return rows


def _cell(rows: list[dict[str, Any]], raw: dict[tuple, dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    outcomes = [outcome(r) for r in rows]
    failed = sum(o != "success" for o in outcomes)
    lo, hi = wilson(failed, n)
    entry: dict[str, Any] = {
        "eligible": n,
        "failed": failed,
        "unresolved": outcomes.count("unresolved"),
        "failed_resolved": outcomes.count("failed_resolved"),
        "rfr": round(failed / n, 4) if n else None,
        "ci95": [round(lo, 4), round(hi, 4)],
        "unsafe": {u: sum(u in r["kinds"] for r in rows) for u in UNSAFE},
        "driver_errors": sum("driver_error" in r["kinds"] for r in rows),
    }
    if rows and rows[0]["arm"] != "raw":
        paired = [(r, raw.get(_key(r))) for r in rows]
        entry["induced_vs_raw"] = sum(
            1 for r, b in paired if b is not None and b["success"] and not r["success"]
        )
        entry["recovered_vs_raw"] = sum(
            1 for r, b in paired if b is not None and not b["success"] and r["success"]
        )
        entry["unpaired"] = sum(1 for _, b in paired if b is None)
    return entry


def _key(row: dict[str, Any]) -> tuple:
    return (row["suite"], row["server"], row["op"], row["mode"], row["trial"])


def table(rows: list[dict[str, Any]], by_category: bool = False) -> dict[str, Any]:
    raw = {_key(r): r for r in rows if r["arm"] == "raw"}
    groups: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
    for r in rows:
        fam = family(r)
        groups[(r["suite"], r["server"], r["arm"], fam)].append(r)
        if fam not in ("fault_free", "drift", "slow"):  # every injected fault, together
            groups[(r["suite"], r["server"], r["arm"], "faulted")].append(r)
    out: dict[str, Any] = {}
    for key in sorted(groups, key=_sort):
        out["/".join(key)] = _cell(groups[key], raw)
    result: dict[str, Any] = {"families": out}
    if by_category:
        cats: dict[tuple, list[dict[str, Any]]] = defaultdict(list)
        for r in rows:
            fault = "drift" if r["op"] == "drift" else r["mode"]
            if fault == "none":
                continue
            letter = CATEGORY.get(fault, ("?", ""))[0]
            cats[(r["suite"], r["server"], r["arm"], f"{letter}:{fault}")].append(r)
        result["categories"] = {"/".join(k): _cell(cats[k], raw) for k in sorted(cats, key=_sort)}
    return result


def _sort(key: tuple) -> tuple:
    suite, server, arm, fam = key
    rank = ORDER.index(arm) if arm in ORDER else 99
    frank = FAMILIES.index(fam) if fam in FAMILIES else (len(FAMILIES) if fam == "faulted" else 9)
    return (suite, server, frank, fam, rank)


def _fmt(name: str, e: dict[str, Any]) -> str:
    lo, hi = e["ci95"]
    unsafe = sum(e["unsafe"].values())
    if "induced_vs_raw" in e:
        extra = f" | {e['induced_vs_raw']} | {e['recovered_vs_raw']}"
    else:
        extra = " | - | -"
    return (
        f"| {name} | {e['failed']}/{e['eligible']} | {e['rfr']:.1%} ({lo:.1%}-{hi:.1%}) | "
        f"{e['unresolved']} | {e['failed_resolved']} | {unsafe}{extra} |"
    )


def render(result: dict[str, Any]) -> str:
    head = (
        "| suite/server/arm/family | failed/eligible | RFR (Wilson 95%) | unresolved | "
        "failed, resolved | unsafe | induced vs raw | recovered vs raw |\n"
        "|---|---|---|---|---|---|---|---|"
    )
    lines = [head] + [_fmt(k, e) for k, e in result["families"].items()]
    if "categories" in result:
        lines += ["", head.replace("family", "category:fault")]
        lines += [_fmt(k, e) for k, e in result["categories"].items()]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    out_json = None
    if "--json" in argv:
        i = argv.index("--json")
        out_json = argv[i + 1]
        argv = argv[:i] + argv[i + 2 :]
    by_category = "--by-category" in argv
    batches = [Path(a) for a in argv if a != "--by-category"]
    if not batches:
        raise SystemExit(__doc__)
    rows = [r for b in batches for r in load(b)]
    result = table(rows, by_category)
    result["batches"] = [str(b) for b in batches]
    result["rows"] = len(rows)
    print(f"{len(rows)} rows from {', '.join(str(b) for b in batches)}")
    print(render(result))
    if out_json:
        Path(out_json).write_text(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

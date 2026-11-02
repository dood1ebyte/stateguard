"""
Aggregate a ``contract_v2_relay.py`` results file (isolated tool).

usage: contract_v2_summary.py RESULTS_JSON OUT_JSON

Every number is a count over trials with its denominator. "At answer" means
the sandbox snapshot taken when the client received its answer -- the closest
independent measurement to the probes' observation time (it is slightly later).
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path
from typing import Any


def _q(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    v = sorted(values)
    return {
        "n": len(v),
        "median": round(statistics.median(v), 3),
        "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 3),
        "max": round(v[-1], 3),
    }


def _executed(r: dict[str, Any]) -> bool:
    late = set(r["late_effects"])
    return bool(r["original_ok_by_server"]) or bool(late & {"moved_late", "worker_moved"})


def _exactly_one(r: dict[str, Any]) -> bool:
    """Every request id answered exactly once; reconciled answers while the session was open."""
    if r["wire"] == "none":  # shadow pass-through: the relay adds no answer of its own
        return all(n == 0 for n in r["answers"].values()) or all(
            n == 1 for n in r["answers"].values()
        )
    ok = all(n == 1 for n in r["answers"].values())
    return ok and r["answers_after_close"].get("T-1", 0) == 0


def per_config(results: list[dict[str, Any]], config: str) -> dict[str, Any]:
    rs = [r for r in results if r["config"] == config]
    probed = [r for r in rs if r["probe_count"] > 0]
    posts = {p: sum(r["postcondition"] == p for r in rs)
             for p in ("observed_satisfied", "observed_not_satisfied", "undetermined")}  # fmt: skip
    return {
        "trials": len(rs),
        "postconditions": posts,
        "no_postcondition": sum(r["postcondition"] is None for r in rs),
        "undetermined_rate": f"{posts['undetermined']}/{len(rs)}",
        "satisfied_correct": sum(r["classification"] == "satisfied_correct" for r in rs),
        "satisfied_false_at_answer": sum(
            r["classification"] == "satisfied_false_at_answer" for r in rs
        ),
        "not_satisfied_correct": sum(r["classification"] == "not_satisfied_correct" for r in rs),
        "not_satisfied_false_at_answer": sum(
            r["classification"] == "not_satisfied_false_at_answer" for r in rs
        ),
        "satisfied_but_intent_wrong_at_answer": sum(
            r["postcondition"] == "observed_satisfied" and not r["intended_at_answer"] for r in rs
        ),
        "satisfied_but_original_did_not_cause": sum(
            r["postcondition"] == "observed_satisfied"
            and (not r["original_ok_by_server"] or r["predicate_before"] or bool(r["actor_ops"]))
            for r in rs
        ),
        "satisfied_then_reverted": sum(
            r["postcondition"] == "observed_satisfied" and not r["predicate_final"] for r in rs
        ),
        "not_satisfied_then_executed_later": sum(
            r["postcondition"] == "observed_not_satisfied"
            and bool(set(r["late_effects"]) & {"moved_late", "worker_moved"})
            for r in rs
        ),
        "originals_executed_ground_truth": sum(_executed(r) for r in rs),
        "execution_or_attribution_claims": sum(
            r["claims"]["executionState"] not in (None, "unknown")
            or r["claims"]["attribution"] not in (None, "unknown")
            for r in rs
        ),
        "durability_or_success_claims": sum(
            r["claims"]["durability"] not in (None, "not_established")
            or r["claims"]["operationOutcome"] not in (None, "unknown")
            for r in rs
        ),
        "duplicate_original_deliveries": sum(r["original_deliveries"] > 1 for r in rs),
        "relay_resends": sum(r["later_generation_writes"] > 0 for r in rs),
        "exactly_one_response_per_request": f"{sum(_exactly_one(r) for r in rs)}/{len(rs)}",
        "probes": sum(r["probe_count"] for r in rs),
        "probe_failures": sum(r["probe_failures"] for r in rs),
        "trials_with_probes": len(probed),
        "termination_established": sum(
            any(kind == "scope_ended" and applies for kind, applies in r["termination"]) for r in rs
        ),
        "latency_s": _q([r["latency_s"] for r in rs]),
        "latency_probed_s": _q([r["latency_s"] for r in probed]),
        "observation_window_s": _q(
            [r["observation_window_s"] for r in rs if r["observation_window_s"] is not None]
        ),
        "audit_content_leaks": sum(r["audit_leaks_content"] for r in rs),
        "audit_reconcile_events_with_paths": sum(r["audit_reconcile_paths"] for r in rs),
        "sentinel_changed": sum(not r["sentinel_unchanged"] for r in rs),
    }


def main() -> None:
    data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    results = data["results"]
    configs = sorted({r["config"] for r in results})
    refusal = data.get("refusal", [])
    summary = {
        "trials": len(results),
        "by_config": {c: per_config(results, c) for c in configs},
        "refusal": {
            "runs": len(refusal),
            "exit_code_2": sum(r["exit_code"] == 2 for r in refusal),
            "server_started": sum(r["server_started"] for r in refusal),
            "actionable_message": sum(r["actionable"] for r in refusal),
        },
        "by_scenario": data["summary"],
    }
    Path(sys.argv[2]).write_text(json.dumps(summary, indent=1), encoding="utf-8")
    print(json.dumps({k: summary[k] for k in ("trials", "refusal")}, indent=1))
    for c in configs:
        print(c, json.dumps(summary["by_config"][c]))


if __name__ == "__main__":
    main()

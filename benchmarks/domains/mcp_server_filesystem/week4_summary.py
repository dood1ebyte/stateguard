"""
Derive every Week 4A batch figure from the raw results (isolated tool).

usage: week4_summary.py RESULTS_JSON [CLIENTS_JSON] OUT_JSON

Reads only what ``week4_scope_relay.py`` (and, optionally,
``week4_client_semantics.py``) wrote. Every figure carries its denominator. The
output is deterministic (sorted keys, no timestamps), so running it twice on the
same raw results must give identical bytes.
"""

from __future__ import annotations

import json
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

#: Configurations in which the relay itself may answer the client.
ANSWERING = ("cli_scope", "cli_raw", "cli_naive", "emb_direct")
#: What a reconciled answer may claim, and nothing else.
ALLOWED_CLAIMS = {
    "executionState": (None, "unknown"),
    "attribution": (None, "unknown"),
    "durability": (None, "not_established"),
    "operationOutcome": (None, "unknown"),
    "treatAsSuccess": (None, False),
}


def frac(n: int, d: int) -> str:
    return f"{n}/{d}"


def _dist(values: list[float]) -> dict[str, Any]:
    if not values:
        return {"n": 0}
    v = sorted(values)
    return {
        "n": len(v),
        "median": round(statistics.median(v), 3),
        "p95": round(v[min(len(v) - 1, int(0.95 * len(v)))], 3),
        "max": round(v[-1], 3),
    }


def _false_claims(r: dict[str, Any]) -> list[str]:
    bad = [k for k, ok in ALLOWED_CLAIMS.items() if r["claims"].get(k) not in ok]
    # A successful result for a call that never executed is a false success claim.
    if r["wire"] == "result" and not r["executed"]:
        bad.append("success_result_without_execution")
    return bad


def _observation_correct(r: dict[str, Any]) -> bool | None:
    if r["postcondition"] == "observed_satisfied":
        return bool(r["predicate_at_answer"])
    if r["postcondition"] == "observed_not_satisfied":
        return not r["predicate_at_answer"]
    return None


def _duplicate(r: dict[str, Any]) -> bool:
    return bool(
        r["original_deliveries"] > 1
        or r["later_generation_writes"] > 0
        or (r["executed"] and r["resend_executed"])
    )


def summarize(results: list[dict[str, Any]], refusal: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    by_config: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for r in results:
        by_config[r["config"]].append(r)
    scenarios = sorted({r["scenario"] for r in results})
    out["design"] = {
        "scenarios": len(scenarios),
        "scenario_ids": scenarios,
        "configs": sorted(by_config),
        "scenario_config_cells": len({(r["scenario"], r["config"]) for r in results}),
        "reps_per_cell": dict(
            sorted(Counter(Counter((r["scenario"], r["config"]) for r in results).values()).items())
        ),
        "trials": len(results),
        "trials_by_config": {c: len(v) for c, v in sorted(by_config.items())},
    }
    excluded = [
        f"{r['scenario']}-{r['config']}-{r['rep']}" for r in results if not r["initialized"]
    ]
    out["excluded_or_incomplete"] = {
        "not_initialized": excluded,
        "reruns": [],
        "note": "no trial was excluded or re-run; every trial is counted",
    }
    per: dict[str, Any] = {}
    for config, rs in sorted(by_config.items()):
        n = len(rs)
        verdicts = [r for r in rs if r["postcondition"] in ("observed_satisfied",
                                                            "observed_not_satisfied")]  # fmt: skip
        correct = sum(bool(_observation_correct(r)) for r in verdicts)
        false = [(f"{r['scenario']}-{r['rep']}", _false_claims(r)) for r in rs if _false_claims(r)]
        requests = [(r, rid, k) for r in rs for rid, k in r["answers"].items()]
        per[config] = {
            "trials": n,
            "executed_by_ground_truth": frac(sum(r["executed"] for r in rs), n),
            # The hand-off fixture's effect is made by an external worker, after the answer.
            "executed_including_external_worker": frac(
                sum(r["executed"] or "worker_moved" in r["late_effects"] for r in rs), n
            ),
            "predicate_true_at_answer": frac(sum(r["predicate_at_answer"] for r in rs), n),
            "postconditions": dict(sorted(Counter(str(r["postcondition"]) for r in rs).items())),
            "observations_correct": frac(correct, len(verdicts)),
            "observations_incorrect": sorted(
                f"{r['scenario']}-{r['rep']}" for r in verdicts if not _observation_correct(r)
            ),
            "undetermined": frac(sum(r["postcondition"] == "undetermined" for r in rs), n),
            "trials_with_false_claims": frac(len(false), n),
            "false_claims": false,
            "duplicate_side_effects": frac(sum(_duplicate(r) for r in rs), n),
            "relay_resends_of_original": sum(r["later_generation_writes"] for r in rs),
            "client_resends": sum("T-1-resend" in r["answers"] for r in rs),
            "client_resends_that_executed": sum(r["resend_executed"] for r in rs),
            "requests": len(requests),
            "requests_answered_exactly_once": frac(
                sum(k == 1 for _, _, k in requests), len(requests)
            ),  # fmt: skip
            "requests_answered_more_than_once": sum(k > 1 for _, _, k in requests),
            "requests_unanswered": sorted(
                f"{r['scenario']}-{r['rep']}:{rid}" for r, rid, k in requests if k == 0
            ),
            "answers_only_at_close": sum(
                sum(v for v in r["answers_after_close"].values()) > 0 for r in rs
            ),
            "termination_evidence": dict(
                sorted(Counter(f"{k}/{a}" for r in rs for k, a in r["termination"]).items())
            ),  # fmt: skip
            "scope_unconfirmed_trials": sorted(
                f"{r['scenario']}-{r['rep']}"
                for r in rs
                if any(k == "scope_unconfirmed" for k, _ in r["termination"])
            ),
            "probes": sum(r["probe_count"] for r in rs),
            "probe_failures": sum(r["probe_failures"] for r in rs),
            "trials_with_probes": frac(sum(r["probe_count"] > 0 for r in rs), n),
            "budget_exhausted_events": sum(r["budget_exhausted"] for r in rs),
            "answers_unconfirmed_events": sum(r["answers_unconfirmed"] for r in rs),
            "latency_s": _dist([r["latency_s"] for r in rs]),
            "observation_window_s": _dist(
                [r["observation_window_s"] for r in rs if r["observation_window_s"] is not None]
            ),
            "late_effects": dict(sorted(Counter(e for r in rs for e in r["late_effects"]).items())),
            "satisfied_then_changed_by_end": frac(
                sum(
                    r["postcondition"] == "observed_satisfied" and not r["predicate_final"]
                    for r in rs
                ),
                sum(r["postcondition"] == "observed_satisfied" for r in rs),
            ),  # fmt: skip
            "orphans": sum(r["orphans"] for r in rs),
            "trials_with_orphans": sorted(
                f"{r['scenario']}-{r['rep']}" for r in rs if r["orphans"]
            ),
            "privacy": {
                "audit_leaks_content": sum(r["audit_leaks_content"] for r in rs),
                "audit_reconcile_events_with_paths": sum(r["audit_reconcile_paths"] for r in rs),
                "sentinel_changed": sum(not r["sentinel_unchanged"] for r in rs),
            },
        }
    out["by_config"] = per
    cells: dict[str, Any] = {}
    for key in sorted({(r["scenario"], r["config"]) for r in results}):
        rs = [r for r in results if (r["scenario"], r["config"]) == key]
        cells[f"{key[0]}/{key[1]}"] = {
            "trials": len(rs),
            "executed": sum(r["executed"] for r in rs),
            "predicate_at_answer": sum(r["predicate_at_answer"] for r in rs),
            "classes": dict(sorted(Counter(r["classification"] for r in rs).items())),
            "termination": dict(
                sorted(Counter(f"{k}/{a}" for r in rs for k, a in r["termination"]).items())
            ),  # fmt: skip
            "probes": sum(r["probe_count"] for r in rs),
            "late_effects": sum(len(r["late_effects"]) for r in rs),
            "orphans": sum(r["orphans"] for r in rs),
            "latency_median_s": _dist([r["latency_s"] for r in rs]).get("median"),
        }
    out["cells"] = cells
    # Shadow byte fidelity: the client's whole stream, shadow (scope absent and
    # present) against no relay at all, for the same scenario and repetition.
    streams = {(r["scenario"], r["config"], r["rep"]): r["client_stream_sha256"] for r in results}
    pairs: dict[str, list[bool]] = {"cli_shadow": [], "cli_shadow_scope": []}
    for (sc, config, rep), digest in sorted(streams.items()):
        if config in pairs and (sc, "direct", rep) in streams:
            pairs[config].append(digest == streams[(sc, "direct", rep)])
    out["shadow_byte_fidelity"] = {c: frac(sum(v), len(v)) for c, v in sorted(pairs.items())}
    answering = [r for r in results if r["config"] in ANSWERING]
    reqs = [k for r in answering for k in r["answers"].values()]
    allreqs = [k for r in results for k in r["answers"].values()]
    out["answers"] = {
        "answering_configs_requests_exactly_once": frac(sum(k == 1 for k in reqs), len(reqs)),
        "all_configs_requests_at_most_once": frac(sum(k <= 1 for k in allreqs), len(allreqs)),
        "pass_through_configs_unanswered_requests": sum(
            k == 0 for r in results if r["config"] not in ANSWERING for k in r["answers"].values()
        ),
    }
    out["refusal"] = {
        "cases": len(refusal),
        "exit_code_2": frac(sum(c["exit_code"] == 2 for c in refusal), len(refusal)),
        "server_started": sum(c["server_started"] for c in refusal),
        "actionable_message": frac(sum(c["actionable"] for c in refusal), len(refusal)),
        "detail": {
            c["case"]: [c["exit_code"], c["server_started"], c["actionable"]] for c in refusal
        },  # fmt: skip
    }
    return out


def clients(data: dict[str, Any]) -> dict[str, Any]:
    out = {}
    for key, r in sorted(data.items()):
        response = r.get("response") if isinstance(r.get("response"), dict) else None
        if response is not None:  # the raw JSON-RPC client keeps the whole response
            error = response.get("error") or {}
            payload = error.get("data") or {}
            code, post = error.get("code"), payload.get("io.stateguard/postcondition")
            keys = sorted(payload)
            raised = "error" in response and "result" not in response
        else:
            code, post = r.get("code"), r.get("postcondition")
            keys = r.get("data_keys") or []
            raised = bool(r.get("raised"))
        out[key] = {
            "error_not_result": raised,
            "code": code,
            "postcondition": post,
            "treatAsSuccess_in_data": "io.stateguard/treatAsSuccess" in keys,
            "termination": r.get("termination"),
            "final_checkpoint": r.get("final_checkpoint"),
        }
    return out


def main() -> None:
    args = sys.argv[1:]
    raw = json.loads(Path(args[0]).read_text())
    summary = summarize(raw["results"], raw.get("refusal", []))
    if len(args) == 3:
        summary["client_semantics"] = clients(json.loads(Path(args[1]).read_text()))
    Path(args[-1]).write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    d = summary
    print("design:", json.dumps(d["design"], sort_keys=True))
    for config, row in d["by_config"].items():
        print(f"\n[{config}] trials={row['trials']}")
        for key in ("executed_by_ground_truth", "predicate_true_at_answer", "postconditions",
                    "observations_correct", "undetermined", "trials_with_false_claims",
                    "duplicate_side_effects", "relay_resends_of_original", "client_resends",
                    "requests_answered_exactly_once", "requests_unanswered",
                    "termination_evidence", "scope_unconfirmed_trials", "probes",
                    "probe_failures", "latency_s", "late_effects",
                    "satisfied_then_changed_by_end", "orphans", "privacy"):  # fmt: skip
            print(f"  {key}: {row[key]}")
    print("\nshadow_byte_fidelity:", d["shadow_byte_fidelity"])
    print("answers:", d["answers"])
    print("refusal:", {k: v for k, v in d["refusal"].items() if k != "detail"})
    if "client_semantics" in d:
        print("client_semantics:", json.dumps(d["client_semantics"], sort_keys=True))


if __name__ == "__main__":
    main()

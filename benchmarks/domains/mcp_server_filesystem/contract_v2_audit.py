"""
Independent audit of the Week 3 contract-v2 batch figures (isolated tool).

usage: contract_v2_audit.py EVIDENCE_DIR OUT_JSON

EVIDENCE_DIR is ``/root/stateguard-captures/week3-contract-v2``. Every figure is
re-derived from raw evidence only:

* ``batch/results.json``            the per-trial records written by the runner;
* ``batch/ws/trials/<tag>/audit.jsonl``  the relay's own audit log per trial;
* ``batch-s12a/results.json``       the separate S12a re-run.

and then compared with the figure the report
(``docs/runtime/WEEK3_RECONCILIATION_CONTRACT_V2.md``) printed. A figure that
cannot be reconstructed from raw evidence is reported as unavailable, with why.
"""

from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any

#: Figures as printed in WEEK3_RECONCILIATION_CONTRACT_V2.md (the claims under audit).
REPORTED: dict[str, Any] = {
    "emb_pidns.trials": 188,
    "emb_pidns.observed_satisfied": 60,
    "emb_pidns.observed_not_satisfied": 55,
    "emb_pidns.undetermined": 53,
    "emb_pidns.satisfied_correct": 50,
    "emb_pidns.satisfied_false_at_answer": 10,
    "emb_pidns.not_satisfied_correct": 55,
    "emb_pidns.restart_reports_established": 168,
    "emb_pidns.trials_with_established_restart": 158,
    "emb_pidns.probes": 290,
    "emb_pidns.probe_failures": 45,
    "emb_pidns.trials_with_probes": 145,
    "emb_direct.undetermined": 168,
    "emb_direct.restart_reports_insufficient": 158,
    "held_reconciliations_all_configs": 306,
    "held_written_to_stream": 306,
    "held_unconfirmed": 0,
    "exactly_one_response_all_trials": 426,
}


def _events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def audit(evidence: Path) -> dict[str, Any]:
    results = json.loads((evidence / "batch" / "results.json").read_text())["results"]
    trials_dir = evidence / "batch" / "ws" / "trials"
    derived: dict[str, Any] = {}
    notes: dict[str, str] = {}

    by_config: dict[str, list[dict[str, Any]]] = {}
    for r in results:
        by_config.setdefault(r["config"], []).append(r)
    derived["trials_total"] = len(results)
    derived["trials_by_config"] = {c: len(v) for c, v in sorted(by_config.items())}

    # --- the 188 vs 168 question ----------------------------------------------
    for config in ("emb_pidns", "emb_direct"):
        rs = by_config.get(config, [])
        posts = Counter(str(r["postcondition"]) for r in rs)
        derived[f"{config}.trials"] = len(rs)
        derived[f"{config}.observed_satisfied"] = posts["observed_satisfied"]
        derived[f"{config}.observed_not_satisfied"] = posts["observed_not_satisfied"]
        derived[f"{config}.undetermined"] = posts["undetermined"]
        none = [r for r in rs if r["postcondition"] is None]
        derived[f"{config}.no_postcondition"] = len(none)
        derived[f"{config}.no_postcondition_by_scenario"] = dict(
            sorted(Counter(r["scenario"] for r in none).items())
        )
        classes = Counter(r["classification"] for r in rs)
        for name in ("satisfied_correct", "satisfied_false_at_answer", "not_satisfied_correct"):
            derived[f"{config}.{name}"] = classes[name]
        derived[f"{config}.probes"] = sum(r["probe_count"] for r in rs)
        derived[f"{config}.probe_failures"] = sum(r["probe_failures"] for r in rs)
        derived[f"{config}.trials_with_probes"] = sum(r["probe_count"] > 0 for r in rs)

    # --- restart reports vs trials, held vs answered, from the audit logs -------
    restart_reports: Counter[tuple[str, str, bool]] = Counter()
    trials_with_restart: Counter[str] = Counter()
    held = answered = written = unconfirmed = 0
    probe_events = probe_completed = 0
    held_not_answered_once: list[str] = []
    missing_logs: list[str] = []
    for r in results:
        tag = f"{r['scenario']}-{r['config']}-{r['rep']}"
        log = trials_dir / tag / "audit.jsonl"
        if not log.exists():
            missing_logs.append(tag)
            continue
        events = _events(log)
        reports = [e for e in events if e.get("event") == "executor_termination"]
        for e in reports:
            restart_reports[(r["config"], str(e.get("kind")), bool(e.get("applies")))] += 1
        if reports:
            trials_with_restart[r["config"]] += 1
        pending = [e for e in events if e.get("event") == "reconcile_pending"]
        done = [e for e in events if e.get("event") == "reconcile_answered"]
        held += len(pending)
        answered += len(done)
        written += sum(1 for e in done if e.get("written_to_stream") is True)
        unconfirmed += sum(1 for e in events if e.get("event") == "reconcile_answer_unconfirmed")
        probes = [e for e in events if e.get("event") == "reconcile_probe"]
        probe_events += len(probes)
        probe_completed += sum(1 for p in probes if p.get("status") in ("satisfied", "unsatisfied"))
        if pending and r["answers"].get("T-1") != 1:
            held_not_answered_once.append(tag)
        if len(probes) != r["probe_count"]:
            notes[f"probe_mismatch:{tag}"] = f"{len(probes)} events vs {r['probe_count']} recorded"
    derived["audit_logs_missing"] = missing_logs
    derived["restart_reports"] = {
        f"{c}/{k}/{a}": n for (c, k, a), n in sorted(restart_reports.items())
    }
    derived["trials_with_restart_report"] = dict(sorted(trials_with_restart.items()))
    derived["emb_pidns.restart_reports_established"] = restart_reports[
        ("emb_pidns", "scope_ended", True)
    ]
    derived["emb_pidns.trials_with_established_restart"] = trials_with_restart["emb_pidns"]
    derived["emb_direct.restart_reports_insufficient"] = restart_reports[
        ("emb_direct", "direct_child_reaped", False)
    ]
    derived["held_reconciliations_all_configs"] = held
    derived["held_answered_events"] = answered
    derived["held_written_to_stream"] = written
    derived["held_unconfirmed"] = unconfirmed
    derived["held_trials_without_exactly_one_client_answer"] = held_not_answered_once
    derived["probe_events_in_audit_logs"] = probe_events
    derived["probe_checks_completed"] = probe_completed
    derived["probe_checks_failed"] = probe_events - probe_completed

    # --- exactly-once, from the client's end-of-stream record -------------------
    exactly = after_close = 0
    after_close_tags: list[str] = []
    for r in results:
        counts = r["answers"]
        late = r["answers_after_close"]
        if r["wire"] == "none":  # shadow pass-through: the relay adds no answer of its own
            ok = all(n in (0, 1) for n in counts.values())
        else:
            ok = all(n == 1 for n in counts.values())
        exactly += ok
        if any(late.values()):
            after_close += 1
            after_close_tags.append(f"{r['scenario']}-{r['config']}-{r['rep']}:{late}")
    derived["exactly_one_response_all_trials"] = exactly
    # "Exactly one" is wrong wording for a request the relay never answered at all:
    # shadow trials whose server died -- the client saw the pipe close, as with no relay.
    zero = [r for r in results if r["answers"].get("T-1", 0) == 0]
    derived["trials_with_T1_unanswered"] = len(zero)
    derived["trials_with_T1_unanswered_by_scenario_config"] = dict(
        sorted(Counter(f"{r['scenario']}/{r['config']}" for r in zero).items())
    )
    derived["trials_with_every_request_answered_exactly_once"] = sum(
        all(n == 1 for n in r["answers"].values()) for r in results
    )
    derived["trials_at_most_one_response_per_request"] = sum(
        all(n <= 1 for n in r["answers"].values()) for r in results
    )
    for config, rs in sorted(by_config.items()):
        derived[f"{config}.trials_every_request_exactly_once"] = sum(
            all(n == 1 for n in r["answers"].values()) for r in rs
        )
        derived[f"{config}.trials_at_most_one_response_per_request"] = sum(
            all(n <= 1 for n in r["answers"].values()) for r in rs
        )
    derived["trials_with_any_answer_after_client_close"] = after_close
    derived["answers_after_close_detail"] = after_close_tags

    # --- the S12a follow-up ---------------------------------------------------------
    rerun_file = evidence / "batch-s12a" / "results.json"
    if rerun_file.exists():
        rerun = json.loads(rerun_file.read_text())["results"]
        derived["s12a_rerun_trials"] = len(rerun)
        derived["s12a_rerun_T2_answered_while_open"] = sum(
            r["answers"].get("T-2") == 1 and r["answers_after_close"].get("T-2", 0) == 0
            for r in rerun
        )
        derived["s12a_rerun_by_config"] = dict(Counter(r["config"] for r in rerun))
        notes["s12a"] = (
            "the main batch used the runner before the T-2 wait (batch/code-under-test.sha256 "
            "hash 6a… for contract_v2_relay.py differs from the final runner); the re-run used "
            "the final runner; the re-run does not replace the main-batch rows"
        )

    # --- compare with what the report printed ------------------------------------------
    comparison = {}
    for key, claimed in REPORTED.items():
        got = derived.get(key)
        comparison[key] = {"reported": claimed, "derived": got, "match": got == claimed}
    return {"derived": derived, "comparison": comparison, "notes": notes}


def main() -> None:
    evidence, out = Path(sys.argv[1]), Path(sys.argv[2])
    report = audit(evidence)
    out.write_text(json.dumps(report, indent=1), encoding="utf-8")
    for key, row in report["comparison"].items():
        flag = "ok  " if row["match"] else "DIFF"
        print(f"{flag} {key:45} reported={row['reported']!s:6} derived={row['derived']}")
    d = report["derived"]
    for key in ("emb_pidns.no_postcondition", "emb_pidns.no_postcondition_by_scenario",
                "trials_with_restart_report", "restart_reports", "held_answered_events",
                "held_trials_without_exactly_one_client_answer", "probe_events_in_audit_logs",
                "probe_checks_completed", "trials_with_any_answer_after_client_close",
                "answers_after_close_detail", "audit_logs_missing", "s12a_rerun_trials",
                "s12a_rerun_T2_answered_while_open", "s12a_rerun_by_config",
                "trials_with_T1_unanswered", "trials_with_T1_unanswered_by_scenario_config",
                "trials_with_every_request_answered_exactly_once",
                "trials_at_most_one_response_per_request",
                *(f"{c}.trials_every_request_exactly_once" for c in sorted(d["trials_by_config"])),
                *(f"{c}.trials_at_most_one_response_per_request"
                  for c in sorted(d["trials_by_config"]))):  # fmt: skip
        print(f"     {key}: {d.get(key)}")


if __name__ == "__main__":
    main()

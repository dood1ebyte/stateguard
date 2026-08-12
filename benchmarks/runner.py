#!/usr/bin/env python3
"""
StateGuard benchmark runner.

Loads every case in ``benchmarks/cases/*.json``, runs it through
``ContractGuard.with_dict_schema()``, compares the actual outcome against
the case's ``expected_result``, and prints + persists a summary.

Usage
-----
::

    python benchmarks/runner.py
    python benchmarks/runner.py --cases-dir benchmarks/cases --results-dir benchmarks/results
    python benchmarks/runner.py --verbose

Case format
-----------
Each ``benchmarks/cases/*.json`` file is a single JSON object::

    {
      "name": "short_unique_identifier",
      "description": "Human-readable explanation of what this case proves.",
      "expected_schema": { ... DictContractAdapter schema ... },
      "broken_payload": { ... payload to repair ... },
      "expected_result": {
        "status": "success" | "partial" | "failed" | "already_valid",
        "min_confidence": 0.0-1.0   # optional; only checked for non-failed cases
      }
    }

See ``benchmarks/README.md`` for the full format specification and
guidance on adding new cases.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Allow running as `python benchmarks/runner.py` without installing the
# package, by adding the repo's src/ to sys.path if stateguard isn't
# already importable.
try:
    import stateguard  # noqa: F401
except ImportError:
    _repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(_repo_root / "src"))

from stateguard.guard import ContractGuard  # noqa: E402


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class CaseOutcome:
    """The outcome of running a single benchmark case."""

    name: str
    description: str
    expected_status: str
    actual_status: str
    passed: bool
    trusts: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def average_trust(self) -> float | None:
        if not self.trusts:
            return None
        return sum(self.trusts) / len(self.trusts)


@dataclass
class BenchmarkSummary:
    """Aggregate results across all cases in a benchmark run."""

    timestamp: str
    total_cases: int
    passed_cases: int
    failed_cases: int

    # A case is *repairable* when its own expected_result says so. Cases
    # expecting "already_valid" have nothing to repair, and the case expecting
    # "failed" exists to prove StateGuard refuses to guess -- repairing it
    # would be a bug. Dividing by total_cases counted both as repair failures,
    # which made 77.8% the maximum achievable score on a suite where every
    # case was behaving correctly.
    #
    # A repairable case counts in the numerator only when it *passed* -- the
    # status matches exactly and any declared min_confidence was met. Asking
    # only whether the outcome landed somewhere in {success, partial} let a
    # case degrade from success to partial while repair_rate still read 100%,
    # so the headline number could not see the regression it exists to catch.
    repairable_cases: int
    repaired_correctly: int
    repair_rate: float  # repaired_correctly / repairable_cases -- recall

    # Of the cases StateGuard chose to repair, how many should it have?
    # A case that repairs when its expected_result says "failed" is a false
    # positive: a silently wrong repair, which is the failure mode that
    # actually costs users data.
    attempted_repairs: int
    false_positives: int
    precision: float | None

    average_trust: float | None
    outcomes: list[CaseOutcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "repairable_cases": self.repairable_cases,
            "repaired_correctly": self.repaired_correctly,
            "repair_rate": self.repair_rate,
            "attempted_repairs": self.attempted_repairs,
            "false_positives": self.false_positives,
            "precision": self.precision,
            "average_trust": self.average_trust,
            "outcomes": [
                {
                    "name": o.name,
                    "description": o.description,
                    "expected_status": o.expected_status,
                    "actual_status": o.actual_status,
                    "passed": o.passed,
                    "average_trust": o.average_trust,
                    "error": o.error,
                }
                for o in self.outcomes
            ],
        }


# ---------------------------------------------------------------------------
# Case loading
# ---------------------------------------------------------------------------


def load_cases(cases_dir: Path) -> list[dict[str, Any]]:
    """Load every ``*.json`` file in *cases_dir*, sorted by filename."""
    cases = []
    for case_file in sorted(cases_dir.glob("*.json")):
        with open(case_file, encoding="utf-8") as f:
            case = json.load(f)
        case["_source_file"] = case_file.name
        cases.append(case)
    return cases


# ---------------------------------------------------------------------------
# Running a single case
# ---------------------------------------------------------------------------


def run_case(case: dict[str, Any]) -> CaseOutcome:
    """Run a single benchmark case and return its outcome."""
    name = case.get("name", case.get("_source_file", "<unnamed>"))
    description = case.get("description", "")
    expected = case.get("expected_result", {})
    expected_status = expected.get("status", "")

    try:
        guard = ContractGuard.with_dict_schema()
        # Use a fresh copy of the payload -- ContractGuard.repair never
        # mutates its input, but this keeps each case fully independent
        # regardless of that guarantee.
        payload = json.loads(json.dumps(case["broken_payload"]))
        result = guard.repair(case["expected_schema"], payload)

        actual_status = result.status.value
        passed = actual_status == expected_status

        # `min_confidence` keeps its name in the case files (it is the on-disk
        # format), but the value it is compared against is the policy-computed
        # trust score -- op.confidence is a deprecated read-only alias due to
        # be removed, and reads identically.
        min_confidence = expected.get("min_confidence")
        trusts = [op.trust for attempt in result.attempts for op in attempt.applied_operations]
        if passed and min_confidence is not None and trusts and min(trusts) < min_confidence:
            passed = False

        return CaseOutcome(
            name=name,
            description=description,
            expected_status=expected_status,
            actual_status=actual_status,
            passed=passed,
            trusts=trusts,
        )
    except Exception as exc:  # noqa: BLE001 -- a case-level crash must not kill the run
        return CaseOutcome(
            name=name,
            description=description,
            expected_status=expected_status,
            actual_status="error",
            passed=False,
            error=f"{type(exc).__name__}: {exc}",
        )


# ---------------------------------------------------------------------------
# Running the full suite
# ---------------------------------------------------------------------------


def run_benchmark(cases: list[dict[str, Any]]) -> BenchmarkSummary:
    """Run every case in *cases* and aggregate the results."""
    outcomes = [run_case(case) for case in cases]

    total = len(outcomes)
    passed = sum(1 for o in outcomes if o.passed)
    failed = total - passed

    did_repair = {"success", "partial"}
    repairable = [o for o in outcomes if o.expected_status in did_repair]
    attempted = [o for o in outcomes if o.actual_status in did_repair]

    # `o.passed`, not `o.actual_status in did_repair`: a repairable case only
    # counts as repaired when it landed on the status it was supposed to.
    repaired_correctly = sum(1 for o in repairable if o.passed)
    false_positives = sum(1 for o in attempted if o.expected_status not in did_repair)

    repair_rate = repaired_correctly / len(repairable) if repairable else 0.0
    precision = (len(attempted) - false_positives) / len(attempted) if attempted else None

    all_trust = [t for o in outcomes for t in o.trusts]
    average_trust = sum(all_trust) / len(all_trust) if all_trust else None

    return BenchmarkSummary(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        repairable_cases=len(repairable),
        repaired_correctly=repaired_correctly,
        repair_rate=repair_rate,
        attempted_repairs=len(attempted),
        false_positives=false_positives,
        precision=precision,
        average_trust=average_trust,
        outcomes=outcomes,
    )


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_summary(summary: BenchmarkSummary, verbose: bool = False) -> None:
    """Print a human-readable summary table to stdout."""
    print()
    print("StateGuard Benchmark Results")
    print("=" * 60)
    print(f"  Total cases:        {summary.total_cases}")
    print(f"  Passed:             {summary.passed_cases}")
    print(f"  Failed:             {summary.failed_cases}")
    print()
    print(
        f"  Repair rate:        {summary.repair_rate:.1%}  "
        f"({summary.repaired_correctly}/{summary.repairable_cases} repairable cases repaired)"
    )
    if summary.precision is None:
        print("  Precision:          n/a  (no repairs attempted)")
    else:
        print(
            f"  Precision:          {summary.precision:.1%}  "
            f"({summary.false_positives} wrong repair(s) "
            f"of {summary.attempted_repairs} attempted)"
        )
    avg = summary.average_trust
    print(f"  Average trust:      {avg:.3f}" if avg is not None else "  Average trust:      n/a")
    print("=" * 60)
    print()

    for outcome in summary.outcomes:
        icon = "✓" if outcome.passed else "✗"
        line = f"  {icon} {outcome.name:45} expected={outcome.expected_status:14} actual={outcome.actual_status}"
        print(line)
        if outcome.error:
            print(f"      ERROR: {outcome.error}")
        elif verbose and outcome.description:
            print(f"      {outcome.description}")
    print()


def write_results(summary: BenchmarkSummary, results_dir: Path) -> Path:
    """Write *summary* as a timestamped JSON file in *results_dir*."""
    results_dir.mkdir(parents=True, exist_ok=True)
    safe_timestamp = summary.timestamp.replace(":", "-")
    out_path = results_dir / f"run_{safe_timestamp}.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(summary.to_dict(), f, indent=2, default=str)
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the StateGuard benchmark suite.")
    parser.add_argument(
        "--cases-dir",
        type=Path,
        default=Path(__file__).parent / "cases",
        help="Directory containing benchmark case JSON files (default: benchmarks/cases).",
    )
    parser.add_argument(
        "--results-dir",
        type=Path,
        default=Path(__file__).parent / "results",
        help="Directory to write the results JSON file to (default: benchmarks/results).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each case's description alongside its result.",
    )
    parser.add_argument(
        "--no-write",
        action="store_true",
        help="Print the summary but do not write a results file.",
    )
    args = parser.parse_args(argv)

    cases = load_cases(args.cases_dir)
    if not cases:
        print(f"No benchmark cases found in {args.cases_dir}", file=sys.stderr)
        return 1

    summary = run_benchmark(cases)
    print_summary(summary, verbose=args.verbose)

    if not args.no_write:
        out_path = write_results(summary, args.results_dir)
        print(f"Results written to: {out_path}")

    return 0 if summary.failed_cases == 0 else 1


if __name__ == "__main__":
    sys.exit(main())

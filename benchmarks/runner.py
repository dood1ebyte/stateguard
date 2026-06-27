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
    confidences: list[float] = field(default_factory=list)
    error: str | None = None

    @property
    def average_confidence(self) -> float | None:
        if not self.confidences:
            return None
        return sum(self.confidences) / len(self.confidences)


@dataclass
class BenchmarkSummary:
    """Aggregate results across all cases in a benchmark run."""

    timestamp: str
    total_cases: int
    passed_cases: int
    failed_cases: int
    repaired_cases: int  # cases whose actual_status is success or partial
    repair_rate: float  # repaired_cases / total_cases
    average_confidence: float | None
    outcomes: list[CaseOutcome]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "total_cases": self.total_cases,
            "passed_cases": self.passed_cases,
            "failed_cases": self.failed_cases,
            "repaired_cases": self.repaired_cases,
            "repair_rate": self.repair_rate,
            "average_confidence": self.average_confidence,
            "outcomes": [
                {
                    "name": o.name,
                    "description": o.description,
                    "expected_status": o.expected_status,
                    "actual_status": o.actual_status,
                    "passed": o.passed,
                    "average_confidence": o.average_confidence,
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

        min_confidence = expected.get("min_confidence")
        confidences = [
            op.confidence
            for attempt in result.attempts
            for op in attempt.applied_operations
        ]
        if passed and min_confidence is not None and confidences:
            if min(confidences) < min_confidence:
                passed = False

        return CaseOutcome(
            name=name,
            description=description,
            expected_status=expected_status,
            actual_status=actual_status,
            passed=passed,
            confidences=confidences,
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
    repaired = sum(1 for o in outcomes if o.actual_status in ("success", "partial"))
    repair_rate = repaired / total if total else 0.0

    all_confidences = [c for o in outcomes for c in o.confidences]
    average_confidence = (
        sum(all_confidences) / len(all_confidences) if all_confidences else None
    )

    return BenchmarkSummary(
        timestamp=datetime.now(timezone.utc).isoformat(),
        total_cases=total,
        passed_cases=passed,
        failed_cases=failed,
        repaired_cases=repaired,
        repair_rate=repair_rate,
        average_confidence=average_confidence,
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
    print(f"  Repaired:           {summary.repaired_cases}")
    print(f"  Repair rate:        {summary.repair_rate:.1%}")
    avg = summary.average_confidence
    print(f"  Average confidence: {avg:.3f}" if avg is not None else "  Average confidence: n/a")
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
    parser = argparse.ArgumentParser(
        description="Run the StateGuard benchmark suite."
    )
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

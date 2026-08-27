#!/usr/bin/env python3
"""
StateGuard calibration harness.

Answers the one question the case-based benchmark cannot: **when StateGuard
says trust 0.85, is it right about 85% of the time?**

Until this existed, "trust score" was a rename. The bands in
``stateguard.core.trust`` were *fitted* to a handful of required outcomes,
which is not the same as being calibrated -- fitting says "these five cases
land on the right side of the line", calibration says "the number means what
it claims across a corpus you did not tune it on".

Usage
-----
::

    python benchmarks/calibrate.py
    python benchmarks/calibrate.py --verbose        # explain every gap
    python benchmarks/calibrate.py --corpus-dir benchmarks/calibration

Exit code
---------
``1`` on a **regression** (a case failing that the corpus does not already
document) or on a **stale marker** (a documented gap that now passes).
``0`` otherwise -- a documented gap failing is the corpus doing its job, not
CI breaking, so it does not block.

That split is what lets the harness be a gate *and* an honest record at the
same time. Without it the only options are to delete the cases the engine
gets wrong, which hides them, or to leave the build red forever, which trains
people to ignore it.

Corpus format
-------------
Each file in ``benchmarks/calibration/`` is one JSON object::

    {
      "verdict": "repair" | "abstain" | "refuse",
      "description": "why this family exists",
      "cases": [
        {
          "name": "...",
          "description": "...",
          "schema": { ...DictContractAdapter schema... },
          "payload": <the input, any JSON value>,
          "expected_payload": <what the payload should look like when
                               StateGuard is done>,
          "known_gap": "optional -- why this case is currently accepted
                        as failing, with the measurement behind it"
        }
      ]
    }

``expected_payload`` is the ground truth for *every* verdict, not just
``repair``:

* ``repair``  -- the correctly repaired payload;
* ``abstain`` -- the payload with any legitimate side-repairs applied and the
  contested one left alone;
* ``refuse``  -- identical to ``payload``.

Having one ground-truth payload for every case is what makes per-operation
scoring possible, which is what the reliability curve needs.

How an operation is judged correct
-----------------------------------
An applied operation is correct when the **final** payload at its
``target_path`` matches the ground truth at that path.

Judging against the *final* state rather than the operation's immediate
effect is deliberate. A rename that exposes a type mismatch leaves ``"31.5"``
at ``temperature``, which the next pass coerces to ``31.5``; scoring the
rename against its own intermediate value would mark a correct field
correspondence wrong and systematically under-report calibration. The
question the curve asks is "when the engine was this confident, did the field
end up right", and the final state is what answers it.

A wrong operation is therefore one that put a value somewhere the ground
truth does not have it -- including a rename into a field that should have
stayed empty, which is exactly the ``user_email`` failure mode.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import stateguard  # noqa: F401
except ImportError:  # pragma: no cover -- convenience for uninstalled runs
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from stateguard.core.models.config import GuardConfig  # noqa: E402
from stateguard.guard import ContractGuard  # noqa: E402

#: Width of each reliability bucket. 0.05 rather than the 0.1 a textbook
#: reliability diagram uses, because the engine's trust values cluster hard at
#: 0.80/0.85/0.95/1.00 -- at 0.1 resolution almost everything lands in a
#: single bucket and the curve says nothing.
BUCKET_WIDTH = 0.05

_ABSENT = object()


# ---------------------------------------------------------------------------
# Comparison
# ---------------------------------------------------------------------------


def identical(left: Any, right: Any) -> bool:
    """
    Value equality that does not conflate values of different types.

    ``5 == 5.0`` and ``1 == True`` are both ``True`` in Python, so a harness
    using ``==`` would score a coercion correct that produced an int where the
    ground truth says float. For a harness whose entire output is a claim
    about correctness, that is not a distinction to lose.
    """
    if left is _ABSENT or right is _ABSENT:
        return left is right
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            identical(left[k], right[k]) for k in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            identical(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def value_at(payload: Any, path: str) -> Any:
    """Value at dot-notation *path*, or ``_ABSENT``."""
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _ABSENT
        current = current[part]
    return current


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass
class OperationOutcome:
    """One applied operation, and whether it was the right thing to do."""

    case: str
    verdict: str
    op_type: str
    target_path: str
    trust: float
    risk: str
    correct: bool


@dataclass
class CaseOutcome:
    name: str
    verdict: str
    description: str
    passed: bool
    reason: str
    status: str
    operations: list[OperationOutcome] = field(default_factory=list)
    #: Set when the corpus documents this case as a measured, accepted gap
    #: rather than a bug to be caught. Carries the reason.
    known_gap: str = ""

    @property
    def regression(self) -> bool:
        """A failure the corpus did not already know about."""
        return not self.passed and not self.known_gap

    @property
    def unexpectedly_fixed(self) -> bool:
        """
        A documented gap that now passes.

        Deliberately fails the run, the way ``xfail(strict=True)`` does: a gap
        that has quietly closed is a stale claim in the corpus and in the
        plan, and leaving the marker in place means the next real regression
        hides behind it.
        """
        return self.passed and bool(self.known_gap)


@dataclass
class Bucket:
    low: float
    high: float
    total: int = 0
    correct: int = 0

    @property
    def midpoint(self) -> float:
        return (self.low + self.high) / 2.0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 0.0

    @property
    def gap(self) -> float:
        """Signed distance from perfect calibration. Positive = overconfident."""
        return self.midpoint - self.accuracy


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


def load_corpus(corpus_dir: Path) -> list[dict[str, Any]]:
    """Flatten every corpus file into a list of cases carrying their verdict."""
    cases: list[dict[str, Any]] = []
    for path in sorted(corpus_dir.glob("*.json")):
        block = json.loads(path.read_text(encoding="utf-8"))
        for case in block["cases"]:
            cases.append({**case, "verdict": block["verdict"], "_source": path.name})
    return cases


def _final_payload(result: Any, original: Any) -> Any:
    """
    What the payload actually ended up as.

    ``repaired_output`` when the repair was committed, ``proposed_output``
    under shadow, otherwise the last state the loop reached -- a FAILED run
    can still have applied operations before giving up, and those operations
    are exactly the ones a false-positive audit needs to see.
    """
    if result.repaired_output is not None:
        return result.repaired_output
    if result.proposed_output is not None:
        return result.proposed_output
    if result.attempts:
        return result.attempts[-1].data_after
    return original


def run_case(case: dict[str, Any]) -> CaseOutcome:
    """Run one labelled case and judge it against its ground truth."""
    verdict = case["verdict"]
    expected = case["expected_payload"]

    guard = ContractGuard.with_dict_schema(config=GuardConfig())
    payload = json.loads(json.dumps(case["payload"]))

    try:
        result = guard.repair(case["schema"], payload)
    except Exception as exc:  # noqa: BLE001 -- a crash is a result, not a stop
        return CaseOutcome(
            name=case["name"],
            verdict=verdict,
            description=case.get("description", ""),
            passed=False,
            reason=f"raised {type(exc).__name__}: {exc}",
            status="error",
            known_gap=case.get("known_gap", ""),
        )

    final = _final_payload(result, case["payload"])
    applied = [op for attempt in result.attempts for op in attempt.applied_operations]

    operations = [
        OperationOutcome(
            case=case["name"],
            verdict=verdict,
            op_type=op.op_type.value,
            target_path=op.target_path,
            trust=op.trust,
            risk=op.risk.name,
            correct=identical(value_at(final, op.target_path), value_at(expected, op.target_path)),
        )
        for op in applied
    ]

    passed, reason = _judge(verdict, result, final, expected, applied)
    return CaseOutcome(
        name=case["name"],
        verdict=verdict,
        description=case.get("description", ""),
        passed=passed,
        reason=reason,
        status=result.status.value,
        operations=operations,
        known_gap=case.get("known_gap", ""),
    )


def _judge(
    verdict: str,
    result: Any,
    final: Any,
    expected: Any,
    applied: list[Any],
) -> tuple[bool, str]:
    """Return ``(passed, reason)`` for one case against its labelled verdict."""
    if not identical(final, expected):
        return False, f"payload is {final!r}, expected {expected!r}"

    if verdict == "refuse" and applied:
        # Reachable when an operation is applied and then undone or overwritten
        # so the payload still matches -- still a false positive worth naming.
        return False, f"{len(applied)} operation(s) applied when none should have been"

    if verdict == "abstain" and not result.has_ambiguous_repairs:
        return False, "no candidate surfaced on RepairResult.ambiguous"

    return True, ""


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def reliability_curve(operations: list[OperationOutcome]) -> list[Bucket]:
    """Bucket applied operations by trust and score each bucket."""
    count = int(round(1.0 / BUCKET_WIDTH))
    buckets = [Bucket(low=i * BUCKET_WIDTH, high=(i + 1) * BUCKET_WIDTH) for i in range(count)]
    for op in operations:
        # Round away binary-float noise before flooring. ``0.95 / 0.05`` is
        # 18.999999999999996, so a plain ``int()`` files a trust-0.95
        # operation one bucket too low -- and 0.60 and 0.70, which are
        # INFERRED's reject_below and REVERSIBLE's apply_at, land wrong the
        # same way. Getting this subtly wrong misreports the one number this
        # harness exists to publish.
        scaled = round(op.trust / BUCKET_WIDTH, 9)
        index = min(int(scaled), count - 1)
        buckets[index].total += 1
        buckets[index].correct += int(op.correct)
    return [b for b in buckets if b.total]


def expected_calibration_error(buckets: list[Bucket]) -> float | None:
    """
    Weighted mean distance between a bucket's confidence and its accuracy.

    The standard one-number summary of a reliability diagram. 0.0 is perfect;
    anything above ~0.1 means the score is not usable as a probability.
    """
    total = sum(b.total for b in buckets)
    if not total:
        return None
    return sum(b.total * abs(b.gap) for b in buckets) / total


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------


def print_report(outcomes: list[CaseOutcome], verbose: bool = False) -> None:
    operations = [op for o in outcomes for op in o.operations]
    buckets = reliability_curve(operations)

    print()
    print("StateGuard Calibration Report")
    print("=" * 74)

    print("\nCorpus")
    print("-" * 74)
    for verdict in ("repair", "abstain", "refuse"):
        family = [o for o in outcomes if o.verdict == verdict]
        if not family:
            continue
        passed = sum(1 for o in family if o.passed)
        gaps = sum(1 for o in family if o.known_gap and not o.passed)
        note = f"   ({gaps} known gap{'s' if gaps != 1 else ''})" if gaps else ""
        print(
            f"  {verdict:<9} {passed:>3}/{len(family):<3} cases match their labelled verdict{note}"
        )
    total_passed = sum(1 for o in outcomes if o.passed)
    total_gaps = sum(1 for o in outcomes if o.known_gap and not o.passed)
    print(f"  {'TOTAL':<9} {total_passed:>3}/{len(outcomes):<3}   ({total_gaps} known gaps)")

    print("\nApplied operations")
    print("-" * 74)
    if operations:
        correct = sum(1 for op in operations if op.correct)
        print(f"  Applied:   {len(operations)}")
        print(f"  Correct:   {correct}")
        print(f"  Precision: {correct / len(operations):.1%}")
    else:
        print("  (none)")

    print("\nReliability curve")
    print("-" * 74)
    print(f"  {'bucket':<14}{'n':>5}{'correct':>9}{'accuracy':>10}{'expected':>10}{'gap':>9}")
    for b in buckets:
        flag = "  overconfident" if b.gap > 0.05 else ""
        print(
            f"  [{b.low:.2f}, {b.high:.2f})"
            f"{b.total:>5}{b.correct:>9}{b.accuracy:>10.1%}{b.midpoint:>10.1%}{b.gap:>+9.1%}{flag}"
        )
    ece = expected_calibration_error(buckets)
    if ece is not None:
        print(f"\n  Expected calibration error: {ece:.3f}")

    regressions = [o for o in outcomes if o.regression]
    if regressions:
        print(f"\nRegressions ({len(regressions)})")
        print("-" * 74)
        for o in regressions:
            print(f"  ✗ [{o.verdict}] {o.name}  (status={o.status})")
            print(f"      {o.reason}")
            if verbose and o.description:
                print(f"      why it is labelled that way: {o.description}")

    gaps = [o for o in outcomes if o.known_gap and not o.passed]
    if gaps:
        print(f"\nKnown gaps ({len(gaps)}) — measured, accepted, documented")
        print("-" * 74)
        for o in gaps:
            print(f"  ~ [{o.verdict}] {o.name}  (status={o.status})")
            if verbose:
                print(f"      {o.known_gap}")
        if not verbose:
            print("\n  Re-run with --verbose for why each is accepted.")

    fixed = [o for o in outcomes if o.unexpectedly_fixed]
    if fixed:
        print(f"\nDocumented gaps that now pass ({len(fixed)})")
        print("-" * 74)
        for o in fixed:
            print(f"  ! {o.name} — remove its known_gap marker and update the plan.")
    print()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the StateGuard calibration corpus.")
    parser.add_argument(
        "--corpus-dir",
        type=Path,
        default=Path(__file__).parent / "calibration",
        help="Directory of corpus JSON files (default: benchmarks/calibration).",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Print each failing case's labelling rationale.",
    )
    args = parser.parse_args(argv)

    cases = load_corpus(args.corpus_dir)
    if not cases:
        print(f"No calibration cases found in {args.corpus_dir}", file=sys.stderr)
        return 1

    outcomes = [run_case(case) for case in cases]
    print_report(outcomes, verbose=args.verbose)

    # A documented gap failing is the corpus working as intended, so it must
    # not block CI. A gap that has closed, or any undocumented failure, must.
    blocking = [o for o in outcomes if o.regression or o.unexpectedly_fixed]
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())

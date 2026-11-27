"""The strict-RIFR scorer's arithmetic: intervals, pairing, unsafe outcomes, mixture."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.measure import Result  # noqa: E402
from runtime.rifr import ASSUMED_FAILURE_SHARES, score, wilson  # noqa: E402


def _r(
    scenario: str, category: str, arm: str, success: bool, kinds: list[str] | None = None
) -> Result:
    return Result(
        scenario=scenario,
        category=category,
        injector="x",
        arm=arm,
        success=success,
        kinds=kinds or ([] if success else ["not_executed"]),
        executions=1,
        action="forward",
        rewrote=False,
        internal_errors=0,
        tools_call_sent=1,
        own_listings=0,
        latency_ms=1.0,
    )


def test_wilson_matches_known_values() -> None:
    low, high = wilson(0, 30)
    assert low == 0.0 and high == pytest.approx(0.1135, abs=1e-3)  # ~ rule of three, 3.7/30
    low, high = wilson(8, 10000)
    assert low < 0.0008 < high and high == pytest.approx(0.00158, abs=2e-4)
    assert wilson(0, 0) == (0.0, 1.0)


def test_shares_sum_to_one() -> None:
    assert sum(ASSUMED_FAILURE_SHARES.values()) == pytest.approx(1.0)


def test_pairing_unsafe_and_strictness() -> None:
    results = [
        _r("a", "F", "raw", False),
        _r("a", "F", "auto", True),  # recovered
        _r("b", "N", "raw", True),
        _r("b", "N", "auto", False, ["surfaced_unknown"]),  # induced, and UNKNOWN is a failure
        _r("c", "I", "raw", False, ["duplicate_execution"]),
        _r("c", "I", "auto", False, ["surfaced_unknown"]),
    ]
    summary = score(results, ["raw", "auto"])
    auto = summary["arms"]["auto"]
    assert auto["recovered_vs_raw"] == 1 and auto["induced_vs_raw"] == 1
    assert auto["induced_scenarios"] == ["b"]
    assert auto["control"]["failures"] == 1 and auto["faulted"]["failures"] == 1
    assert summary["arms"]["raw"]["unsafe"]["duplicate_effect"] == 1
    assert auto["unsafe"]["duplicate_effect"] == 0
    assert auto["surfaced_unknown"] == 2


def test_mixture_counts_unmeasured_and_unmodelled_as_failing() -> None:
    results = [_r("n", "N", "auto", True)]  # control only: no category measured
    (low_rate, *_) = score(results, ["auto"])["arms"]["auto"]["mixture"]
    # every fault share counts at residual 1.0; the clean share at 0
    assert low_rate["rifr"] == pytest.approx(low_rate["assumed_fault_rate"])

"""
The before/after measurement slice (benchmarks/runtime) holds its invariants.

Runs the full scenario set at one invocation per injected fault, in all four
arms, and checks the properties that must hold whatever the numbers are:

* shadow behaves exactly like raw (same success, same server executions);
* auto never fails an invocation that raw got right (no StateGuard-induced
  failure) and never executes anything twice;
* every category but those listed as untested is exercised.

The numbers themselves are recorded in docs/runtime/TASKMAP.md, not asserted
here: they describe a synthetic corpus and are expected to move as
primitives land.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.measure import ARMS, Report, run_one, summarise  # noqa: E402
from runtime.scenarios import build, drift_corpus  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def summary() -> dict[str, Any]:
    corpus = drift_corpus()
    report = Report(seed=0, per_fault=1, provenance=corpus.provenance)
    for scenario in build(0, corpus=corpus, per_fault=1):
        for arm in ARMS:
            report.results.append(run_one(scenario, arm))
    return summarise(report)


def test_shadow_is_indistinguishable_from_raw(summary: dict[str, Any]) -> None:
    for cat, row in summary["categories"].items():
        if row["exercised"]:
            assert row["shadow_matches_raw"], cat


def test_auto_induces_no_failure_and_no_duplicate(summary: dict[str, Any]) -> None:
    for cat, row in summary["categories"].items():
        if row["exercised"]:
            assert row["auto_vs_raw"]["stateguard_induced"] == 0, cat
            assert row["auto_vs_raw"]["duplicate_execution"] == 0, cat
            assert row["auto_vs_raw"]["crashes"] == 0, cat


def test_auto_prevents_contract_failures(summary: dict[str, Any]) -> None:
    a = summary["categories"]["A"]["auto_vs_raw"]
    assert a["failures_after"] < a["failures_before"]


def test_naive_retry_duplicates_side_effects(summary: dict[str, Any]) -> None:
    """The reason blind retry is not a recovery strategy."""
    assert (
        sum(
            row["naive_retry"]["duplicate_execution"]
            for row in summary["categories"].values()
            if row["exercised"]
        )
        > 0
    )


def test_untested_categories_are_reported_not_hidden(summary: dict[str, Any]) -> None:
    assert summary["untested"] == ["D"]
    assert summary["synthetic"] is True

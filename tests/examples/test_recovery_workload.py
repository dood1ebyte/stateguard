"""
The session recovery workload (benchmarks/runtime/recovery.py) holds its
invariants. One session per kind, every arm. The numbers are recorded in
docs/runtime/TASKMAP.md; what is asserted here holds whatever they are:

* no request is replayed and none executes twice, in any arm;
* no StateGuard arm fails an invocation raw got right (category Z = 0);
* shadow is indistinguishable from raw;
* UNKNOWN is the same set of invocations in every StateGuard arm: a later
  restart never resolves one into success;
* recovery appears only in restart arms, and only as an execution on a
  restarted server;
* restart failure is bounded: attempts never exceed the budget per session.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "benchmarks"))

from runtime.recovery import ARMS, MAX_RESTARTS, build, run_session, summarise  # noqa: E402

pytestmark = pytest.mark.integration


@pytest.fixture(scope="module")
def results() -> list[Any]:
    return [run_session(s, arm) for s in build(0, per_type=1) for arm in ARMS]


def test_no_replay_and_no_duplicate_execution(results: list[Any]) -> None:
    summary = summarise(results)
    for arm, row in summary["arms"].items():
        assert row["replayed"] == 0, arm
        assert row["duplicate_execution"] == 0, arm


def test_no_stateguard_induced_failure(results: list[Any]) -> None:
    for arm, row in summarise(results)["arms"].items():
        assert row["stateguard_induced"] == 0, arm


def test_shadow_equals_raw(results: list[Any]) -> None:
    arms = summarise(results)["arms"]
    assert arms["shadow"]["outcomes"] == arms["raw"]["outcomes"]


def test_unknown_is_never_resolved_by_a_restart(results: list[Any]) -> None:
    unknown = {
        arm: {
            (inv.session, inv.index)
            for r in results
            if r.arm == arm
            for inv in r.invocations
            if inv.outcome == "unknown"
        }
        for arm in ("auto", "restart_idle", "restart_after_unknown")
    }
    assert unknown["auto"] and unknown["auto"] == unknown["restart_idle"]
    assert unknown["auto"] == unknown["restart_after_unknown"]


def test_recovery_only_by_executing_on_a_restarted_server(results: list[Any]) -> None:
    for r in results:
        for inv in r.invocations:
            if inv.outcome == "recovered":
                assert r.arm.startswith("restart")
                assert inv.on_generation and inv.executions == 1


def test_restart_attempts_are_bounded(results: list[Any]) -> None:
    for r in results:
        assert r.reconnect_attempted <= MAX_RESTARTS, (r.session, r.arm)

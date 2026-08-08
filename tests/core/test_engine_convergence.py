"""
Engine convergence and multi-step repair.

Covers the iterative-repair contract described under "Termination and
convergence" in ``stateguard.core.engine``:

* A repair that *exposes* the next problem is progress, not a regression --
  the loop must continue rather than bail on the first newly-surfaced
  violation kind.
* A genuine regression (the violation set got worse) discards only the bad
  attempt; repairs accepted before it are preserved.
* The loop terminates on its own, without relying on ``max_attempts``.
* The final status agrees with the loop's own notion of progress.

These are the behaviours that the pre-existing subset-based regression check
got wrong: it compared each revalidation against the *initial* violation
signatures, so any repair that changed a violation's *kind* at the same path
was misclassified as a regression and the whole repair was discarded.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.engine import RepairEngine
from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import RepairStatus
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.config import RepairConfig
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.registry import StrategyRegistry
from stateguard.guard import ContractGuard
from tests.conftest import MockContractAdapter, MockRepairStrategy
from tests.core.test_engine import make_engine


# ===========================================================================
# _progress_key -- the metric the loop and the status determination share
# ===========================================================================


class TestProgressKey:
    @staticmethod
    def _v(severity: ViolationSeverity, path: str = "a") -> ContractViolation:
        return ContractViolation(
            field_path=path,
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
            severity=severity,
            message="",
        )

    def test_empty_is_zero_zero(self) -> None:
        assert RepairEngine._progress_key([]) == (0, 0)

    def test_counts_errors_and_total(self) -> None:
        violations = [
            self._v(ViolationSeverity.ERROR, "a"),
            self._v(ViolationSeverity.WARNING, "b"),
            self._v(ViolationSeverity.WARNING, "c"),
        ]
        assert RepairEngine._progress_key(violations) == (1, 3)

    def test_resolving_an_error_beats_gaining_warnings(self) -> None:
        """Errors dominate: 1 error + 0 warnings is better than 2 errors + 0."""
        two_errors = [self._v(ViolationSeverity.ERROR, "a"), self._v(ViolationSeverity.ERROR, "b")]
        one_error_many_warnings = [
            self._v(ViolationSeverity.ERROR, "a"),
            *[self._v(ViolationSeverity.WARNING, f"w{i}") for i in range(9)],
        ]
        assert RepairEngine._progress_key(one_error_many_warnings) < RepairEngine._progress_key(
            two_errors
        )

    def test_total_breaks_ties_at_equal_error_count(self) -> None:
        """
        This tie-break is what makes multi-step repair work.

        A rename turns {MISSING (error), UNEXPECTED (warning)} into
        {TYPE_MISMATCH (error)}: the error count is unchanged, but the total
        fell. Measuring errors alone would call that "no progress" and stop
        before the coercion step ever runs.
        """
        before = [self._v(ViolationSeverity.ERROR, "a"), self._v(ViolationSeverity.WARNING, "b")]
        after = [self._v(ViolationSeverity.ERROR, "a")]
        assert RepairEngine._progress_key(after) < RepairEngine._progress_key(before)


# ===========================================================================
# Multi-step repair through the real strategy set
# ===========================================================================


class TestMultiStepRepair:
    """End-to-end through ContractGuard, using the real four-strategy registry."""

    SCHEMA = {
        "fields": [
            {"path": "temperature", "type": "float"},
            {"path": "humidity", "type": "integer"},
        ]
    }

    def test_rename_then_coerce_succeeds(self) -> None:
        """
        The README's headline case. Previously returned FAILED: the fuzzy
        rename exposed a TYPE_MISMATCH at 'temperature' that was not in the
        initial signature set, which the old subset check read as a regression.
        """
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.SCHEMA, {"temp_celsius": "31.5", "humidity": 80})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"temperature": 31.5, "humidity": 80}

    def test_rename_then_coerce_uses_two_attempts(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.SCHEMA, {"temp_celsius": "31.5", "humidity": 80})

        assert [a.strategy_name for a in result.attempts] == [
            "FuzzyFieldMatchStrategy",
            "TypeCoercionStrategy",
        ]

    def test_no_regression_logged_for_a_legitimate_multi_step_repair(self) -> None:
        guard = ContractGuard.with_dict_schema()
        result = guard.repair(self.SCHEMA, {"temp_celsius": "31.5", "humidity": 80})

        events = {entry.event for entry in result.repair_log}
        assert "repair.regression_detected" not in events

    def test_multi_issue_payload_converges_in_one_call(self) -> None:
        """Three distinct problems, three strategies, one repair() call."""
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer", "default": 60},
                {"path": "station", "type": "string", "known_aliases": ["station_id"]},
            ]
        }
        guard = ContractGuard.with_dict_schema()
        # alias rename (station_id), fuzzy rename + coercion (temp_celsius),
        # and a default fill (humidity absent).
        result = guard.repair(schema, {"station_id": "KBOS", "temp_celsius": "31.5"})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {
            "station": "KBOS",
            "temperature": 31.5,
            "humidity": 60,
        }

    def test_rename_exposing_a_constraint_violation_keeps_the_rename(self) -> None:
        """
        The rename is correct; the value is merely out of range. That is a
        PARTIAL with the repair retained, not a FAILED with the work discarded.
        """
        pydantic = pytest.importorskip("pydantic")

        class Person(pydantic.BaseModel):
            age: int = pydantic.Field(ge=0)

        guard = ContractGuard.with_pydantic()
        result = guard.repair(Person, {"agee": -5})

        assert result.status is RepairStatus.PARTIAL
        assert result.repaired_output == {"age": -5}
        assert [v.violation_type for v in result.remaining_violations] == [
            ViolationType.VALUE_CONSTRAINT_VIOLATION
        ]


# ===========================================================================
# Regression handling -- last-good state is preserved
# ===========================================================================


class _WhileMissing(IRepairStrategy):
    """
    Applies its operations only while *trigger_path* is still reported
    missing.

    ``MockRepairStrategy`` takes a fixed ``handle`` flag, which is not enough
    here: the engine always selects the highest-priority *applicable*
    strategy, so a strategy that claims to handle everything forever would
    never let a lower-priority one run. Making applicability conditional is
    what lets this test stage a good repair *before* a bad one.
    """

    def __init__(
        self,
        name: str,
        priority: int,
        trigger_path: str,
        operations: list[FieldOperation],
    ) -> None:
        self._name = name
        self._priority = priority
        self._trigger_path = trigger_path
        self._operations = operations

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return any(
            v.field_path == self._trigger_path
            and v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
            for v in violations
        )

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        return list(self._operations)


class TestRegressionPreservesEarlierRepairs:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=True),
                FieldSpec("c", FieldType.INTEGER, required=False),
            ]
        )

    @staticmethod
    def _registry() -> StrategyRegistry:
        """
        FixA runs first and correctly fills 'a', then stops being applicable.
        BreakC then fires on the still-missing 'b' and writes a type-invalid
        value into optional 'c', making the violation set strictly worse.
        """
        fix_a = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=1.0,
            rationale="fills a correctly",
            value=1,
        )
        break_c = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="c",
            confidence=1.0,
            rationale="writes a type-invalid value into optional c",
            value="oops",
        )
        return StrategyRegistry(
            [
                _WhileMissing("FixA", priority=10, trigger_path="a", operations=[fix_a]),
                _WhileMissing("BreakC", priority=20, trigger_path="b", operations=[break_c]),
            ]
        )

    def test_regression_is_detected(self) -> None:
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=10))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        events = {entry.event for entry in result.repair_log}
        assert "repair.regression_detected" in events

    def test_repair_from_before_the_regression_is_kept(self) -> None:
        """
        FixA runs first and correctly fills 'a'. BreakC then makes things
        worse. The bad attempt is discarded, but 'a' must survive -- the old
        behaviour threw the whole run away and returned None.
        """
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=10))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert result.repaired_output is not None
        assert result.repaired_output["a"] == 1
        assert "c" not in result.repaired_output

    def test_status_is_partial_not_failed(self) -> None:
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=10))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert result.status is RepairStatus.PARTIAL

    def test_remaining_violations_describe_the_last_good_state(self) -> None:
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=10))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        # 'a' was repaired; 'b' is still missing; 'c' was never written.
        assert [v.field_path for v in result.remaining_violations] == ["b"]

    def test_the_failing_attempt_is_still_recorded(self) -> None:
        """A discarded attempt must remain visible in the audit trail."""
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=10))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert [a.strategy_name for a in result.attempts] == ["FixA", "BreakC"]
        assert result.attempts[-1].succeeded is False


# ===========================================================================
# Termination
# ===========================================================================


class TestTermination:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=True),
            ]
        )

    @staticmethod
    def _noop_registry() -> StrategyRegistry:
        """Applies the same fix forever; must be stopped by the cycle check."""
        fix_a = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=1.0,
            rationale="fix a, repeatedly",
            value=1,
        )
        return StrategyRegistry(
            [MockRepairStrategy(name="FixA", priority=10, handle=True, operations=[fix_a])]
        )

    def test_loop_stops_without_exhausting_max_attempts(self) -> None:
        """
        Termination comes from the convergence check, not the attempt bound.
        With max_attempts=50 the loop must still stop after two attempts:
        one that makes progress, one that repeats it.
        """
        engine = make_engine(registry=self._noop_registry(), config=RepairConfig(max_attempts=50))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert len(result.attempts) == 2
        assert result.status is RepairStatus.PARTIAL

    def test_max_attempts_exhaustion_is_not_reached(self) -> None:
        engine = make_engine(registry=self._noop_registry(), config=RepairConfig(max_attempts=50))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        events = {entry.event for entry in result.repair_log}
        assert "repair.max_attempts_exhausted" not in events
        assert "repair.no_progress" in events

    @pytest.mark.parametrize("max_attempts", [1, 2, 3, 6, 10])
    def test_attempts_never_exceed_the_bound(self, max_attempts: int) -> None:
        engine = make_engine(
            registry=self._noop_registry(), config=RepairConfig(max_attempts=max_attempts)
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert len(result.attempts) <= max_attempts

    def test_max_attempts_one_still_stops_after_one(self) -> None:
        engine = make_engine(registry=self._noop_registry(), config=RepairConfig(max_attempts=1))
        result = engine.repair(self._contract(), {}, MockContractAdapter())

        assert len(result.attempts) == 1
        assert result.status is RepairStatus.PARTIAL


# ===========================================================================
# CLI / library default parity
# ===========================================================================


def test_cli_defaults_match_repair_config() -> None:
    """
    The CLI previously hardcoded max_attempts=5 while RepairConfig defaulted
    to 3. Both are now derived from RepairConfig; this guards the drift.
    """
    from stateguard.cli import _build_parser

    parser = _build_parser()
    args = parser.parse_args(["check", "--schema", "s.json", "--payload", "p.json"])
    defaults = RepairConfig()

    assert args.max_attempts == defaults.max_attempts
    assert args.confidence_threshold == defaults.min_confidence_threshold

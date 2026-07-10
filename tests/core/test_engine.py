"""Tests for stateguard.core.engine."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.engine import (
    RepairEngine,
    _COERCE_FAILED,
    _NOT_FOUND,
    _coerce_value,
    _delete_nested,
    _find_field_spec,
    _get_nested,
    _set_nested,
)
from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import RepairStatus, ValidationResult
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.config import RepairConfig
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType, UnionMember
from stateguard.core.strategies import (
    DefaultValueFillStrategy,
    ExactAliasStrategy,
    FuzzyFieldMatchStrategy,
    StrategyRegistry,
    TypeCoercionStrategy,
)
from stateguard.logging.logger import RepairLogger
from tests.conftest import (
    CapturingTelemetryHook,
    MockContractAdapter,
    MockRepairStrategy,
    make_violation,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def full_registry() -> StrategyRegistry:
    """The complete V1 strategy set in canonical priority order."""
    return StrategyRegistry(
        [
            ExactAliasStrategy(),
            FuzzyFieldMatchStrategy(),
            TypeCoercionStrategy(),
            DefaultValueFillStrategy(),
        ]
    )


def make_engine(
    registry: StrategyRegistry | None = None,
    config: RepairConfig | None = None,
    telemetry: Any = None,
) -> RepairEngine:
    return RepairEngine(
        registry=registry if registry is not None else full_registry(),
        config=config if config is not None else RepairConfig(),
        logger=RepairLogger(),
        telemetry=telemetry,
    )


# ===========================================================================
# ALREADY_VALID
# ===========================================================================


class TestAlreadyValid:
    def test_valid_data_returns_already_valid(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("temperature", FieldType.FLOAT),
                FieldSpec("humidity", FieldType.INTEGER),
            ]
        )
        data = {"temperature": 31.5, "humidity": 80}
        engine = make_engine()
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.ALREADY_VALID
        assert result.is_already_valid is True

    def test_already_valid_has_no_attempts(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        assert result.attempts == []

    def test_already_valid_repaired_output_equals_input(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        data = {"x": "hello"}
        engine = make_engine()
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.repaired_output == data

    def test_already_valid_initial_violations_empty_for_clean_data(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        assert result.initial_violations == []
        assert result.remaining_violations == []

    def test_already_valid_with_warning_violation_includes_it(self) -> None:
        """Non-strict unexpected field is WARNING -> still ALREADY_VALID,
        but the warning appears in initial/remaining violations."""
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "hello", "extra": 1}, MockContractAdapter())
        assert result.status is RepairStatus.ALREADY_VALID
        assert len(result.initial_violations) == 1
        assert result.initial_violations[0].violation_type is ViolationType.UNEXPECTED_FIELD
        assert result.remaining_violations == result.initial_violations

    def test_already_valid_does_not_mutate_input(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        data = {"x": "hello"}
        engine = make_engine()
        engine.repair(contract, data, MockContractAdapter())
        assert data == {"x": "hello"}

    def test_already_valid_repaired_output_is_independent_copy(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        data = {"x": "hello"}
        engine = make_engine()
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.repaired_output is not data
        assert result.repaired_output is not result.original_input

    def test_contract_id_set_on_already_valid(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "y"}, MockContractAdapter())
        assert result.contract_id == contract.contract_id


# ===========================================================================
# SUCCESS — single attempt (fuzzy rename)
# ===========================================================================


class TestSuccessSingleAttempt:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )

    def test_status_is_success(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert result.status is RepairStatus.SUCCESS
        assert result.is_success is True

    def test_repaired_output_correct(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert result.repaired_output == {"city": "Mumbai", "population": 1000000}

    def test_single_attempt_recorded(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert len(result.attempts) == 1

    def test_strategy_name_is_fuzzy(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert result.attempts[0].strategy_name == "FuzzyFieldMatchStrategy"

    def test_no_remaining_violations(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert result.remaining_violations == []

    def test_attempt_succeeded_flag(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        assert result.attempts[0].succeeded is True

    def test_original_input_unchanged(self) -> None:
        data = {"cty": "Mumbai", "population": 1000000}
        engine = make_engine()
        engine.repair(self._contract(), data, MockContractAdapter())
        assert data == {"cty": "Mumbai", "population": 1000000}

    def test_original_input_recorded_on_result(self) -> None:
        data = {"cty": "Mumbai", "population": 1000000}
        engine = make_engine()
        result = engine.repair(self._contract(), data, MockContractAdapter())
        assert result.original_input == {"cty": "Mumbai", "population": 1000000}

    def test_initial_violations_recorded(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        types = {v.violation_type for v in result.initial_violations}
        assert ViolationType.MISSING_REQUIRED_FIELD in types
        assert ViolationType.UNEXPECTED_FIELD in types

    def test_applied_operation_is_rename(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        applied = result.attempts[0].applied_operations
        assert len(applied) == 1
        assert applied[0].op_type is FieldOpType.RENAME
        assert applied[0].source_path == "cty"
        assert applied[0].target_path == "city"

    def test_data_before_and_after_snapshots(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(),
            {"cty": "Mumbai", "population": 1000000},
            MockContractAdapter(),
        )
        attempt = result.attempts[0]
        assert attempt.data_before == {"cty": "Mumbai", "population": 1000000}
        assert attempt.data_after == {"city": "Mumbai", "population": 1000000}


# ===========================================================================
# SUCCESS — two attempts (ExactAlias then TypeCoercion)
# ===========================================================================


class TestSuccessTwoAttempts:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING, known_aliases=["town"]),
                FieldSpec("count", FieldType.INTEGER),
            ]
        )

    def test_status_success(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        assert result.status is RepairStatus.SUCCESS

    def test_two_attempts_recorded(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        assert len(result.attempts) == 2

    def test_attempt_order_alias_then_coercion(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        names = [a.strategy_name for a in result.attempts]
        assert names == ["ExactAliasStrategy", "TypeCoercionStrategy"]

    def test_attempt_numbers_sequential(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        numbers = [a.attempt_number for a in result.attempts]
        assert numbers == [1, 2]

    def test_final_repaired_output(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        assert result.repaired_output == {"city": "Mumbai", "count": 5}

    def test_second_attempt_coerces_to_int(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        coerce_attempt = result.attempts[1]
        assert coerce_attempt.applied_operations[0].op_type is FieldOpType.COERCE
        assert coerce_attempt.data_after["count"] == 5
        assert isinstance(coerce_attempt.data_after["count"], int)

    def test_first_attempt_not_yet_succeeded(self) -> None:
        engine = make_engine()
        result = engine.repair(
            self._contract(), {"town": "Mumbai", "count": "5"}, MockContractAdapter()
        )
        assert result.attempts[0].succeeded is False
        assert result.attempts[1].succeeded is True


# ===========================================================================
# No applicable strategy -> FAILED immediately
# ===========================================================================


class TestNoApplicableStrategy:
    def test_failed_immediately(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {}, MockContractAdapter())
        assert result.status is RepairStatus.FAILED

    def test_no_attempts_recorded(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {}, MockContractAdapter())
        assert result.attempts == []

    def test_repaired_output_is_none(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {}, MockContractAdapter())
        assert result.repaired_output is None

    def test_remaining_violations_equal_initial(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {}, MockContractAdapter())
        assert len(result.remaining_violations) == len(result.initial_violations) == 1
        assert result.remaining_violations[0].violation_type is ViolationType.MISSING_REQUIRED_FIELD

    def test_empty_registry_always_fails(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine(registry=StrategyRegistry([]))
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        assert result.status is RepairStatus.FAILED
        assert result.attempts == []


# ===========================================================================
# No-progress detection
# ===========================================================================


class TestNoProgressDetection:
    def test_strategy_proposing_nothing_yields_failed(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        registry = StrategyRegistry(
            [MockRepairStrategy(name="DoNothing", priority=10, handle=True, operations=[])]
        )
        engine = make_engine(registry=registry)
        result = engine.repair(contract, {}, MockContractAdapter())

        assert result.status is RepairStatus.FAILED
        assert len(result.attempts) == 1
        assert result.attempts[0].applied_operations == []
        assert result.attempts[0].succeeded is False

    def test_no_progress_stops_before_max_attempts(self) -> None:
        """Even with max_attempts=10, a no-op strategy stops after 1 attempt."""
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        registry = StrategyRegistry(
            [MockRepairStrategy(name="DoNothing", priority=10, handle=True, operations=[])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(max_attempts=10))
        result = engine.repair(contract, {}, MockContractAdapter())
        assert len(result.attempts) == 1

    def test_operation_with_no_effect_yields_no_progress(self) -> None:
        """A SET_VALUE on an already-correct value changes nothing -> no progress."""
        contract = ContractSpec(
            fields=[
                FieldSpec("x", FieldType.STRING),
                FieldSpec("y", FieldType.INTEGER),
            ]
        )
        # 'y' is missing; the mock strategy "fixes" 'x' (already correct,
        # so SET_VALUE has no effect) instead of 'y'.
        noop_op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="x",
            confidence=1.0,
            rationale="no-op",
            value="hello",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Noop", priority=10, handle=True, operations=[noop_op])]
        )
        engine = make_engine(registry=registry)
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())

        assert result.status is RepairStatus.FAILED
        assert len(result.attempts) == 1
        assert result.attempts[0].applied_operations == [noop_op]


# ===========================================================================
# Regression detection
# ===========================================================================


class TestRegressionDetection:
    def test_new_violation_type_triggers_failed(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=False),
            ]
        )
        bad_op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="b",
            confidence=1.0,
            rationale="introduces a type mismatch on b",
            value="oops",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Regress", priority=10, handle=True, operations=[bad_op])]
        )
        engine = make_engine(registry=registry)
        result = engine.repair(contract, {}, MockContractAdapter())

        assert result.status is RepairStatus.FAILED
        assert result.repaired_output is None

    def test_regression_stops_after_one_attempt(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=False),
            ]
        )
        bad_op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="b",
            confidence=1.0,
            rationale="introduces a type mismatch on b",
            value="oops",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Regress", priority=10, handle=True, operations=[bad_op])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(max_attempts=10))
        result = engine.repair(contract, {}, MockContractAdapter())
        assert len(result.attempts) == 1

    def test_regression_attempt_marked_unsuccessful(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=False),
            ]
        )
        bad_op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="b",
            confidence=1.0,
            rationale="introduces a type mismatch on b",
            value="oops",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Regress", priority=10, handle=True, operations=[bad_op])]
        )
        engine = make_engine(registry=registry)
        result = engine.repair(contract, {}, MockContractAdapter())
        assert result.attempts[0].succeeded is False


# ===========================================================================
# Partial repair / allow_partial_repair
# ===========================================================================


class TestPartialRepair:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=True),
            ]
        )

    @staticmethod
    def _registry() -> StrategyRegistry:
        fix_a = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=1.0,
            rationale="fix a only",
            value=1,
        )
        return StrategyRegistry(
            [MockRepairStrategy(name="FixA", priority=10, handle=True, operations=[fix_a])]
        )

    def test_allow_partial_repair_true_yields_partial(self) -> None:
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=True),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert result.status is RepairStatus.PARTIAL

    def test_allow_partial_repair_true_repaired_output_set(self) -> None:
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=True),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert result.repaired_output == {"a": 1}

    def test_allow_partial_repair_true_remaining_violations(self) -> None:
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=True),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert len(result.remaining_violations) == 1
        assert result.remaining_violations[0].field_path == "b"

    def test_allow_partial_repair_false_yields_failed(self) -> None:
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=False),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert result.status is RepairStatus.FAILED

    def test_allow_partial_repair_false_repaired_output_none(self) -> None:
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=False),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert result.repaired_output is None

    def test_partial_terminates_via_no_progress(self) -> None:
        """Second iteration of FixA makes no further progress -> stops."""
        engine = make_engine(
            registry=self._registry(),
            config=RepairConfig(max_attempts=5, allow_partial_repair=True),
        )
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert len(result.attempts) == 2


# ===========================================================================
# max_attempts respected / exhaustion (for/else)
# ===========================================================================


class TestMaxAttemptsExhaustion:
    @staticmethod
    def _contract() -> ContractSpec:
        return ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER, required=True),
                FieldSpec("b", FieldType.INTEGER, required=True),
            ]
        )

    @staticmethod
    def _registry() -> StrategyRegistry:
        fix_a = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=1.0,
            rationale="fix a only",
            value=1,
        )
        return StrategyRegistry(
            [MockRepairStrategy(name="FixA", priority=10, handle=True, operations=[fix_a])]
        )

    def test_max_attempts_one_exhausted_with_progress(self) -> None:
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=1))
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        assert len(result.attempts) == 1
        assert result.status is RepairStatus.PARTIAL

    def test_max_attempts_never_exceeded(self) -> None:
        for max_attempts in (1, 2, 3, 5):
            engine = make_engine(
                registry=self._registry(),
                config=RepairConfig(max_attempts=max_attempts),
            )
            result = engine.repair(self._contract(), {}, MockContractAdapter())
            assert len(result.attempts) <= max_attempts

    def test_max_attempts_exhaustion_logged(self) -> None:
        engine = make_engine(registry=self._registry(), config=RepairConfig(max_attempts=1))
        result = engine.repair(self._contract(), {}, MockContractAdapter())
        events = [e.event for e in result.repair_log]
        assert "repair.max_attempts_exhausted" in events


# ===========================================================================
# Confidence threshold filtering
# ===========================================================================


class TestConfidenceThresholdFiltering:
    def test_low_confidence_operation_rejected(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        high = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=0.9,
            rationale="high confidence",
            value=1,
        )
        low = FieldOperation(
            op_type=FieldOpType.REMOVE,
            target_path="nonexistent",
            confidence=0.3,
            rationale="low confidence",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Mixed", priority=10, handle=True, operations=[high, low])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(min_confidence_threshold=0.7))
        result = engine.repair(contract, {}, MockContractAdapter())

        applied = result.attempts[0].applied_operations
        rejected = result.attempts[0].rejected_operations
        assert applied == [high]
        assert rejected == [low]

    def test_rejected_operation_not_applied_to_data(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("a", FieldType.INTEGER),
                FieldSpec("b", FieldType.STRING, required=False),
            ]
        )
        high = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=0.9,
            rationale="high confidence",
            value=1,
        )
        low = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="b",
            confidence=0.3,
            rationale="low confidence",
            value="should not be applied",
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Mixed", priority=10, handle=True, operations=[high, low])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(min_confidence_threshold=0.7))
        result = engine.repair(contract, {}, MockContractAdapter())

        assert "b" not in result.attempts[0].data_after

    def test_threshold_exactly_at_confidence_is_applied(self) -> None:
        """confidence >= threshold (not strictly >) is applied."""
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=0.7,
            rationale="exactly at threshold",
            value=1,
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="Exact", priority=10, handle=True, operations=[op])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(min_confidence_threshold=0.7))
        result = engine.repair(contract, {}, MockContractAdapter())
        assert result.attempts[0].applied_operations == [op]
        assert result.attempts[0].rejected_operations == []

    def test_all_operations_rejected_yields_no_progress(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        low = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=0.1,
            rationale="too low",
            value=1,
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="AllLow", priority=10, handle=True, operations=[low])]
        )
        engine = make_engine(registry=registry, config=RepairConfig(min_confidence_threshold=0.7))
        result = engine.repair(contract, {}, MockContractAdapter())

        assert result.attempts[0].applied_operations == []
        assert result.attempts[0].rejected_operations == [low]
        assert result.status is RepairStatus.FAILED


# ===========================================================================
# Violation correlation
# ===========================================================================


class TestViolationCorrelation:
    def test_correlate_links_missing_and_unexpected(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        result = RepairEngine._correlate_violations([missing, unexpected])

        assert unexpected.violation_id in missing.related_ids
        assert missing.violation_id in unexpected.related_ids
        assert result is not None

    def test_correlate_full_cross_product(self) -> None:
        m1 = make_violation(field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        m2 = make_violation(
            field_path="zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        u1 = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        u2 = make_violation(
            field_path="zipcode",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        RepairEngine._correlate_violations([m1, m2, u1, u2])

        assert set(m1.related_ids) == {u1.violation_id, u2.violation_id}
        assert set(m2.related_ids) == {u1.violation_id, u2.violation_id}
        assert set(u1.related_ids) == {m1.violation_id, m2.violation_id}
        assert set(u2.related_ids) == {m1.violation_id, m2.violation_id}

    def test_correlate_no_missing_leaves_unexpected_unlinked(self) -> None:
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        RepairEngine._correlate_violations([unexpected])
        assert unexpected.related_ids == []

    def test_correlate_no_unexpected_leaves_missing_unlinked(self) -> None:
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        RepairEngine._correlate_violations([missing])
        assert missing.related_ids == []

    def test_correlate_idempotent(self) -> None:
        """Running correlation twice does not duplicate related_ids."""
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        unexpected = make_violation(
            field_path="cty",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        RepairEngine._correlate_violations([missing, unexpected])
        RepairEngine._correlate_violations([missing, unexpected])
        assert missing.related_ids == [unexpected.violation_id]
        assert unexpected.related_ids == [missing.violation_id]

    def test_correlate_ignores_other_violation_types(self) -> None:
        type_mismatch = make_violation(
            field_path="count",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
        )
        missing = make_violation(
            field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD
        )
        RepairEngine._correlate_violations([type_mismatch, missing])
        assert type_mismatch.related_ids == []
        assert missing.related_ids == []

    def test_end_to_end_correlation_visible_in_attempt(self) -> None:
        """In a real repair, violations_targeted include correlated IDs."""
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        # Both the MISSING and UNEXPECTED violation_ids should be present
        # in violations_targeted for the single attempt.
        targeted = result.attempts[0].violations_targeted
        assert len(targeted) == 2


# ===========================================================================
# Hashing / signature
# ===========================================================================


class TestHashingAndSignatures:
    def test_violation_signature(self) -> None:
        v = make_violation(field_path="city", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        assert RepairEngine._violation_signature(v) == ("city", "missing_required_field")

    def test_hash_same_for_same_violations(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        assert RepairEngine._compute_violation_hash([v1]) == RepairEngine._compute_violation_hash(
            [v2]
        )

    def test_hash_order_independent(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(field_path="b", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        assert RepairEngine._compute_violation_hash(
            [v1, v2]
        ) == RepairEngine._compute_violation_hash([v2, v1])

    def test_hash_differs_for_different_field_path(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(field_path="b", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        assert RepairEngine._compute_violation_hash([v1]) != RepairEngine._compute_violation_hash(
            [v2]
        )

    def test_hash_differs_for_different_violation_type(self) -> None:
        v1 = make_violation(field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD)
        v2 = make_violation(
            field_path="a",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
        )
        assert RepairEngine._compute_violation_hash([v1]) != RepairEngine._compute_violation_hash(
            [v2]
        )

    def test_hash_differs_for_different_severity(self) -> None:
        v1 = make_violation(
            field_path="a",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        v2 = make_violation(
            field_path="a",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.ERROR,
        )
        assert RepairEngine._compute_violation_hash([v1]) != RepairEngine._compute_violation_hash(
            [v2]
        )

    def test_hash_empty_list(self) -> None:
        assert RepairEngine._compute_violation_hash([]) == RepairEngine._compute_violation_hash([])

    def test_hash_ignores_violation_id(self) -> None:
        """Two violations with different UUIDs but same signature hash the same."""
        v1 = make_violation(
            field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD, violation_id="id-1"
        )
        v2 = make_violation(
            field_path="a", violation_type=ViolationType.MISSING_REQUIRED_FIELD, violation_id="id-2"
        )
        assert RepairEngine._compute_violation_hash([v1]) == RepairEngine._compute_violation_hash(
            [v2]
        )


# ===========================================================================
# Operation application — internal helpers
# ===========================================================================


class TestGetSetDeleteNested:
    def test_get_top_level(self) -> None:
        assert _get_nested({"a": 1}, "a") == 1

    def test_get_nested(self) -> None:
        assert _get_nested({"a": {"b": 2}}, "a.b") == 2

    def test_get_missing_returns_not_found(self) -> None:
        assert _get_nested({"a": 1}, "b") is _NOT_FOUND

    def test_get_missing_nested_returns_not_found(self) -> None:
        assert _get_nested({"a": {}}, "a.b") is _NOT_FOUND

    def test_get_intermediate_not_dict_returns_not_found(self) -> None:
        assert _get_nested({"a": 1}, "a.b") is _NOT_FOUND

    def test_get_deep_intermediate_missing_returns_not_found(self) -> None:
        """A multi-level path where an intermediate key is absent entirely."""
        assert _get_nested({"a": {}}, "a.b.c") is _NOT_FOUND

    def test_set_top_level(self) -> None:
        data: dict[str, Any] = {}
        _set_nested(data, "a", 1)
        assert data == {"a": 1}

    def test_set_nested_creates_intermediate_dict(self) -> None:
        data: dict[str, Any] = {}
        _set_nested(data, "a.b", 2)
        assert data == {"a": {"b": 2}}

    def test_set_nested_existing_dict(self) -> None:
        data: dict[str, Any] = {"a": {"x": 1}}
        _set_nested(data, "a.b", 2)
        assert data == {"a": {"x": 1, "b": 2}}

    def test_set_overwrites_non_dict_intermediate(self) -> None:
        data: dict[str, Any] = {"a": "not a dict"}
        _set_nested(data, "a.b", 2)
        assert data == {"a": {"b": 2}}

    def test_delete_top_level(self) -> None:
        data = {"a": 1, "b": 2}
        _delete_nested(data, "a")
        assert data == {"b": 2}

    def test_delete_nested(self) -> None:
        data: dict[str, Any] = {"a": {"b": 1, "c": 2}}
        _delete_nested(data, "a.b")
        assert data == {"a": {"c": 2}}

    def test_delete_nonexistent_top_level_is_noop(self) -> None:
        data = {"a": 1}
        _delete_nested(data, "z")
        assert data == {"a": 1}

    def test_delete_nonexistent_nested_is_noop(self) -> None:
        data: dict[str, Any] = {"a": {"b": 1}}
        _delete_nested(data, "a.z")
        assert data == {"a": {"b": 1}}

    def test_delete_intermediate_not_dict_is_noop(self) -> None:
        data: dict[str, Any] = {"a": 1}
        _delete_nested(data, "a.b")
        assert data == {"a": 1}

    def test_delete_deep_intermediate_missing_is_noop(self) -> None:
        data: dict[str, Any] = {"a": {}}
        _delete_nested(data, "a.b.c")
        assert data == {"a": {}}


class TestFindFieldSpecEngine:
    def test_top_level(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        spec = _find_field_spec(contract, "a")
        assert spec is not None
        assert spec.field_type is FieldType.INTEGER

    def test_nested(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("zip_code", FieldType.INTEGER)])
        contract = ContractSpec(
            fields=[
                FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
            ]
        )
        spec = _find_field_spec(contract, "address.zip_code")
        assert spec is not None
        assert spec.field_type is FieldType.INTEGER

    def test_not_found(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        assert _find_field_spec(contract, "b") is None

    def test_matched_path_segment_but_no_nested_spec_for_rest(self) -> None:
        """Field matches the first segment but has no nested_spec to recurse into."""
        contract = ContractSpec(fields=[FieldSpec("address", FieldType.OBJECT)])
        assert _find_field_spec(contract, "address.city") is None


class TestCoerceValue:
    def test_str_to_int(self) -> None:
        assert _coerce_value("5", FieldType.INTEGER) == 5

    def test_str_to_int_invalid(self) -> None:
        assert _coerce_value("five", FieldType.INTEGER) is _COERCE_FAILED

    def test_str_to_float(self) -> None:
        assert _coerce_value("3.14", FieldType.FLOAT) == 3.14

    def test_int_to_float(self) -> None:
        result = _coerce_value(5, FieldType.FLOAT)
        assert result == 5.0
        assert isinstance(result, float)

    def test_bool_to_float_fails(self) -> None:
        assert _coerce_value(True, FieldType.FLOAT) is _COERCE_FAILED

    def test_str_true_to_bool(self) -> None:
        assert _coerce_value("true", FieldType.BOOLEAN) is True

    def test_str_false_to_bool(self) -> None:
        assert _coerce_value("false", FieldType.BOOLEAN) is False

    def test_str_1_to_bool(self) -> None:
        assert _coerce_value("1", FieldType.BOOLEAN) is True

    def test_str_0_to_bool(self) -> None:
        assert _coerce_value("0", FieldType.BOOLEAN) is False

    def test_invalid_bool_string_fails(self) -> None:
        assert _coerce_value("maybe", FieldType.BOOLEAN) is _COERCE_FAILED

    def test_unsupported_target_type_fails(self) -> None:
        assert _coerce_value("x", FieldType.STRING) is _COERCE_FAILED
        assert _coerce_value("x", FieldType.OBJECT) is _COERCE_FAILED
        # ARRAY without a declared item_type: wrap is refused.
        assert _coerce_value("x", FieldType.ARRAY) is _COERCE_FAILED

    def test_array_wrap_with_matching_item_type(self) -> None:
        assert _coerce_value("x", FieldType.ARRAY, item_type=FieldType.STRING) == ["x"]

    def test_array_wrap_with_mismatched_item_type_fails(self) -> None:
        assert _coerce_value("x", FieldType.ARRAY, item_type=FieldType.INTEGER) is _COERCE_FAILED

    def test_array_wrap_of_list_value_fails(self) -> None:
        """Never double-wrap: a value that is already a list is refused."""
        result = _coerce_value(["x"], FieldType.ARRAY, item_type=FieldType.STRING)
        assert result is _COERCE_FAILED

    def test_union_resolves_to_array_member_and_wraps(self) -> None:
        members = (
            UnionMember(FieldType.STRING),
            UnionMember(FieldType.ARRAY, item_type=FieldType.ANY),
        )
        value = {"low_level": [], "high_level": []}
        assert _coerce_value(value, FieldType.UNION, union_members=members) == [value]

    def test_union_resolves_to_scalar_member(self) -> None:
        members = (UnionMember(FieldType.INTEGER), UnionMember(FieldType.OBJECT))
        assert _coerce_value("42", FieldType.UNION, union_members=members) == 42

    def test_union_ambiguous_tie_fails(self) -> None:
        members = (UnionMember(FieldType.INTEGER), UnionMember(FieldType.FLOAT))
        assert _coerce_value("42", FieldType.UNION, union_members=members) is _COERCE_FAILED

    def test_union_without_members_fails(self) -> None:
        assert _coerce_value("42", FieldType.UNION) is _COERCE_FAILED

    def test_non_string_non_bool_to_integer_fails(self) -> None:
        """A float (not str) value to INTEGER is not a supported cast."""
        assert _coerce_value(5.5, FieldType.INTEGER) is _COERCE_FAILED

    def test_dict_to_float_fails(self) -> None:
        assert _coerce_value({"a": 1}, FieldType.FLOAT) is _COERCE_FAILED

    def test_non_numeric_string_to_float_fails(self) -> None:
        assert _coerce_value("not a number", FieldType.FLOAT) is _COERCE_FAILED

    def test_non_string_to_boolean_fails(self) -> None:
        assert _coerce_value(1, FieldType.BOOLEAN) is _COERCE_FAILED
        assert _coerce_value(1.0, FieldType.BOOLEAN) is _COERCE_FAILED


class TestApplyOperationDirect:
    def _engine(self) -> RepairEngine:
        return make_engine()

    def test_apply_rename_top_level(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        data: dict[str, Any] = {"temp_celsius": 31.5}
        op = FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="temperature",
            confidence=1.0,
            rationale="r",
            source_path="temp_celsius",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"temperature": 31.5}

    def test_apply_rename_nested(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("address", FieldType.OBJECT)])
        data: dict[str, Any] = {"address": {"zip": "400001"}}
        op = FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="address.zip_code",
            confidence=1.0,
            rationale="r",
            source_path="address.zip",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"address": {"zip_code": "400001"}}

    def test_apply_rename_missing_source_is_noop(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        data: dict[str, Any] = {"other": 1}
        op = FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="temperature",
            confidence=1.0,
            rationale="r",
            source_path="missing_source",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"other": 1}

    def test_apply_coerce(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        data: dict[str, Any] = {"count": "5"}
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="count",
            confidence=0.95,
            rationale="r",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"count": 5}
        assert isinstance(data["count"], int)

    def test_apply_coerce_missing_value_is_noop(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        data: dict[str, Any] = {}
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="count",
            confidence=0.95,
            rationale="r",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {}

    def test_apply_coerce_unknown_field_is_noop(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        data: dict[str, Any] = {"other": "5"}
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="other",
            confidence=0.95,
            rationale="r",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"other": "5"}

    def test_apply_coerce_failed_cast_is_noop(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        data: dict[str, Any] = {"count": "not a number"}
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="count",
            confidence=0.95,
            rationale="r",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"count": "not a number"}

    def test_apply_set_default(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("humidity", FieldType.INTEGER, default=60)])
        data: dict[str, Any] = {}
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="humidity",
            confidence=1.0,
            rationale="r",
            value=60,
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"humidity": 60}

    def test_apply_remove(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)], strict_mode=True)
        data: dict[str, Any] = {"x": "ok", "extra": "remove me"}
        op = FieldOperation(
            op_type=FieldOpType.REMOVE,
            target_path="extra",
            confidence=1.0,
            rationale="r",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"x": "ok"}

    def test_apply_set_value(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("status", FieldType.STRING)])
        data: dict[str, Any] = {}
        op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="status",
            confidence=0.5,
            rationale="r",
            value="forced",
        )
        self._engine()._apply_operation(data, op, contract)
        assert data == {"status": "forced"}


# ===========================================================================
# Telemetry emission
# ===========================================================================


class TestTelemetryEmission:
    def test_already_valid_sequence(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        hook = CapturingTelemetryHook()
        engine = make_engine(telemetry=hook)
        engine.repair(contract, {"x": "hello"}, MockContractAdapter())

        assert hook.event_types() == ["validation_started", "repair_completed"]

    def test_success_sequence(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        hook = CapturingTelemetryHook()
        engine = make_engine(telemetry=hook)
        engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())

        types = hook.event_types()
        assert types[0] == "validation_started"
        assert types.count("violation_detected") == 2
        assert "repair_started" in types
        assert "strategy_selected" in types
        assert "repair_attempt_started" in types
        assert "operation_applied" in types
        assert "revalidation_started" in types
        assert types[-1] == "repair_completed"

    def test_success_sequence_order(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        hook = CapturingTelemetryHook()
        engine = make_engine(telemetry=hook)
        engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        types = hook.event_types()

        idx_validation = types.index("validation_started")
        idx_repair_started = types.index("repair_started")
        idx_strategy = types.index("strategy_selected")
        idx_attempt = types.index("repair_attempt_started")
        idx_applied = types.index("operation_applied")
        idx_revalidation = types.index("revalidation_started")
        idx_completed = types.index("repair_completed")

        assert (
            idx_validation
            < idx_repair_started
            < idx_strategy
            < idx_attempt
            < idx_applied
            < idx_revalidation
            < idx_completed
        )

    def test_failed_sequence_ends_with_repair_failed(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        hook = CapturingTelemetryHook()
        engine = make_engine(telemetry=hook)
        engine.repair(contract, {}, MockContractAdapter())

        types = hook.event_types()
        assert types[-1] == "repair_failed"
        assert "repair_completed" not in types

    def test_operation_rejected_emitted(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.INTEGER)])
        low = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="a",
            confidence=0.1,
            rationale="too low",
            value=1,
        )
        registry = StrategyRegistry(
            [MockRepairStrategy(name="AllLow", priority=10, handle=True, operations=[low])]
        )
        hook = CapturingTelemetryHook()
        engine = make_engine(
            registry=registry, config=RepairConfig(min_confidence_threshold=0.7), telemetry=hook
        )
        engine.repair(contract, {}, MockContractAdapter())

        assert "operation_rejected" in hook.event_types()

    def test_default_telemetry_is_noop(self) -> None:
        """No telemetry argument -> NoopTelemetry; repair still works."""
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = RepairEngine(
            registry=full_registry(), config=RepairConfig(), logger=RepairLogger()
        )
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        assert result.status is RepairStatus.ALREADY_VALID

    def test_contract_id_in_telemetry_events(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        hook = CapturingTelemetryHook()
        engine = make_engine(telemetry=hook)
        engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        for event in hook.events:
            assert event.contract_id == contract.contract_id


# ===========================================================================
# RepairResult construction / audit log
# ===========================================================================


class TestRepairResultConstruction:
    def test_repair_log_non_empty_on_success(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        assert len(result.repair_log) > 0

    def test_repair_log_non_empty_on_failed(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("widget_id", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {}, MockContractAdapter())
        assert len(result.repair_log) > 0

    def test_repair_log_non_empty_on_already_valid(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        assert len(result.repair_log) > 0

    def test_log_entries_have_required_fields(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        for entry in result.repair_log:
            assert entry.timestamp is not None
            assert entry.level is not None
            assert entry.event
            assert entry.message

    def test_contract_id_matches_input_contract(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        assert result.contract_id == contract.contract_id

    def test_repaired_at_is_set(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        engine = make_engine()
        result = engine.repair(contract, {"x": "hello"}, MockContractAdapter())
        assert result.repaired_at is not None

    def test_deep_copy_mutation_safety(self) -> None:
        """Mutating the returned repaired_output must not affect original_input."""
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        data = {"cty": "Mumbai", "population": 1}
        engine = make_engine()
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.repaired_output is not None
        result.repaired_output["city"] = "Modified"
        assert result.original_input.get("cty") == "Mumbai"
        assert data == {"cty": "Mumbai", "population": 1}

    def test_attempt_data_snapshots_are_independent_copies(self) -> None:
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(contract, {"cty": "Mumbai", "population": 1}, MockContractAdapter())
        attempt = result.attempts[0]
        attempt.data_after["mutated"] = True
        assert "mutated" not in result.repaired_output  # type: ignore[operator]


# ===========================================================================
# Merged validation (architectural correction)
# ===========================================================================


class _StripUnexpectedAdapter(IContractAdapter):
    """
    Wraps MockContractAdapter but strips UNEXPECTED_FIELD violations,
    simulating a framework validator (e.g. Pydantic without
    extra='forbid') that does not report extra fields at all.
    """

    def __init__(self) -> None:
        self._inner = MockContractAdapter()

    def extract_contract(self, schema: Any) -> ContractSpec:
        return self._inner.extract_contract(schema)

    def validate(self, contract: ContractSpec, data: dict[str, Any]) -> ValidationResult:
        result = self._inner.validate(contract, data)
        filtered = [
            v for v in result.violations if v.violation_type is not ViolationType.UNEXPECTED_FIELD
        ]
        is_valid = not any(v.severity is ViolationSeverity.ERROR for v in filtered)
        return ValidationResult(
            is_valid=is_valid,
            violations=filtered,
            raw_input=result.raw_input,
            contract_id=result.contract_id,
        )

    def wrap(self, contract: ContractSpec, data: dict[str, Any]) -> Any:
        return self._inner.wrap(contract, data)


class TestMergedValidation:
    def test_adapter_without_unexpected_field_still_repairs(self) -> None:
        """
        Even when the adapter never reports UNEXPECTED_FIELD,
        ContractValidator fills the gap so FuzzyFieldMatchStrategy can fire.
        """
        contract = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        engine = make_engine()
        result = engine.repair(
            contract, {"cty": "Mumbai", "population": 1}, _StripUnexpectedAdapter()
        )
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {"city": "Mumbai", "population": 1}

    def test_merged_validate_includes_unexpected_from_core(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        engine = make_engine()
        merged = engine._validate(
            contract, {"city": "Mumbai", "extra": 1}, _StripUnexpectedAdapter()
        )
        types = {v.violation_type for v in merged.violations}
        assert ViolationType.UNEXPECTED_FIELD in types

    def test_merged_validate_is_valid_true_for_clean_data(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        engine = make_engine()
        merged = engine._validate(contract, {"city": "Mumbai"}, _StripUnexpectedAdapter())
        assert merged.is_valid is True
        assert merged.violations == []

    def test_merged_validate_with_mock_adapter_no_duplicates(self) -> None:
        """MockContractAdapter already reports UNEXPECTED_FIELD; merging
        with ContractValidator must not duplicate it."""
        contract = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        engine = make_engine()
        merged = engine._validate(contract, {"city": "Mumbai", "extra": 1}, MockContractAdapter())
        unexpected = [
            v for v in merged.violations if v.violation_type is ViolationType.UNEXPECTED_FIELD
        ]
        assert len(unexpected) == 1

    def test_core_only_error_makes_invalid_in_strict_mode(self) -> None:
        """
        In strict_mode, ContractValidator marks UNEXPECTED_FIELD as ERROR.
        If the adapter doesn't know about strict_mode and says is_valid=True,
        the merged result must still be invalid.
        """
        contract = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)], strict_mode=True)
        engine = make_engine()
        merged = engine._validate(
            contract, {"city": "Mumbai", "extra": 1}, _StripUnexpectedAdapter()
        )
        assert merged.is_valid is False
        unexpected = [
            v for v in merged.violations if v.violation_type is ViolationType.UNEXPECTED_FIELD
        ]
        assert unexpected[0].severity is ViolationSeverity.ERROR

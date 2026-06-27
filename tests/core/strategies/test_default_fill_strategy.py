"""Tests for stateguard.core.strategies.default_fill."""

from __future__ import annotations

from stateguard.core.errors.operations import FieldOpType
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.default_fill import (
    DefaultValueFillStrategy,
    _find_field_spec,
)
from tests.conftest import make_violation


# ===========================================================================
# Identity
# ===========================================================================


class TestIdentity:

    def test_name(self) -> None:
        assert DefaultValueFillStrategy().name == "DefaultValueFillStrategy"

    def test_priority(self) -> None:
        assert DefaultValueFillStrategy().priority == 40


# ===========================================================================
# can_handle
# ===========================================================================


class TestCanHandle:

    def test_true_when_default_declared(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v], contract, {}) is True

    def test_false_when_default_is_missing_sentinel(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER),  # default=MISSING
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_true_when_default_is_explicit_none(self) -> None:
        """default=None is distinct from MISSING and is a valid default."""
        contract = ContractSpec(fields=[
            FieldSpec("description", FieldType.STRING, default=None),
        ])
        v = make_violation(
            field_path="description",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v], contract, {}) is True

    def test_false_with_no_violations(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([], contract, {}) is False

    def test_false_with_only_non_missing_violations(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_false_when_field_spec_not_found(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="nonexistent",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_true_with_multiple_violations_one_matching(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),  # no default
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v1 = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        v2 = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        assert strategy.can_handle([v1, v2], contract, {}) is True


# ===========================================================================
# propose
# ===========================================================================


class TestPropose:

    def test_int_default_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})

        assert len(ops) == 1
        op = ops[0]
        assert op.op_type is FieldOpType.SET_DEFAULT
        assert op.target_path == "humidity"
        assert op.confidence == 1.0
        assert op.value == 60
        assert op.source_path is None

    def test_string_default_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("status", FieldType.STRING, default="unknown"),
        ])
        v = make_violation(
            field_path="status",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert ops[0].value == "unknown"

    def test_float_default_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, default=20.0),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert ops[0].value == 20.0

    def test_bool_default_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("active", FieldType.BOOLEAN, default=False),
        ])
        v = make_violation(
            field_path="active",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert ops[0].value is False

    def test_none_default_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("description", FieldType.STRING, default=None),
        ])
        v = make_violation(
            field_path="description",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert len(ops) == 1
        assert ops[0].value is None

    def test_missing_sentinel_default_not_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER),  # default=MISSING
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert ops == []

    def test_non_missing_violation_ignored(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {"humidity": "not an int"})
        assert ops == []

    def test_field_spec_not_found_proposes_nothing(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="nonexistent",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert ops == []

    def test_multiple_missing_fields_with_defaults(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
            FieldSpec("status", FieldType.STRING, default="unknown"),
        ])
        v1 = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        v2 = make_violation(
            field_path="status",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v1, v2], contract, {})
        assert len(ops) == 2
        values = {op.target_path: op.value for op in ops}
        assert values == {"humidity": 60, "status": "unknown"}

    def test_mixed_fields_only_those_with_defaults_filled(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),  # no default
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v1 = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        v2 = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v1, v2], contract, {})
        assert len(ops) == 1
        assert ops[0].target_path == "humidity"

    def test_rationale_mentions_field_and_value(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        v = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {})
        assert "humidity" in ops[0].rationale
        assert "60" in ops[0].rationale

    def test_empty_violations_returns_empty(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("humidity", FieldType.INTEGER, default=60),
        ])
        strategy = DefaultValueFillStrategy()
        assert strategy.propose([], contract, {}) == []


# ===========================================================================
# propose — nested fields
# ===========================================================================


class TestProposeNested:

    def test_nested_field_default_filled(self) -> None:
        inner = ContractSpec(fields=[
            FieldSpec("country", FieldType.STRING, default="India"),
        ])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])
        v = make_violation(
            field_path="address.country",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {"address": {}})
        assert len(ops) == 1
        assert ops[0].target_path == "address.country"
        assert ops[0].value == "India"

    def test_deeply_nested_field_default(self) -> None:
        level2 = ContractSpec(fields=[
            FieldSpec("code", FieldType.STRING, default="IN"),
        ])
        level1 = ContractSpec(fields=[
            FieldSpec("country", FieldType.OBJECT, nested_spec=level2),
        ])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=level1),
        ])
        v = make_violation(
            field_path="address.country.code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = DefaultValueFillStrategy()
        ops = strategy.propose([v], contract, {"address": {"country": {}}})
        assert len(ops) == 1
        assert ops[0].target_path == "address.country.code"
        assert ops[0].value == "IN"


# ===========================================================================
# _find_field_spec — direct tests
# ===========================================================================


class TestFindFieldSpec:

    def test_top_level_field_found(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("humidity", FieldType.INTEGER)])
        spec = _find_field_spec(contract, "humidity")
        assert spec is not None
        assert spec.path == "humidity"

    def test_top_level_field_not_found(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("humidity", FieldType.INTEGER)])
        assert _find_field_spec(contract, "temperature") is None

    def test_nested_field_found(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])
        spec = _find_field_spec(contract, "address.city")
        assert spec is not None
        assert spec.path == "city"

    def test_nested_field_not_found_no_nested_spec(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT),
        ])
        assert _find_field_spec(contract, "address.city") is None

    def test_intermediate_segment_not_found(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])
        assert _find_field_spec(contract, "billing.city") is None

    def test_default_sentinel_returned_correctly(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("humidity", FieldType.INTEGER)])
        spec = _find_field_spec(contract, "humidity")
        assert spec is not None
        assert spec.default is MISSING

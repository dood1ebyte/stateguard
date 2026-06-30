"""Tests for stateguard.core.strategies.alias."""

from __future__ import annotations

from stateguard.core.errors.operations import FieldOpType
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies.alias import (
    ExactAliasStrategy,
    _find_field_spec,
    _get_dict_at_path,
    _split_path,
)
from tests.conftest import make_violation


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


class TestIdentity:

    def test_name(self) -> None:
        assert ExactAliasStrategy().name == "ExactAliasStrategy"

    def test_priority(self) -> None:
        assert ExactAliasStrategy().priority == 10


# ---------------------------------------------------------------------------
# can_handle
# ---------------------------------------------------------------------------


class TestCanHandle:

    def test_true_when_missing_field_has_aliases(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([v], contract, {"temp": 30.0}) is True

    def test_false_when_missing_field_has_no_aliases(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_false_when_no_violations(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([], contract, {}) is False

    def test_false_when_only_unexpected_field_violations(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="extra",
            violation_type=ViolationType.UNEXPECTED_FIELD,
            severity=ViolationSeverity.WARNING,
        )
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_false_when_field_spec_not_found(self) -> None:
        """Violation references a field_path not present in the contract."""
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="nonexistent",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([v], contract, {}) is False

    def test_true_with_multiple_violations_one_matching(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        v1 = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        v2 = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        assert strategy.can_handle([v1, v2], contract, {}) is True


# ---------------------------------------------------------------------------
# propose — top-level
# ---------------------------------------------------------------------------


class TestProposeTopLevel:

    def test_exact_alias_present_proposes_rename(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"temp": 30.0, "humidity": 80}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)

        assert len(ops) == 1
        op = ops[0]
        assert op.op_type is FieldOpType.RENAME
        assert op.source_path == "temp"
        assert op.target_path == "temperature"
        assert op.confidence == 1.0

    def test_alias_not_present_proposes_nothing(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"unrelated_key": 30.0}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert ops == []

    def test_no_field_spec_proposes_nothing(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="nonexistent",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, {"temp": 30.0})
        assert ops == []

    def test_no_known_aliases_proposes_nothing(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, {"temp": 30.0})
        assert ops == []

    def test_non_missing_violation_ignored(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
        )
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, {"temp": 30.0})
        assert ops == []

    def test_multiple_aliases_first_match_wins(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "temperature",
                FieldType.FLOAT,
                known_aliases=["temp", "temp_c"],
            ),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        # Only the second alias is present in data.
        data = {"temp_c": 30.0}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert len(ops) == 1
        assert ops[0].source_path == "temp_c"

    def test_first_alias_preferred_when_both_present(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "temperature",
                FieldType.FLOAT,
                known_aliases=["temp", "temp_c"],
            ),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"temp": 30.0, "temp_c": 31.0}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert len(ops) == 1
        assert ops[0].source_path == "temp"

    def test_multiple_missing_fields_each_repaired(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
            FieldSpec("humidity", FieldType.INTEGER, known_aliases=["rh"]),
        ])
        v1 = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        v2 = make_violation(
            field_path="humidity",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"temp": 30.0, "rh": 80}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v1, v2], contract, data)
        assert len(ops) == 2
        targets = {op.target_path for op in ops}
        assert targets == {"temperature", "humidity"}

    def test_rationale_mentions_alias_and_field(self) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT, known_aliases=["temp"]),
        ])
        v = make_violation(
            field_path="temperature",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, {"temp": 30.0})
        assert "temp" in ops[0].rationale
        assert "temperature" in ops[0].rationale


# ---------------------------------------------------------------------------
# propose — nested fields
# ---------------------------------------------------------------------------


class TestProposeNested:

    def _address_contract(self) -> ContractSpec:
        inner = ContractSpec(fields=[
            FieldSpec("zip_code", FieldType.STRING, known_aliases=["zip"]),
            FieldSpec("city", FieldType.STRING),
        ])
        return ContractSpec(fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])

    def test_nested_alias_match(self) -> None:
        contract = self._address_contract()
        v = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "zip": "400001"},
        }
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert len(ops) == 1
        op = ops[0]
        assert op.target_path == "address.zip_code"
        assert op.source_path == "address.zip"
        assert op.confidence == 1.0

    def test_nested_alias_not_present(self) -> None:
        contract = self._address_contract()
        v = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"name": "Alice", "address": {"city": "Mumbai"}}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert ops == []

    def test_nested_parent_missing_returns_none_local_data(self) -> None:
        """If the parent dict itself is absent, no rename is proposed."""
        contract = self._address_contract()
        v = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"name": "Alice"}  # 'address' key entirely absent
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert ops == []

    def test_nested_parent_not_a_dict(self) -> None:
        contract = self._address_contract()
        v = make_violation(
            field_path="address.zip_code",
            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
        )
        data = {"name": "Alice", "address": "not a dict"}
        strategy = ExactAliasStrategy()
        ops = strategy.propose([v], contract, data)
        assert ops == []


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


class TestFindFieldSpec:

    def test_top_level_field_found(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        spec = _find_field_spec(contract, "temperature")
        assert spec is not None
        assert spec.path == "temperature"

    def test_top_level_field_not_found(self) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        assert _find_field_spec(contract, "humidity") is None

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
            FieldSpec("address", FieldType.OBJECT),  # no nested_spec
        ])
        assert _find_field_spec(contract, "address.city") is None

    def test_deeply_nested_field(self) -> None:
        level2 = ContractSpec(fields=[FieldSpec("code", FieldType.STRING)])
        level1 = ContractSpec(fields=[
            FieldSpec("country", FieldType.OBJECT, nested_spec=level2),
        ])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=level1),
        ])
        spec = _find_field_spec(contract, "address.country.code")
        assert spec is not None
        assert spec.path == "code"

    def test_intermediate_segment_not_found(self) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        contract = ContractSpec(fields=[
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])
        assert _find_field_spec(contract, "billing.city") is None


class TestSplitPath:

    def test_top_level_path(self) -> None:
        assert _split_path("temperature") == ("", "temperature")

    def test_nested_path(self) -> None:
        assert _split_path("address.city") == ("address", "city")

    def test_deeply_nested_path(self) -> None:
        assert _split_path("a.b.c") == ("a.b", "c")


class TestGetDictAtPath:

    def test_root_path_returns_data_itself(self) -> None:
        data = {"a": 1}
        assert _get_dict_at_path(data, "") is data

    def test_single_level_path(self) -> None:
        data = {"address": {"city": "Mumbai"}}
        assert _get_dict_at_path(data, "address") == {"city": "Mumbai"}

    def test_multi_level_path(self) -> None:
        data = {"a": {"b": {"c": 1}}}
        assert _get_dict_at_path(data, "a.b") == {"c": 1}

    def test_missing_path_returns_none(self) -> None:
        data = {"a": 1}
        assert _get_dict_at_path(data, "b") is None

    def test_path_to_non_dict_returns_none(self) -> None:
        data = {"a": "not a dict"}
        assert _get_dict_at_path(data, "a") is None

    def test_intermediate_non_dict_returns_none(self) -> None:
        data = {"a": "not a dict"}
        assert _get_dict_at_path(data, "a.b") is None

"""Tests for stateguard.core.validator."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)
from stateguard.core.validator import ContractValidator


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def validator() -> ContractValidator:
    return ContractValidator()


def _violation_types(violations: list[Any]) -> set[ViolationType]:
    return {v.violation_type for v in violations}


def _find(violations: list[Any], field_path: str) -> Any:
    matches = [v for v in violations if v.field_path == field_path]
    assert len(matches) == 1, (
        f"Expected exactly one violation for '{field_path}', "
        f"found {len(matches)}: {matches}"
    )
    return matches[0]


# ===========================================================================
# Empty / trivial cases
# ===========================================================================


class TestEmptyAndTrivial:

    def test_empty_contract_empty_data_is_valid(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[])
        result = validator.validate(contract, {})
        assert result.is_valid is True
        assert result.violations == []

    def test_empty_contract_with_data_strict_false(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[], strict_mode=False)
        result = validator.validate(contract, {"extra": 1})
        assert result.is_valid is True
        assert len(result.violations) == 1
        assert result.violations[0].violation_type is ViolationType.UNEXPECTED_FIELD
        assert result.violations[0].severity is ViolationSeverity.WARNING

    def test_empty_contract_with_data_strict_true(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[], strict_mode=True)
        result = validator.validate(contract, {"extra": 1})
        assert result.is_valid is False
        assert result.violations[0].severity is ViolationSeverity.ERROR

    def test_result_contract_id_matches(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        result = validator.validate(contract, {"x": "hello"})
        assert result.contract_id == contract.contract_id

    def test_result_raw_input_is_copy(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)])
        data = {"x": "hello"}
        result = validator.validate(contract, data)
        assert result.raw_input == data
        assert result.raw_input is not data


# ===========================================================================
# Exact match — no violations
# ===========================================================================


class TestExactMatch:

    def test_single_string_field_valid(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("name", FieldType.STRING)])
        result = validator.validate(contract, {"name": "Alice"})
        assert result.is_valid is True
        assert result.violations == []

    def test_weather_example_valid(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        result = validator.validate(contract, {"temperature": 31.5, "humidity": 80})
        assert result.is_valid is True
        assert result.violations == []

    def test_integer_value_for_float_field_is_valid(
        self, validator: ContractValidator
    ) -> None:
        """An int is an acceptable value for a FLOAT field."""
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        result = validator.validate(contract, {"temperature": 30})
        assert result.is_valid is True

    def test_all_primitive_types_valid(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("s", FieldType.STRING),
            FieldSpec("i", FieldType.INTEGER),
            FieldSpec("f", FieldType.FLOAT),
            FieldSpec("b", FieldType.BOOLEAN),
        ])
        result = validator.validate(contract, {"s": "x", "i": 1, "f": 1.5, "b": True})
        assert result.is_valid is True
        assert result.violations == []

    def test_any_field_accepts_anything(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.ANY)])
        for value in [1, "str", 1.5, True, [1, 2], {"a": 1}]:
            result = validator.validate(contract, {"x": value})
            assert result.is_valid is True, f"ANY field rejected {value!r}"


# ===========================================================================
# Missing required fields
# ===========================================================================


class TestMissingRequiredField:

    def test_single_missing_required_field(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        result = validator.validate(contract, {})
        assert result.is_valid is False
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.severity is ViolationSeverity.ERROR
        assert v.field_path == "temperature"
        assert v.expected_type is FieldType.FLOAT

    def test_multiple_missing_required_fields(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        result = validator.validate(contract, {})
        assert result.is_valid is False
        types = _violation_types(result.violations)
        assert types == {ViolationType.MISSING_REQUIRED_FIELD}
        paths = {v.field_path for v in result.violations}
        assert paths == {"temperature", "humidity"}

    def test_one_missing_one_present(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        result = validator.validate(contract, {"humidity": 80})
        assert result.is_valid is False
        assert len(result.violations) == 1
        assert result.violations[0].field_path == "temperature"

    def test_optional_field_missing_is_not_a_violation(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("description", FieldType.STRING, required=False),
        ])
        result = validator.validate(contract, {"temperature": 30.0})
        assert result.is_valid is True
        assert result.violations == []

    def test_missing_required_field_message_mentions_path(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("user_id", FieldType.STRING)])
        result = validator.validate(contract, {})
        assert "user_id" in result.violations[0].message


# ===========================================================================
# Unexpected fields
# ===========================================================================


class TestUnexpectedField:

    def test_unexpected_field_non_strict_is_warning(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        result = validator.validate(
            contract, {"temperature": 30.0, "temp_celsius": 30.0}
        )
        # is_valid True since UNEXPECTED_FIELD is WARNING in non-strict mode
        assert result.is_valid is True
        v = _find(result.violations, "temp_celsius")
        assert v.violation_type is ViolationType.UNEXPECTED_FIELD
        assert v.severity is ViolationSeverity.WARNING
        assert v.received_value == 30.0

    def test_unexpected_field_strict_is_error(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(
            fields=[FieldSpec("temperature", FieldType.FLOAT)],
            strict_mode=True,
        )
        result = validator.validate(
            contract, {"temperature": 30.0, "temp_celsius": 30.0}
        )
        assert result.is_valid is False
        v = _find(result.violations, "temp_celsius")
        assert v.severity is ViolationSeverity.ERROR

    def test_multiple_unexpected_fields(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.STRING)])
        result = validator.validate(contract, {"a": "x", "b": 1, "c": 2})
        unexpected = [
            v for v in result.violations
            if v.violation_type is ViolationType.UNEXPECTED_FIELD
        ]
        assert {v.field_path for v in unexpected} == {"b", "c"}

    def test_weather_drift_scenario(self, validator: ContractValidator) -> None:
        """
        The canonical scenario: temp_celsius is unexpected,
        temperature is missing — both detected, non-halting.
        """
        contract = ContractSpec(fields=[
            FieldSpec("temperature", FieldType.FLOAT),
            FieldSpec("humidity", FieldType.INTEGER),
        ])
        result = validator.validate(
            contract, {"temp_celsius": 31.5, "humidity": 80}
        )
        assert result.is_valid is False
        types = _violation_types(result.violations)
        assert ViolationType.MISSING_REQUIRED_FIELD in types
        assert ViolationType.UNEXPECTED_FIELD in types

        missing = _find(result.violations, "temperature")
        assert missing.violation_type is ViolationType.MISSING_REQUIRED_FIELD

        unexpected = _find(result.violations, "temp_celsius")
        assert unexpected.violation_type is ViolationType.UNEXPECTED_FIELD
        assert unexpected.received_value == 31.5


# ===========================================================================
# Type mismatch
# ===========================================================================


class TestTypeMismatch:

    def test_string_where_int_expected(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        result = validator.validate(contract, {"count": "5"})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.severity is ViolationSeverity.ERROR
        assert v.expected_type is FieldType.INTEGER
        assert v.received_value == "5"

    def test_bool_is_not_valid_integer(self, validator: ContractValidator) -> None:
        """bool is a subclass of int but must not satisfy INTEGER."""
        contract = ContractSpec(fields=[FieldSpec("count", FieldType.INTEGER)])
        result = validator.validate(contract, {"count": True})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_bool_is_not_valid_float(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("value", FieldType.FLOAT)])
        result = validator.validate(contract, {"value": False})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_string_where_float_expected(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("temperature", FieldType.FLOAT)])
        result = validator.validate(contract, {"temperature": "31.5"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_int_where_string_expected(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[FieldSpec("name", FieldType.STRING)])
        result = validator.validate(contract, {"name": 123})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_none_where_string_expected_no_not_null_is_not_type_mismatch(
        self, validator: ContractValidator
    ) -> None:
        """
        A None value without NOT_NULL constraint produces no violation
        (None handling short-circuits before type checking).
        """
        contract = ContractSpec(fields=[FieldSpec("name", FieldType.STRING)])
        result = validator.validate(contract, {"name": None})
        assert result.is_valid is True
        assert result.violations == []

    def test_null_field_type_with_none_value_is_valid(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.NULL)])
        result = validator.validate(contract, {"x": None})
        assert result.is_valid is True

    def test_null_field_type_with_non_none_value_is_type_mismatch(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("x", FieldType.NULL)])
        result = validator.validate(contract, {"x": "not null"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH
        assert result.violations[0].expected_type is FieldType.NULL

    @pytest.mark.parametrize(
        ("field_type", "value"),
        [
            (FieldType.STRING, 123),
            (FieldType.INTEGER, "5"),
            (FieldType.INTEGER, 5.5),
            (FieldType.FLOAT, "5.5"),
            (FieldType.BOOLEAN, "true"),
            (FieldType.BOOLEAN, 1),
        ],
    )
    def test_type_mismatch_matrix(
        self,
        validator: ContractValidator,
        field_type: FieldType,
        value: Any,
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("field", field_type)])
        result = validator.validate(contract, {"field": value})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    @pytest.mark.parametrize(
        ("field_type", "value"),
        [
            (FieldType.STRING, "hello"),
            (FieldType.INTEGER, 5),
            (FieldType.FLOAT, 5),
            (FieldType.FLOAT, 5.5),
            (FieldType.BOOLEAN, True),
            (FieldType.BOOLEAN, False),
        ],
    )
    def test_type_match_matrix(
        self,
        validator: ContractValidator,
        field_type: FieldType,
        value: Any,
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("field", field_type)])
        result = validator.validate(contract, {"field": value})
        assert result.is_valid is True


# ===========================================================================
# Null handling
# ===========================================================================


class TestNullHandling:

    def test_not_null_constraint_violated(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "user_id",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.NOT_NULL, True)],
            )
        ])
        result = validator.validate(contract, {"user_id": None})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.NULL_NOT_ALLOWED
        assert v.severity is ViolationSeverity.ERROR
        assert v.received_value is None

    def test_not_null_constraint_satisfied(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "user_id",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.NOT_NULL, True)],
            )
        ])
        result = validator.validate(contract, {"user_id": "abc"})
        assert result.is_valid is True

    def test_none_without_not_null_constraint_is_valid(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("optional_field", FieldType.STRING)])
        result = validator.validate(contract, {"optional_field": None})
        assert result.is_valid is True
        assert result.violations == []

    def test_none_skips_constraint_checks(self, validator: ContractValidator) -> None:
        """A None value (without NOT_NULL) should not trigger other constraints."""
        contract = ContractSpec(fields=[
            FieldSpec(
                "age",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MINIMUM, 0)],
            )
        ])
        result = validator.validate(contract, {"age": None})
        assert result.is_valid is True


# ===========================================================================
# Value constraint violations
# ===========================================================================


class TestValueConstraints:

    def test_minimum_violated(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "age",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MINIMUM, 0)],
            )
        ])
        result = validator.validate(contract, {"age": -5})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 0
        assert v.received_value == -5

    def test_minimum_satisfied_at_boundary(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "age",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MINIMUM, 0)],
            )
        ])
        result = validator.validate(contract, {"age": 0})
        assert result.is_valid is True

    def test_maximum_violated(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "percent",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MAXIMUM, 100)],
            )
        ])
        result = validator.validate(contract, {"percent": 150})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_maximum_satisfied_at_boundary(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "percent",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MAXIMUM, 100)],
            )
        ])
        result = validator.validate(contract, {"percent": 100})
        assert result.is_valid is True

    def test_min_length_violated_string(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "name",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.MIN_LENGTH, 3)],
            )
        ])
        result = validator.validate(contract, {"name": "ab"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_min_length_satisfied_string(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "name",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.MIN_LENGTH, 3)],
            )
        ])
        result = validator.validate(contract, {"name": "abc"})
        assert result.is_valid is True

    def test_max_length_violated_string(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "code",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.MAX_LENGTH, 5)],
            )
        ])
        result = validator.validate(contract, {"code": "toolong"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_max_length_satisfied_string(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "code",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.MAX_LENGTH, 5)],
            )
        ])
        result = validator.validate(contract, {"code": "abc"})
        assert result.is_valid is True

    def test_min_length_on_array(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "items",
                FieldType.ARRAY,
                constraints=[FieldConstraint(FieldConstraintType.MIN_LENGTH, 1)],
            )
        ])
        result = validator.validate(contract, {"items": []})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_pattern_violated(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "zip_code",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.PATTERN, r"^\d{6}$")],
            )
        ])
        result = validator.validate(contract, {"zip_code": "abc123"})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == r"^\d{6}$"

    def test_pattern_satisfied(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "zip_code",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.PATTERN, r"^\d{6}$")],
            )
        ])
        result = validator.validate(contract, {"zip_code": "400001"})
        assert result.is_valid is True

    def test_enum_values_violated(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "status",
                FieldType.STRING,
                constraints=[
                    FieldConstraint(
                        FieldConstraintType.ENUM_VALUES,
                        ("active", "inactive", "pending"),
                    )
                ],
            )
        ])
        result = validator.validate(contract, {"status": "archived"})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == ("active", "inactive", "pending")
        assert v.received_value == "archived"

    def test_enum_values_satisfied(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "status",
                FieldType.STRING,
                constraints=[
                    FieldConstraint(
                        FieldConstraintType.ENUM_VALUES,
                        ("active", "inactive", "pending"),
                    )
                ],
            )
        ])
        result = validator.validate(contract, {"status": "active"})
        assert result.is_valid is True

    def test_constraint_skipped_on_type_mismatch(
        self, validator: ContractValidator
    ) -> None:
        """
        If type checking fails, constraint checking should not run
        (avoids spurious len()/comparison errors on wrong types).
        """
        contract = ContractSpec(fields=[
            FieldSpec(
                "age",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MINIMUM, 0)],
            )
        ])
        result = validator.validate(contract, {"age": "not a number"})
        assert result.is_valid is False
        # Only TYPE_MISMATCH, not also VALUE_CONSTRAINT_VIOLATION
        types = _violation_types(result.violations)
        assert types == {ViolationType.TYPE_MISMATCH}

    def test_multiple_constraints_all_checked(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec(
                "age",
                FieldType.INTEGER,
                constraints=[
                    FieldConstraint(FieldConstraintType.MINIMUM, 0),
                    FieldConstraint(FieldConstraintType.MAXIMUM, 150),
                ],
            )
        ])
        # Violates only MAXIMUM
        result = validator.validate(contract, {"age": 200})
        assert result.is_valid is False
        assert len(result.violations) == 1
        assert result.violations[0].expected_value == 150


# ===========================================================================
# Nested schemas
# ===========================================================================


class TestNestedSchemas:

    @staticmethod
    def _address_contract(strict: bool = False) -> ContractSpec:
        inner = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("zip_code", FieldType.STRING),
            ],
            strict_mode=strict,
        )
        return ContractSpec(fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=inner),
        ])

    def test_valid_nested_data(self, validator: ContractValidator) -> None:
        contract = self._address_contract()
        result = validator.validate(contract, {
            "name": "Alice",
            "address": {"city": "Mumbai", "zip_code": "400001"},
        })
        assert result.is_valid is True
        assert result.violations == []

    def test_missing_nested_required_field(
        self, validator: ContractValidator
    ) -> None:
        contract = self._address_contract()
        result = validator.validate(contract, {
            "name": "Alice",
            "address": {"city": "Mumbai"},
        })
        assert result.is_valid is False
        v = _find(result.violations, "address.zip_code")
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD

    def test_missing_whole_nested_object_emits_single_violation(
        self, validator: ContractValidator
    ) -> None:
        """
        If the entire 'address' object is missing, exactly one
        MISSING_REQUIRED_FIELD for 'address' is emitted — not one
        per nested field.
        """
        contract = self._address_contract()
        result = validator.validate(contract, {"name": "Alice"})
        assert result.is_valid is False
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.field_path == "address"

    def test_nested_object_not_a_dict_is_structural_mismatch(
        self, validator: ContractValidator
    ) -> None:
        contract = self._address_contract()
        result = validator.validate(contract, {
            "name": "Alice",
            "address": ["not", "a", "dict"],
        })
        assert result.is_valid is False
        v = _find(result.violations, "address")
        assert v.violation_type is ViolationType.STRUCTURAL_MISMATCH
        assert v.expected_type is FieldType.OBJECT

    def test_nested_unexpected_field_non_strict(
        self, validator: ContractValidator
    ) -> None:
        contract = self._address_contract(strict=False)
        result = validator.validate(contract, {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "zip_code": "400001",
                "country": "India",
            },
        })
        v = _find(result.violations, "address.country")
        assert v.violation_type is ViolationType.UNEXPECTED_FIELD
        assert v.severity is ViolationSeverity.WARNING
        # WARNING does not affect overall validity
        assert result.is_valid is True

    def test_nested_unexpected_field_strict(
        self, validator: ContractValidator
    ) -> None:
        contract = self._address_contract(strict=True)
        result = validator.validate(contract, {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "zip_code": "400001",
                "country": "India",
            },
        })
        v = _find(result.violations, "address.country")
        assert v.severity is ViolationSeverity.ERROR
        assert result.is_valid is False

    def test_nested_type_mismatch(self, validator: ContractValidator) -> None:
        contract = self._address_contract()
        result = validator.validate(contract, {
            "name": "Alice",
            "address": {"city": "Mumbai", "zip_code": 400001},
        })
        assert result.is_valid is False
        v = _find(result.violations, "address.zip_code")
        assert v.violation_type is ViolationType.TYPE_MISMATCH

    def test_two_level_nesting(self, validator: ContractValidator) -> None:
        country_spec = ContractSpec(fields=[FieldSpec("code", FieldType.STRING)])
        address_spec = ContractSpec(fields=[
            FieldSpec("city", FieldType.STRING),
            FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
        ])
        contract = ContractSpec(fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
        ])

        result = validator.validate(contract, {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "country": {"code": "IN"},
            },
        })
        assert result.is_valid is True

    def test_two_level_nesting_missing_deep_field(
        self, validator: ContractValidator
    ) -> None:
        country_spec = ContractSpec(fields=[FieldSpec("code", FieldType.STRING)])
        address_spec = ContractSpec(fields=[
            FieldSpec("city", FieldType.STRING),
            FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
        ])
        contract = ContractSpec(fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
        ])

        result = validator.validate(contract, {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "country": {},
            },
        })
        assert result.is_valid is False
        v = _find(result.violations, "address.country.code")
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD

    def test_optional_nested_object_absent_is_valid(
        self, validator: ContractValidator
    ) -> None:
        inner = ContractSpec(fields=[FieldSpec("city", FieldType.STRING)])
        contract = ContractSpec(fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec(
                "address", FieldType.OBJECT, required=False, nested_spec=inner
            ),
        ])
        result = validator.validate(contract, {"name": "Alice"})
        assert result.is_valid is True
        assert result.violations == []

    def test_object_field_without_nested_spec(
        self, validator: ContractValidator
    ) -> None:
        """An OBJECT field with no nested_spec just checks dict-ness."""
        contract = ContractSpec(fields=[
            FieldSpec("metadata", FieldType.OBJECT),
        ])
        result = validator.validate(contract, {"metadata": {"any": "thing"}})
        assert result.is_valid is True

    def test_object_field_without_nested_spec_wrong_type(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("metadata", FieldType.OBJECT),
        ])
        result = validator.validate(contract, {"metadata": "not a dict"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.STRUCTURAL_MISMATCH


# ===========================================================================
# Array fields
# ===========================================================================


class TestArrayFields:

    def test_array_of_strings_valid(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("tags", FieldType.ARRAY, item_type=FieldType.STRING)
        ])
        result = validator.validate(contract, {"tags": ["a", "b", "c"]})
        assert result.is_valid is True

    def test_array_with_wrong_item_type(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("tags", FieldType.ARRAY, item_type=FieldType.STRING)
        ])
        result = validator.validate(contract, {"tags": ["a", 2, "c"]})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_array_field_not_a_list(self, validator: ContractValidator) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("tags", FieldType.ARRAY, item_type=FieldType.STRING)
        ])
        result = validator.validate(contract, {"tags": "not a list"})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH

    def test_empty_array_valid_without_min_length(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("tags", FieldType.ARRAY, item_type=FieldType.STRING)
        ])
        result = validator.validate(contract, {"tags": []})
        assert result.is_valid is True

    def test_array_without_item_type_accepts_mixed(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("items", FieldType.ARRAY)])
        result = validator.validate(contract, {"items": [1, "two", 3.0, True]})
        assert result.is_valid is True

    def test_array_item_none_with_typed_items_is_type_mismatch(
        self, validator: ContractValidator
    ) -> None:
        """A None element in a typed array fails the item type check."""
        contract = ContractSpec(fields=[
            FieldSpec("tags", FieldType.ARRAY, item_type=FieldType.STRING)
        ])
        result = validator.validate(contract, {"tags": ["a", None, "c"]})
        assert result.is_valid is False
        assert result.violations[0].violation_type is ViolationType.TYPE_MISMATCH


class TestTypeMatchesDirect:
    """Direct unit tests for ContractValidator._type_matches (static method)."""

    def test_any_matches_everything(self) -> None:
        for value in [1, "x", 1.5, True, None, [], {}]:
            assert ContractValidator._type_matches(value, FieldType.ANY) is True

    def test_null_matches_none_only(self) -> None:
        assert ContractValidator._type_matches(None, FieldType.NULL) is True
        assert ContractValidator._type_matches("x", FieldType.NULL) is False

    def test_object_matches_dict_only(self) -> None:
        assert ContractValidator._type_matches({}, FieldType.OBJECT) is True
        assert ContractValidator._type_matches({"a": 1}, FieldType.OBJECT) is True
        assert ContractValidator._type_matches([], FieldType.OBJECT) is False
        assert ContractValidator._type_matches("x", FieldType.OBJECT) is False

    def test_array_matches_list_only(self) -> None:
        assert ContractValidator._type_matches([], FieldType.ARRAY) is True
        assert ContractValidator._type_matches([1, 2], FieldType.ARRAY) is True
        assert ContractValidator._type_matches({}, FieldType.ARRAY) is False
        assert ContractValidator._type_matches("x", FieldType.ARRAY) is False

    def test_none_does_not_match_non_null_non_any_types(self) -> None:
        for ft in [
            FieldType.STRING, FieldType.INTEGER, FieldType.FLOAT,
            FieldType.BOOLEAN, FieldType.OBJECT, FieldType.ARRAY,
        ]:
            assert ContractValidator._type_matches(None, ft) is False


# ===========================================================================
# Non-halting behaviour
# ===========================================================================


class TestNonHalting:

    def test_all_violation_types_collected_simultaneously(
        self, validator: ContractValidator
    ) -> None:
        """
        Construct a contract + data combination that triggers every
        ViolationType in a single call, and verify all are reported.
        """
        nested = ContractSpec(fields=[FieldSpec("code", FieldType.STRING)])
        contract = ContractSpec(fields=[
            FieldSpec("missing_field", FieldType.STRING),                     # MISSING
            FieldSpec("type_mismatch_field", FieldType.INTEGER),              # TYPE_MISMATCH
            FieldSpec(
                "constrained_field",
                FieldType.INTEGER,
                constraints=[FieldConstraint(FieldConstraintType.MINIMUM, 0)],
            ),                                                                # VALUE_CONSTRAINT
            FieldSpec(
                "not_null_field",
                FieldType.STRING,
                constraints=[FieldConstraint(FieldConstraintType.NOT_NULL, True)],
            ),                                                                # NULL_NOT_ALLOWED
            FieldSpec("nested_obj", FieldType.OBJECT, nested_spec=nested),    # STRUCTURAL_MISMATCH
        ])

        data = {
            # missing_field: absent
            "type_mismatch_field": "not an int",
            "constrained_field": -5,
            "not_null_field": None,
            "nested_obj": "not a dict",
            "unexpected_extra": "surprise",                                   # UNEXPECTED_FIELD
        }

        result = validator.validate(contract, data)
        assert result.is_valid is False

        types = _violation_types(result.violations)
        assert types == {
            ViolationType.MISSING_REQUIRED_FIELD,
            ViolationType.TYPE_MISMATCH,
            ViolationType.VALUE_CONSTRAINT_VIOLATION,
            ViolationType.NULL_NOT_ALLOWED,
            ViolationType.STRUCTURAL_MISMATCH,
            ViolationType.UNEXPECTED_FIELD,
        }

    def test_violation_count_matches_problems(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("a", FieldType.STRING),
            FieldSpec("b", FieldType.STRING),
            FieldSpec("c", FieldType.STRING),
        ])
        result = validator.validate(contract, {"d": "extra"})
        # a, b, c missing (3) + d unexpected (1) = 4
        assert len(result.violations) == 4


# ===========================================================================
# is_valid semantics
# ===========================================================================


class TestIsValidSemantics:

    def test_warning_only_is_still_valid(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[FieldSpec("a", FieldType.STRING)])
        result = validator.validate(contract, {"a": "x", "extra": 1})
        assert all(v.severity is ViolationSeverity.WARNING for v in result.violations)
        assert result.is_valid is True

    def test_single_error_makes_invalid_even_with_warnings(
        self, validator: ContractValidator
    ) -> None:
        contract = ContractSpec(fields=[
            FieldSpec("a", FieldType.STRING),
            FieldSpec("b", FieldType.INTEGER),
        ])
        result = validator.validate(contract, {"a": "x", "extra": 1})
        # 'b' missing => ERROR, 'extra' unexpected => WARNING
        assert result.is_valid is False
        severities = {v.severity for v in result.violations}
        assert ViolationSeverity.ERROR in severities
        assert ViolationSeverity.WARNING in severities

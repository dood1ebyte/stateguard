"""Tests for stateguard.core.models.field_types."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)


# ---------------------------------------------------------------------------
# FieldType
# ---------------------------------------------------------------------------


class TestFieldType:
    def test_all_expected_values_present(self) -> None:
        expected = {
            "string",
            "bytes",
            "integer",
            "float",
            "boolean",
            "object",
            "array",
            "any",
            "null",
            "union",
        }
        assert {ft.value for ft in FieldType} == expected

    def test_member_count(self) -> None:
        assert len(FieldType) == 10

    def test_string_equality_string(self) -> None:
        assert FieldType.STRING == "string"

    def test_string_equality_integer(self) -> None:
        assert FieldType.INTEGER == "integer"

    def test_string_equality_float(self) -> None:
        assert FieldType.FLOAT == "float"

    def test_string_equality_boolean(self) -> None:
        assert FieldType.BOOLEAN == "boolean"

    def test_string_equality_object(self) -> None:
        assert FieldType.OBJECT == "object"

    def test_string_equality_array(self) -> None:
        assert FieldType.ARRAY == "array"

    def test_string_equality_any(self) -> None:
        assert FieldType.ANY == "any"

    def test_string_equality_null(self) -> None:
        assert FieldType.NULL == "null"

    def test_access_by_name(self) -> None:
        assert FieldType["STRING"] is FieldType.STRING
        assert FieldType["INTEGER"] is FieldType.INTEGER
        assert FieldType["OBJECT"] is FieldType.OBJECT

    def test_access_by_value(self) -> None:
        assert FieldType("string") is FieldType.STRING
        assert FieldType("integer") is FieldType.INTEGER
        assert FieldType("null") is FieldType.NULL

    def test_identity_is_stable(self) -> None:
        assert FieldType.STRING is FieldType.STRING
        assert FieldType.FLOAT is FieldType.FLOAT

    def test_string_equality_union(self) -> None:
        assert FieldType.UNION == "union"

    def test_string_equality_bytes(self) -> None:
        assert FieldType.BYTES == "bytes"

    def test_is_iterable(self) -> None:
        members = list(FieldType)
        assert len(members) == 10

    def test_is_not_equal_to_other_member(self) -> None:
        assert FieldType.STRING != FieldType.INTEGER
        assert FieldType.OBJECT != FieldType.ARRAY

    def test_value_attribute_returns_underlying_string(self) -> None:
        # In Python 3.12+, str(StrEnum) returns 'ClassName.MEMBER', not the value.
        # Use .value to retrieve the underlying string.
        assert FieldType.STRING.value == "string"
        assert FieldType.INTEGER.value == "integer"

    def test_repr_is_non_empty(self) -> None:
        assert repr(FieldType.FLOAT)

    @pytest.mark.parametrize("member", list(FieldType))
    def test_every_member_round_trips_via_value(self, member: FieldType) -> None:
        assert FieldType(member.value) is member

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            FieldType("not_a_real_type")


# ---------------------------------------------------------------------------
# FieldConstraintType
# ---------------------------------------------------------------------------


class TestFieldConstraintType:
    def test_all_expected_values_present(self) -> None:
        expected = {
            "minimum",
            "maximum",
            "min_length",
            "max_length",
            "pattern",
            "enum_values",
            "not_null",
        }
        assert {fct.value for fct in FieldConstraintType} == expected

    def test_member_count(self) -> None:
        assert len(FieldConstraintType) == 7

    def test_string_equality_minimum(self) -> None:
        assert FieldConstraintType.MINIMUM == "minimum"

    def test_string_equality_maximum(self) -> None:
        assert FieldConstraintType.MAXIMUM == "maximum"

    def test_string_equality_min_length(self) -> None:
        assert FieldConstraintType.MIN_LENGTH == "min_length"

    def test_string_equality_max_length(self) -> None:
        assert FieldConstraintType.MAX_LENGTH == "max_length"

    def test_string_equality_pattern(self) -> None:
        assert FieldConstraintType.PATTERN == "pattern"

    def test_string_equality_enum_values(self) -> None:
        assert FieldConstraintType.ENUM_VALUES == "enum_values"

    def test_string_equality_not_null(self) -> None:
        assert FieldConstraintType.NOT_NULL == "not_null"

    @pytest.mark.parametrize("member", list(FieldConstraintType))
    def test_every_member_round_trips_via_value(self, member: FieldConstraintType) -> None:
        assert FieldConstraintType(member.value) is member

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            FieldConstraintType("nonexistent")


# ---------------------------------------------------------------------------
# FieldConstraint
# ---------------------------------------------------------------------------


class TestFieldConstraint:
    # --- Construction ---------------------------------------------------------

    def test_minimum_with_int(self) -> None:
        c = FieldConstraint(constraint_type=FieldConstraintType.MINIMUM, value=0)
        assert c.constraint_type is FieldConstraintType.MINIMUM
        assert c.value == 0

    def test_maximum_with_float(self) -> None:
        c = FieldConstraint(FieldConstraintType.MAXIMUM, 100.0)
        assert c.constraint_type is FieldConstraintType.MAXIMUM
        assert c.value == 100.0

    def test_min_length_with_int(self) -> None:
        c = FieldConstraint(FieldConstraintType.MIN_LENGTH, 1)
        assert c.value == 1

    def test_max_length_with_int(self) -> None:
        c = FieldConstraint(FieldConstraintType.MAX_LENGTH, 255)
        assert c.value == 255

    def test_pattern_with_string(self) -> None:
        pattern = r"^\d{4}-\d{2}-\d{2}$"
        c = FieldConstraint(FieldConstraintType.PATTERN, pattern)
        assert c.value == pattern

    def test_enum_values_with_tuple(self) -> None:
        choices = ("active", "inactive", "pending")
        c = FieldConstraint(FieldConstraintType.ENUM_VALUES, choices)
        assert c.value == choices

    def test_not_null_with_true(self) -> None:
        c = FieldConstraint(FieldConstraintType.NOT_NULL, True)
        assert c.constraint_type is FieldConstraintType.NOT_NULL
        assert c.value is True

    def test_zero_minimum_is_distinct_from_false(self) -> None:
        # Guards against accidental truthiness comparisons
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        assert c.value == 0
        assert c.value is not False

    # --- Immutability ---------------------------------------------------------

    def test_constraint_type_is_immutable(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        with pytest.raises(FrozenInstanceError):
            c.constraint_type = FieldConstraintType.MAXIMUM  # type: ignore[misc]

    def test_value_is_immutable(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        with pytest.raises(FrozenInstanceError):
            c.value = 99  # type: ignore[misc]

    # --- Equality -------------------------------------------------------------

    def test_equal_when_same_type_and_value(self) -> None:
        c1 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        c2 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        assert c1 == c2

    def test_not_equal_when_different_type(self) -> None:
        c1 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        c2 = FieldConstraint(FieldConstraintType.MAXIMUM, 0)
        assert c1 != c2

    def test_not_equal_when_different_value(self) -> None:
        c1 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        c2 = FieldConstraint(FieldConstraintType.MINIMUM, 1)
        assert c1 != c2

    def test_not_equal_to_none(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        assert c is not None

    # --- Hashability ----------------------------------------------------------

    def test_is_hashable(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        h = hash(c)
        assert isinstance(h, int)

    def test_hash_is_stable(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        assert hash(c) == hash(c)

    def test_equal_constraints_have_equal_hashes(self) -> None:
        c1 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        c2 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        assert hash(c1) == hash(c2)

    def test_can_be_stored_in_set(self) -> None:
        c1 = FieldConstraint(FieldConstraintType.MINIMUM, 0)
        c2 = FieldConstraint(FieldConstraintType.MINIMUM, 0)  # duplicate
        c3 = FieldConstraint(FieldConstraintType.MAXIMUM, 100)
        s = {c1, c2, c3}
        assert len(s) == 2

    def test_can_be_used_as_dict_key(self) -> None:
        c = FieldConstraint(FieldConstraintType.PATTERN, r"\d+")
        d = {c: "regex constraint"}
        assert d[c] == "regex constraint"

    # --- Repr -----------------------------------------------------------------

    def test_repr_contains_class_name(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 5)
        assert "FieldConstraint" in repr(c)

    def test_repr_contains_value(self) -> None:
        c = FieldConstraint(FieldConstraintType.MINIMUM, 42)
        assert "42" in repr(c)

    # --- Parametrised: every FieldConstraintType is constructable -------------

    @pytest.mark.parametrize(
        ("ctype", "value"),
        [
            (FieldConstraintType.MINIMUM, 0),
            (FieldConstraintType.MAXIMUM, 100),
            (FieldConstraintType.MIN_LENGTH, 1),
            (FieldConstraintType.MAX_LENGTH, 50),
            (FieldConstraintType.PATTERN, r"\w+"),
            (FieldConstraintType.ENUM_VALUES, ("a", "b")),
            (FieldConstraintType.NOT_NULL, True),
        ],
    )
    def test_every_constraint_type_is_constructable(
        self, ctype: FieldConstraintType, value: object
    ) -> None:
        c = FieldConstraint(constraint_type=ctype, value=value)
        assert c.constraint_type is ctype
        assert c.value == value

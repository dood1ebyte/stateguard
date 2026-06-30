"""Tests for stateguard.adapters.pydantic.violation_mapper."""

from __future__ import annotations

from typing import List

import pydantic
import pytest
from pydantic import BaseModel, Field

from stateguard.adapters.pydantic.extractor import PydanticContractExtractor
from stateguard.adapters.pydantic.violation_mapper import (
    PydanticViolationMapper,
    _classify,
    _loc_to_field_path,
)
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.models.field_types import FieldType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_and_map(model_class: type[BaseModel], data: dict) -> list:
    contract = PydanticContractExtractor.extract(model_class)
    try:
        model_class.model_validate(data)
    except pydantic.ValidationError as exc:
        return PydanticViolationMapper.map(exc, contract)
    raise AssertionError("model_validate did not raise ValidationError")


# ===========================================================================
# _loc_to_field_path
# ===========================================================================


class TestLocToFieldPath:
    def test_single_segment(self) -> None:
        assert _loc_to_field_path(("temperature",)) == "temperature"

    def test_nested_segments(self) -> None:
        assert _loc_to_field_path(("address", "zip_code")) == "address.zip_code"

    def test_list_index_segment(self) -> None:
        assert _loc_to_field_path(("tags", 1)) == "tags.1"

    def test_empty_loc(self) -> None:
        assert _loc_to_field_path(()) == ""

    def test_deeply_nested(self) -> None:
        assert _loc_to_field_path(("a", "b", "c")) == "a.b.c"


# ===========================================================================
# _classify
# ===========================================================================


class TestClassify:
    def test_missing(self) -> None:
        assert _classify("missing") is ViolationType.MISSING_REQUIRED_FIELD

    def test_extra_forbidden(self) -> None:
        assert _classify("extra_forbidden") is ViolationType.UNEXPECTED_FIELD

    def test_none_required(self) -> None:
        assert _classify("none_required") is ViolationType.NULL_NOT_ALLOWED

    @pytest.mark.parametrize(
        "error_type",
        [
            "string_type",
            "int_type",
            "float_type",
            "bool_type",
            "model_type",
            "list_type",
            "dict_type",
            "none_type",
        ],
    )
    def test_type_suffix(self, error_type: str) -> None:
        assert _classify(error_type) is ViolationType.TYPE_MISMATCH

    @pytest.mark.parametrize(
        "error_type",
        ["int_parsing", "float_parsing", "bool_parsing", "date_parsing"],
    )
    def test_parsing_suffix(self, error_type: str) -> None:
        assert _classify(error_type) is ViolationType.TYPE_MISMATCH

    @pytest.mark.parametrize(
        "error_type",
        [
            "greater_than",
            "greater_than_equal",
            "less_than",
            "less_than_equal",
            "string_pattern_mismatch",
            "literal_error",
            "value_error",
        ],
    )
    def test_constraint_exact(self, error_type: str) -> None:
        assert _classify(error_type) is ViolationType.VALUE_CONSTRAINT_VIOLATION

    @pytest.mark.parametrize(
        "error_type",
        ["string_too_short", "string_too_long", "too_short", "too_long", "list_too_long"],
    )
    def test_constraint_suffix(self, error_type: str) -> None:
        assert _classify(error_type) is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_unrecognized_falls_back_to_constraint_violation(self) -> None:
        assert _classify("totally_made_up_error") is ViolationType.VALUE_CONSTRAINT_VIOLATION

    def test_unrecognized_type_suffix_classified_as_type_mismatch(self) -> None:
        """Any unrecognized '*_type' error is treated as TYPE_MISMATCH,
        consistent with known Pydantic error types (string_type, int_type, ...)."""
        assert _classify("totally_made_up_type") is ViolationType.TYPE_MISMATCH


# ===========================================================================
# map — missing fields
# ===========================================================================


class TestMapMissing:
    def test_single_missing_field(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        violations = _raise_and_map(Weather, {"humidity": 80})
        assert len(violations) == 1
        v = violations[0]
        assert v.field_path == "temperature"
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.severity is ViolationSeverity.ERROR
        assert v.expected_type is FieldType.FLOAT

    def test_missing_received_value_is_none(self) -> None:
        class Weather(BaseModel):
            temperature: float

        violations = _raise_and_map(Weather, {})
        assert violations[0].received_value is None

    def test_multiple_missing_fields(self) -> None:
        class Multi(BaseModel):
            a: int
            b: str
            c: float

        violations = _raise_and_map(Multi, {"b": "hello"})
        paths = {v.field_path for v in violations}
        assert paths == {"a", "c"}
        for v in violations:
            assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD

    def test_missing_nested_field(self) -> None:
        class Address(BaseModel):
            city: str
            zip_code: str

        class User(BaseModel):
            name: str
            address: Address

        violations = _raise_and_map(User, {"name": "Alice", "address": {"city": "Mumbai"}})
        assert len(violations) == 1
        v = violations[0]
        assert v.field_path == "address.zip_code"
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.expected_type is FieldType.STRING


# ===========================================================================
# map — type mismatches
# ===========================================================================


class TestMapTypeMismatch:
    def test_string_type_error(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        violations = _raise_and_map(Weather, {"temperature": [1, 2], "humidity": 80})
        v = violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.field_path == "temperature"
        assert v.expected_type is FieldType.FLOAT
        assert v.received_value == [1, 2]

    def test_int_parsing_error(self) -> None:
        class Weather(BaseModel):
            humidity: int

        violations = _raise_and_map(Weather, {"humidity": "not_an_int"})
        v = violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.expected_type is FieldType.INTEGER
        assert v.received_value == "not_an_int"

    def test_float_parsing_error(self) -> None:
        class Weather(BaseModel):
            temperature: float

        violations = _raise_and_map(Weather, {"temperature": "hot"})
        v = violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.expected_type is FieldType.FLOAT

    def test_bool_parsing_error(self) -> None:
        class Flag(BaseModel):
            active: bool

        violations = _raise_and_map(Flag, {"active": "notabool"})
        v = violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.expected_type is FieldType.BOOLEAN

    def test_nested_model_type_error(self) -> None:
        class Address(BaseModel):
            city: str

        class User(BaseModel):
            name: str
            address: Address

        violations = _raise_and_map(User, {"name": "Alice", "address": "not a dict"})
        v = violations[0]
        assert v.field_path == "address"
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.expected_type is FieldType.OBJECT

    def test_list_item_type_error(self) -> None:
        class WithList(BaseModel):
            tags: List[int]

        violations = _raise_and_map(WithList, {"tags": [1, "two", 3]})
        v = violations[0]
        assert v.field_path == "tags.1"
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.received_value == "two"

    def test_none_for_non_optional_field_is_type_mismatch(self) -> None:
        class M(BaseModel):
            x: int

        violations = _raise_and_map(M, {"x": None})
        v = violations[0]
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.received_value is None


# ===========================================================================
# map — unexpected fields (extra_forbidden)
# ===========================================================================


class TestMapUnexpectedField:
    def test_extra_forbidden(self) -> None:
        class Strict(BaseModel):
            model_config = {"extra": "forbid"}
            x: int

        violations = _raise_and_map(Strict, {"x": 1, "y": 2})
        v = violations[0]
        assert v.field_path == "y"
        assert v.violation_type is ViolationType.UNEXPECTED_FIELD
        assert v.severity is ViolationSeverity.ERROR
        assert v.received_value == 2

    def test_extra_forbidden_expected_type_is_none(self) -> None:
        """An extra field has no corresponding FieldSpec -> expected_type=None."""

        class Strict(BaseModel):
            model_config = {"extra": "forbid"}
            x: int

        violations = _raise_and_map(Strict, {"x": 1, "y": 2})
        assert violations[0].expected_type is None


# ===========================================================================
# map — constraint violations
# ===========================================================================


class TestMapConstraintViolations:
    def test_greater_than_equal(self) -> None:
        class Bounded(BaseModel):
            value: int = Field(ge=0)

        violations = _raise_and_map(Bounded, {"value": -1})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 0
        assert v.received_value == -1
        assert v.expected_type is FieldType.INTEGER

    def test_less_than_equal(self) -> None:
        class Bounded(BaseModel):
            value: int = Field(le=100)

        violations = _raise_and_map(Bounded, {"value": 200})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 100

    def test_string_too_short(self) -> None:
        class M(BaseModel):
            code: str = Field(min_length=3)

        violations = _raise_and_map(M, {"code": "ab"})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 3
        assert v.received_value == "ab"

    def test_string_too_long(self) -> None:
        class M(BaseModel):
            code: str = Field(max_length=3)

        violations = _raise_and_map(M, {"code": "abcdef"})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 3

    def test_pattern_mismatch(self) -> None:
        class M(BaseModel):
            code: str = Field(pattern=r"^[A-Z]+$")

        violations = _raise_and_map(M, {"code": "abc"})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == r"^[A-Z]+$"
        assert v.received_value == "abc"

    def test_custom_value_error(self) -> None:
        class M(BaseModel):
            x: int

            @pydantic.field_validator("x")
            @classmethod
            def must_be_positive(cls, v: int) -> int:
                if v < 0:
                    raise ValueError("must be positive")
                return v

        violations = _raise_and_map(M, {"x": -5})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert "must be positive" in v.message

    def test_literal_error(self) -> None:
        from typing import Literal

        class M(BaseModel):
            status: Literal["active", "inactive"]

        violations = _raise_and_map(M, {"status": "archived"})
        v = violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.received_value == "archived"


# ===========================================================================
# map — multiple violations / ordering
# ===========================================================================


class TestMapMultiple:
    def test_multiple_errors_all_mapped(self) -> None:
        class Multi(BaseModel):
            a: int
            b: str
            c: float

        violations = _raise_and_map(Multi, {"b": 123})
        assert len(violations) == 3
        types = {v.violation_type for v in violations}
        assert ViolationType.MISSING_REQUIRED_FIELD in types
        assert ViolationType.TYPE_MISMATCH in types

    def test_all_violations_are_error_severity(self) -> None:
        class Multi(BaseModel):
            a: int
            b: str

        violations = _raise_and_map(Multi, {})
        assert all(v.severity is ViolationSeverity.ERROR for v in violations)

    def test_order_matches_pydantic_errors_order(self) -> None:
        class Multi(BaseModel):
            a: int
            b: str
            c: float

        violations = _raise_and_map(Multi, {"b": 123})
        # pydantic reports errors in field-declaration order: a (missing), b (type), c (missing)
        assert [v.field_path for v in violations] == ["a", "b", "c"]


# ===========================================================================
# map — message and rationale content
# ===========================================================================


class TestMapMessages:
    def test_message_is_non_empty(self) -> None:
        class M(BaseModel):
            x: int

        violations = _raise_and_map(M, {})
        assert violations[0].message
        assert isinstance(violations[0].message, str)

    def test_message_comes_from_pydantic(self) -> None:
        class M(BaseModel):
            x: int

        violations = _raise_and_map(M, {})
        assert violations[0].message == "Field required"


# ===========================================================================
# PydanticViolationMapper.map — top-level
# ===========================================================================


class TestMapTopLevel:
    def test_returns_list(self) -> None:
        class M(BaseModel):
            x: int

        contract = PydanticContractExtractor.extract(M)
        try:
            M.model_validate({})
        except pydantic.ValidationError as exc:
            violations = PydanticViolationMapper.map(exc, contract)
        assert isinstance(violations, list)
        assert all(hasattr(v, "violation_id") for v in violations)

    def test_violation_ids_are_unique(self) -> None:
        class Multi(BaseModel):
            a: int
            b: str

        violations = _raise_and_map(Multi, {})
        ids = {v.violation_id for v in violations}
        assert len(ids) == len(violations)

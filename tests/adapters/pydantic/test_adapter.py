"""Tests for stateguard.adapters.pydantic.adapter."""

from __future__ import annotations

from typing import List, Optional

import pytest
from pydantic import BaseModel, Field

from stateguard.adapters.pydantic.adapter import PydanticAdapter
from stateguard.core.errors.violations import ViolationSeverity, ViolationType
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def adapter() -> PydanticAdapter:
    return PydanticAdapter()


class Weather(BaseModel):
    temperature: float
    humidity: int


class Address(BaseModel):
    city: str
    zip_code: str


class User(BaseModel):
    name: str
    address: Address


class Bounded(BaseModel):
    value: int = Field(ge=0, le=100)


class WithDefaults(BaseModel):
    temperature: float
    humidity: int = 60


# ===========================================================================
# Conformance
# ===========================================================================


class TestConformance:
    def test_implements_icontractadapter(self, adapter: PydanticAdapter) -> None:
        assert isinstance(adapter, IContractAdapter)

    def test_with_defaults_returns_instance(self) -> None:
        instance = PydanticAdapter.with_defaults()
        assert isinstance(instance, PydanticAdapter)

    def test_with_defaults_is_independent_instance(self) -> None:
        a = PydanticAdapter.with_defaults()
        b = PydanticAdapter.with_defaults()
        assert a is not b


# ===========================================================================
# extract_contract
# ===========================================================================


class TestExtractContract:
    def test_returns_contract_spec(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        assert isinstance(contract, ContractSpec)

    def test_source_ref_is_model_class(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        assert contract.source_ref is Weather

    def test_field_paths(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        paths = {f.path for f in contract.fields}
        assert paths == {"temperature", "humidity"}

    def test_non_basemodel_class_raises_type_error(self, adapter: PydanticAdapter) -> None:
        with pytest.raises(TypeError, match="type\\[BaseModel\\]"):
            adapter.extract_contract(dict)

    def test_non_class_raises_type_error(self, adapter: PydanticAdapter) -> None:
        with pytest.raises(TypeError, match="type\\[BaseModel\\]"):
            adapter.extract_contract({"not": "a model"})

    def test_basemodel_instance_raises_type_error(self, adapter: PydanticAdapter) -> None:
        """An instance (not the class) is not a valid schema."""
        instance = Weather(temperature=1.0, humidity=2)
        with pytest.raises(TypeError, match="type\\[BaseModel\\]"):
            adapter.extract_contract(instance)

    def test_nested_model_extraction(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(User)
        address_field = next(f for f in contract.fields if f.path == "address")
        assert address_field.field_type is FieldType.OBJECT
        assert address_field.nested_spec is not None
        assert address_field.nested_spec.source_ref is Address


# ===========================================================================
# validate — success
# ===========================================================================


class TestValidateSuccess:
    def test_valid_data_returns_valid_result(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {"temperature": 31.5, "humidity": 80})
        assert result.is_valid is True
        assert result.violations == []

    def test_valid_result_contract_id(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {"temperature": 31.5, "humidity": 80})
        assert result.contract_id == contract.contract_id

    def test_valid_result_raw_input(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        data = {"temperature": 31.5, "humidity": 80}
        result = adapter.validate(contract, data)
        assert result.raw_input == data

    def test_int_for_float_field_is_valid(self, adapter: PydanticAdapter) -> None:
        """Pydantic accepts int where float is expected."""
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {"temperature": 30, "humidity": 80})
        assert result.is_valid is True

    def test_missing_optional_field_with_default_is_valid(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(WithDefaults)
        result = adapter.validate(contract, {"temperature": 31.5})
        assert result.is_valid is True

    def test_valid_nested_data(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(User)
        result = adapter.validate(
            contract,
            {"name": "Alice", "address": {"city": "Mumbai", "zip_code": "400001"}},
        )
        assert result.is_valid is True

    def test_extra_field_ignored_by_default(self, adapter: PydanticAdapter) -> None:
        """Pydantic's default extra='ignore' means extra keys don't fail validate()."""
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(
            contract, {"temperature": 31.5, "humidity": 80, "extra_field": "ignored"}
        )
        assert result.is_valid is True
        assert result.violations == []


# ===========================================================================
# validate — failure
# ===========================================================================


class TestValidateFailure:
    def test_missing_required_field(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {"humidity": 80})
        assert result.is_valid is False
        assert len(result.violations) == 1
        v = result.violations[0]
        assert v.field_path == "temperature"
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.severity is ViolationSeverity.ERROR

    def test_type_mismatch(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {"temperature": [1, 2], "humidity": 80})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.field_path == "temperature"
        assert v.violation_type is ViolationType.TYPE_MISMATCH
        assert v.expected_type is FieldType.FLOAT

    def test_multiple_violations(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {})
        assert result.is_valid is False
        assert len(result.violations) == 2
        paths = {v.field_path for v in result.violations}
        assert paths == {"temperature", "humidity"}

    def test_constraint_violation(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Bounded)
        result = adapter.validate(contract, {"value": 200})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.violation_type is ViolationType.VALUE_CONSTRAINT_VIOLATION
        assert v.expected_value == 100

    def test_nested_missing_field(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(User)
        result = adapter.validate(contract, {"name": "Alice", "address": {"city": "Mumbai"}})
        assert result.is_valid is False
        v = result.violations[0]
        assert v.field_path == "address.zip_code"
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD

    def test_all_violations_error_severity(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.validate(contract, {})
        assert all(v.severity is ViolationSeverity.ERROR for v in result.violations)

    def test_invalid_result_raw_input(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        data = {"humidity": 80}
        result = adapter.validate(contract, data)
        assert result.raw_input == data

    def test_does_not_mutate_input_data(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        data = {"temperature": "bad", "humidity": 80}
        adapter.validate(contract, data)
        assert data == {"temperature": "bad", "humidity": 80}


# ===========================================================================
# wrap — success
# ===========================================================================


class TestWrapSuccess:
    def test_returns_basemodel_instance(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.wrap(contract, {"temperature": 31.5, "humidity": 80})
        assert isinstance(result, Weather)

    def test_wrapped_values_correct(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        result = adapter.wrap(contract, {"temperature": 31.5, "humidity": 80})
        assert result.temperature == 31.5
        assert result.humidity == 80

    def test_wrap_nested_model(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(User)
        result = adapter.wrap(
            contract,
            {"name": "Alice", "address": {"city": "Mumbai", "zip_code": "400001"}},
        )
        assert isinstance(result, User)
        assert isinstance(result.address, Address)
        assert result.address.city == "Mumbai"

    def test_wrap_applies_defaults(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(WithDefaults)
        result = adapter.wrap(contract, {"temperature": 31.5})
        assert result.humidity == 60

    def test_wrap_with_int_for_float_field(self, adapter: PydanticAdapter) -> None:
        """Pydantic coerces int -> float during wrap."""
        contract = adapter.extract_contract(Weather)
        result = adapter.wrap(contract, {"temperature": 30, "humidity": 80})
        assert result.temperature == 30.0
        assert isinstance(result.temperature, float)


# ===========================================================================
# wrap — failure
# ===========================================================================


class TestWrapFailure:
    def test_invalid_data_raises_runtime_error(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        with pytest.raises(RuntimeError, match="failed to rehydrate"):
            adapter.wrap(contract, {"temperature": "not a number", "humidity": 80})

    def test_missing_field_raises_runtime_error(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        with pytest.raises(RuntimeError):
            adapter.wrap(contract, {"temperature": 31.5})

    def test_runtime_error_mentions_model_name(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        with pytest.raises(RuntimeError, match="Weather"):
            adapter.wrap(contract, {})


# ===========================================================================
# _model_class — source_ref validation
# ===========================================================================


class TestModelClassValidation:
    def test_validate_with_non_basemodel_source_ref_raises(self, adapter: PydanticAdapter) -> None:
        bad_contract = ContractSpec(
            fields=[FieldSpec("x", FieldType.STRING)], source_ref="not a model"
        )
        with pytest.raises(TypeError, match="source_ref"):
            adapter.validate(bad_contract, {"x": "y"})

    def test_wrap_with_non_basemodel_source_ref_raises(self, adapter: PydanticAdapter) -> None:
        bad_contract = ContractSpec(
            fields=[FieldSpec("x", FieldType.STRING)], source_ref="not a model"
        )
        with pytest.raises(TypeError, match="source_ref"):
            adapter.wrap(bad_contract, {"x": "y"})

    def test_validate_with_none_source_ref_raises(self, adapter: PydanticAdapter) -> None:
        bad_contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)], source_ref=None)
        with pytest.raises(TypeError, match="source_ref"):
            adapter.validate(bad_contract, {"x": "y"})

    def test_wrap_with_dict_source_ref_raises(self, adapter: PydanticAdapter) -> None:
        bad_contract = ContractSpec(fields=[FieldSpec("x", FieldType.STRING)], source_ref=dict)
        with pytest.raises(TypeError, match="source_ref"):
            adapter.wrap(bad_contract, {"x": "y"})


# ===========================================================================
# Round-trip
# ===========================================================================


class TestRoundTrip:
    def test_extract_validate_wrap_round_trip(self, adapter: PydanticAdapter) -> None:
        contract = adapter.extract_contract(Weather)
        data = {"temperature": 31.5, "humidity": 80}

        validation = adapter.validate(contract, data)
        assert validation.is_valid is True

        model = adapter.wrap(contract, data)
        assert model.model_dump() == data

    def test_round_trip_with_nested_and_defaults(self, adapter: PydanticAdapter) -> None:
        class Profile(BaseModel):
            bio: Optional[str] = None
            tags: List[str] = Field(default_factory=list)

        class FullUser(BaseModel):
            name: str
            profile: Profile

        contract = adapter.extract_contract(FullUser)
        data = {"name": "Alice", "profile": {}}

        validation = adapter.validate(contract, data)
        assert validation.is_valid is True

        model = adapter.wrap(contract, data)
        assert model.name == "Alice"
        assert model.profile.bio is None
        assert model.profile.tags == []

    def test_round_trip_aliased_model(self, adapter: PydanticAdapter) -> None:
        class WeatherAliased(BaseModel):
            temperature: float = Field(alias="temp_c")
            humidity: int

        contract = adapter.extract_contract(WeatherAliased)
        data = {"temp_c": 31.5, "humidity": 80}

        validation = adapter.validate(contract, data)
        assert validation.is_valid is True

        model = adapter.wrap(contract, data)
        assert model.temperature == 31.5

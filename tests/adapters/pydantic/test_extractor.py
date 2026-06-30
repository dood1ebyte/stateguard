"""Tests for stateguard.adapters.pydantic.extractor."""

from __future__ import annotations

from typing import List, Literal, Optional

import pytest
from pydantic import BaseModel, Field

from stateguard.adapters.pydantic.extractor import PydanticContractExtractor
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldConstraintType, FieldType


def _field(spec: ContractSpec, path: str) -> FieldSpec:
    for f in spec.fields:
        if f.path == path:
            return f
    raise AssertionError(f"No field with path '{path}' in {[f.path for f in spec.fields]}")


# ===========================================================================
# Basic extraction
# ===========================================================================


class TestBasicExtraction:

    def test_simple_model_field_count(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        spec = PydanticContractExtractor.extract(Weather)
        assert len(spec.fields) == 2

    def test_field_paths_match_attribute_names(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        spec = PydanticContractExtractor.extract(Weather)
        paths = {f.path for f in spec.fields}
        assert paths == {"temperature", "humidity"}

    def test_field_types_mapped_correctly(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        spec = PydanticContractExtractor.extract(Weather)
        assert _field(spec, "temperature").field_type is FieldType.FLOAT
        assert _field(spec, "humidity").field_type is FieldType.INTEGER

    def test_source_ref_is_model_class(self) -> None:
        class Weather(BaseModel):
            temperature: float

        spec = PydanticContractExtractor.extract(Weather)
        assert spec.source_ref is Weather

    def test_contract_id_generated(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        spec = PydanticContractExtractor.extract(Weather)
        assert len(spec.contract_id) == 16

    def test_strict_mode_defaults_false(self) -> None:
        class Weather(BaseModel):
            temperature: float

        spec = PydanticContractExtractor.extract(Weather)
        assert spec.strict_mode is False


# ===========================================================================
# Required vs optional
# ===========================================================================


class TestRequiredFields:

    def test_field_without_default_is_required(self) -> None:
        class Weather(BaseModel):
            temperature: float

        spec = PydanticContractExtractor.extract(Weather)
        assert _field(spec, "temperature").required is True

    def test_field_with_default_is_not_required(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int = 60

        spec = PydanticContractExtractor.extract(Weather)
        assert _field(spec, "humidity").required is False

    def test_optional_field_with_none_default_not_required(self) -> None:
        class Weather(BaseModel):
            description: Optional[str] = None

        spec = PydanticContractExtractor.extract(Weather)
        f = _field(spec, "description")
        assert f.required is False
        assert f.field_type is FieldType.STRING


# ===========================================================================
# Defaults
# ===========================================================================


class TestDefaults:

    def test_no_default_is_missing_sentinel(self) -> None:
        class Weather(BaseModel):
            temperature: float

        spec = PydanticContractExtractor.extract(Weather)
        assert _field(spec, "temperature").default is MISSING

    def test_int_default(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int = 60

        spec = PydanticContractExtractor.extract(Weather)
        assert _field(spec, "humidity").default == 60

    def test_explicit_none_default_is_not_missing(self) -> None:
        class Weather(BaseModel):
            description: Optional[str] = None

        spec = PydanticContractExtractor.extract(Weather)
        f = _field(spec, "description")
        assert f.default is None
        assert f.default is not MISSING

    def test_string_default(self) -> None:
        class M(BaseModel):
            status: str = "unknown"

        spec = PydanticContractExtractor.extract(M)
        assert _field(spec, "status").default == "unknown"

    def test_bool_default(self) -> None:
        class M(BaseModel):
            active: bool = False

        spec = PydanticContractExtractor.extract(M)
        assert _field(spec, "active").default is False

    def test_default_factory_list(self) -> None:
        class M(BaseModel):
            tags: List[str] = Field(default_factory=list)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "tags")
        assert f.default == []
        assert f.required is False

    def test_default_factory_called_once_per_extraction(self) -> None:
        """Each extraction call invokes the factory independently."""
        class M(BaseModel):
            tags: List[str] = Field(default_factory=list)

        spec1 = PydanticContractExtractor.extract(M)
        spec2 = PydanticContractExtractor.extract(M)
        d1 = _field(spec1, "tags").default
        d2 = _field(spec2, "tags").default
        assert d1 == [] and d2 == []
        assert d1 is not d2  # distinct list instances


# ===========================================================================
# Alias resolution
# ===========================================================================


class TestAliasResolution:

    def test_field_with_alias_path_is_alias(self) -> None:
        class WeatherAliased(BaseModel):
            temperature: float = Field(alias="temp_c")
            humidity: int

        spec = PydanticContractExtractor.extract(WeatherAliased)
        paths = {f.path for f in spec.fields}
        assert "temp_c" in paths
        assert "temperature" not in paths

    def test_field_with_alias_known_aliases_contains_attribute_name(self) -> None:
        class WeatherAliased(BaseModel):
            temperature: float = Field(alias="temp_c")
            humidity: int

        spec = PydanticContractExtractor.extract(WeatherAliased)
        f = _field(spec, "temp_c")
        assert f.known_aliases == ["temperature"]

    def test_field_without_alias_has_no_known_aliases(self) -> None:
        class WeatherAliased(BaseModel):
            temperature: float = Field(alias="temp_c")
            humidity: int

        spec = PydanticContractExtractor.extract(WeatherAliased)
        f = _field(spec, "humidity")
        assert f.known_aliases == []
        assert f.path == "humidity"

    def test_validation_alias_different_from_alias(self) -> None:
        class M(BaseModel):
            humidity: int = Field(validation_alias="rh")

        spec = PydanticContractExtractor.extract(M)
        paths = {f.path for f in spec.fields}
        assert "rh" in paths
        f = _field(spec, "rh")
        assert f.known_aliases == ["humidity"]

    def test_validation_alias_equal_to_field_name(self) -> None:
        """If validation_alias == field_name, no alias treatment is needed."""
        class M(BaseModel):
            x: int = Field(validation_alias="x")

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "x")
        assert f.known_aliases == []


# ===========================================================================
# Constraints
# ===========================================================================


class TestConstraints:

    def test_ge_constraint(self) -> None:
        class M(BaseModel):
            value: int = Field(ge=0)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "value")
        types = {c.constraint_type for c in f.constraints}
        assert FieldConstraintType.MINIMUM in types
        minimum = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.MINIMUM)
        assert minimum.value == 0

    def test_le_constraint(self) -> None:
        class M(BaseModel):
            value: int = Field(le=100)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "value")
        maximum = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.MAXIMUM)
        assert maximum.value == 100

    def test_ge_and_le_together(self) -> None:
        class M(BaseModel):
            value: int = Field(ge=0, le=100)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "value")
        constraint_types = {c.constraint_type for c in f.constraints}
        assert constraint_types == {FieldConstraintType.MINIMUM, FieldConstraintType.MAXIMUM}

    def test_min_length_constraint(self) -> None:
        class M(BaseModel):
            code: str = Field(min_length=2)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "code")
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.MIN_LENGTH)
        assert c.value == 2

    def test_max_length_constraint(self) -> None:
        class M(BaseModel):
            code: str = Field(max_length=10)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "code")
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.MAX_LENGTH)
        assert c.value == 10

    def test_pattern_constraint(self) -> None:
        class M(BaseModel):
            code: str = Field(pattern=r"^[A-Z]+$")

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "code")
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.PATTERN)
        assert c.value == r"^[A-Z]+$"

    def test_min_max_length_and_pattern_together(self) -> None:
        class M(BaseModel):
            code: str = Field(min_length=2, max_length=10, pattern=r"^[A-Z]+$")

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "code")
        constraint_types = {c.constraint_type for c in f.constraints}
        assert constraint_types == {
            FieldConstraintType.MIN_LENGTH,
            FieldConstraintType.MAX_LENGTH,
            FieldConstraintType.PATTERN,
        }

    def test_field_without_constraints_has_empty_list(self) -> None:
        class M(BaseModel):
            x: int

        spec = PydanticContractExtractor.extract(M)
        assert _field(spec, "x").constraints == []

    def test_gt_lt_not_extracted(self) -> None:
        """Gt/Lt have no corresponding FieldConstraintType in V1."""
        class M(BaseModel):
            value: int = Field(gt=0, lt=10)

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "value")
        constraint_types = {c.constraint_type for c in f.constraints}
        assert FieldConstraintType.MINIMUM not in constraint_types
        assert FieldConstraintType.MAXIMUM not in constraint_types


# ===========================================================================
# Literal -> ENUM_VALUES
# ===========================================================================


class TestLiteralConstraints:

    def test_string_literal_produces_enum_values(self) -> None:
        class M(BaseModel):
            status: Literal["active", "inactive", "pending"]

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "status")
        assert f.field_type is FieldType.STRING
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.ENUM_VALUES)
        assert c.value == ("active", "inactive", "pending")

    def test_int_literal_produces_enum_values(self) -> None:
        class M(BaseModel):
            level: Literal[1, 2, 3]

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "level")
        assert f.field_type is FieldType.INTEGER
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.ENUM_VALUES)
        assert c.value == (1, 2, 3)

    def test_optional_literal_with_default(self) -> None:
        class M(BaseModel):
            status: Literal["active", "inactive"] = "active"

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "status")
        assert f.required is False
        assert f.default == "active"
        c = next(c for c in f.constraints if c.constraint_type is FieldConstraintType.ENUM_VALUES)
        assert c.value == ("active", "inactive")


# ===========================================================================
# Nested models
# ===========================================================================


class TestNestedModels:

    def test_nested_model_produces_object_field(self) -> None:
        class Address(BaseModel):
            city: str
            zip_code: str

        class User(BaseModel):
            name: str
            address: Address

        spec = PydanticContractExtractor.extract(User)
        f = _field(spec, "address")
        assert f.field_type is FieldType.OBJECT
        assert f.nested_spec is not None

    def test_nested_spec_has_correct_fields(self) -> None:
        class Address(BaseModel):
            city: str
            zip_code: str

        class User(BaseModel):
            name: str
            address: Address

        spec = PydanticContractExtractor.extract(User)
        nested = _field(spec, "address").nested_spec
        assert nested is not None
        paths = {f.path for f in nested.fields}
        assert paths == {"city", "zip_code"}

    def test_nested_spec_source_ref_is_nested_model(self) -> None:
        class Address(BaseModel):
            city: str

        class User(BaseModel):
            name: str
            address: Address

        spec = PydanticContractExtractor.extract(User)
        nested = _field(spec, "address").nested_spec
        assert nested is not None
        assert nested.source_ref is Address

    def test_two_level_nesting(self) -> None:
        class Country(BaseModel):
            code: str

        class Address(BaseModel):
            city: str
            country: Country

        class User(BaseModel):
            name: str
            address: Address

        spec = PydanticContractExtractor.extract(User)
        address_spec = _field(spec, "address").nested_spec
        assert address_spec is not None
        country_spec = _field(address_spec, "country").nested_spec
        assert country_spec is not None
        assert {f.path for f in country_spec.fields} == {"code"}

    def test_optional_nested_model(self) -> None:
        class Address(BaseModel):
            city: str

        class User(BaseModel):
            name: str
            address: Optional[Address] = None

        spec = PydanticContractExtractor.extract(User)
        f = _field(spec, "address")
        assert f.field_type is FieldType.OBJECT
        assert f.required is False
        assert f.nested_spec is not None

    def test_non_object_field_has_no_nested_spec(self) -> None:
        class M(BaseModel):
            x: int

        spec = PydanticContractExtractor.extract(M)
        assert _field(spec, "x").nested_spec is None


# ===========================================================================
# Arrays
# ===========================================================================


class TestArrays:

    def test_list_of_str_item_type(self) -> None:
        class M(BaseModel):
            tags: List[str] = []

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "tags")
        assert f.field_type is FieldType.ARRAY
        assert f.item_type is FieldType.STRING

    def test_list_of_int_item_type(self) -> None:
        class M(BaseModel):
            scores: List[int]

        spec = PydanticContractExtractor.extract(M)
        f = _field(spec, "scores")
        assert f.item_type is FieldType.INTEGER

    def test_list_of_basemodel_no_nested_spec(self) -> None:
        """Per V1 scope: List[Model] does not get a nested_spec."""
        class Item(BaseModel):
            name: str

        class Order(BaseModel):
            items: List[Item]

        spec = PydanticContractExtractor.extract(Order)
        f = _field(spec, "items")
        assert f.field_type is FieldType.ARRAY
        assert f.item_type is FieldType.OBJECT
        assert f.nested_spec is None

    def test_non_array_field_has_no_item_type(self) -> None:
        class M(BaseModel):
            x: int

        spec = PydanticContractExtractor.extract(M)
        assert _field(spec, "x").item_type is None


# ===========================================================================
# Complex / combined model
# ===========================================================================


class TestComplexModel:

    def test_full_weather_model(self) -> None:
        class Address(BaseModel):
            city: str
            zip_code: str

        class Weather(BaseModel):
            temperature: float
            humidity: int = 60
            description: Optional[str] = None
            tags: List[str] = []
            address: Address
            status: Literal["active", "inactive"] = "active"
            score: int = Field(ge=0, le=100)
            code: str = Field(min_length=2, max_length=10, pattern=r"^[A-Z]+$")

        spec = PydanticContractExtractor.extract(Weather)
        assert len(spec.fields) == 8

        assert _field(spec, "temperature").required is True
        assert _field(spec, "humidity").required is False
        assert _field(spec, "humidity").default == 60
        assert _field(spec, "description").default is None
        assert _field(spec, "tags").item_type is FieldType.STRING
        assert _field(spec, "address").nested_spec is not None
        assert _field(spec, "status").default == "active"
        assert _field(spec, "score").required is True

        score_constraints = {c.constraint_type for c in _field(spec, "score").constraints}
        assert score_constraints == {FieldConstraintType.MINIMUM, FieldConstraintType.MAXIMUM}

        code_constraints = {c.constraint_type for c in _field(spec, "code").constraints}
        assert code_constraints == {
            FieldConstraintType.MIN_LENGTH,
            FieldConstraintType.MAX_LENGTH,
            FieldConstraintType.PATTERN,
        }


# ===========================================================================
# Determinism
# ===========================================================================


class TestDeterminism:

    def test_same_model_same_contract_id(self) -> None:
        class Weather(BaseModel):
            temperature: float
            humidity: int

        spec1 = PydanticContractExtractor.extract(Weather)
        spec2 = PydanticContractExtractor.extract(Weather)
        assert spec1.contract_id == spec2.contract_id

    def test_field_order_independent_contract_id(self) -> None:
        class A(BaseModel):
            x: int
            y: str

        class B(BaseModel):
            y: str
            x: int

        spec_a = PydanticContractExtractor.extract(A)
        spec_b = PydanticContractExtractor.extract(B)
        assert spec_a.contract_id == spec_b.contract_id

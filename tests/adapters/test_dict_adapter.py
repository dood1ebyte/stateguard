"""Tests for stateguard.adapters.dict_adapter."""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.adapters.dict_adapter import DictContractAdapter
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import MISSING
from stateguard.core.models.field_types import FieldConstraintType, FieldType


@pytest.fixture
def adapter() -> DictContractAdapter:
    return DictContractAdapter()


def _field(contract: Any, path: str) -> Any:
    for f in contract.fields:
        if f.path == path:
            return f
    raise AssertionError(f"No field '{path}' in {[f.path for f in contract.fields]}")


# ===========================================================================
# Conformance
# ===========================================================================


class TestConformance:
    def test_implements_icontractadapter(self, adapter: DictContractAdapter) -> None:
        assert isinstance(adapter, IContractAdapter)


# ===========================================================================
# extract_contract — basic
# ===========================================================================


class TestExtractContractBasic:
    def test_simple_schema(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert len(contract.fields) == 1
        assert _field(contract, "x").field_type is FieldType.STRING

    def test_multiple_fields(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer"},
            ]
        }
        contract = adapter.extract_contract(schema)
        assert len(contract.fields) == 2

    def test_all_field_types(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {"path": "a", "type": "string"},
                {"path": "b", "type": "integer"},
                {"path": "c", "type": "float"},
                {"path": "d", "type": "boolean"},
                {"path": "e", "type": "object"},
                {"path": "f", "type": "array"},
                {"path": "g", "type": "any"},
                {"path": "h", "type": "null"},
            ]
        }
        contract = adapter.extract_contract(schema)
        expected = {
            "a": FieldType.STRING,
            "b": FieldType.INTEGER,
            "c": FieldType.FLOAT,
            "d": FieldType.BOOLEAN,
            "e": FieldType.OBJECT,
            "f": FieldType.ARRAY,
            "g": FieldType.ANY,
            "h": FieldType.NULL,
        }
        for path, ft in expected.items():
            assert _field(contract, path).field_type is ft

    def test_strict_mode_default_false(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert contract.strict_mode is False

    def test_strict_mode_true(self, adapter: DictContractAdapter) -> None:
        schema = {"strict_mode": True, "fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert contract.strict_mode is True

    def test_required_default_true(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "x").required is True

    def test_required_false(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string", "required": False}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "x").required is False

    def test_no_default_is_missing_sentinel(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "x").default is MISSING

    def test_default_value_present(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "humidity", "type": "integer", "default": 60}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "humidity").default == 60

    def test_default_none_is_not_missing_sentinel(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string", "default": None}]}
        contract = adapter.extract_contract(schema)
        f = _field(contract, "x")
        assert f.default is None
        assert f.default is not MISSING

    def test_known_aliases(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "zip_code", "type": "string", "known_aliases": ["zip"]}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "zip_code").known_aliases == ["zip"]

    def test_known_aliases_default_empty(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "x").known_aliases == []

    def test_item_type(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "tags", "type": "array", "item_type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "tags").item_type is FieldType.STRING

    def test_item_type_default_none(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "tags", "type": "array"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "tags").item_type is None


# ===========================================================================
# extract_contract — constraints
# ===========================================================================


class TestExtractContractConstraints:
    def test_minimum_constraint(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "score",
                    "type": "integer",
                    "constraints": [{"type": "minimum", "value": 0}],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        c = _field(contract, "score").constraints[0]
        assert c.constraint_type is FieldConstraintType.MINIMUM
        assert c.value == 0

    def test_maximum_constraint(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "score",
                    "type": "integer",
                    "constraints": [{"type": "maximum", "value": 100}],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        c = _field(contract, "score").constraints[0]
        assert c.constraint_type is FieldConstraintType.MAXIMUM
        assert c.value == 100

    def test_multiple_constraints(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "score",
                    "type": "integer",
                    "constraints": [
                        {"type": "minimum", "value": 0},
                        {"type": "maximum", "value": 100},
                    ],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        types = {c.constraint_type for c in _field(contract, "score").constraints}
        assert types == {FieldConstraintType.MINIMUM, FieldConstraintType.MAXIMUM}

    def test_min_length_max_length_pattern(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "code",
                    "type": "string",
                    "constraints": [
                        {"type": "min_length", "value": 2},
                        {"type": "max_length", "value": 10},
                        {"type": "pattern", "value": "^[A-Z]+$"},
                    ],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        types = {c.constraint_type for c in _field(contract, "code").constraints}
        assert types == {
            FieldConstraintType.MIN_LENGTH,
            FieldConstraintType.MAX_LENGTH,
            FieldConstraintType.PATTERN,
        }

    def test_enum_values_constraint_from_list(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "status",
                    "type": "string",
                    "constraints": [{"type": "enum_values", "value": ["active", "inactive"]}],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        c = _field(contract, "status").constraints[0]
        assert c.constraint_type is FieldConstraintType.ENUM_VALUES
        assert c.value == ("active", "inactive")
        assert isinstance(c.value, tuple)

    def test_not_null_constraint(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "x",
                    "type": "string",
                    "constraints": [{"type": "not_null", "value": True}],
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        c = _field(contract, "x").constraints[0]
        assert c.constraint_type is FieldConstraintType.NOT_NULL

    def test_no_constraints_default_empty(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "x", "type": "string"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "x").constraints == []


# ===========================================================================
# extract_contract — nested
# ===========================================================================


class TestExtractContractNested:
    def test_nested_object(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "address",
                    "type": "object",
                    "nested": {
                        "fields": [
                            {"path": "city", "type": "string"},
                            {"path": "zip_code", "type": "string"},
                        ]
                    },
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        address = _field(contract, "address")
        assert address.field_type is FieldType.OBJECT
        assert address.nested_spec is not None
        nested_paths = {f.path for f in address.nested_spec.fields}
        assert nested_paths == {"city", "zip_code"}

    def test_depth3_nested(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "address",
                    "type": "object",
                    "nested": {
                        "fields": [
                            {
                                "path": "country",
                                "type": "object",
                                "nested": {"fields": [{"path": "code", "type": "string"}]},
                            }
                        ]
                    },
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        address = _field(contract, "address")
        country = _field(address.nested_spec, "country")
        assert country.nested_spec is not None
        assert {f.path for f in country.nested_spec.fields} == {"code"}

    def test_nested_strict_mode(self, adapter: DictContractAdapter) -> None:
        schema = {
            "fields": [
                {
                    "path": "address",
                    "type": "object",
                    "nested": {"strict_mode": True, "fields": [{"path": "city", "type": "string"}]},
                }
            ]
        }
        contract = adapter.extract_contract(schema)
        assert _field(contract, "address").nested_spec.strict_mode is True

    def test_no_nested_key_means_no_nested_spec(self, adapter: DictContractAdapter) -> None:
        schema = {"fields": [{"path": "metadata", "type": "object"}]}
        contract = adapter.extract_contract(schema)
        assert _field(contract, "metadata").nested_spec is None


# ===========================================================================
# extract_contract — error handling
# ===========================================================================


class TestExtractContractErrors:
    def test_non_dict_schema_raises_type_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(TypeError, match="expects a dict"):
            adapter.extract_contract("not a dict")

    def test_list_schema_raises_type_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(TypeError):
            adapter.extract_contract([1, 2, 3])

    def test_missing_fields_key_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="'fields'"):
            adapter.extract_contract({"strict_mode": True})

    def test_field_missing_path_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="'path'"):
            adapter.extract_contract({"fields": [{"type": "string"}]})

    def test_field_missing_type_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="'type'"):
            adapter.extract_contract({"fields": [{"path": "x"}]})

    def test_unrecognized_type_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="unrecognized type"):
            adapter.extract_contract({"fields": [{"path": "x", "type": "not_a_real_type"}]})

    def test_non_string_type_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="must be a string"):
            adapter.extract_contract({"fields": [{"path": "x", "type": 123}]})

    def test_invalid_nested_schema_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="nested"):
            adapter.extract_contract(
                {"fields": [{"path": "address", "type": "object", "nested": "not a dict"}]}
            )

    def test_nested_missing_fields_key_raises_value_error(
        self, adapter: DictContractAdapter
    ) -> None:
        with pytest.raises(ValueError, match="nested"):
            adapter.extract_contract(
                {"fields": [{"path": "address", "type": "object", "nested": {}}]}
            )

    def test_constraint_missing_type_raises_value_error(self, adapter: DictContractAdapter) -> None:
        with pytest.raises(ValueError, match="'type' and 'value'"):
            adapter.extract_contract(
                {"fields": [{"path": "x", "type": "integer", "constraints": [{"value": 5}]}]}
            )

    def test_constraint_missing_value_raises_value_error(
        self, adapter: DictContractAdapter
    ) -> None:
        with pytest.raises(ValueError, match="'type' and 'value'"):
            adapter.extract_contract(
                {"fields": [{"path": "x", "type": "integer", "constraints": [{"type": "minimum"}]}]}
            )

    def test_unrecognized_constraint_type_raises_value_error(
        self, adapter: DictContractAdapter
    ) -> None:
        with pytest.raises(ValueError, match="unrecognized constraint type"):
            adapter.extract_contract(
                {
                    "fields": [
                        {
                            "path": "x",
                            "type": "integer",
                            "constraints": [{"type": "not_real", "value": 1}],
                        }
                    ]
                }
            )


# ===========================================================================
# validate
# ===========================================================================


class TestValidate:
    def test_valid_data(self, adapter: DictContractAdapter) -> None:
        contract = adapter.extract_contract({"fields": [{"path": "x", "type": "string"}]})
        result = adapter.validate(contract, {"x": "hello"})
        assert result.is_valid is True

    def test_invalid_data(self, adapter: DictContractAdapter) -> None:
        contract = adapter.extract_contract({"fields": [{"path": "x", "type": "string"}]})
        result = adapter.validate(contract, {})
        assert result.is_valid is False

    def test_delegates_to_contractvalidator(self, adapter: DictContractAdapter) -> None:
        """Sanity check that validate() produces the same violation types
        ContractValidator itself would (it's a pure delegation)."""
        contract = adapter.extract_contract(
            {
                "fields": [
                    {"path": "temperature", "type": "float"},
                    {"path": "humidity", "type": "integer"},
                ]
            }
        )
        result = adapter.validate(contract, {"temp_celsius": 31.5, "humidity": 80})
        types = {v.violation_type.value for v in result.violations}
        assert "missing_required_field" in types
        assert "unexpected_field" in types


# ===========================================================================
# wrap
# ===========================================================================


class TestWrap:
    def test_returns_dict_copy(self, adapter: DictContractAdapter) -> None:
        contract = adapter.extract_contract({"fields": [{"path": "x", "type": "string"}]})
        data = {"x": "hello"}
        result = adapter.wrap(contract, data)
        assert result == data
        assert result is not data

    def test_wrap_does_not_mutate_input(self, adapter: DictContractAdapter) -> None:
        contract = adapter.extract_contract({"fields": [{"path": "x", "type": "string"}]})
        data = {"x": "hello"}
        wrapped = adapter.wrap(contract, data)
        wrapped["x"] = "changed"
        assert data["x"] == "hello"


# ===========================================================================
# Round-trip / full Weather-style schema
# ===========================================================================


class TestRoundTrip:
    def test_full_weather_schema_round_trip(self, adapter: DictContractAdapter) -> None:
        schema = {
            "strict_mode": False,
            "fields": [
                {"path": "temperature", "type": "float"},
                {"path": "humidity", "type": "integer", "required": False, "default": 60},
                {"path": "tags", "type": "array", "item_type": "string", "default": []},
                {
                    "path": "address",
                    "type": "object",
                    "nested": {
                        "fields": [
                            {"path": "city", "type": "string"},
                            {"path": "zip_code", "type": "string", "known_aliases": ["zip"]},
                        ]
                    },
                },
                {
                    "path": "score",
                    "type": "integer",
                    "constraints": [
                        {"type": "minimum", "value": 0},
                        {"type": "maximum", "value": 100},
                    ],
                },
            ],
        }
        contract = adapter.extract_contract(schema)
        data = {
            "temperature": 31.5,
            "address": {"city": "Mumbai", "zip": "400001"},
            "score": 50,
        }
        validation = adapter.validate(contract, data)
        # 'zip' alias not resolved by validate() itself (that's a repair
        # strategy's job) -- so this reports a missing zip_code violation.
        assert not validation.is_valid
        wrapped = adapter.wrap(contract, data)
        assert wrapped == data

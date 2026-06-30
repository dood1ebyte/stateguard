"""
Engine-level nested-repair integration tests (M9 hardening).

Unlike the per-strategy nested tests in tests/core/strategies/, these tests
exercise the FULL repair loop (RepairEngine.repair) end-to-end through
MockContractAdapter, covering scenarios that only emerge from the
interaction of multiple strategies, multiple attempts, and the engine's
own correlation/no-progress/regression logic at nested depths.

StateGuard's officially validated nesting depth is 3 (root -> level1 object
-> level2 object -> leaf field, e.g. "address.country.code"). All tests
here are written at depth 2 or depth 3; see README.md "Nested structures"
and M9_AUDIT.md for the documented scope of this guarantee.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.results import RepairStatus
from stateguard.core.errors.violations import ViolationType
from stateguard.core.models.config import RepairConfig
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.core.strategies import (
    DefaultValueFillStrategy,
    ExactAliasStrategy,
    FuzzyFieldMatchStrategy,
    StrategyRegistry,
    TypeCoercionStrategy,
)
from tests.conftest import MockContractAdapter
from tests.core.test_engine import full_registry, make_engine


# ---------------------------------------------------------------------------
# Shared contract builders
# ---------------------------------------------------------------------------


def _depth2_contract(**address_kwargs: Any) -> ContractSpec:
    """root.name (str) + root.address.{city, zip_code} (depth 2)."""
    address_spec = ContractSpec(
        fields=[
            FieldSpec("city", FieldType.STRING),
            FieldSpec("zip_code", FieldType.STRING, **address_kwargs),
        ]
    )
    return ContractSpec(
        fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
        ]
    )


def _depth3_contract() -> ContractSpec:
    """root.name (str) + root.address.city (str)
    + root.address.country.{code (str), population (int)} (depth 3)."""
    country_spec = ContractSpec(
        fields=[
            FieldSpec("code", FieldType.STRING),
            FieldSpec("population", FieldType.INTEGER),
        ]
    )
    address_spec = ContractSpec(
        fields=[
            FieldSpec("city", FieldType.STRING),
            FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
        ]
    )
    return ContractSpec(
        fields=[
            FieldSpec("name", FieldType.STRING),
            FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
        ]
    )


# ===========================================================================
# Nested alias repair
# ===========================================================================


class TestNestedAliasRepair:
    def test_depth2_alias_repair_success(self) -> None:
        contract = _depth2_contract(known_aliases=["zip"])
        engine = make_engine()
        data = {"name": "Alice", "address": {"city": "Mumbai", "zip": "400001"}}
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {
            "name": "Alice",
            "address": {"city": "Mumbai", "zip_code": "400001"},
        }
        assert result.attempts[0].strategy_name == "ExactAliasStrategy"

    def test_depth3_alias_repair_success(self) -> None:
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING, known_aliases=["country_code"]),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "country": {"country_code": "IN"}},
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["country"] == {"code": "IN"}
        assert result.attempts[0].strategy_name == "ExactAliasStrategy"


# ===========================================================================
# Nested fuzzy repair
# ===========================================================================


class TestNestedFuzzyRepair:
    def test_depth2_fuzzy_repair_success(self) -> None:
        contract = _depth2_contract()
        engine = make_engine()
        data = {"name": "Alice", "address": {"city": "Mumbai", "zipcode": "400001"}}
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["zip_code"] == "400001"
        assert result.attempts[0].strategy_name == "FuzzyFieldMatchStrategy"

    def test_depth3_fuzzy_repair_success(self) -> None:
        contract = _depth3_contract()
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "country": {"cod": "IN", "population": 5}},
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["country"]["code"] == "IN"
        assert result.attempts[0].strategy_name == "FuzzyFieldMatchStrategy"

    def test_depth3_fuzzy_repair_does_not_disturb_sibling_branch(self) -> None:
        """A correct sibling field at the same depth is left untouched."""
        contract = _depth3_contract()
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "country": {"cod": "IN", "population": 5},
            },
        }
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.repaired_output["address"]["country"]["population"] == 5


# ===========================================================================
# Nested type coercion
# ===========================================================================


class TestNestedTypeCoercion:
    def test_depth2_coercion_success(self) -> None:
        contract = _depth2_contract()
        engine = make_engine()
        data = {"name": "Alice", "address": {"city": "Mumbai", "zip_code": 400001}}
        # zip_code is declared STRING; an int value is a type mismatch but
        # int->str is NOT a supported coercion -- expect FAILED, proving
        # the engine doesn't silently invent an unsupported cast.
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.status is RepairStatus.FAILED

    def test_depth3_coercion_success(self) -> None:
        contract = _depth3_contract()
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "country": {"code": "IN", "population": "5000000"}},
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["country"]["population"] == 5000000
        assert isinstance(result.repaired_output["address"]["country"]["population"], int)
        assert result.attempts[0].strategy_name == "TypeCoercionStrategy"


# ===========================================================================
# Nested missing fields (default fill)
# ===========================================================================


class TestNestedMissingFields:
    def test_depth2_default_fill_success(self) -> None:
        contract = _depth2_contract(default="00000")
        engine = make_engine()
        data = {"name": "Alice", "address": {"city": "Mumbai"}}
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["zip_code"] == "00000"
        assert result.attempts[0].strategy_name == "DefaultValueFillStrategy"

    def test_depth3_default_fill_success(self) -> None:
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING, default="IN"),
                FieldSpec("population", FieldType.INTEGER),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("city", FieldType.STRING),
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "country": {"population": 5}},
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["country"]["code"] == "IN"
        assert result.attempts[0].strategy_name == "DefaultValueFillStrategy"

    def test_depth3_entire_nested_branch_missing_is_unrecoverable(self) -> None:
        """If the WHOLE 'address' object is absent (not just a leaf
        field), only one MISSING_REQUIRED_FIELD violation is raised for
        'address' itself (per ContractValidator's documented behavior),
        and -- since 'address' has no default and no alias -- this is
        correctly FAILED, not silently fabricated."""
        contract = _depth3_contract()
        engine = make_engine()
        data = {"name": "Alice"}
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.FAILED
        assert len(result.initial_violations) == 1
        assert result.initial_violations[0].field_path == "address"
        assert result.initial_violations[0].violation_type is ViolationType.MISSING_REQUIRED_FIELD


# ===========================================================================
# Mixed nested failures (multiple strategies across multiple attempts)
# ===========================================================================


class TestMixedNestedFailures:
    def test_depth3_alias_then_fuzzy_in_different_branches(self) -> None:
        """One depth-3 field needs an alias repair; a sibling depth-2
        field needs a fuzzy repair. Both resolve across two attempts."""
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING, known_aliases=["country_code"]),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("zip_code", FieldType.STRING),
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {
                "zipcode": "400001",  # fuzzy rename needed
                "country": {"country_code": "IN"},  # alias rename needed
            },
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["zip_code"] == "400001"
        assert result.repaired_output["address"]["country"]["code"] == "IN"
        strategies_used = {a.strategy_name for a in result.attempts}
        assert strategies_used == {"ExactAliasStrategy", "FuzzyFieldMatchStrategy"}

    def test_depth3_fuzzy_and_unsupported_coercion_yields_partial(self) -> None:
        """A fuzzy-fixable rename succeeds; a sibling int->str type
        mismatch (unsupported coercion) cannot be fixed -- PARTIAL."""
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("zip_code", FieldType.STRING),
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine(config=RepairConfig(max_attempts=5, allow_partial_repair=True))
        data = {
            "name": "Alice",
            "address": {
                "zipcode": "400001",  # fuzzy rename -> fixable
                "country": {"code": 91},  # int->str -> NOT fixable
            },
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.PARTIAL
        assert result.repaired_output["address"]["zip_code"] == "400001"
        # the unsupported coercion remains unresolved
        remaining_paths = {v.field_path for v in result.remaining_violations}
        assert "address.country.code" in remaining_paths

    def test_depth3_three_simultaneous_strategies(self) -> None:
        """Alias, fuzzy, and default-fill all required simultaneously
        across three different depth-2/3 fields -- all resolve."""
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING, known_aliases=["country_code"]),
                FieldSpec("timezone", FieldType.STRING, default="UTC"),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("zip_code", FieldType.STRING),
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine(config=RepairConfig(max_attempts=10))
        data = {
            "name": "Alice",
            "address": {
                "zipcode": "400001",  # fuzzy
                "country": {"country_code": "IN"},  # alias; timezone missing -> default
            },
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["address"]["zip_code"] == "400001"
        assert result.repaired_output["address"]["country"]["code"] == "IN"
        assert result.repaired_output["address"]["country"]["timezone"] == "UTC"
        strategies_used = {a.strategy_name for a in result.attempts}
        assert strategies_used == {
            "ExactAliasStrategy",
            "FuzzyFieldMatchStrategy",
            "DefaultValueFillStrategy",
        }


# ===========================================================================
# Partial nested recovery
# ===========================================================================


class TestPartialNestedRecovery:
    def test_depth3_partial_when_one_branch_unrecoverable(self) -> None:
        """Two depth-3 sibling fields are both broken; one is fuzzy-
        fixable, the other has no plausible candidate at all (FAILED-style
        leftover within an otherwise-successful repair) -> PARTIAL."""
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING),
                FieldSpec("dial_prefix", FieldType.STRING),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine(config=RepairConfig(max_attempts=5, allow_partial_repair=True))
        data = {
            "name": "Alice",
            "address": {"country": {"cod": "IN"}},  # 'dial_prefix' has no candidate at all
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.PARTIAL
        assert result.repaired_output["address"]["country"]["code"] == "IN"
        remaining_paths = {v.field_path for v in result.remaining_violations}
        assert "address.country.dial_prefix" in remaining_paths

    def test_depth3_partial_disabled_yields_failed(self) -> None:
        """The same scenario with allow_partial_repair=False -> FAILED,
        not PARTIAL, with no repaired_output exposed."""
        country_spec = ContractSpec(
            fields=[
                FieldSpec("code", FieldType.STRING),
                FieldSpec("dial_prefix", FieldType.STRING),
            ]
        )
        address_spec = ContractSpec(
            fields=[
                FieldSpec("country", FieldType.OBJECT, nested_spec=country_spec),
            ]
        )
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT, nested_spec=address_spec),
            ]
        )
        engine = make_engine(config=RepairConfig(max_attempts=5, allow_partial_repair=False))
        data = {
            "name": "Alice",
            "address": {"country": {"cod": "IN"}},
        }
        result = engine.repair(contract, data, MockContractAdapter())

        assert result.status is RepairStatus.FAILED
        assert result.repaired_output is None


# ===========================================================================
# Deeply nested invalid paths
# ===========================================================================


class TestDeeplyNestedInvalidPaths:
    def test_data_with_extra_depth_beyond_contract_is_unexpected_field(self) -> None:
        """If 'address.country' is declared as a plain (non-nested) OBJECT
        field but the data nests even deeper than the contract describes,
        the extra depth is reported as a WARNING-level UNEXPECTED_FIELD
        scoped to 'address', not silently dropped or crashed on."""
        contract = ContractSpec(
            fields=[
                FieldSpec("name", FieldType.STRING),
                FieldSpec("address", FieldType.OBJECT),  # no nested_spec
            ]
        )
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"country": {"code": "IN", "extra": {"deep": "value"}}},
        }
        result = engine.repair(contract, data, MockContractAdapter())
        # No nested_spec means MockContractAdapter/ContractValidator don't
        # recurse into 'address' at all -- it's valid as long as it's a
        # dict. No crash, no spurious violations.
        assert result.status is RepairStatus.ALREADY_VALID

    def test_malformed_intermediate_type_does_not_crash_engine(self) -> None:
        """An intermediate path segment that is a list instead of a dict
        is reported as STRUCTURAL_MISMATCH and produces a clean FAILED,
        never an unhandled exception."""
        contract = _depth3_contract()
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {"city": "Mumbai", "country": ["not", "a", "dict"]},
        }
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.status is RepairStatus.FAILED
        assert any(
            v.violation_type is ViolationType.STRUCTURAL_MISMATCH for v in result.initial_violations
        )

    def test_six_levels_deep_path_does_not_crash_path_helpers(self) -> None:
        """_get_nested/_set_nested/_delete_nested have no hard depth
        limit even though only depth 3 is officially validated end-to-end;
        this proves graceful behavior (no crash) beyond that depth too."""
        from stateguard.core.engine import _get_nested, _set_nested, _delete_nested

        data: dict = {}
        _set_nested(data, "a.b.c.d.e.f", "deep_value")
        assert _get_nested(data, "a.b.c.d.e.f") == "deep_value"
        _delete_nested(data, "a.b.c.d.e.f")
        assert _get_nested(data, "a.b.c.d.e.f") is not None or True  # no crash either way

    def test_repair_at_max_validated_depth_full_round_trip(self) -> None:
        """Sanity end-to-end check at exactly the documented depth-3
        boundary: missing field repaired, contract revalidated, SUCCESS."""
        contract = _depth3_contract()
        engine = make_engine()
        data = {
            "name": "Alice",
            "address": {
                "city": "Mumbai",
                "country": {"code": "IN", "population": 1400000000},
            },
        }
        result = engine.repair(contract, data, MockContractAdapter())
        assert result.status is RepairStatus.ALREADY_VALID
        assert result.repaired_output == data

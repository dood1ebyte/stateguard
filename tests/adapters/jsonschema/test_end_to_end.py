"""
Repairing real payloads against a JSON Schema through ``ContractGuard``.

The proposal's success criterion for the MCP adapter is that *"an MCP
tool-call payload with a drifted schema is detected and repaired through the
same ContractGuard entrypoint as the existing REST/API demo"*. That is what
``TestToolCallDemo`` checks, using the schema and payload written into
``MCP_ADAPTER_PLAN.md`` §7 verbatim.

``TestStrictModeIsAFloor`` and ``TestDeclaredDefaultsAreMaterialised`` were
``xfail(strict=True)`` while the seam problems they describe were open. Both
are now decided and enforced -- the strict marker is what made the fixes
announce themselves rather than passing unnoticed.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard import ContractGuard
from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import GuardConfig, RepairMode

# The server's current inputSchema, from MCP_ADAPTER_PLAN.md §7.
WEATHER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
        "days": {"type": "integer", "minimum": 1, "maximum": 14},
        "unit": {
            "type": "string",
            "enum": ["celsius", "fahrenheit"],
            "default": "celsius",
        },
    },
    "required": ["location", "days"],
}


@pytest.fixture
def guard() -> ContractGuard:
    return ContractGuard.with_json_schema()


# ===========================================================================
# The demo
# ===========================================================================


class TestToolCallDemo:
    def test_drifted_tool_call_is_repaired(self, guard: ContractGuard) -> None:
        """
        The headline case: the server changed its signature, the model is
        still calling the old shape. Old param name *and* wrong type
        together -- the combined case the plan calls the most representative
        input in the category.
        """
        result = guard.repair(WEATHER_SCHEMA, {"loc": "Mumbai", "days": "5"})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["location"] == "Mumbai"
        assert result.repaired_output["days"] == 5

    def test_repair_takes_a_rename_and_a_coercion(self, guard: ContractGuard) -> None:
        """Two operations across two passes -- the convergence path."""
        result = guard.repair(WEATHER_SCHEMA, {"loc": "Mumbai", "days": "5"})
        applied = [
            (op.op_type.value, op.source_path, op.target_path)
            for attempt in result.attempts
            for op in attempt.applied_operations
        ]
        assert ("rename", "loc", "location") in applied
        assert ("coerce", None, "days") in applied

    def test_enum_drift_is_repaired(self, guard: ContractGuard) -> None:
        """
        §8 lists enum drift as the #1 real MCP failure and, at the time the
        plan was written, unrepairable. Enum normalisation shipped with the
        core hardening, so it now repairs through this adapter for free.
        """
        result = guard.repair(WEATHER_SCHEMA, {"location": "Mumbai", "days": 5, "unit": "Celsius"})
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["unit"] == "celsius"

    def test_everything_at_once(self, guard: ContractGuard) -> None:
        result = guard.repair(WEATHER_SCHEMA, {"loc": "Mumbai", "dayz": "5", "unit": "FAHRENHEIT"})
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {
            "location": "Mumbai",
            "days": 5,
            "unit": "fahrenheit",
        }

    def test_an_unrepairable_payload_fails_rather_than_guessing(self, guard: ContractGuard) -> None:
        result = guard.repair(WEATHER_SCHEMA, {"quux": 1, "zzz": 2})
        assert result.status in (RepairStatus.FAILED, RepairStatus.AMBIGUOUS)
        assert result.repaired_output is None

    def test_shadow_mode_withholds_the_repair(self) -> None:
        guard = ContractGuard.with_json_schema(config=GuardConfig(mode=RepairMode.SHADOW))
        result = guard.repair(WEATHER_SCHEMA, {"loc": "Mumbai", "days": "5"})

        assert result.repaired_output is None
        assert result.proposed_output is not None
        assert result.proposed_output["location"] == "Mumbai"


# ===========================================================================
# Validation semantics
# ===========================================================================


class TestValidationSemantics:
    def test_valid_payload_is_already_valid(self, guard: ContractGuard) -> None:
        result = guard.repair(WEATHER_SCHEMA, {"location": "Mumbai", "days": 5})
        assert result.status is RepairStatus.ALREADY_VALID

    def test_constraint_violation_is_detected(self, guard: ContractGuard) -> None:
        """``maximum: 14`` must actually be enforced."""
        assert guard.validate(WEATHER_SCHEMA, {"location": "M", "days": 99}).is_valid is False

    def test_null_for_a_non_nullable_field_is_rejected(self, guard: ContractGuard) -> None:
        """
        The ADR-driven divergence: without the ``NOT_NULL`` this adapter
        emits, ``ContractValidator`` would accept this and report SUCCESS on
        a payload JSON Schema plainly forbids.
        """
        assert guard.validate(WEATHER_SCHEMA, {"location": None, "days": 5}).is_valid is False

    def test_nullable_field_accepts_null(self) -> None:
        schema = {
            "type": "object",
            "properties": {"note": {"type": ["string", "null"]}},
            "required": ["note"],
        }
        assert ContractGuard.with_json_schema().validate(schema, {"note": None}).is_valid

    def test_unsupported_root_payload_does_not_raise(self, guard: ContractGuard) -> None:
        assert guard.repair(WEATHER_SCHEMA, None).status is RepairStatus.FAILED


# ===========================================================================
# Seam findings -- decided, and now enforced
# ===========================================================================


class TestStrictModeIsAFloor:
    """
    ``GuardConfig.strict_mode`` used to overwrite the adapter's answer in
    both directions, so a schema's ``additionalProperties: false`` lost to
    the config default of ``False`` -- while ``ContractSpec.strict_mode``
    documented the opposite precedence. It composes as a floor now: strict
    if either says so.

    ``PydanticAdapter`` never surfaced this because Pydantic enforces
    ``extra='forbid'`` in its own validator rather than through
    ``strict_mode``. Here ``strict_mode`` is the only enforcement path.
    """

    CLOSED: dict[str, Any] = {
        "type": "object",
        "properties": {"a": {"type": "string"}},
        "additionalProperties": False,
    }

    def test_schema_closes_the_contract_against_a_permissive_config(self) -> None:
        guard = ContractGuard.with_json_schema(config=GuardConfig(strict_mode=False))
        assert guard.repair(self.CLOSED, {"a": "x", "extra": 1}).status is not (
            RepairStatus.ALREADY_VALID
        )

    def test_config_can_still_tighten_an_open_schema(self) -> None:
        """
        The floor keeps the config useful for schema formats with no way to
        declare themselves closed -- it only stops it loosening one that has.
        """
        open_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        guard = ContractGuard.with_json_schema(config=GuardConfig(strict_mode=True))
        assert guard.repair(open_schema, {"a": "x", "extra": 1}).status is not (
            RepairStatus.ALREADY_VALID
        )

    def test_an_open_schema_and_open_config_stay_open(self) -> None:
        open_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        guard = ContractGuard.with_json_schema(config=GuardConfig(strict_mode=False))
        assert guard.repair(open_schema, {"a": "x"}).status is RepairStatus.ALREADY_VALID


class TestDeclaredDefaultsAreMaterialised:
    """
    ``PydanticAdapter.wrap`` calls ``model_validate``, which applies the
    model's defaults; this adapter returned the dict untouched. The two
    visibly disagreed on the same schema, and §7's demo narrative -- step 3
    fills ``unit`` from the schema's default -- did not actually happen.
    """

    def test_absent_optional_is_filled_from_the_schema(self, guard: ContractGuard) -> None:
        result = guard.repair(WEATHER_SCHEMA, {"location": "Mumbai", "days": 5})
        assert result.repaired_output.get("unit") == "celsius"

    def test_a_supplied_value_is_not_overwritten(self, guard: ContractGuard) -> None:
        result = guard.repair(
            WEATHER_SCHEMA, {"location": "Mumbai", "days": 5, "unit": "fahrenheit"}
        )
        assert result.repaired_output["unit"] == "fahrenheit"

    def test_a_field_with_no_declared_default_stays_absent(self) -> None:
        """Filling means *declared* defaults, not inventing values."""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
            "required": ["a"],
        }
        guard = ContractGuard.with_json_schema()
        result = guard.repair(schema, {"a": "x"})
        assert "b" not in result.repaired_output

    def test_mutable_defaults_are_not_shared_between_payloads(self) -> None:
        """
        A schema default may be a list or an object. Handing the same
        instance to every caller would let one payload's mutation surface in
        the next one's.
        """
        schema = {
            "type": "object",
            "properties": {"tags": {"type": "array", "default": []}},
        }
        guard = ContractGuard.with_json_schema()
        first = guard.repair(schema, {}).repaired_output
        first["tags"].append("mutated")
        second = guard.repair(schema, {}).repaired_output
        assert second["tags"] == []

    def test_nested_defaults_are_filled_too(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "opts": {
                    "type": "object",
                    "properties": {"unit": {"type": "string", "default": "celsius"}},
                }
            },
        }
        guard = ContractGuard.with_json_schema()
        result = guard.repair(schema, {"opts": {}})
        assert result.repaired_output["opts"]["unit"] == "celsius"

    def test_matches_the_pydantic_adapter_on_the_same_contract(self) -> None:
        """
        The disagreement this closes, asserted directly rather than
        described. Both adapters are handed the same logical contract with
        the same optional-with-default field, and both must return it
        populated.
        """
        pydantic = pytest.importorskip("pydantic")

        class Weather(pydantic.BaseModel):
            location: str
            days: int
            unit: str = "celsius"

        payload = {"location": "Mumbai", "days": 5}
        through_pydantic = ContractGuard.with_pydantic().repair(Weather, payload)
        through_schema = ContractGuard.with_json_schema().repair(WEATHER_SCHEMA, payload)

        assert through_pydantic.repaired_output.unit == "celsius"
        assert through_schema.repaired_output["unit"] == "celsius"


# ===========================================================================
# Parity with the Pydantic adapter
# ===========================================================================


class TestParityWithPydantic:
    """
    The same logical contract, described both ways, must repair the same.

    The two adapters feed identical strategies, so a field that reads
    differently through one path would be priced differently by the trust
    model for no reason a user could see.
    """

    def test_same_drift_repairs_the_same_through_both_adapters(self) -> None:
        pydantic = pytest.importorskip("pydantic")

        class Weather(pydantic.BaseModel):
            location: str
            days: int

        schema = {
            "type": "object",
            "properties": {"location": {"type": "string"}, "days": {"type": "integer"}},
            "required": ["location", "days"],
        }
        payload = {"loc": "Mumbai", "days": "5"}

        from_pydantic = ContractGuard.with_pydantic().repair(Weather, payload)
        from_schema = ContractGuard.with_json_schema().repair(schema, payload)

        assert from_pydantic.status is from_schema.status
        assert from_schema.repaired_output == {"location": "Mumbai", "days": 5}
        assert from_pydantic.repaired_output.model_dump() == from_schema.repaired_output

    def test_generated_schema_from_a_model_extracts_equivalently(self) -> None:
        """
        A Pydantic-generated schema (with ``$defs``) is the realistic MCP
        input -- most servers are Pydantic-backed. Phase 1's exit criterion.
        """
        pydantic = pytest.importorskip("pydantic")

        class Address(pydantic.BaseModel):
            city: str

        class User(pydantic.BaseModel):
            name: str
            address: Address

        result = ContractGuard.with_json_schema().repair(
            User.model_json_schema(), {"nam": "Ada", "address": {"city": "Cambridge"}}
        )
        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output["name"] == "Ada"

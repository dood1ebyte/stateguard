"""
Repairing real payloads against a JSON Schema through ``ContractGuard``.

The proposal's success criterion for the MCP adapter is that *"an MCP
tool-call payload with a drifted schema is detected and repaired through the
same ContractGuard entrypoint as the existing REST/API demo"*. That is what
``TestToolCallDemo`` checks, using the schema and payload written into
``MCP_ADAPTER_PLAN.md`` §7 verbatim.

Two tests are ``xfail(strict=True)``. They record seam problems found while
building this adapter that need a decision rather than a quiet workaround --
they will fail loudly the moment the behaviour changes, which is the point.
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
# Seam findings -- recorded, not worked around
# ===========================================================================


class TestKnownSeamProblems:
    @pytest.mark.xfail(
        strict=True,
        reason=(
            "ContractGuard._extract_contract rebuilds the ContractSpec with "
            "GuardConfig.strict_mode whenever it differs, so a schema's "
            "additionalProperties:false is silently overridden by the config "
            "default of False. The Pydantic adapter never hits this because "
            "extra='forbid' is enforced by Pydantic's own validator, not by "
            "strict_mode -- but this adapter has no native validator, so "
            "strict_mode is the only enforcement path. Needs a decision: make "
            "GuardConfig.strict_mode a floor (strict if either says so), or "
            "tri-state it so 'unset' defers to the schema."
        ),
    )
    def test_additional_properties_false_is_enforced(self, guard: ContractGuard) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "additionalProperties": False,
        }
        assert guard.repair(schema, {"a": "x", "extra": 1}).status is not (
            RepairStatus.ALREADY_VALID
        )

    @pytest.mark.xfail(
        strict=True,
        reason=(
            "An optional field with a declared default is not materialised. "
            "The engine repairs identically either way; the difference is in "
            "wrap(): PydanticAdapter.wrap calls model_validate, which applies "
            "defaults, while this adapter (like DictContractAdapter) returns "
            "a plain dict and nothing applies them. Arguably correct -- the "
            "payload is valid without it and the server will apply its own "
            "default -- but it makes the two adapters visibly disagree on the "
            "same schema. Needs a decision."
        ),
    )
    def test_optional_default_is_materialised(self, guard: ContractGuard) -> None:
        result = guard.repair(WEATHER_SCHEMA, {"location": "Mumbai", "days": 5})
        assert result.repaired_output.get("unit") == "celsius"


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

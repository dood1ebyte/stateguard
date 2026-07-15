"""
The canonical living red test.

This file defines the complete end-to-end behaviour of ContractGuard V1
in concrete, executable terms.  Every test in this file is FAILING until
M8 (guard.py + PydanticAdapter) is complete.

Do not mark these tests as xfail or skip.
They are intentionally RED — they exist to make "done" unambiguous.

Repair scenario tested here
----------------------------
Schema  : Weather(temperature: float, humidity: int)
Input   : {"temp_celsius": 31.5, "humidity": 80}
Expected: RepairStatus.SUCCESS, {"temperature": 31.5, "humidity": 80}
Strategy: FuzzyFieldMatchStrategy detects temp_celsius ≈ temperature
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.core.errors.results import RepairStatus


# ---------------------------------------------------------------------------
# Helper: import guard — makes test FAIL (not ERROR) when M8 is pending
# ---------------------------------------------------------------------------


def _require_guard() -> Any:
    """
    Import ContractGuard.  If it is not yet implemented, fail the test
    with a clear message rather than raising an unhandled ImportError.
    """
    try:
        from stateguard import ContractGuard  # noqa: PLC0415

        return ContractGuard
    except (ImportError, AttributeError) as exc:
        pytest.fail(
            f"ContractGuard is not yet available (M8 pending): {exc}",
            pytrace=False,
        )


def _require_pydantic_adapter() -> Any:
    """
    Import PydanticAdapter.  Fails the test if not yet implemented.
    """
    try:
        from stateguard.adapters.pydantic import PydanticAdapter  # noqa: PLC0415

        return PydanticAdapter
    except (ImportError, AttributeError) as exc:
        pytest.fail(
            f"PydanticAdapter is not yet available (M7 pending): {exc}",
            pytrace=False,
        )


def _require_pydantic() -> Any:
    """Skip the test if pydantic itself is not installed."""
    pydantic = pytest.importorskip("pydantic", reason="pydantic not installed")
    return pydantic


# ===========================================================================
# Fuzzy rename repair — the canonical scenario
# ===========================================================================


@pytest.mark.integration
def test_weather_fuzzy_rename_returns_success() -> None:
    """FuzzyFieldMatchStrategy renames temp_celsius → temperature."""
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert result.status is RepairStatus.SUCCESS


@pytest.mark.integration
def test_weather_fuzzy_rename_repaired_output_correct() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    # On SUCCESS, repaired_output is the framework-native object (a
    # validated Weather instance), not a plain dict — see ContractGuard.repair.
    assert isinstance(result.repaired_output, Weather)
    assert result.repaired_output.temperature == 31.5
    assert result.repaired_output.humidity == 80


@pytest.mark.integration
def test_weather_fuzzy_rename_no_remaining_violations() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert result.remaining_violations == []


@pytest.mark.integration
def test_weather_fuzzy_rename_single_attempt() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert len(result.attempts) == 1


@pytest.mark.integration
def test_weather_fuzzy_rename_strategy_name_recorded() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert result.attempts[0].strategy_name == "FuzzyFieldMatchStrategy"


@pytest.mark.integration
def test_weather_fuzzy_rename_original_input_preserved() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    original = {"temp_celsius": 31.5, "humidity": 80}
    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, original)

    assert result.original_input == {"temp_celsius": 31.5, "humidity": 80}
    # The caller's dict must not be mutated
    assert original == {"temp_celsius": 31.5, "humidity": 80}


@pytest.mark.integration
def test_weather_fuzzy_rename_returns_pydantic_model() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    repaired = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert repaired.is_success
    model = repaired.repaired_output
    # The engine returns a validated BaseModel instance via PydanticAdapter.wrap
    assert isinstance(model, Weather) or isinstance(model, dict)


# ===========================================================================
# Already-valid input
# ===========================================================================


@pytest.mark.integration
def test_already_valid_input_returns_already_valid_status() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temperature": 31.5, "humidity": 80})

    assert result.status is RepairStatus.ALREADY_VALID


@pytest.mark.integration
def test_already_valid_input_has_no_attempts() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temperature": 31.5, "humidity": 80})

    assert result.attempts == []


# ===========================================================================
# Exact alias repair
# ===========================================================================


@pytest.mark.integration
def test_exact_alias_repair_returns_success() -> None:
    """
    ExactAliasStrategy fires when a tool returns the Python attribute name
    instead of the schema's declared alias.

    PydanticContractExtractor sets the contract's field path to the
    declared alias ("temp") -- that is what model_validate actually
    expects as input. Supplying "temp" directly would already be valid
    with no repair needed. The repair scenario this strategy targets is
    the reverse: a tool that returns "temperature" (the Python attribute
    name) instead of "temp" (the alias) is repaired by renaming it back.
    """
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class WeatherAliased(pydantic.BaseModel):
        temperature: float = pydantic.Field(alias="temp")
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(WeatherAliased, {"temperature": 31.5, "humidity": 80})

    assert result.status is RepairStatus.SUCCESS
    assert result.attempts[0].strategy_name == "ExactAliasStrategy"


# ===========================================================================
# Type coercion repair
# ===========================================================================


@pytest.mark.integration
def test_type_coercion_string_to_float_returns_success() -> None:
    """TypeCoercionStrategy repairs '31.5' → 31.5 for a float field."""
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temperature": "31.5", "humidity": 80})

    assert result.status is RepairStatus.SUCCESS


# ===========================================================================
# Default value fill repair
# ===========================================================================


@pytest.mark.integration
def test_default_fill_returns_success() -> None:
    """
    A field with a Pydantic-declared default is extracted as optional
    (required=False) and auto-filled by Pydantic's own model_validate --
    so this scenario is ALREADY_VALID with no repair engine intervention.

    DefaultValueFillStrategy itself targets adapters where "required" and
    "default" can coexist independently (e.g. JSON Schema-style schemas,
    where a field can be both in the "required" list and carry a
    "default"). For the Pydantic adapter specifically, a declared default
    always makes the field optional, so the strategy is exercised at the
    engine/strategy unit level instead -- see
    tests/core/strategies/test_default_fill_strategy.py and
    tests/core/test_engine.py::TestPartialRepair.
    """
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class WeatherWithDefault(pydantic.BaseModel):
        temperature: float
        humidity: int = 60

    guard = ContractGuard.with_pydantic()
    result = guard.repair(WeatherWithDefault, {"temperature": 31.5})

    assert result.status is RepairStatus.ALREADY_VALID
    assert result.repaired_output.humidity == 60


# ===========================================================================
# Unrecoverable failure
# ===========================================================================


@pytest.mark.integration
def test_unrecoverable_violation_returns_failed() -> None:
    """Completely unrelated field names cannot be repaired → FAILED."""
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    # 'xyz' and 'abc' have no fuzzy similarity to 'temperature' or 'humidity'
    result = guard.repair(Weather, {"xyz": 31.5, "abc": 80})

    assert result.status is RepairStatus.FAILED
    assert result.repaired_output is None


# ===========================================================================
# Audit trail
# ===========================================================================


@pytest.mark.integration
def test_repair_log_is_non_empty_on_success() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert len(result.repair_log) > 0


@pytest.mark.integration
def test_contract_id_is_set_on_result() -> None:
    ContractGuard = _require_guard()
    _require_pydantic_adapter()
    pydantic = _require_pydantic()

    class Weather(pydantic.BaseModel):
        temperature: float
        humidity: int

    guard = ContractGuard.with_pydantic()
    result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

    assert result.contract_id
    assert isinstance(result.contract_id, str)


# ===========================================================================
# Wrap-in-list coercion (autoclip_mvp #17 shape)
# ===========================================================================


class TestWrapInListRepair:
    """str where List[str] is expected -> wrapped as a one-element list."""

    def test_str_content_wrapped_into_list(self) -> None:
        ContractGuard = _require_guard()
        from typing import List, Optional

        from pydantic import BaseModel

        class Clip(BaseModel):
            id: str
            outline: str
            content: List[str]
            title: Optional[str] = None

        guard = ContractGuard.with_pydantic()
        data = {"id": "c1", "outline": "o", "content": "a single sentence"}
        result = guard.repair(Clip, data)

        assert result.status is RepairStatus.SUCCESS
        assert isinstance(result.repaired_output, Clip)
        assert result.repaired_output.content == ["a single sentence"]

    def test_wrap_refused_when_item_type_mismatches(self) -> None:
        ContractGuard = _require_guard()
        from typing import List

        from pydantic import BaseModel

        class M(BaseModel):
            nums: List[int]

        guard = ContractGuard.with_pydantic()
        result = guard.repair(M, {"nums": "not a number"})
        assert result.status is RepairStatus.FAILED


# ===========================================================================
# Union coercion (graph-rag-agent #49 shape)
# ===========================================================================


class TestUnionRepair:
    """dict where str | list[str | dict] is expected -> wrapped into the
    list member (the LangChain AIMessage.content shape)."""

    def test_dict_content_wrapped_into_list_member(self) -> None:
        ContractGuard = _require_guard()
        from typing import Any, Dict, List, Union

        from pydantic import BaseModel

        class Message(BaseModel):
            content: Union[str, List[Union[str, Dict[str, Any]]]]
            type: str = "ai"

        guard = ContractGuard.with_pydantic()
        payload = {"low_level": ["kw1"], "high_level": []}
        result = guard.repair(Message, {"content": payload})

        assert result.status is RepairStatus.SUCCESS
        assert isinstance(result.repaired_output, Message)
        assert result.repaired_output.content == [payload]

    def test_union_branch_errors_collapse_to_single_violation(self) -> None:
        ContractGuard = _require_guard()
        from typing import Any, Dict, List, Union

        from pydantic import BaseModel

        class Message(BaseModel):
            content: Union[str, List[Union[str, Dict[str, Any]]]]

        guard = ContractGuard.with_pydantic()
        validation = guard.validate(Message, {"content": {"a": 1}})

        assert validation.is_valid is False
        content_violations = [v for v in validation.violations if v.field_path == "content"]
        assert len(content_violations) == 1
        # No polluted union-branch paths like "content.str" survive.
        assert all("." not in v.field_path for v in validation.violations)


# ===========================================================================
# JSON-serialise coercion (openai-python #2702 shape)
# ===========================================================================


class TestJsonSerializeRepair:
    """dict where str / bytes is expected -> json.dumps'd back into a
    string (the openai-agents @function_tool write_file shape: an agent
    harness parses the LLM's raw JSON text argument into an object)."""

    PACKAGE_JSON = {"name": "hello_world", "dependencies": {"express": "^5.1.0"}}

    def test_dict_payload_for_str_field_repairs(self) -> None:
        import json

        ContractGuard = _require_guard()
        from pydantic import BaseModel

        class WriteFileArgs(BaseModel):
            file_path: str
            content: str

        guard = ContractGuard.with_pydantic()
        result = guard.repair(
            WriteFileArgs, {"file_path": "package.json", "content": self.PACKAGE_JSON}
        )

        assert result.status is RepairStatus.SUCCESS
        assert isinstance(result.repaired_output, WriteFileArgs)
        assert result.repaired_output.content == json.dumps(self.PACKAGE_JSON)

    def test_dict_payload_for_bytes_field_repairs(self) -> None:
        import json

        ContractGuard = _require_guard()
        from pydantic import BaseModel

        class WriteFileArgs(BaseModel):
            file_path: str
            content: bytes

        guard = ContractGuard.with_pydantic()
        result = guard.repair(
            WriteFileArgs, {"file_path": "package.json", "content": self.PACKAGE_JSON}
        )

        # Pydantic's lax str -> bytes encoding turns the serialised str
        # into bytes when the repaired output is rehydrated.
        assert result.status is RepairStatus.SUCCESS
        assert isinstance(result.repaired_output, WriteFileArgs)
        assert result.repaired_output.content == json.dumps(self.PACKAGE_JSON).encode()

    def test_genuine_bytes_value_stays_already_valid(self) -> None:
        """Regression guard for the BYTES mapping: a real (even non-UTF-8)
        bytes value must not be churned through the repair loop."""
        ContractGuard = _require_guard()
        from pydantic import BaseModel

        class WriteFileArgs(BaseModel):
            file_path: str
            content: bytes

        guard = ContractGuard.with_pydantic()
        result = guard.repair(
            WriteFileArgs, {"file_path": "logo.png", "content": b"\x89PNG\r\n\x1a\n"}
        )

        assert result.status is RepairStatus.ALREADY_VALID
        assert result.attempts == []

    def test_dict_schema_bytes_type_parses_and_repairs(self) -> None:
        import json

        ContractGuard = _require_guard()

        guard = ContractGuard.with_dict_schema()
        contract = {
            "fields": [
                {"path": "file_path", "type": "string"},
                {"path": "content", "type": "bytes"},
            ]
        }
        result = guard.repair(contract, {"file_path": "package.json", "content": self.PACKAGE_JSON})

        assert result.status is RepairStatus.SUCCESS
        assert result.repaired_output == {
            "file_path": "package.json",
            "content": json.dumps(self.PACKAGE_JSON),
        }

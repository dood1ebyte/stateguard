"""Tests for stateguard.guard.ContractGuard."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel

from stateguard.core.errors.results import RepairStatus
from stateguard.core.models.config import GuardConfig, RepairConfig
from stateguard.guard import ContractGuard
from stateguard.logging.repair_history import RepairHistoryRecorder


class Weather(BaseModel):
    temperature: float
    humidity: int


# ===========================================================================
# Construction
# ===========================================================================


class TestConstruction:
    def test_with_pydantic_returns_contractguard(self) -> None:
        guard = ContractGuard.with_pydantic()
        assert isinstance(guard, ContractGuard)

    def test_with_pydantic_accepts_config(self) -> None:
        config = GuardConfig(repair=RepairConfig(max_attempts=10))
        guard = ContractGuard.with_pydantic(config=config)
        assert isinstance(guard, ContractGuard)

    def test_default_history_is_none(self) -> None:
        guard = ContractGuard.with_pydantic()
        assert guard._history is None

    def test_with_pydantic_accepts_history(self, tmp_path: Path) -> None:
        recorder = RepairHistoryRecorder(path=tmp_path / "repairs.jsonl")
        guard = ContractGuard.with_pydantic(history=recorder)
        assert guard._history is recorder

    def test_direct_constructor_accepts_history(self, tmp_path: Path) -> None:
        from stateguard.adapters.pydantic import PydanticAdapter

        recorder = RepairHistoryRecorder(path=tmp_path / "repairs.jsonl")
        guard = ContractGuard(adapter=PydanticAdapter.with_defaults(), history=recorder)
        assert guard._history is recorder


# ===========================================================================
# repair() — history integration
# ===========================================================================


class TestRepairHistoryIntegration:
    def test_history_recorder_receives_result(self, tmp_path: Path) -> None:
        history_path = tmp_path / "repairs.jsonl"
        recorder = RepairHistoryRecorder(path=history_path)
        guard = ContractGuard.with_pydantic(history=recorder)

        guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})

        assert history_path.exists()
        records = recorder.read_all()
        assert len(records) >= 1

    def test_no_history_recorder_writes_nothing(self, tmp_path: Path) -> None:
        guard = ContractGuard.with_pydantic()
        guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})
        # No recorder configured -- nothing to check on disk, but this
        # documents that repair() works fine with history=None (default).

    def test_history_recording_failure_does_not_break_repair(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Even if the history recorder's own internal safety net somehow
        fails to catch an exception, ContractGuard.repair's belt-and-
        suspenders try/except must still prevent it from propagating."""
        recorder = RepairHistoryRecorder(path=tmp_path / "repairs.jsonl")

        def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("simulated total failure")

        monkeypatch.setattr(recorder, "record", boom)
        guard = ContractGuard.with_pydantic(history=recorder)

        result = guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})
        assert result.status is RepairStatus.SUCCESS

    def test_history_records_already_valid_case(self, tmp_path: Path) -> None:
        history_path = tmp_path / "repairs.jsonl"
        recorder = RepairHistoryRecorder(path=history_path)
        guard = ContractGuard.with_pydantic(history=recorder)

        result = guard.repair(Weather, {"temperature": 31.5, "humidity": 80})
        assert result.status is RepairStatus.ALREADY_VALID

        records = recorder.read_all()
        assert len(records) == 1
        assert records[0]["status"] == "already_valid"

    def test_multiple_repairs_accumulate_in_history(self, tmp_path: Path) -> None:
        history_path = tmp_path / "repairs.jsonl"
        recorder = RepairHistoryRecorder(path=history_path)
        guard = ContractGuard.with_pydantic(history=recorder)

        guard.repair(Weather, {"temp_celsius": 31.5, "humidity": 80})
        guard.repair(Weather, {"temperature": 1.0, "humidity": 2})

        records = recorder.read_all()
        assert len(records) >= 2


# ===========================================================================
# validate()
# ===========================================================================


class TestValidate:
    def test_validate_valid_data(self) -> None:
        guard = ContractGuard.with_pydantic()
        result = guard.validate(Weather, {"temperature": 31.5, "humidity": 80})
        assert result.is_valid is True

    def test_validate_invalid_data(self) -> None:
        guard = ContractGuard.with_pydantic()
        result = guard.validate(Weather, {"temp_celsius": 31.5, "humidity": 80})
        assert result.is_valid is False

    def test_validate_does_not_repair(self) -> None:
        """validate() never mutates or fixes anything -- it only reports."""
        guard = ContractGuard.with_pydantic()
        data = {"temp_celsius": 31.5, "humidity": 80}
        guard.validate(Weather, data)
        assert data == {"temp_celsius": 31.5, "humidity": 80}

    def test_validate_matches_repair_already_valid(self) -> None:
        """validate(...).is_valid is True exactly when repair(...) would
        return ALREADY_VALID -- documented contract between the two methods."""
        guard = ContractGuard.with_pydantic()
        data = {"temperature": 31.5, "humidity": 80}

        validation = guard.validate(Weather, data)
        repair_result = guard.repair(Weather, data)

        assert validation.is_valid is True
        assert repair_result.status is RepairStatus.ALREADY_VALID

    def test_validate_reports_unexpected_field(self) -> None:
        """validate() surfaces UNEXPECTED_FIELD via the merged validator,
        even though Pydantic alone (extra='ignore') would not."""
        guard = ContractGuard.with_pydantic()
        result = guard.validate(Weather, {"temperature": 31.5, "humidity": 80, "extra": "field"})
        violation_types = {v.violation_type.value for v in result.violations}
        assert "unexpected_field" in violation_types


# ===========================================================================
# with_pydantic() — ImportError path
# ===========================================================================


class TestWithPydanticImportError:
    def test_import_error_message_mentions_extra(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import builtins

        real_import = builtins.__import__

        def fake_import(name: str, *args: Any, **kwargs: Any) -> Any:
            if name == "stateguard.adapters.pydantic" or name.startswith(
                "stateguard.adapters.pydantic"
            ):
                raise ImportError("simulated: pydantic not installed")
            return real_import(name, *args, **kwargs)

        monkeypatch.setattr(builtins, "__import__", fake_import)

        with pytest.raises(ImportError, match="pip install stateguard\\[pydantic\\]"):
            ContractGuard.with_pydantic()


# ===========================================================================
# strict_mode propagation
# ===========================================================================


class TestStrictModePropagation:
    def test_strict_mode_true_makes_extra_field_an_error(self) -> None:
        guard = ContractGuard.with_pydantic(config=GuardConfig(strict_mode=True))
        result = guard.repair(
            Weather, {"temperature": 31.5, "humidity": 80, "extra_unrecoverable_field_xyz": 1}
        )
        # 'extra' has no plausible fuzzy match -> repair cannot resolve it
        # under strict mode, where it's an ERROR rather than a WARNING.
        assert result.status is not RepairStatus.ALREADY_VALID

    def test_strict_mode_false_extra_field_is_already_valid(self) -> None:
        guard = ContractGuard.with_pydantic(config=GuardConfig(strict_mode=False))
        result = guard.repair(Weather, {"temperature": 31.5, "humidity": 80, "extra_field_xyz": 1})
        assert result.status is RepairStatus.ALREADY_VALID

    def test_strict_mode_default_matches_guardconfig_default(self) -> None:
        default_config = GuardConfig()
        guard_default = ContractGuard.with_pydantic()
        guard_explicit = ContractGuard.with_pydantic(config=default_config)
        data = {"temperature": 31.5, "humidity": 80, "extra_field_xyz": 1}
        assert (
            guard_default.repair(Weather, dict(data)).status
            == guard_explicit.repair(Weather, dict(data)).status
        )


class TestStrictModeFloorReachesNestedContracts:
    """
    ``GuardConfig.strict_mode`` is a floor, and a floor with no depth limit.

    It used to be applied to the root ``ContractSpec`` only, so one config
    flag -- which says nothing about depth -- made an undeclared key an
    ``ERROR`` at the top level and a ``WARNING`` one level down. What the
    floor must *not* do is loosen: a nested object that declared itself
    closed stays closed whatever the config says, and a lax config leaves
    every level exactly as the schema wrote it.
    """

    SCHEMA: dict[str, Any] = {
        "type": "object",
        "properties": {
            "user": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
            }
        },
    }
    DATA: dict[str, Any] = {"user": {"name": "a", "EXTRA": 1}, "TOP_EXTRA": 2}

    @staticmethod
    def _severities(guard: ContractGuard, schema: Any, data: dict[str, Any]) -> dict[str, str]:
        result = guard.repair(schema, dict(data))
        return {v.field_path: v.severity.value for v in result.remaining_violations}

    def test_config_strict_reaches_a_nested_object(self) -> None:
        guard = ContractGuard.with_json_schema(config=GuardConfig(strict_mode=True))
        severities = self._severities(guard, self.SCHEMA, self.DATA)
        assert severities["TOP_EXTRA"] == "error"
        assert severities["user.EXTRA"] == "error"

    def test_a_lax_config_leaves_every_level_as_the_schema_wrote_it(self) -> None:
        severities = self._severities(ContractGuard.with_json_schema(), self.SCHEMA, self.DATA)
        assert severities["TOP_EXTRA"] == "warning"
        assert severities["user.EXTRA"] == "warning"

    def test_a_nested_object_that_declared_itself_closed_stays_closed(self) -> None:
        """The floor never loosens -- including below the root."""
        schema = {
            "type": "object",
            "properties": {
                "user": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {"name": {"type": "string"}},
                }
            },
        }
        severities = self._severities(ContractGuard.with_json_schema(), schema, self.DATA)
        assert severities["user.EXTRA"] == "error"
        assert severities["TOP_EXTRA"] == "warning"

    def test_tightening_does_not_leak_into_a_shared_cached_contract(self) -> None:
        """
        ``MCPToolAdapter`` caches contracts and hands the same instance to
        every holder, so the floor has to rebuild rather than mutate. Two
        guards sharing one adapter must not see each other's configuration.
        """
        from stateguard.adapters.mcp import MCPToolAdapter

        shared = MCPToolAdapter()
        tool = {"name": "t", "inputSchema": self.SCHEMA}

        strict = ContractGuard(adapter=shared, config=GuardConfig(strict_mode=True))
        lax = ContractGuard(adapter=shared)

        assert self._severities(strict, tool, self.DATA)["user.EXTRA"] == "error"
        assert self._severities(lax, tool, self.DATA)["user.EXTRA"] == "warning"

"""
Top-level pytest configuration and shared test infrastructure.

Milestone contents
------------------
M0/M1  markers only
M3     factory functions, MockContractAdapter, MockRepairStrategy,
       CapturingTelemetryHook, and common fixtures
M6+    engine-level fixtures added as milestones land
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Generator

import pytest

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import ValidationResult
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.config import GuardConfig, RepairConfig
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType
from stateguard.telemetry.hooks import ITelemetryHook, TelemetryEvent


# ---------------------------------------------------------------------------
# Pytest markers
# ---------------------------------------------------------------------------


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "unit: Unit tests for individual components")
    config.addinivalue_line("markers", "integration: Integration tests across components")
    config.addinivalue_line("markers", "isolation: Import isolation tests (use subprocess)")


# ---------------------------------------------------------------------------
# Factory functions
# ---------------------------------------------------------------------------


def make_field_spec(
    path: str = "temperature",
    field_type: FieldType = FieldType.FLOAT,
    required: bool = True,
    **kwargs: Any,
) -> FieldSpec:
    """Build a FieldSpec with sensible defaults; any field can be overridden."""
    return FieldSpec(path=path, field_type=field_type, required=required, **kwargs)


def make_contract_spec(
    fields: list[FieldSpec] | None = None,
    strict_mode: bool = False,
    **kwargs: Any,
) -> ContractSpec:
    """
    Build a ContractSpec.

    If *fields* is omitted, a single ``temperature: FLOAT`` field is used
    so that callers that only need a non-empty contract don't have to
    construct fields manually.
    """
    if fields is None:
        fields = [make_field_spec()]
    return ContractSpec(fields=fields, strict_mode=strict_mode, **kwargs)


def make_violation(
    field_path: str = "temperature",
    violation_type: ViolationType = ViolationType.MISSING_REQUIRED_FIELD,
    severity: ViolationSeverity = ViolationSeverity.ERROR,
    message: str = "",
    **kwargs: Any,
) -> ContractViolation:
    """Build a ContractViolation with sensible defaults."""
    if not message:
        message = f"Test violation: {violation_type.value} at '{field_path}'"
    return ContractViolation(
        field_path=field_path,
        violation_type=violation_type,
        severity=severity,
        message=message,
        **kwargs,
    )


def make_operation(
    op_type: FieldOpType = FieldOpType.RENAME,
    target_path: str = "temperature",
    confidence: float = 0.9,
    rationale: str = "Test operation",
    **kwargs: Any,
) -> FieldOperation:
    """
    Build a FieldOperation with sensible defaults.

    For RENAME operations, ``source_path`` defaults to ``"temp_celsius"``
    if not provided, satisfying the required-source-path invariant.
    """
    if op_type is FieldOpType.RENAME and "source_path" not in kwargs:
        kwargs["source_path"] = "temp_celsius"
    return FieldOperation(
        op_type=op_type,
        target_path=target_path,
        confidence=confidence,
        rationale=rationale,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Internal helpers for MockContractAdapter
# ---------------------------------------------------------------------------


def _type_matches(value: Any, field_type: FieldType) -> bool:
    """
    Return True if *value*'s Python type is compatible with *field_type*.

    Rules
    -----
    * BOOLEAN  — only ``bool``  (bool is a subclass of int; must check first)
    * INTEGER  — ``int`` but not ``bool``
    * FLOAT    — ``int`` or ``float`` but not ``bool`` (int is valid float)
    * STRING   — only ``str``
    * OBJECT   — only ``dict``
    * ARRAY    — only ``list``
    * ANY      — always True
    * NULL     — only ``None``
    """
    if value is None:
        return field_type is FieldType.NULL or field_type is FieldType.ANY
    if field_type is FieldType.ANY:
        return True
    if field_type is FieldType.NULL:
        return False  # value is not None
    if field_type is FieldType.BOOLEAN:
        return isinstance(value, bool)
    if field_type is FieldType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type is FieldType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type is FieldType.STRING:
        return isinstance(value, str)
    if field_type is FieldType.OBJECT:
        return isinstance(value, dict)
    if field_type is FieldType.ARRAY:
        return isinstance(value, list)
    return True


# ---------------------------------------------------------------------------
# MockContractAdapter
# ---------------------------------------------------------------------------


class MockContractAdapter(IContractAdapter):
    """
    Stdlib-only IContractAdapter implementation for engine unit tests.

    Accepts a ``ContractSpec`` directly as the *schema* argument to
    ``extract_contract``.  Performs flat-field validation (top-level paths
    only; dot-notation nested paths are not traversed in V1 mock).

    Zero external dependencies — does not import pydantic.
    """

    def extract_contract(self, schema: Any) -> ContractSpec:
        if not isinstance(schema, ContractSpec):
            raise TypeError(
                f"MockContractAdapter.extract_contract expects a ContractSpec, "
                f"got {type(schema).__name__!r}"
            )
        return schema

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        violations: list[ContractViolation] = []

        # --- Missing required / type mismatch per FieldSpec ------------------
        for field_spec in contract.fields:
            if field_spec.path not in data:
                if field_spec.required:
                    violations.append(
                        ContractViolation(
                            field_path=field_spec.path,
                            violation_type=ViolationType.MISSING_REQUIRED_FIELD,
                            severity=ViolationSeverity.ERROR,
                            message=(f"Required field '{field_spec.path}' is missing."),
                            expected_type=field_spec.field_type,
                        )
                    )
            else:
                value = data[field_spec.path]
                if not _type_matches(value, field_spec.field_type):
                    violations.append(
                        ContractViolation(
                            field_path=field_spec.path,
                            violation_type=ViolationType.TYPE_MISMATCH,
                            severity=ViolationSeverity.ERROR,
                            message=(
                                f"Field '{field_spec.path}' type mismatch: "
                                f"expected {field_spec.field_type.value}, "
                                f"got {type(value).__name__}."
                            ),
                            expected_type=field_spec.field_type,
                            received_value=value,
                        )
                    )

        # --- Unexpected fields -----------------------------------------------
        known_paths = {f.path for f in contract.fields}
        for key in data:
            if key not in known_paths:
                severity = (
                    ViolationSeverity.ERROR if contract.strict_mode else ViolationSeverity.WARNING
                )
                violations.append(
                    ContractViolation(
                        field_path=key,
                        violation_type=ViolationType.UNEXPECTED_FIELD,
                        severity=severity,
                        message=f"Unexpected field '{key}' is not in the contract.",
                        received_value=data[key],
                    )
                )

        is_valid = not any(v.severity is ViolationSeverity.ERROR for v in violations)
        return ValidationResult(
            is_valid=is_valid,
            violations=violations,
            raw_input=dict(data),
            contract_id=contract.contract_id,
        )

    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """Return a copy of *data* — no framework rehydration needed."""
        return dict(data)


# ---------------------------------------------------------------------------
# MockRepairStrategy
# ---------------------------------------------------------------------------


class MockRepairStrategy(IRepairStrategy):
    """
    Configurable IRepairStrategy for engine unit tests.

    Behaviour is fully injectable via constructor parameters so that tests
    can exercise specific engine paths (no applicable strategy, confidence
    threshold rejection, no-progress detection, etc.) without needing real
    strategy implementations.

    Parameters
    ----------
    name:
        Strategy identifier, used in RepairAttempt.strategy_name.
    priority:
        Execution priority (lower = earlier).
    handle:
        Return value of ``can_handle``.  Set to ``False`` to simulate a
        strategy that declines all violation sets.
    operations:
        List of ``FieldOperation`` objects returned by ``propose``.
        Defaults to empty list (strategy proposes nothing).
    """

    def __init__(
        self,
        name: str = "MockStrategy",
        priority: int = 10,
        handle: bool = True,
        operations: list[FieldOperation] | None = None,
    ) -> None:
        self._name = name
        self._priority = priority
        self._handle = handle
        self._operations: list[FieldOperation] = operations or []

    @property
    def name(self) -> str:
        return self._name

    @property
    def priority(self) -> int:
        return self._priority

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return self._handle

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        return list(self._operations)


# ---------------------------------------------------------------------------
# CapturingTelemetryHook
# ---------------------------------------------------------------------------


class CapturingTelemetryHook:
    """
    ITelemetryHook that records every emitted event for assertion.

    Used in integration tests that verify the engine emits the correct
    telemetry sequence.

    Structurally satisfies ``ITelemetryHook`` (Protocol) without inheriting.
    """

    def __init__(self) -> None:
        self.events: list[TelemetryEvent] = []

    def emit(self, event: TelemetryEvent) -> None:
        self.events.append(event)

    def event_types(self) -> list[str]:
        """Return event_type.value for each captured event, in order."""
        return [e.event_type.value for e in self.events]

    def clear(self) -> None:
        self.events.clear()


# ---------------------------------------------------------------------------
# Pytest fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_adapter() -> MockContractAdapter:
    """A fresh MockContractAdapter instance."""
    return MockContractAdapter()


@pytest.fixture
def weather_contract() -> ContractSpec:
    """The canonical Weather contract: temperature (FLOAT) + humidity (INTEGER)."""
    return make_contract_spec(
        fields=[
            make_field_spec("temperature", FieldType.FLOAT),
            make_field_spec("humidity", FieldType.INTEGER),
        ]
    )


@pytest.fixture
def weather_valid_data() -> dict[str, Any]:
    """Valid data for the Weather contract."""
    return {"temperature": 31.5, "humidity": 80}


@pytest.fixture
def weather_drifted_data() -> dict[str, Any]:
    """Drifted data simulating a schema-renamed field (temp_celsius → temperature)."""
    return {"temp_celsius": 31.5, "humidity": 80}


@pytest.fixture
def capturing_telemetry() -> CapturingTelemetryHook:
    """A CapturingTelemetryHook with empty event history."""
    return CapturingTelemetryHook()


@pytest.fixture
def default_repair_config() -> RepairConfig:
    return RepairConfig()


@pytest.fixture
def default_guard_config() -> GuardConfig:
    return GuardConfig()

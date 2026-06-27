"""
Telemetry hook interface and event types.

Telemetry is disabled by default — the engine uses NoopTelemetry unless a
concrete hook is injected.  This module defines the contract that hook
implementations must satisfy.

This module has zero external dependencies and is part of Layer 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ITelemetryHook",
    "TelemetryEvent",
    "TelemetryEventType",
]


# ---------------------------------------------------------------------------
# TelemetryEventType
# ---------------------------------------------------------------------------


class TelemetryEventType(StrEnum):
    """
    Ordered lifecycle events emitted by the repair engine.

    For a successful repair the engine emits events in this sequence::

        VALIDATION_STARTED
        VIOLATION_DETECTED        (once per violation detected)
        REPAIR_STARTED
        STRATEGY_SELECTED         (once per strategy chosen for the attempt)
        REPAIR_ATTEMPT_STARTED
        OPERATION_APPLIED         (once per operation that met confidence threshold)
        OPERATION_REJECTED        (once per operation below confidence threshold)
        REVALIDATION_STARTED
        REPAIR_COMPLETED

    REPAIR_FAILED replaces REPAIR_COMPLETED when the engine gives up.
    """

    VALIDATION_STARTED = "validation_started"
    VIOLATION_DETECTED = "violation_detected"
    REPAIR_STARTED = "repair_started"
    STRATEGY_SELECTED = "strategy_selected"
    REPAIR_ATTEMPT_STARTED = "repair_attempt_started"
    OPERATION_APPLIED = "operation_applied"
    OPERATION_REJECTED = "operation_rejected"
    REVALIDATION_STARTED = "revalidation_started"
    REPAIR_COMPLETED = "repair_completed"
    REPAIR_FAILED = "repair_failed"


# ---------------------------------------------------------------------------
# TelemetryEvent
# ---------------------------------------------------------------------------


@dataclass
class TelemetryEvent:
    """
    A single event emitted by the repair engine to the telemetry hook.

    Attributes
    ----------
    event_type:
        Which lifecycle stage this event represents.
    contract_id:
        The ``ContractSpec.contract_id`` of the schema being repaired.
    data:
        Structured key/value payload — field paths, strategy names,
        confidence scores, etc.  No raw field *values* by default.
    emitted_at:
        UTC timestamp set automatically at construction.
    """

    event_type: TelemetryEventType
    contract_id: str
    data: dict[str, Any] = field(default_factory=dict)
    emitted_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )


# ---------------------------------------------------------------------------
# ITelemetryHook
# ---------------------------------------------------------------------------


@runtime_checkable
class ITelemetryHook(Protocol):
    """
    Structural protocol for telemetry hook implementations.

    Any object that provides ``emit(event: TelemetryEvent) -> None``
    satisfies this protocol.  Explicit inheritance from ``ITelemetryHook``
    is not required.

    Example
    -------
    ::

        class PrintHook:
            def emit(self, event: TelemetryEvent) -> None:
                print(event.event_type, event.contract_id)

        guard = ContractGuard.with_pydantic(telemetry=PrintHook())
    """

    def emit(self, event: TelemetryEvent) -> None:
        """Receive and process a telemetry event."""
        ...

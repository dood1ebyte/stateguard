"""
No-op telemetry implementation — the default used by ContractGuard.

StateGuard ships with telemetry disabled.  Passing ``NoopTelemetry()``
(or omitting the ``telemetry`` argument entirely) causes all engine events
to be silently discarded.  No data leaves the process.

To enable telemetry, implement ``ITelemetryHook`` and pass your instance
to ``ContractGuard`` or ``RepairEngine`` at construction time.
"""

from __future__ import annotations

from stateguard.telemetry.hooks import ITelemetryHook, TelemetryEvent

__all__ = ["NoopTelemetry"]


class NoopTelemetry:
    """
    Default telemetry hook that silently discards every event.

    Structurally satisfies ``ITelemetryHook`` without explicit inheritance,
    because Python's ``Protocol`` matching is structural.

    Example
    -------
    ::

        # These two are equivalent — NoopTelemetry is used in both cases.
        guard = ContractGuard.with_pydantic()
        guard = ContractGuard.with_pydantic(telemetry=NoopTelemetry())
    """

    def emit(self, event: TelemetryEvent) -> None:
        """Accept and silently discard *event*."""


def _assert_noop_satisfies_protocol() -> None:
    """
    Static assertion: NoopTelemetry must satisfy ITelemetryHook.

    This function is never called at runtime; it exists so that mypy
    validates the structural conformance at type-check time.
    """
    _hook: ITelemetryHook = NoopTelemetry()
    del _hook

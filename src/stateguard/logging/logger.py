"""
Structured repair audit logger.

RepairLogger is created once per repair invocation and passed to the engine.
Its entries are attached to RepairResult.repair_log after repair completes.

This module has zero external dependencies and is part of Layer 0.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

__all__ = [
    "LogLevel",
    "RepairLogEntry",
    "RepairLogger",
]


# ---------------------------------------------------------------------------
# LogLevel
# ---------------------------------------------------------------------------


class LogLevel(StrEnum):
    """Severity levels for repair log entries."""

    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


# ---------------------------------------------------------------------------
# RepairLogEntry
# ---------------------------------------------------------------------------


@dataclass
class RepairLogEntry:
    """
    A single structured entry in the repair audit log.

    Attributes
    ----------
    timestamp:
        UTC timestamp of when this entry was created.
    level:
        Severity of the log entry.
    event:
        Machine-readable event name using dot-notation
        (e.g. ``"strategy.applied"``, ``"repair.complete"``).
        Stable across versions — downstream consumers may key on this.
    message:
        Human-readable description for display and debugging.
    data:
        Structured key/value context attached to the event.
        Contains field *names*, strategy names, attempt numbers, etc.

        .. caution::
           Field *values* from the runtime data are **not** included by
           default.  They are added only when
           ``RepairConfig.include_values_in_log`` is ``True``, to prevent
           accidental exposure of sensitive data in log pipelines.
    """

    timestamp: datetime
    level: LogLevel
    event: str
    message: str
    data: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# RepairLogger
# ---------------------------------------------------------------------------


class RepairLogger:
    """
    Collects ``RepairLogEntry`` objects during a single repair session.

    The engine calls ``debug``/``info``/``warning``/``error`` methods as it
    processes each phase.  After the repair loop exits, the engine passes
    ``logger.entries`` to ``RepairResult.repair_log``.
    """

    def __init__(self) -> None:
        self._entries: list[RepairLogEntry] = []

    @property
    def entries(self) -> list[RepairLogEntry]:
        """Return a snapshot copy of all entries recorded so far."""
        return list(self._entries)

    def _record(
        self,
        level: LogLevel,
        event: str,
        message: str,
        **data: Any,
    ) -> None:
        self._entries.append(
            RepairLogEntry(
                timestamp=datetime.now(tz=UTC),
                level=level,
                event=event,
                message=message,
                data=dict(data),
            )
        )

    def debug(self, event: str, message: str, **data: Any) -> None:
        self._record(LogLevel.DEBUG, event, message, **data)

    def info(self, event: str, message: str, **data: Any) -> None:
        self._record(LogLevel.INFO, event, message, **data)

    def warning(self, event: str, message: str, **data: Any) -> None:
        self._record(LogLevel.WARNING, event, message, **data)

    def error(self, event: str, message: str, **data: Any) -> None:
        self._record(LogLevel.ERROR, event, message, **data)

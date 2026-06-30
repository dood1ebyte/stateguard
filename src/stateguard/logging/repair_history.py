"""
RepairHistoryRecorder — optional, local, append-only JSONL repair history.

This is a *persistence* mechanism, distinct from ``RepairLogger``
(``stateguard.logging.logger``), which produces the in-memory audit trail
attached to ``RepairResult.repair_log`` for a single repair call.
``RepairHistoryRecorder`` instead writes a durable record of repair
outcomes to a local file across many calls and many process invocations,
so an engineer can answer "what has StateGuard been doing to my data over
the last week?" without wiring up any external service.

Design constraints (per product requirements)
-----------------------------------------------
* **Local only.** Appends to a plain file on disk
  (default ``~/.stateguard/repairs.jsonl``). No network calls, no external
  services, no telemetry uploads of any kind.
* **Append-only JSONL.** One JSON object per line. Safe to `tail -f`,
  safe to parse incrementally, safe to grep.
* **Fully optional.** ``ContractGuard`` does not use this unless a
  ``RepairHistoryRecorder`` instance is explicitly passed to its
  constructor — mirroring the existing ``NoopTelemetry``-by-default
  pattern for the telemetry hook.
* **Never breaks repairs.** Every public method swallows its own
  exceptions (filesystem permission errors, disk full, missing parent
  directory that can't be created, non-JSON-serializable values, etc.)
  and returns a boolean success indicator rather than raising. A failure
  to log is observable via the return value but must never propagate out
  of ``ContractGuard.repair()``.

Record granularity
-------------------
One JSONL line is written per **applied** ``FieldOperation``, across every
``RepairAttempt`` in a single ``repair()`` call -- this is the natural
"one repair event" unit, since each operation carries its own strategy,
field path, confidence, and before/after values. For the case where no
attempts occurred at all (``ALREADY_VALID`` or an immediate ``FAILED``
with no applicable strategy), exactly one summary record is written
instead, with the operation-specific fields set to ``None``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    # Deferred to type-checking only to avoid a circular import:
    # stateguard.core.errors.results imports stateguard.logging.logger,
    # which (via the stateguard.logging package __init__) would otherwise
    # trigger this module to import back from stateguard.core.errors.results
    # at package-load time. `from __future__ import annotations` makes all
    # annotations below lazy strings, so this import is never needed at
    # runtime -- only by static type checkers.
    from stateguard.core.errors.operations import FieldOperation
    from stateguard.core.errors.results import RepairAttempt, RepairResult
    from stateguard.core.errors.violations import ContractViolation

__all__ = ["RepairHistoryRecorder"]


# ---------------------------------------------------------------------------
# Default location
# ---------------------------------------------------------------------------

DEFAULT_HISTORY_PATH = Path.home() / ".stateguard" / "repairs.jsonl"


# ---------------------------------------------------------------------------
# Path helper (private to this module; mirrors the same small dotted-path
# getter duplicated across strategy modules and engine.py, per existing
# codebase convention of each module owning its own copy rather than
# importing another module's private helper).
# ---------------------------------------------------------------------------


class _NotFound:
    """Sentinel distinguishing 'path does not exist' from a value of None."""

    def __repr__(self) -> str:
        return "NOT_FOUND"


_NOT_FOUND = _NotFound()


def _get_nested_value(data: dict[str, Any], path: str) -> Any:
    """Navigate *data* via dot-notation *path*; return ``_NOT_FOUND`` if absent."""
    if not path:
        return _NOT_FOUND
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _NOT_FOUND
        current = current[part]
    return current


def _violation_type_for_path(
    violations: list[ContractViolation],
    field_path: str,
) -> str | None:
    """
    Best-effort lookup of the ``ViolationType`` associated with *field_path*
    among *violations*, returning its ``.value`` string or ``None`` if no
    violation matches.

    Used to recover "violation type" for a history record, since
    ``FieldOperation`` itself does not carry a direct reference to the
    violation(s) that motivated it -- only ``RepairAttempt.violations_targeted``
    (a list of violation IDs) does, indirectly.
    """
    for v in violations:
        if v.field_path == field_path:
            return v.violation_type.value
    return None


# ---------------------------------------------------------------------------
# RepairHistoryRecorder
# ---------------------------------------------------------------------------


class RepairHistoryRecorder:
    """
    Appends repair-event records to a local JSONL file.

    Parameters
    ----------
    path:
        File to append to. Defaults to ``~/.stateguard/repairs.jsonl``.
        Parent directories are created on first successful write if they
        do not already exist.
    enabled:
        If ``False``, every call to ``record`` is a no-op that returns
        ``True`` immediately without touching the filesystem. Useful for
        toggling history recording off without restructuring caller code
        (e.g. via an environment variable or CLI flag).

    Thread / process safety
    ------------------------
    Each ``record`` call opens the file in append mode, writes one or more
    complete lines, and closes it. On POSIX systems, ``open(..., "a")``
    appends are atomic for writes smaller than the OS pipe buffer size,
    so concurrent single-process-at-a-time CLI usage is safe. This class
    does not implement cross-process locking; high-concurrency server
    deployments wanting strict interleaving guarantees should write to
    separate per-process files or use an external log aggregator instead.
    """

    def __init__(
        self,
        path: Path | str | None = None,
        enabled: bool = True,
    ) -> None:
        self._path = Path(path) if path is not None else DEFAULT_HISTORY_PATH
        self._enabled = enabled

    @property
    def path(self) -> Path:
        """The file this recorder appends to."""
        return self._path

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def record(self, result: RepairResult) -> bool:
        """
        Append history record(s) for *result* to the configured file.

        Returns
        -------
        bool
            ``True`` if the record(s) were written successfully (or if
            this recorder is disabled, in which case nothing is written
            but this still counts as success). ``False`` if any
            filesystem or serialization error occurred -- the error
            itself is swallowed; this method never raises.
        """
        if not self._enabled:
            return True

        try:
            lines = self._build_lines(result)
            self._append_lines(lines)
            return True
        except Exception:
            # Logging must never break a repair. Any failure here --
            # permission denied, disk full, unrepresentable value, a
            # read-only filesystem, etc. -- is swallowed.
            return False

    # ------------------------------------------------------------------
    # Internal: building records
    # ------------------------------------------------------------------

    def _build_lines(self, result: RepairResult) -> list[str]:
        """Build the JSONL line(s) for *result*, without writing anything."""
        records = self._build_records(result)
        lines = []
        for record in records:
            # default=str gracefully degrades any value that isn't natively
            # JSON-serializable (e.g. a datetime, a custom object) into its
            # string representation rather than raising.
            lines.append(json.dumps(record, default=str))
        return lines

    def _build_records(self, result: RepairResult) -> list[dict[str, Any]]:
        """Build one dict per applied operation, or one summary dict if
        there were no attempts at all."""
        timestamp = datetime.now(UTC).isoformat()
        base = {
            "timestamp": timestamp,
            "contract_id": result.contract_id,
            "status": result.status.value,
        }

        if not result.attempts:
            return [
                {
                    **base,
                    "strategy": None,
                    "violation_type": None,
                    "field_path": None,
                    "field_before": None,
                    "field_after": None,
                    "confidence": None,
                    "success": result.status.value in ("success", "already_valid"),
                    "attempt_number": None,
                    "op_type": None,
                }
            ]

        records: list[dict[str, Any]] = []
        for attempt in result.attempts:
            for op in attempt.applied_operations:
                records.append(self._build_operation_record(base, result, attempt, op))
        return records

    def _build_operation_record(
        self,
        base: dict[str, Any],
        result: RepairResult,
        attempt: RepairAttempt,
        op: FieldOperation,
    ) -> dict[str, Any]:
        before_path = op.source_path if op.source_path is not None else op.target_path
        field_before = _get_nested_value(attempt.data_before, before_path)
        field_after = _get_nested_value(attempt.data_after, op.target_path)

        violation_type = _violation_type_for_path(result.initial_violations, op.target_path)
        if violation_type is None and op.source_path is not None:
            violation_type = _violation_type_for_path(result.initial_violations, op.source_path)

        return {
            **base,
            "strategy": attempt.strategy_name,
            "violation_type": violation_type,
            "field_path": op.target_path,
            "field_before": None if field_before is _NOT_FOUND else field_before,
            "field_after": None if field_after is _NOT_FOUND else field_after,
            "confidence": op.confidence,
            "success": attempt.succeeded,
            "attempt_number": attempt.attempt_number,
            "op_type": op.op_type.value,
        }

    # ------------------------------------------------------------------
    # Internal: filesystem
    # ------------------------------------------------------------------

    def _append_lines(self, lines: list[str]) -> None:
        """Create parent directories if needed, then append *lines*."""
        if not lines:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._path, "a", encoding="utf-8") as f:
            for line in lines:
                f.write(line)
                f.write("\n")

    # ------------------------------------------------------------------
    # Read-back helper (primarily for tests and CLI inspection tooling)
    # ------------------------------------------------------------------

    def read_all(self) -> list[dict[str, Any]]:
        """
        Read and parse every record currently in the history file.

        Returns an empty list if the file does not exist or cannot be
        read. Malformed individual lines are skipped rather than raising.
        Intended for diagnostics and tests, not for performance-critical
        paths -- it loads the entire file into memory.
        """
        try:
            if not self._path.exists():
                return []
            records: list[dict[str, Any]] = []
            with open(self._path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        records.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue
            return records
        except Exception:
            return []

"""
Repair result types: the complete output model of a repair session.

These dataclasses carry the full audit trail from a single
``ContractGuard.repair()`` invocation: what was found, what was attempted,
what was applied, and what remains unresolved.

Layer 1 — depends on:
  stateguard.core.errors.violations
  stateguard.core.errors.operations
  stateguard.logging.logger
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from stateguard.core.errors.operations import FieldOperation
from stateguard.core.errors.violations import ContractViolation
from stateguard.logging.logger import RepairLogEntry

__all__ = [
    "RepairAttempt",
    "RepairResult",
    "RepairStatus",
    "ValidationResult",
]


# ---------------------------------------------------------------------------
# RepairStatus
# ---------------------------------------------------------------------------


class RepairStatus(StrEnum):
    """
    Terminal state of a repair session.

    Members
    -------
    SUCCESS:
        All violations were resolved.  ``RepairResult.repaired_output`` is
        a non-``None`` dict that passes full contract validation.
    PARTIAL:
        At least one violation was resolved, but some remain.
        ``RepairResult.repaired_output`` is set when
        ``RepairConfig.allow_partial_repair`` is ``True``; ``None`` when
        ``False``.
    FAILED:
        No violations were resolved (no applicable strategy, max attempts
        exhausted, no-progress detected, or regression introduced).
        ``RepairResult.repaired_output`` is always ``None``.
    ALREADY_VALID:
        The input data passed contract validation without any repair.
        The engine exits immediately; ``RepairResult.attempts`` is empty.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    ALREADY_VALID = "already_valid"


# ---------------------------------------------------------------------------
# ValidationResult
# ---------------------------------------------------------------------------


@dataclass
class ValidationResult:
    """
    The output of a single validation pass (initial or revalidation).

    Produced by ``IContractAdapter.validate()`` — which uses the framework's
    own validator — and also by ``ContractValidator`` for initial violation
    analysis.

    Attributes
    ----------
    is_valid:
        ``True`` if the data satisfies the contract with no ERROR-severity
        violations.  WARNING violations do not set this to ``False``.
    violations:
        All violations detected in this pass.  Empty when ``is_valid`` is
        ``True``.
    raw_input:
        A snapshot of the data dict that was validated.  The engine stores
        a deep copy; callers must not mutate this after construction.
    contract_id:
        The ``ContractSpec.contract_id`` against which this validation ran.
    validated_at:
        UTC timestamp, auto-set at construction.
    """

    is_valid: bool
    violations: list[ContractViolation]
    raw_input: dict[str, Any]
    contract_id: str
    validated_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )


# ---------------------------------------------------------------------------
# RepairAttempt
# ---------------------------------------------------------------------------


@dataclass
class RepairAttempt:
    """
    Records a single iteration of the repair loop.

    One ``RepairAttempt`` is created per strategy application.  If the engine
    runs three iterations it produces three ``RepairAttempt`` objects in
    ``RepairResult.attempts``.

    Attributes
    ----------
    attempt_number:
        1-indexed position of this attempt within the repair session.
    strategy_name:
        ``IRepairStrategy.name`` of the strategy that was executed.
    violations_targeted:
        ``violation_id`` values of the violations this strategy addressed.
    proposed_operations:
        All ``FieldOperation`` objects returned by the strategy's
        ``propose()`` method.
    applied_operations:
        Subset of ``proposed_operations`` where
        ``confidence >= RepairConfig.min_confidence_threshold``.
    rejected_operations:
        Subset of ``proposed_operations`` that fell below the threshold.
    data_before:
        Deep copy of the working data dict *before* operations were applied.
    data_after:
        Deep copy of the working data dict *after* operations were applied.
    succeeded:
        ``True`` if revalidation after this attempt found no remaining
        ERROR-severity violations.
    attempt_id:
        UUID4 string, auto-generated at construction.
    attempted_at:
        UTC timestamp, auto-set at construction.
    """

    # Required fields (no defaults)
    attempt_number: int
    strategy_name: str
    violations_targeted: list[str]
    proposed_operations: list[FieldOperation]
    applied_operations: list[FieldOperation]
    rejected_operations: list[FieldOperation]
    data_before: dict[str, Any]
    data_after: dict[str, Any]
    succeeded: bool

    # Auto-generated fields
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    attempted_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )


# ---------------------------------------------------------------------------
# RepairResult
# ---------------------------------------------------------------------------


@dataclass
class RepairResult:
    """
    The complete output of a ``ContractGuard.repair()`` invocation.

    Carries everything needed to understand what happened: the final status,
    the repaired data (if available), the full violation inventory, every
    repair attempt with its operations and before/after snapshots, and the
    structured audit log.

    Attributes
    ----------
    status:
        Terminal state of the repair session.
    original_input:
        Deep copy of the data dict as received before any repair.
        Never mutated by the engine.
    initial_violations:
        All violations found in the first validation pass, before any repair.
    remaining_violations:
        Violations that were not resolved.  Empty on ``SUCCESS``.
        Non-empty on ``PARTIAL`` and ``FAILED``.
    attempts:
        Ordered list of repair attempts.  Empty on ``ALREADY_VALID``.
    repair_log:
        Structured audit log entries from the engine.
    contract_id:
        The ``ContractSpec.contract_id`` that was validated against.
    repaired_output:
        The repaired data dict, or ``None``.

        * ``SUCCESS``       → non-``None`` dict that passes full validation.
        * ``PARTIAL``       → non-``None`` when ``allow_partial_repair=True``;
                              ``None`` when ``allow_partial_repair=False``.
        * ``FAILED``        → always ``None``.
        * ``ALREADY_VALID`` → the original input (no repair was needed).
    repaired_at:
        UTC timestamp, auto-set at construction.

    Convenience properties
    ----------------------
    ``is_success``, ``is_partial``, ``is_failed``, ``is_already_valid`` —
    boolean shorthands for the four ``RepairStatus`` values.
    """

    # Required
    status: RepairStatus
    original_input: dict[str, Any]
    initial_violations: list[ContractViolation]
    remaining_violations: list[ContractViolation]
    attempts: list[RepairAttempt]
    repair_log: list[RepairLogEntry]
    contract_id: str

    # Optional / auto-generated
    repaired_output: dict[str, Any] | None = None
    repaired_at: datetime = field(
        default_factory=lambda: datetime.now(tz=UTC)
    )

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def is_success(self) -> bool:
        """``True`` when ``status`` is ``RepairStatus.SUCCESS``."""
        return self.status is RepairStatus.SUCCESS

    @property
    def is_partial(self) -> bool:
        """``True`` when ``status`` is ``RepairStatus.PARTIAL``."""
        return self.status is RepairStatus.PARTIAL

    @property
    def is_failed(self) -> bool:
        """``True`` when ``status`` is ``RepairStatus.FAILED``."""
        return self.status is RepairStatus.FAILED

    @property
    def is_already_valid(self) -> bool:
        """``True`` when ``status`` is ``RepairStatus.ALREADY_VALID``."""
        return self.status is RepairStatus.ALREADY_VALID

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
    AMBIGUOUS:
        A repair *was* found, but the evidence did not justify applying it
        unsupervised, and nothing else resolved the violation either.
        Distinct from ``FAILED``, which means no repair was found at all —
        the difference matters because an ambiguous result is actionable:
        ``RepairResult.ambiguous`` carries the candidates so a caller can
        re-prompt, a reviewer can choose, or Shadow Mode can display them.
    """

    SUCCESS = "success"
    PARTIAL = "partial"
    FAILED = "failed"
    ALREADY_VALID = "already_valid"
    AMBIGUOUS = "ambiguous"


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
        A snapshot of the data that was validated.  Normally a ``dict``, but
        typed ``Any`` because a payload whose root is not an object is
        reported rather than rejected at the boundary -- see
        ``stateguard.core.validator.root_structural_violation``.  Callers
        must not mutate this after construction.
    contract_id:
        The ``ContractSpec.contract_id`` against which this validation ran.
    validated_at:
        UTC timestamp, auto-set at construction.
    """

    is_valid: bool
    violations: list[ContractViolation]
    raw_input: Any
    contract_id: str
    validated_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


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
        Subset of ``proposed_operations`` that ``TrustPolicy`` cleared for
        application *and* that actually changed the payload.
    rejected_operations:
        Subset of ``proposed_operations`` whose evidence was too weak to
        consider a repair at all, plus any that turned out to change nothing
        when applied.
    abstained_operations:
        Subset of ``proposed_operations`` that ``TrustPolicy`` withheld -- a
        real repair was found, but not confidently enough to apply it
        unsupervised.  Without this an abstained operation appeared in
        neither list and the attempt read as "the strategy did nothing".
        Also collected across the whole run on ``RepairResult.ambiguous``.
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

    # Optional / auto-generated fields
    abstained_operations: list[FieldOperation] = field(default_factory=list)
    attempt_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    attempted_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))


# ---------------------------------------------------------------------------
# AmbiguousRepair
# ---------------------------------------------------------------------------


@dataclass
class AmbiguousRepair:
    """
    A repair the engine found but declined to apply on its own authority.

    This is the abstain region of the trust model made visible.  A binary
    apply/reject threshold has to force every borderline case to one side, so
    it either guesses or silently drops the proposal — and the caller learns
    nothing either way.  Surfacing the candidates instead turns "I'm not sure"
    into something a caller can act on.

    Attributes
    ----------
    target_path:
        The field the proposed repair would have written.
    candidates:
        The operations considered, ranked by ``trust`` descending.  Usually
        one; more when several strategies proposed competing fixes.
    reason:
        Why none was applied — the trust achieved against the bar its risk
        tier requires.
    """

    target_path: str
    candidates: list[FieldOperation]
    reason: str

    @property
    def best(self) -> FieldOperation | None:
        """The highest-trust candidate, or ``None`` if there were none."""
        return self.candidates[0] if self.candidates else None


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
        Deep copy of the data exactly as received, before any repair --
        including the root-shape normalisation the engine applies to a
        JSON-string or single-element-sequence payload.  Typed ``Any``
        because that received value is not necessarily a ``dict``.
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
    original_input: Any
    initial_violations: list[ContractViolation]
    remaining_violations: list[ContractViolation]
    attempts: list[RepairAttempt]
    repair_log: list[RepairLogEntry]
    contract_id: str

    # Optional / auto-generated
    repaired_output: dict[str, Any] | None = None
    ambiguous: list[AmbiguousRepair] = field(default_factory=list)
    repaired_at: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

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

    @property
    def is_ambiguous(self) -> bool:
        """``True`` when ``status`` is ``RepairStatus.AMBIGUOUS``."""
        return self.status is RepairStatus.AMBIGUOUS

    @property
    def has_ambiguous_repairs(self) -> bool:
        """
        ``True`` when any proposal landed in the abstain band.

        Distinct from :attr:`is_ambiguous`: a run can abstain on one field and
        still repair another, ending ``SUCCESS`` or ``PARTIAL`` while carrying
        candidates worth surfacing.
        """
        return bool(self.ambiguous)

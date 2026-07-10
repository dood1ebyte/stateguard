"""
RepairEngine — orchestrates the repair loop.

This is the heart of the core engine: it correlates violations, selects
strategies via the ``StrategyRegistry``, applies proposed ``FieldOperation``
objects that meet the confidence threshold, revalidates via the adapter,
and assembles the final ``RepairResult`` with a full audit trail.

Validation strategy
--------------------
Both the adapter's native validator (``IContractAdapter.validate``) and the
framework-agnostic ``ContractValidator`` are consulted for every validation
pass (initial and revalidation), and their violations are merged:

* ``adapter.validate`` is the **source of truth for correctness** — its
  ``is_valid`` flag (and any ERROR-severity violations it reports) determine
  whether the data is acceptable to the underlying framework.
* ``ContractValidator`` fills in violation types the adapter may not surface
  — most importantly ``UNEXPECTED_FIELD``, which most framework validators
  (e.g. Pydantic without ``extra="forbid"``) do not report at all. Without
  these, ``ExactAliasStrategy`` and ``FuzzyFieldMatchStrategy`` would never
  have a MISSING/UNEXPECTED pair to correlate and could never fire.

Merging is by ``(field_path, violation_type)`` signature: adapter violations
are kept as-is, and any ``ContractValidator`` violation with a signature not
already present is appended. ``is_valid`` is
``adapter_result.is_valid and not <any ERROR-severity violation contributed
only by ContractValidator>``.

Zero external dependencies — part of Layer 5 (depends on Layers 0-4:
models, errors, interfaces, strategies, validator).
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import (
    RepairAttempt,
    RepairResult,
    RepairStatus,
    ValidationResult,
)
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.config import RepairConfig
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType, UnionMember
from stateguard.core.strategies.coerce import _array_wrap_is_safe, resolve_union_member
from stateguard.core.strategies.registry import StrategyRegistry
from stateguard.core.validator import ContractValidator
from stateguard.logging.logger import RepairLogger
from stateguard.telemetry.hooks import ITelemetryHook, TelemetryEvent, TelemetryEventType
from stateguard.telemetry.noop import NoopTelemetry

__all__ = ["RepairEngine"]


# ---------------------------------------------------------------------------
# Path navigation helpers (private to this module)
# ---------------------------------------------------------------------------


class _NotFound:
    """Sentinel distinguishing 'path does not exist' from a value of None."""

    def __repr__(self) -> str:
        return "NOT_FOUND"


_NOT_FOUND = _NotFound()


class _CoerceFailed:
    """Sentinel returned by ``_coerce_value`` when no cast is possible."""

    def __repr__(self) -> str:
        return "COERCE_FAILED"


_COERCE_FAILED = _CoerceFailed()


def _get_nested(data: dict[str, Any], path: str) -> Any:
    """Return the value at dot-notation *path* in *data*, or ``_NOT_FOUND``."""
    parts = path.split(".")
    current: Any = data
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return _NOT_FOUND
        current = current[part]
    if not isinstance(current, dict) or parts[-1] not in current:
        return _NOT_FOUND
    return current[parts[-1]]


def _set_nested(data: dict[str, Any], path: str, value: Any) -> None:
    """
    Set *value* at dot-notation *path* in *data*, creating intermediate
    dicts as needed.
    """
    parts = path.split(".")
    current: dict[str, Any] = data
    for part in parts[:-1]:
        existing = current.get(part)
        if not isinstance(existing, dict):
            existing = {}
            current[part] = existing
        current = existing
    current[parts[-1]] = value


def _delete_nested(data: dict[str, Any], path: str) -> None:
    """Delete the key at dot-notation *path* in *data*, if present."""
    parts = path.split(".")
    current: Any = data
    for part in parts[:-1]:
        if not isinstance(current, dict) or part not in current:
            return
        current = current[part]
    if isinstance(current, dict) and parts[-1] in current:
        del current[parts[-1]]


def _find_field_spec(contract: ContractSpec, full_path: str) -> FieldSpec | None:
    """
    Locate the ``FieldSpec`` for a dot-notation *full_path* within *contract*,
    recursing into ``nested_spec`` for nested paths.

    Used by ``_apply_coerce`` to determine the declared ``FieldType`` for a
    ``COERCE`` operation's target.
    """
    local, _, rest = full_path.partition(".")
    for field_spec in contract.fields:
        if field_spec.path == local:
            if not rest:
                return field_spec
            if field_spec.nested_spec is not None:
                return _find_field_spec(field_spec.nested_spec, rest)
            return None
    return None


def _coerce_value(
    value: Any,
    target_type: FieldType,
    item_type: FieldType | None = None,
    union_members: tuple[UnionMember, ...] | None = None,
) -> Any:
    """
    Cast *value* to *target_type*, returning ``_COERCE_FAILED`` if no
    supported cast applies.

    Mirrors the feasibility checks in
    ``stateguard.core.strategies.coerce``: only the casts that
    ``TypeCoercionStrategy`` proposes are performed here.  ``ARRAY``
    targets wrap the value in a single-element list; ``UNION`` targets
    delegate member selection to ``resolve_union_member`` so that
    application picks the same member the strategy's feasibility check
    did.
    """
    if target_type is FieldType.INTEGER:
        if isinstance(value, str) and not isinstance(value, bool):
            try:
                return int(value)
            except ValueError:
                return _COERCE_FAILED
        return _COERCE_FAILED

    if target_type is FieldType.FLOAT:
        if isinstance(value, bool):
            return _COERCE_FAILED
        if isinstance(value, (int, str)):
            try:
                return float(value)
            except ValueError:
                return _COERCE_FAILED
        return _COERCE_FAILED

    if target_type is FieldType.BOOLEAN:
        if isinstance(value, str):
            lowered = value.strip().lower()
            if lowered in ("true", "1"):
                return True
            if lowered in ("false", "0"):
                return False
        return _COERCE_FAILED

    if target_type is FieldType.ARRAY:
        if _array_wrap_is_safe(value, item_type):
            return [value]
        return _COERCE_FAILED

    if target_type is FieldType.UNION:
        resolved = resolve_union_member(value, union_members)
        if resolved is None:
            return _COERCE_FAILED
        member, _confidence = resolved
        return _coerce_value(value, member.field_type, item_type=member.item_type)

    return _COERCE_FAILED


# ---------------------------------------------------------------------------
# RepairEngine
# ---------------------------------------------------------------------------


class RepairEngine:
    """
    Executes the repair loop: validate -> correlate -> select strategy ->
    apply -> revalidate -> repeat or terminate.

    Parameters
    ----------
    registry:
        Ordered collection of repair strategies.
    config:
        Repair behaviour configuration (thresholds, max attempts, etc.).
    logger:
        Structured audit logger.  Its accumulated entries become
        ``RepairResult.repair_log``.
    telemetry:
        Optional telemetry hook.  Defaults to ``NoopTelemetry`` (disabled).

    One ``RepairEngine`` instance is intended for a single ``repair()``
    invocation's ``logger`` lifetime — construct a fresh ``RepairLogger``
    per call if reusing an engine instance across repairs.
    """

    def __init__(
        self,
        registry: StrategyRegistry,
        config: RepairConfig,
        logger: RepairLogger,
        telemetry: ITelemetryHook | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._logger = logger
        self._telemetry: ITelemetryHook = telemetry if telemetry is not None else NoopTelemetry()
        self._core_validator = ContractValidator()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def repair(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
        adapter: IContractAdapter,
    ) -> RepairResult:
        """
        Run the repair loop for *data* against *contract* using *adapter*.

        Parameters
        ----------
        contract:
            Normalised contract to repair against.
        data:
            Input data.  Never mutated — a deep copy is made immediately.
        adapter:
            Framework adapter.  Used together with ``ContractValidator``
            for both initial validation and revalidation — see the module
            docstring for the merge semantics.

        Returns
        -------
        RepairResult
        """
        original_input = deepcopy(data)
        working_data = deepcopy(data)

        # --- Initial validation ----------------------------------------------
        self._emit(contract, TelemetryEventType.VALIDATION_STARTED)
        initial_result = self._validate(contract, working_data, adapter)

        for violation in initial_result.violations:
            self._emit(
                contract,
                TelemetryEventType.VIOLATION_DETECTED,
                field_path=violation.field_path,
                violation_type=violation.violation_type.value,
                severity=violation.severity.value,
            )
            self._logger.info(
                "violation.detected",
                f"Detected {violation.violation_type.value} at '{violation.field_path}'.",
                field_path=violation.field_path,
                violation_type=violation.violation_type.value,
                severity=violation.severity.value,
            )

        if initial_result.is_valid:
            self._logger.info(
                "validation.already_valid",
                "Input data already satisfies the contract; no repair needed.",
            )
            self._emit(
                contract,
                TelemetryEventType.REPAIR_COMPLETED,
                status=RepairStatus.ALREADY_VALID.value,
                attempts=0,
            )
            return RepairResult(
                status=RepairStatus.ALREADY_VALID,
                original_input=original_input,
                initial_violations=list(initial_result.violations),
                remaining_violations=list(initial_result.violations),
                attempts=[],
                repair_log=self._logger.entries,
                contract_id=contract.contract_id,
                repaired_output=deepcopy(original_input),
            )

        initial_violations = list(initial_result.violations)
        initial_signatures = {self._violation_signature(v) for v in initial_violations}
        initial_error_count = sum(1 for v in initial_violations if v.severity.value == "error")

        self._emit(contract, TelemetryEventType.REPAIR_STARTED)
        self._logger.info(
            "repair.started",
            f"Starting repair loop with {len(initial_violations)} violation(s) detected.",
            violation_count=len(initial_violations),
        )

        current_violations = initial_violations
        previous_hash = self._compute_violation_hash(current_violations)
        attempts: list[RepairAttempt] = []

        status: RepairStatus | None = None
        remaining_violations: list[ContractViolation] = current_violations

        for attempt_number in range(1, self._config.max_attempts + 1):
            correlated = self._correlate_violations(current_violations)

            applicable = self._registry.get_applicable(correlated, contract, working_data)
            if not applicable:
                self._logger.warning(
                    "strategy.none_applicable",
                    "No registered strategy can handle the remaining violations.",
                    attempt_number=attempt_number,
                )
                remaining_violations = correlated
                break

            strategy = applicable[0]
            self._emit(
                contract,
                TelemetryEventType.STRATEGY_SELECTED,
                strategy=strategy.name,
                attempt_number=attempt_number,
            )
            self._logger.info(
                "strategy.selected",
                f"Selected strategy '{strategy.name}' for attempt {attempt_number}.",
                strategy=strategy.name,
                attempt_number=attempt_number,
            )

            self._emit(
                contract,
                TelemetryEventType.REPAIR_ATTEMPT_STARTED,
                attempt_number=attempt_number,
                strategy=strategy.name,
            )

            proposed = strategy.propose(correlated, contract, working_data)

            applied_ops: list[FieldOperation] = []
            rejected_ops: list[FieldOperation] = []
            for op in proposed:
                if op.confidence >= self._config.min_confidence_threshold:
                    applied_ops.append(op)
                else:
                    rejected_ops.append(op)
                    self._emit(
                        contract,
                        TelemetryEventType.OPERATION_REJECTED,
                        op_type=op.op_type.value,
                        target_path=op.target_path,
                        confidence=op.confidence,
                    )
                    self._logger.warning(
                        "operation.rejected",
                        f"Rejected {op.op_type.value} on "
                        f"'{op.target_path}': confidence "
                        f"{op.confidence:.2f} below threshold "
                        f"{self._config.min_confidence_threshold:.2f}.",
                        op_type=op.op_type.value,
                        target_path=op.target_path,
                        confidence=op.confidence,
                        rationale=op.rationale,
                    )

            data_before = deepcopy(working_data)
            new_data = deepcopy(working_data)

            for op in applied_ops:
                self._apply_operation(new_data, op, contract)
                self._emit(
                    contract,
                    TelemetryEventType.OPERATION_APPLIED,
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    confidence=op.confidence,
                )
                self._logger.info(
                    "operation.applied",
                    f"Applied {op.op_type.value} on '{op.target_path}' "
                    f"(confidence {op.confidence:.2f}).",
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    source_path=op.source_path,
                    confidence=op.confidence,
                    rationale=op.rationale,
                )

            data_after = deepcopy(new_data)

            self._emit(contract, TelemetryEventType.REVALIDATION_STARTED)
            revalidation: ValidationResult = self._validate(contract, new_data, adapter)

            attempt_succeeded = revalidation.is_valid
            attempts.append(
                RepairAttempt(
                    attempt_number=attempt_number,
                    strategy_name=strategy.name,
                    violations_targeted=[v.violation_id for v in correlated],
                    proposed_operations=proposed,
                    applied_operations=applied_ops,
                    rejected_operations=rejected_ops,
                    data_before=data_before,
                    data_after=data_after,
                    succeeded=attempt_succeeded,
                )
            )

            if revalidation.is_valid:
                working_data = new_data
                remaining_violations = []
                status = RepairStatus.SUCCESS
                self._logger.info(
                    "repair.succeeded",
                    f"Repair succeeded after {attempt_number} attempt(s).",
                    attempt_number=attempt_number,
                )
                break

            # --- Regression check --------------------------------------------
            new_signatures = {self._violation_signature(v) for v in revalidation.violations}
            if not new_signatures.issubset(initial_signatures):
                self._logger.error(
                    "repair.regression_detected",
                    f"Attempt {attempt_number} ('{strategy.name}') "
                    f"introduced new violations not present in the "
                    f"original input. Aborting repair.",
                    attempt_number=attempt_number,
                    strategy=strategy.name,
                )
                remaining_violations = revalidation.violations
                status = RepairStatus.FAILED
                break

            # --- No-progress check ---------------------------------------------
            new_hash = self._compute_violation_hash(revalidation.violations)
            if new_hash == previous_hash:
                self._logger.warning(
                    "repair.no_progress",
                    f"Attempt {attempt_number} ('{strategy.name}') made "
                    f"no progress; violation set unchanged.",
                    attempt_number=attempt_number,
                    strategy=strategy.name,
                )
                remaining_violations = revalidation.violations
                break

            # --- Progress made; continue looping --------------------------------
            working_data = new_data
            current_violations = revalidation.violations
            remaining_violations = current_violations
            previous_hash = new_hash

        else:
            # for/else: loop exhausted max_attempts without break.
            self._logger.warning(
                "repair.max_attempts_exhausted",
                f"Reached max_attempts ({self._config.max_attempts}) without full repair.",
                max_attempts=self._config.max_attempts,
            )

        # --- Determine final status if not already SUCCESS/FAILED -------------
        if status is None:
            remaining_error_count = sum(
                1 for v in remaining_violations if v.severity.value == "error"
            )
            if remaining_error_count == 0:
                status = RepairStatus.SUCCESS
            elif remaining_error_count < initial_error_count:
                status = (
                    RepairStatus.PARTIAL
                    if self._config.allow_partial_repair
                    else RepairStatus.FAILED
                )
            else:
                status = RepairStatus.FAILED

        # --- Determine repaired_output ------------------------------------------
        if status is RepairStatus.SUCCESS:
            repaired_output: dict[str, Any] | None = deepcopy(working_data)
        elif status is RepairStatus.PARTIAL:
            # PARTIAL only ever occurs when allow_partial_repair is True
            # (see status determination above), so repaired_output is
            # always set here.
            repaired_output = deepcopy(working_data)
        else:
            repaired_output = None

        # --- Final telemetry ------------------------------------------------------
        if status is RepairStatus.FAILED:
            self._emit(
                contract,
                TelemetryEventType.REPAIR_FAILED,
                status=status.value,
                attempts=len(attempts),
            )
        else:
            self._emit(
                contract,
                TelemetryEventType.REPAIR_COMPLETED,
                status=status.value,
                attempts=len(attempts),
            )

        return RepairResult(
            status=status,
            original_input=original_input,
            initial_violations=initial_violations,
            remaining_violations=remaining_violations,
            attempts=attempts,
            repair_log=self._logger.entries,
            contract_id=contract.contract_id,
            repaired_output=repaired_output,
        )

    # ------------------------------------------------------------------
    # Merged validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
        adapter: IContractAdapter,
    ) -> ValidationResult:
        """
        Validate *data* against *contract* using both *adapter*'s native
        validator and the framework-agnostic ``ContractValidator``, merging
        their violations.

        See the module docstring for why this merge is necessary: most
        framework validators do not report ``UNEXPECTED_FIELD`` violations,
        which ``ExactAliasStrategy`` and ``FuzzyFieldMatchStrategy`` require
        to identify rename candidates.

        Merge semantics
        ---------------
        * Adapter violations are kept verbatim.
        * Core-validator violations are appended only if no adapter
          violation shares the same ``(field_path, violation_type)``
          signature.
        * ``is_valid`` is ``True`` iff the adapter considers the data valid
          AND no core-only addition has ``ViolationSeverity.ERROR``.
        """
        adapter_result = adapter.validate(contract, data)
        core_result = self._core_validator.validate(contract, data)

        adapter_signatures = {self._violation_signature(v) for v in adapter_result.violations}

        merged_violations = list(adapter_result.violations)
        core_only_error = False

        for violation in core_result.violations:
            signature = self._violation_signature(violation)
            if signature in adapter_signatures:
                continue
            merged_violations.append(violation)
            if violation.severity is ViolationSeverity.ERROR:
                core_only_error = True

        is_valid = adapter_result.is_valid and not core_only_error

        return ValidationResult(
            is_valid=is_valid,
            violations=merged_violations,
            raw_input=dict(data),
            contract_id=contract.contract_id,
        )

    # ------------------------------------------------------------------
    # Telemetry helper
    # ------------------------------------------------------------------

    def _emit(
        self,
        contract: ContractSpec,
        event_type: TelemetryEventType,
        **data: Any,
    ) -> None:
        self._telemetry.emit(
            TelemetryEvent(
                event_type=event_type,
                contract_id=contract.contract_id,
                data=dict(data),
            )
        )

    # ------------------------------------------------------------------
    # Violation correlation
    # ------------------------------------------------------------------

    @staticmethod
    def _correlate_violations(
        violations: list[ContractViolation],
    ) -> list[ContractViolation]:
        """
        Link every MISSING_REQUIRED_FIELD violation with every
        UNEXPECTED_FIELD violation via ``related_ids``, mutating in place.

        This is a full cross-product correlation: it does not attempt to
        determine which pairs are "the" rename candidates (that is
        ``FuzzyFieldMatchStrategy``'s job).  It records, for audit
        purposes, that these violation sets were considered together
        during this repair iteration.
        """
        missing = [
            v for v in violations if v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        ]
        unexpected = [v for v in violations if v.violation_type is ViolationType.UNEXPECTED_FIELD]

        for m in missing:
            for u in unexpected:
                if u.violation_id not in m.related_ids:
                    m.related_ids.append(u.violation_id)
                if m.violation_id not in u.related_ids:
                    u.related_ids.append(m.violation_id)

        return violations

    # ------------------------------------------------------------------
    # Hashing / signatures
    # ------------------------------------------------------------------

    @staticmethod
    def _violation_signature(violation: ContractViolation) -> tuple[str, str]:
        """A (field_path, violation_type) pair identifying a violation's kind."""
        return (violation.field_path, violation.violation_type.value)

    @staticmethod
    def _compute_violation_hash(violations: list[ContractViolation]) -> str:
        """
        Compute a stable hash of the violation set's
        (field_path, violation_type, severity) signatures.

        Used for no-progress detection: if two consecutive iterations
        produce the same hash, the repair loop is making no progress and
        terminates.
        """
        signatures = sorted(
            (v.field_path, v.violation_type.value, v.severity.value) for v in violations
        )
        return hashlib.sha256(repr(signatures).encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------
    # Operation application
    # ------------------------------------------------------------------

    def _apply_operation(
        self,
        data: dict[str, Any],
        op: FieldOperation,
        contract: ContractSpec,
    ) -> None:
        """Apply a single ``FieldOperation`` to *data* in place."""
        if op.op_type is FieldOpType.RENAME:
            self._apply_rename(data, op)
        elif op.op_type is FieldOpType.COERCE:
            self._apply_coerce(data, op, contract)
        elif op.op_type is FieldOpType.SET_DEFAULT:
            _set_nested(data, op.target_path, op.value)
        elif op.op_type is FieldOpType.REMOVE:
            _delete_nested(data, op.target_path)
        elif op.op_type is FieldOpType.SET_VALUE:
            _set_nested(data, op.target_path, op.value)

    @staticmethod
    def _apply_rename(data: dict[str, Any], op: FieldOperation) -> None:
        """Move the value at ``op.source_path`` to ``op.target_path``."""
        if op.source_path is None:
            return
        value = _get_nested(data, op.source_path)
        if value is _NOT_FOUND:
            return
        _delete_nested(data, op.source_path)
        _set_nested(data, op.target_path, value)

    @staticmethod
    def _apply_coerce(
        data: dict[str, Any],
        op: FieldOperation,
        contract: ContractSpec,
    ) -> None:
        """Cast the value at ``op.target_path`` to its contract-declared type."""
        value = _get_nested(data, op.target_path)
        if value is _NOT_FOUND:
            return

        field_spec = _find_field_spec(contract, op.target_path)
        if field_spec is None:
            return

        coerced = _coerce_value(
            value,
            field_spec.field_type,
            item_type=field_spec.item_type,
            union_members=field_spec.union_members,
        )
        if coerced is _COERCE_FAILED:
            return

        _set_nested(data, op.target_path, coerced)

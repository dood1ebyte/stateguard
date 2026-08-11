"""
RepairEngine — orchestrates the repair loop.

This is the heart of the core engine: it correlates violations, selects
strategies via the ``StrategyRegistry``, scores each proposed
``FieldOperation`` through ``TrustPolicy``, applies the ones that clear the
bar for their risk tier, revalidates via the adapter, and assembles the final
``RepairResult`` with a full audit trail.

Scoring is deliberately not the strategies' job.  They report what they
measured (``RepairEvidence``) and how bad it would be to be wrong
(``RepairRisk``); ``TrustPolicy`` turns that into a number and an
apply/abstain/reject decision.  Proposals that land in the abstain band are
recorded on ``RepairResult.ambiguous`` rather than silently dropped, so a
caller can re-prompt, review, or display them.

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

Termination and convergence
---------------------------
The repair loop is iterative by design: one strategy runs per attempt, and a
successful repair frequently *exposes* the next problem rather than resolving
everything at once.  Renaming ``temp_celsius`` to ``temperature`` replaces a
MISSING_REQUIRED_FIELD with a TYPE_MISMATCH at the new path, which
``TypeCoercionStrategy`` then fixes on the following attempt.

Progress is therefore measured by magnitude, not by kind.  Each iteration
computes ``_progress_key`` — ``(error_count, total_violation_count)`` —
and compares it lexicographically against the state the attempt started from:

* ``new_key < current_key`` — progress.  Accept the data and continue, even
  if the *kinds* of violation changed completely.
* ``new_key > current_key`` — regression.  The attempt made things worse, so
  it is discarded; ``working_data`` still holds the last-good state, which
  means earlier successful repairs in the same run are preserved rather than
  thrown away.
* ``new_key == current_key`` — same magnitude.  Accepted only if the violation
  set has not been seen before in this run (``seen_hashes``); otherwise the
  loop is cycling and stops.

Because the accepted path's key is monotonically non-increasing over a
well-founded order, and any same-magnitude state may be visited at most once,
the loop terminates on its own.  ``RepairConfig.max_attempts`` is a safety
bound, not the primary terminator.

The final status determination uses the same ``_progress_key`` metric, so the
loop's notion of "this attempt made progress" and the result's PARTIAL/FAILED
verdict can never disagree.

Zero external dependencies — part of Layer 5 (depends on Layers 0-4:
models, errors, interfaces, strategies, validator).
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from copy import deepcopy
from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.results import (
    AmbiguousRepair,
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
from stateguard.core.strategies.coerce import (
    _array_wrap_is_safe,
    json_serialized,
    resolve_union_member,
)
from stateguard.core.strategies.registry import StrategyRegistry
from stateguard.core.trust import TrustDecision, TrustPolicy
from stateguard.core.validator import ContractValidator, root_structural_violation
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


def _safe_deepcopy(value: Any) -> Any:
    """
    ``deepcopy(value)``, falling back to *value* itself if it cannot be copied.

    Used only for ``RepairResult.original_input``, which is an audit snapshot
    the engine never writes to.  Some perfectly ordinary payload wrappers
    cannot be deep-copied at all — ``types.MappingProxyType`` raises
    ``TypeError: cannot pickle 'mappingproxy' object`` — and failing to
    snapshot one must not become a way for ``repair()`` to raise.
    """
    try:
        return deepcopy(value)
    except Exception:
        # Any copy failure degrades to sharing the reference. Safe here
        # precisely because original_input is never written to.
        return value


def _identical(left: Any, right: Any) -> bool:
    """
    Value equality that does not conflate values of different types.

    ``==`` is the wrong test for "did this operation change anything":
    ``5 == 5.0`` and ``1 == True`` are both ``True`` in Python, so a coercion
    that correctly turned an ``int`` into a ``float`` would look like a no-op
    and be thrown away.  Comparing types first keeps that distinction while
    still recognising a genuinely redundant write.
    """
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        return left.keys() == right.keys() and all(
            _identical(left[key], right[key]) for key in left
        )
    if isinstance(left, list):
        return len(left) == len(right) and all(
            _identical(a, b) for a, b in zip(left, right, strict=True)
        )
    return bool(left == right)


def _unwrap_single_element(sequence: Sequence[Any]) -> dict[str, Any] | None:
    """Return the sole ``dict`` inside a one-element sequence, else ``None``."""
    if len(sequence) == 1 and isinstance(sequence[0], dict):
        return dict(sequence[0])
    return None


def _pairs_to_dict(sequence: Sequence[Any]) -> dict[str, Any] | None:
    """
    Interpret *sequence* as a list of key/value pairs, or return ``None``.

    This is the wire form of ``dict.items()`` and JavaScript's
    ``Object.entries()`` — ``[["a", 1], ["b", "x"]]`` — which some
    serialisers emit in place of an object.

    Accepted only when the reading is unambiguous and exactly reversible:
    every element is a 2-element sequence that is not itself a string, every
    key is a ``str``, and no key repeats.  A duplicate key would make the
    conversion lossy (one value silently wins), so it is refused rather than
    resolved.

    This is not a reversal of the rule that ``[("a", 1)]`` must not be fed to
    ``dict()``.  That rule was about ``dict()`` swallowing the shape
    *unvalidated* — accepting ``[1, 2]``-shaped garbage by accident.
    Converting behind this guard is a different operation.
    """
    if not sequence:
        return None

    result: dict[str, Any] = {}
    for item in sequence:
        if isinstance(item, (str, bytes)) or not isinstance(item, Sequence):
            return None
        if len(item) != 2:
            return None
        key = item[0]
        if not isinstance(key, str) or key in result:
            return None
        result[key] = item[1]
    return result


def _mapping_like_to_dict(data: Any) -> tuple[dict[str, Any] | None, str]:
    """
    Convert an object that already *is* a key/value structure into a ``dict``.

    These are not inferences.  Each conversion is the type's own canonical
    dict form, defined by the standard library — the keys and values are
    already present and named:

    * any ``collections.abc.Mapping`` that is not a ``dict`` subclass
      (``MappingProxyType``, ``UserDict``, third-party mapping types);
    * a ``dataclass`` instance, via ``dataclasses.asdict``;
    * a ``namedtuple`` instance, via its own ``_asdict``.

    ``isinstance(data, dict)`` is simply too narrow a test for "is this an
    object".

    Framework-native objects are deliberately *not* handled here.  A Pydantic
    ``BaseModel`` instance has an equally canonical ``model_dump``, but
    recognising it in the core engine would make Layer 5 implicitly aware of
    an adapter's type system and break the layering rule the CI isolation job
    exists to enforce.  That belongs behind an ``IContractAdapter``
    normalisation hook — see ``CORE_HARDENING_PLAN.md`` §2b.1.
    """
    try:
        if isinstance(data, Mapping):
            return dict(data), f"converted a {type(data).__name__} mapping to an object"

        if dataclasses.is_dataclass(data) and not isinstance(data, type):
            return (
                dataclasses.asdict(data),
                f"converted the {type(data).__name__} dataclass instance to an object",
            )

        # namedtuple is a tuple subclass, so this must be checked before any
        # generic sequence handling.
        if isinstance(data, tuple) and hasattr(data, "_asdict") and hasattr(data, "_fields"):
            return (
                dict(data._asdict()),
                f"converted the {type(data).__name__} namedtuple to an object",
            )
    except Exception:
        # A conversion that raises (e.g. dataclasses.asdict deep-copying an
        # uncopyable field) means "not recoverable", never a propagated error.
        return None, ""

    return None, ""


def _normalise_root_payload(data: Any) -> tuple[dict[str, Any] | None, str]:
    """
    Attempt to recover an object root from a non-``dict`` *data*.

    Returns ``(recovered_dict, description)``, or ``(None, "")`` when the
    root cannot be recovered safely.

    Two classes of root are recovered.

    **Already an object, just not a ``dict``** (``_mapping_like_to_dict``) --
    mappings that are not ``dict`` subclasses, dataclass instances,
    namedtuples.  These carry named fields already, so converting them is the
    type's own canonical dict form rather than an inference.

    **An object that arrived mis-wrapped:**

    * a JSON-encoded string or bytes -- ``'{"a": 1}'``.  Models routinely
      return an entire tool-call payload as a string instead of an object.
      Parsing is lossless and exactly reversible;
    * a single-element sequence wrapping the object -- ``[{"a": 1}]``;
    * a list of key/value pairs -- ``[["a", 1], ["b", "x"]]``, under the
      strict guard in ``_pairs_to_dict``.

    Everything else is refused, because every remaining reading requires
    guessing at intent: a multi-element sequence has no principled element to
    pick, positional values have no principled field to map onto, ``[]`` and
    ``None`` would mean fabricating an object out of nothing, and a ``set``
    has no key structure at all.

    One case sits deliberately outside this function.  A bare scalar against
    a single-field contract (``"Mumbai"`` for ``{city: str}``) is a plausible
    reading, but it is an inference about *intent* rather than a re-encoding
    of data that is already structured.  It belongs in the confidence model
    as a scored, abstainable proposal -- see ``CORE_HARDENING_PLAN.md``
    §2b.4 -- not here, where it would be applied unconditionally and
    silently.

    ``json.loads`` on a string is safe (no code execution).  Deeply nested
    input raises ``RecursionError`` and invalid encodings raise
    ``UnicodeDecodeError``; both are caught and treated as "not recoverable"
    rather than propagated.
    """
    converted, note = _mapping_like_to_dict(data)
    if converted is not None:
        return converted, note

    if isinstance(data, (str, bytes)):
        try:
            parsed = json.loads(data)
        except (ValueError, RecursionError, UnicodeDecodeError):
            return None, ""
        if isinstance(parsed, dict):
            return parsed, "parsed a JSON object from the string payload"
        if isinstance(parsed, list):
            unwrapped = _unwrap_single_element(parsed)
            if unwrapped is not None:
                return unwrapped, (
                    "parsed JSON from the string payload and unwrapped a single-element array"
                )
            pairs = _pairs_to_dict(parsed)
            if pairs is not None:
                return pairs, (
                    "parsed JSON from the string payload and read it as a key/value pair list"
                )
        return None, ""

    if isinstance(data, (list, tuple)):
        unwrapped = _unwrap_single_element(data)
        if unwrapped is not None:
            return unwrapped, "unwrapped a single-element sequence"
        pairs = _pairs_to_dict(data)
        if pairs is not None:
            return pairs, "read the sequence as a key/value pair list"

    return None, ""


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
    if target_type in (FieldType.STRING, FieldType.BYTES):
        serialized = json_serialized(value)
        if serialized is not None:
            return serialized
        return _COERCE_FAILED

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
        member, _evidence, _risk = resolved
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
        policy: TrustPolicy | None = None,
    ) -> None:
        self._registry = registry
        self._config = config
        self._logger = logger
        self._telemetry: ITelemetryHook = telemetry if telemetry is not None else NoopTelemetry()
        self._core_validator = ContractValidator()
        self._policy = policy if policy is not None else TrustPolicy()

    # ------------------------------------------------------------------
    # Ambiguity
    # ------------------------------------------------------------------

    def _record_decisions(
        self,
        contract: ContractSpec,
        offered: list[tuple[FieldOperation, TrustDecision]],
    ) -> None:
        """Log and emit telemetry for every proposal that was not applied."""
        for op, decision in offered:
            if decision is TrustDecision.APPLY:
                continue

            if decision is TrustDecision.AMBIGUOUS:
                self._logger.warning(
                    "operation.ambiguous",
                    f"Found a {op.op_type.value} for '{op.target_path}' but the "
                    f"evidence does not justify applying it unsupervised.",
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    trust=op.trust,
                    risk=op.risk.name,
                )
            else:
                self._logger.warning(
                    "operation.rejected",
                    f"Rejected {op.op_type.value} on '{op.target_path}'.",
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    trust=op.trust,
                    risk=op.risk.name,
                )

            self._emit(
                contract,
                TelemetryEventType.OPERATION_REJECTED,
                op_type=op.op_type.value,
                target_path=op.target_path,
                trust=op.trust,
                decision=decision.value,
            )
            # Only withheld operations get the full explanation. An applied one
            # already logs its trust and risk, and rendering an explanation for
            # every operation doubled the size of every repair_log.
            self._logger.debug(
                "operation.explained",
                self._policy.explain(op, decision),
                target_path=op.target_path,
            )

    @staticmethod
    def _hold_tainted(
        scored: tuple[FieldOperation, TrustDecision],
        withheld_sources: set[str],
    ) -> tuple[FieldOperation, TrustDecision]:
        """
        Keep withholding a source key the engine has already been unsure about.

        Abstaining on one field and then repairing it another way makes the
        *contest* disappear without resolving anything about the key itself.
        Without this guard the engine talks itself into the exact repair it
        just refused: with ``user_id`` and ``user_name`` both missing and one
        ``user_email`` present, the rename is correctly withheld -- but once
        ``user_name`` is filled from its declared default, ``user_email`` is
        the only candidate for the only remaining field, scores 0.891
        unopposed, and the email address lands in ``user_id`` after all.

        Nothing was learned about where ``user_email`` belongs, so the
        uncertainty stands. A key that has been withheld once stays withheld
        for the rest of the run, and the caller still sees it on
        ``RepairResult.ambiguous``.
        """
        op, decision = scored
        if (
            decision is TrustDecision.APPLY
            and op.source_path is not None
            and op.source_path in withheld_sources
        ):
            return op, TrustDecision.AMBIGUOUS
        return scored

    def _record_ambiguous(
        self,
        ambiguous: list[AmbiguousRepair],
        op: FieldOperation,
    ) -> None:
        """
        Add *op* to the abstained-repair list, merging by target path.

        Merging matters for two reasons.  The loop can re-propose the same
        withheld repair on a later attempt, which would otherwise append a
        duplicate and make a caller prompt twice for one decision.  And when
        several proposals compete for the same field, a caller choosing
        between them needs them together and ranked -- which is the entire
        point of surfacing an abstention rather than dropping it.
        """
        band = self._policy.band_for(op.risk)
        reason = (
            f"trust {op.trust:.2f} is below the {op.risk.name} threshold of {band.apply_at:.2f}"
        )

        for existing in ambiguous:
            if existing.target_path != op.target_path:
                continue
            for candidate in existing.candidates:
                if (
                    candidate.op_type is op.op_type
                    and candidate.source_path == op.source_path
                    and candidate.value == op.value
                ):
                    return  # already recorded on an earlier attempt
            existing.candidates.append(op)
            existing.candidates.sort(key=lambda c: c.trust, reverse=True)
            existing.reason = reason
            return

        ambiguous.append(
            AmbiguousRepair(target_path=op.target_path, candidates=[op], reason=reason)
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def repair(
        self,
        contract: ContractSpec,
        data: Any,
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
            Normally a ``dict``; a non-object root is either normalised (a
            JSON-encoded string, a single-element sequence wrapping the
            object) or reported as a ``STRUCTURAL_MISMATCH`` failure.  It
            never raises.
        adapter:
            Framework adapter.  Used together with ``ContractValidator``
            for both initial validation and revalidation — see the module
            docstring for the merge semantics.

        Returns
        -------
        RepairResult
        """
        original_input = _safe_deepcopy(data)

        # --- Root shape normalisation -----------------------------------------
        # Field-level repair cannot begin until the root is an object -- there
        # is nothing to look field paths up in otherwise. Two non-dict roots
        # are recoverable (see ``_normalise_root_payload``); anything else is
        # a structural failure the engine reports rather than guesses at.
        root_note = ""
        if isinstance(data, dict):
            source: Any = data
        else:
            recovered, root_note = _normalise_root_payload(data)
            if recovered is None:
                return self._root_failure(contract, original_input)
            source = recovered
            self._logger.info(
                "root.normalised",
                f"Payload root was {type(data).__name__}, not an object; {root_note}.",
                received_type=type(data).__name__,
                action=root_note,
            )

        # Copied *after* normalisation, for two reasons: a root that cannot be
        # deep-copied at all (``MappingProxyType``) is converted to a plain
        # dict first, and the recovery helpers return shallow copies, so
        # nested values would otherwise still be shared with the caller's
        # object and mutated in place by the repair loop.
        working_data: dict[str, Any] = deepcopy(source)

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
            # A normalised root means a repair *did* happen, even though no
            # field-level strategy ran -- so this is SUCCESS, not
            # ALREADY_VALID ("the input needed no repair").
            valid_status = RepairStatus.SUCCESS if root_note else RepairStatus.ALREADY_VALID
            self._logger.info(
                "validation.already_valid",
                (
                    f"Payload satisfies the contract once the root was normalised "
                    f"({root_note}); no field-level repair needed."
                    if root_note
                    else "Input data already satisfies the contract; no repair needed."
                ),
            )
            self._emit(
                contract,
                TelemetryEventType.REPAIR_COMPLETED,
                status=valid_status.value,
                attempts=0,
            )
            return RepairResult(
                status=valid_status,
                original_input=original_input,
                initial_violations=list(initial_result.violations),
                remaining_violations=list(initial_result.violations),
                attempts=[],
                repair_log=self._logger.entries,
                contract_id=contract.contract_id,
                repaired_output=deepcopy(working_data),
            )

        initial_violations = list(initial_result.violations)
        initial_key = self._progress_key(initial_violations)

        self._emit(contract, TelemetryEventType.REPAIR_STARTED)
        self._logger.info(
            "repair.started",
            f"Starting repair loop with {len(initial_violations)} violation(s) detected.",
            violation_count=len(initial_violations),
        )

        current_violations = initial_violations
        current_key = initial_key
        seen_hashes: set[str] = {self._compute_violation_hash(current_violations)}
        attempts: list[RepairAttempt] = []
        ambiguous: list[AmbiguousRepair] = []
        # Source keys the engine has already declined to place. See
        # ``_hold_tainted`` -- resolving the competing field by other means
        # must not turn a refused rename into an applied one.
        withheld_sources: set[str] = set()

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

            # --- Select a strategy that actually has something to apply --------
            # Not simply ``applicable[0]``. A strategy can be applicable, propose
            # a repair, and have every proposal land in the abstain band -- which
            # is common now that abstention is a real outcome. Stopping there
            # would let one uncertain rename suppress a *certain*,
            # schema-declared repair from a lower-priority strategy: the loop
            # would see an unchanged payload, call it no progress, and give up.
            # So keep trying strategies in priority order until one has work to
            # do, carrying the abstentions and rejections of the ones passed over.
            strategy = applicable[0]
            proposed: list[FieldOperation] = []
            candidate_ops: list[FieldOperation] = []
            rejected_ops: list[FieldOperation] = []
            abstained_ops: list[FieldOperation] = []

            # Proposals from strategies that were passed over still count: they
            # were considered, and an abstention recorded by one of them must
            # not be erased by the strategy that ends up being selected.
            carried_proposed: list[FieldOperation] = []
            carried_rejected: list[FieldOperation] = []
            carried_abstained: list[FieldOperation] = []

            for nth, considered in enumerate(applicable):
                offered = [
                    self._hold_tainted(self._policy.evaluate(op), withheld_sources)
                    for op in considered.propose(correlated, contract, working_data)
                ]
                appliable = [op for op, decision in offered if decision is TrustDecision.APPLY]
                self._record_decisions(contract, offered)

                strategy = considered
                proposed = [*carried_proposed, *(op for op, _ in offered)]
                candidate_ops = appliable
                rejected_ops = [
                    *carried_rejected,
                    *(op for op, decision in offered if decision is TrustDecision.REJECT),
                ]
                abstained_ops = [
                    *carried_abstained,
                    *(op for op, decision in offered if decision is TrustDecision.AMBIGUOUS),
                ]

                if appliable or nth == len(applicable) - 1:
                    break

                carried_proposed = proposed
                carried_rejected = rejected_ops
                carried_abstained = abstained_ops

                self._logger.info(
                    "strategy.passed_over",
                    f"'{considered.name}' had nothing it could apply; "
                    f"trying the next applicable strategy.",
                    strategy=considered.name,
                    attempt_number=attempt_number,
                )

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

            for op in abstained_ops:
                self._record_ambiguous(ambiguous, op)
                if op.source_path is not None:
                    withheld_sources.add(op.source_path)

            data_before = deepcopy(working_data)
            new_data = deepcopy(working_data)

            # An operation only counts as applied if it actually changed the
            # data. Several apply paths return silently when their target has
            # gone missing or a cast turns out to be impossible; recording
            # those as applied made the audit trail claim edits that never
            # happened.
            applied_ops: list[FieldOperation] = []
            for op in candidate_ops:
                if not self._apply_operation(new_data, op, contract):
                    rejected_ops.append(op)
                    self._logger.warning(
                        "operation.no_effect",
                        f"{op.op_type.value} on '{op.target_path}' left the payload "
                        f"unchanged; not recording it as applied.",
                        op_type=op.op_type.value,
                        target_path=op.target_path,
                        trust=op.trust,
                    )
                    continue

                applied_ops.append(op)
                self._emit(
                    contract,
                    TelemetryEventType.OPERATION_APPLIED,
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    trust=op.trust,
                )
                self._logger.info(
                    "operation.applied",
                    f"Applied {op.op_type.value} on '{op.target_path}' "
                    f"(trust {op.trust:.2f}, risk {op.risk.name}).",
                    op_type=op.op_type.value,
                    target_path=op.target_path,
                    source_path=op.source_path,
                    trust=op.trust,
                    risk=op.risk.name,
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
                    abstained_operations=abstained_ops,
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

            # --- Convergence bookkeeping -----------------------------------------
            # See "Termination and convergence" in the module docstring for why
            # progress is measured by (error_count, total_count) rather than by
            # comparing violation *kinds* against the initial set.
            new_key = self._progress_key(revalidation.violations)
            new_hash = self._compute_violation_hash(revalidation.violations)

            # --- Regression check --------------------------------------------
            if new_key > current_key:
                # Strictly worse than the state this attempt started from.
                # ``working_data`` and ``current_violations`` still hold the
                # last-good state, so simply stopping here discards the bad
                # attempt while keeping every repair that came before it.
                self._logger.error(
                    "repair.regression_detected",
                    f"Attempt {attempt_number} ('{strategy.name}') made the "
                    f"violation set worse "
                    f"(errors {current_key[0]}->{new_key[0]}, "
                    f"total {current_key[1]}->{new_key[1]}). "
                    f"Reverting to the last-good state and aborting repair.",
                    attempt_number=attempt_number,
                    strategy=strategy.name,
                )
                remaining_violations = current_violations
                break

            # --- No-progress / cycle check ---------------------------------------
            if new_key == current_key and new_hash in seen_hashes:
                self._logger.warning(
                    "repair.no_progress",
                    f"Attempt {attempt_number} ('{strategy.name}') made "
                    f"no progress; violation set already seen.",
                    attempt_number=attempt_number,
                    strategy=strategy.name,
                )
                remaining_violations = revalidation.violations
                break

            # --- Progress made; continue looping --------------------------------
            working_data = new_data
            current_violations = revalidation.violations
            current_key = new_key
            remaining_violations = current_violations
            seen_hashes.add(new_hash)

        else:
            # for/else: loop exhausted max_attempts without break.
            self._logger.warning(
                "repair.max_attempts_exhausted",
                f"Reached max_attempts ({self._config.max_attempts}) without full repair.",
                max_attempts=self._config.max_attempts,
            )

        # --- Determine final status if not already SUCCESS/FAILED -------------
        if status is None:
            # Uses the same progress metric as the loop above, so "the loop
            # thought it was making progress" and "the result is PARTIAL" can
            # never disagree.
            remaining_key = self._progress_key(remaining_violations)
            if remaining_key[0] == 0:
                status = RepairStatus.SUCCESS
            elif remaining_key < initial_key:
                status = (
                    RepairStatus.PARTIAL
                    if self._config.allow_partial_repair
                    else RepairStatus.FAILED
                )
            elif ambiguous:
                # Nothing was resolved, but a repair *was* found and withheld.
                # Reporting that as FAILED would throw away the one piece of
                # information the caller can act on.
                status = RepairStatus.AMBIGUOUS
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
        if status in (RepairStatus.FAILED, RepairStatus.AMBIGUOUS):
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
            ambiguous=ambiguous,
        )

    # ------------------------------------------------------------------
    # Root failure
    # ------------------------------------------------------------------

    def _root_failure(self, contract: ContractSpec, original_input: Any) -> RepairResult:
        """
        Build the terminal ``FAILED`` result for a payload root that is not an
        object and could not be normalised into one.

        No adapter is consulted and no strategy runs: with no object to look
        field paths up in there is nothing for either to act on.
        """
        violation = root_structural_violation(original_input)

        self._emit(contract, TelemetryEventType.VALIDATION_STARTED)
        self._emit(
            contract,
            TelemetryEventType.VIOLATION_DETECTED,
            field_path=violation.field_path,
            violation_type=violation.violation_type.value,
            severity=violation.severity.value,
        )
        self._logger.error(
            "root.unsupported_type",
            (
                f"Payload root is {type(original_input).__name__}, not an "
                f"object, and could not be normalised into one. No repair is "
                f"possible."
            ),
            received_type=type(original_input).__name__,
        )
        self._emit(
            contract,
            TelemetryEventType.REPAIR_FAILED,
            status=RepairStatus.FAILED.value,
            attempts=0,
        )

        return RepairResult(
            status=RepairStatus.FAILED,
            original_input=original_input,
            initial_violations=[violation],
            remaining_violations=[violation],
            attempts=[],
            repair_log=self._logger.entries,
            contract_id=contract.contract_id,
            repaired_output=None,
        )

    # ------------------------------------------------------------------
    # Merged validation
    # ------------------------------------------------------------------

    def _validate(
        self,
        contract: ContractSpec,
        data: Any,
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
        # Adapters are entitled to assume an object root (``IContractAdapter``
        # types ``data`` as a dict). ``repair`` normalises or rejects a
        # non-object root before the loop starts, but this method is also
        # reached directly via ``ContractGuard.validate``, which does not.
        if not isinstance(data, dict):
            return ValidationResult(
                is_valid=False,
                violations=[root_structural_violation(data)],
                raw_input=data,
                contract_id=contract.contract_id,
            )

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
    def _progress_key(violations: list[ContractViolation]) -> tuple[int, int]:
        """
        Return ``(error_count, total_count)`` — the loop's progress metric.

        Compared lexicographically, so resolving an ERROR always counts as
        progress regardless of what happens to WARNING-severity violations,
        and a change that only removes warnings still counts as progress when
        the error count holds steady.

        The second element is what makes multi-step repair work.  Renaming
        ``temp_celsius`` to ``temperature`` turns
        ``{MISSING temperature (error), UNEXPECTED temp_celsius (warning)}``
        into ``{TYPE_MISMATCH temperature (error)}``: the error count is
        unchanged at 1, but the total dropped from 2 to 1.  Measuring errors
        alone would score that correct rename as "no progress" and stop before
        ``TypeCoercionStrategy`` ever runs.

        Both components are monotonically non-increasing along the accepted
        path, which is what guarantees the loop terminates — see the module
        docstring.
        """
        errors = sum(1 for v in violations if v.severity is ViolationSeverity.ERROR)
        return (errors, len(violations))

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
    ) -> bool:
        """
        Apply a single ``FieldOperation`` to *data* in place.

        Returns whether the payload actually changed.  Each applier already
        knows this — every one of its early returns *is* the "nothing to do"
        case — so it reports the fact rather than the caller inferring it by
        comparing snapshots.  That inference was both expensive (a full
        ``deepcopy`` per operation) and wrong: ``{"x": 5} == {"x": 5.0}`` is
        ``True`` in Python, so a coercion that correctly changed an ``int``
        into a ``float`` looked like a no-op and was discarded.
        """
        if op.op_type is FieldOpType.RENAME:
            return self._apply_rename(data, op)
        if op.op_type is FieldOpType.COERCE:
            return self._apply_coerce(data, op, contract)
        if op.op_type is FieldOpType.SET_DEFAULT or op.op_type is FieldOpType.SET_VALUE:
            existing = _get_nested(data, op.target_path)
            if existing is not _NOT_FOUND and _identical(existing, op.value):
                return False
            _set_nested(data, op.target_path, op.value)
            return True
        if op.op_type is FieldOpType.REMOVE:
            existed = _get_nested(data, op.target_path) is not _NOT_FOUND
            _delete_nested(data, op.target_path)
            return existed
        return False

    @staticmethod
    def _apply_rename(data: dict[str, Any], op: FieldOperation) -> bool:
        """Move the value at ``op.source_path`` to ``op.target_path``."""
        if op.source_path is None:
            return False
        value = _get_nested(data, op.source_path)
        if value is _NOT_FOUND:
            return False
        _delete_nested(data, op.source_path)
        _set_nested(data, op.target_path, value)
        return True

    @staticmethod
    def _apply_coerce(
        data: dict[str, Any],
        op: FieldOperation,
        contract: ContractSpec,
    ) -> bool:
        """Cast the value at ``op.target_path`` to its contract-declared type."""
        value = _get_nested(data, op.target_path)
        if value is _NOT_FOUND:
            return False

        field_spec = _find_field_spec(contract, op.target_path)
        if field_spec is None:
            return False

        coerced = _coerce_value(
            value,
            field_spec.field_type,
            item_type=field_spec.item_type,
            union_members=field_spec.union_members,
        )
        if coerced is _COERCE_FAILED or _identical(value, coerced):
            return False

        _set_nested(data, op.target_path, coerced)
        return True

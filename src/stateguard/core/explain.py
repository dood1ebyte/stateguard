"""
Per-field decisions and a machine-readable record of one repair.

``RepairResult`` already holds everything that happened -- every attempt,
every operation with its evidence and trust, every abstention, every
violation left over -- but in the shape the engine produced it: by attempt,
not by field. Answering "what happened to ``user_id``?" meant walking
``attempts``, cross-checking ``ambiguous``, and knowing that an attempt the
engine rolled back still lists its operations. This module does that walk
once, deterministically, from the metadata the engine already recorded.
Nothing here re-scores, re-validates, or infers anything new.

Three views, for three readers:

* ``field_decisions(result)`` -- one ``FieldDecision`` per field touched or
  left broken, for code.
* ``explain_result(result)`` -- the same as text, for a log line or a
  terminal. Contains **no payload values**: field names, strategy
  rationales, trust scores and evidence notes only, all of which are
  value-free by construction (see ``RepairEvidence.notes``). Safe to log
  whatever ``RepairConfig.include_values_in_log`` says.
* ``result_to_dict(result)`` -- the whole record as plain JSON-ready data,
  payloads included, for an audit store or a test fixture.

Zero external dependencies.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from stateguard.core.errors.operations import FieldOperation

if TYPE_CHECKING:
    from stateguard.core.errors.results import RepairResult
    from stateguard.core.errors.violations import ContractViolation

__all__ = [
    "FieldDecision",
    "FieldDecisionKind",
    "explain_result",
    "field_decisions",
    "result_to_dict",
    "root_normalisation",
]


class FieldDecisionKind(StrEnum):
    """
    What happened to one field.

    Members
    -------
    REPAIRED:
        An operation was applied and kept. Its effect is in the payload --
        ``repaired_output`` in auto, ``proposed_output`` in shadow -- when
        the run produced one; a ``FAILED`` run produces none, and the
        repair is reported but not delivered.
    HELD:
        A repair was found but not trusted enough to apply unsupervised
        (``RepairResult.ambiguous``). ``FieldDecision.candidates`` carries
        it, ranked.
    DISCARDED:
        An operation was applied to the engine's working copy, then rolled
        back with the rest of its attempt (``RepairAttempt.kept`` is
        ``False``). It is in no payload.
    UNRESOLVED:
        A violation that remains and that no applied repair addressed.
    """

    REPAIRED = "repaired"
    HELD = "held"
    DISCARDED = "discarded"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True)
class FieldDecision:
    """
    One field's outcome, with the recorded reason for it.

    Attributes
    ----------
    path:
        The field the decision is about -- the operation's target, or the
        violation's path.
    kind:
        See ``FieldDecisionKind``.
    reason:
        Why: the strategy's rationale for ``REPAIRED`` / ``DISCARDED``, the
        engine's abstention reason for ``HELD``, the violation type and
        severity for ``UNRESOLVED``. Value-free.
    operation:
        The applied operation (``REPAIRED`` / ``DISCARDED``) or the
        highest-trust candidate (``HELD``). ``None`` for ``UNRESOLVED``.
        Carries ``trust``, ``risk`` and ``evidence``.
    candidates:
        Every candidate for a ``HELD`` field, highest trust first. Empty
        otherwise.
    attempt:
        The attempt number that applied the operation, for ``REPAIRED`` /
        ``DISCARDED``.
    strategy:
        The strategy that acted in that attempt.
    violation:
        The remaining violation, for ``UNRESOLVED``.
    """

    path: str
    kind: FieldDecisionKind
    reason: str
    operation: FieldOperation | None = None
    candidates: tuple[FieldOperation, ...] = ()
    attempt: int | None = None
    strategy: str | None = None
    violation: ContractViolation | None = None


# ---------------------------------------------------------------------------
# Decisions
# ---------------------------------------------------------------------------

#: Engine log events that end the loop by rolling the last attempt back,
#: and what each means in a sentence.
_DISCARD_EVENTS = {
    "repair.regression_detected": "made the violation set worse",
    "repair.no_progress": "returned to a violation state already seen",
}


def field_decisions(result: RepairResult) -> list[FieldDecision]:
    """
    Every field-level decision in *result*, in a deterministic order.

    Applied operations come first, in the order the engine applied them;
    then held repairs, in the order the engine recorded them; then
    violations nothing resolved, in the order they were reported. A field
    with a held repair is not listed again as unresolved -- the hold is the
    more specific answer.
    """
    decisions: list[FieldDecision] = []

    for attempt in result.attempts:
        kind = FieldDecisionKind.REPAIRED if attempt.kept else FieldDecisionKind.DISCARDED
        for op in attempt.applied_operations:
            reason = op.rationale
            if not attempt.kept:
                reason = f"{reason} Rolled back: attempt {attempt.attempt_number} " + (
                    f"{_discard_cause(result, attempt.attempt_number)}."
                )
            decisions.append(
                FieldDecision(
                    path=op.target_path,
                    kind=kind,
                    reason=reason,
                    operation=op,
                    attempt=attempt.attempt_number,
                    strategy=attempt.strategy_name,
                )
            )

    held_paths: set[str] = set()
    for item in result.ambiguous:
        held_paths.add(item.target_path)
        decisions.append(
            FieldDecision(
                path=item.target_path,
                kind=FieldDecisionKind.HELD,
                reason=item.reason,
                operation=item.best,
                candidates=tuple(item.candidates),
            )
        )

    for violation in result.remaining_violations:
        if violation.field_path in held_paths:
            continue
        decisions.append(
            FieldDecision(
                path=violation.field_path,
                kind=FieldDecisionKind.UNRESOLVED,
                reason=f"{violation.violation_type.value} ({violation.severity.value})",
                violation=violation,
            )
        )

    return decisions


def _discard_cause(result: RepairResult, attempt_number: int) -> str:
    for entry in result.repair_log:
        if entry.event in _DISCARD_EVENTS and entry.data.get("attempt_number") == attempt_number:
            return _DISCARD_EVENTS[entry.event]
    return "was discarded by the engine"


def root_normalisation(result: RepairResult) -> str | None:
    """
    How the engine reshaped a payload whose root was not an object, if it did.

    Not a field decision -- it happens before any field is looked at -- so
    it is reported beside them rather than among them.
    """
    for entry in result.repair_log:
        if entry.event == "root.normalised":
            return str(entry.data.get("action", "normalised the payload root"))
    return None


# ---------------------------------------------------------------------------
# Text
# ---------------------------------------------------------------------------


def explain_result(result: RepairResult) -> str:
    """
    Render *result* as a short, value-free, multi-line explanation.

    Deterministic for a given result: no timestamps, ids or payload values,
    so two runs over the same input explain identically and the text is
    safe to log or snapshot in a test.
    """
    payload = _payload_note(result)
    lines = [f"status: {result.status.value} ({result.mode.value}){payload}"]

    root = root_normalisation(result)
    if root is not None:
        lines.append(f"  root        {root}")

    decisions = field_decisions(result)
    if not decisions and root is None:
        lines.append(
            "  (no field needed repair)"
            if result.is_already_valid
            else "  (no field-level decision recorded)"
        )

    for decision in decisions:
        lines.extend(_render(decision))
    return "\n".join(lines)


def _payload_note(result: RepairResult) -> str:
    if result.repaired_output is not None:
        return ""
    if result.proposed_output is not None:
        return " -- proposed, not applied"
    return " -- no payload produced"


def _render(decision: FieldDecision) -> list[str]:
    head = f"  {decision.kind.value:<11} {decision.path}"
    op = decision.operation
    if op is not None:
        source = f" <- {op.source_path}" if op.source_path else ""
        head += f"{source}  [{op.op_type.value}, trust {op.trust:.2f}, {op.risk.name}]"
    lines = [head, f"{'':14}{decision.reason}"]

    if decision.kind is FieldDecisionKind.HELD:
        for candidate in decision.candidates:
            source = f" <- {candidate.source_path}" if candidate.source_path else ""
            lines.append(
                f"{'':14}candidate {candidate.op_type.value}{source}  "
                f"trust {candidate.trust:.2f}, {candidate.risk.name}"
            )
            lines.extend(f"{'':16}- {note}" for note in candidate.evidence.notes)
    elif op is not None:
        lines.extend(f"{'':16}- {note}" for note in op.evidence.notes)
    return lines


# ---------------------------------------------------------------------------
# Machine-readable
# ---------------------------------------------------------------------------


def result_to_dict(result: RepairResult) -> dict[str, Any]:
    """
    The complete record of *result* as plain data, ready for ``json.dumps``.

    Includes the payloads -- ``original_input``, ``repaired_output``,
    ``proposed_output``, and operation values -- so unlike
    ``explain_result`` it is **not** value-free; treat it like the data it
    describes. Payload values are passed through as received: a payload
    holding something JSON cannot encode needs ``json.dumps(..., default=str)``.
    A rehydrated payload (a Pydantic model) is dumped with ``model_dump()``.

    Keys are stable; new ones may be added.
    """
    return {
        "status": result.status.value,
        "mode": result.mode.value,
        "contract_id": result.contract_id,
        "original_input": result.original_input,
        "valid_as_received": result.is_already_valid,
        "repair_attempted": bool(result.attempts),
        "root_normalisation": root_normalisation(result),
        "initial_violations": [_violation_dict(v) for v in result.initial_violations],
        "decisions": [_decision_dict(d) for d in field_decisions(result)],
        "remaining_violations": [_violation_dict(v) for v in result.remaining_violations],
        "attempts": [
            {
                "attempt_number": a.attempt_number,
                "strategy": a.strategy_name,
                "considered_strategies": list(a.considered_strategies),
                "succeeded": a.succeeded,
                "kept": a.kept,
                "applied_operations": [_operation_dict(op) for op in a.applied_operations],
                "abstained_count": len(a.abstained_operations),
                "rejected_count": len(a.rejected_operations),
            }
            for a in result.attempts
        ],
        "repaired_output": _plain(result.repaired_output),
        "proposed_output": _plain(result.proposed_output),
    }


def _decision_dict(decision: FieldDecision) -> dict[str, Any]:
    return {
        "path": decision.path,
        "decision": decision.kind.value,
        "reason": decision.reason,
        "operation": None if decision.operation is None else _operation_dict(decision.operation),
        "candidates": [_operation_dict(c) for c in decision.candidates],
        "attempt": decision.attempt,
        "strategy": decision.strategy,
        "violation": None if decision.violation is None else _violation_dict(decision.violation),
    }


def _operation_dict(op: FieldOperation) -> dict[str, Any]:
    evidence = op.evidence
    return {
        "op_type": op.op_type.value,
        "target_path": op.target_path,
        "source_path": op.source_path,
        "value": op.value,
        "trust": op.trust,
        "risk": op.risk.name,
        "rationale": op.rationale,
        "evidence": {
            "name_match": evidence.name_match,
            "value_preserved": evidence.value_preserved,
            "schema_authority": evidence.schema_authority,
            "margin": evidence.margin,
            "alternatives_considered": evidence.alternatives_considered,
            "signals": {name: score for name, score in evidence.signals},
            "notes": list(evidence.notes),
        },
    }


def _violation_dict(violation: ContractViolation) -> dict[str, Any]:
    return {
        "field_path": violation.field_path,
        "violation_type": violation.violation_type.value,
        "severity": violation.severity.value,
        "message": violation.message,
    }


def _plain(payload: Any) -> Any:
    """A rehydrated payload as plain data; duck-typed so core never imports pydantic."""
    dump = getattr(payload, "model_dump", None)
    return dump() if callable(dump) else payload

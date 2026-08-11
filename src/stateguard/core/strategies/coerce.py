"""
TypeCoercionStrategy — repairs TYPE_MISMATCH via safe, lossless type casts.

Priority 30.  Only proposes casts where the source value unambiguously
represents a value of the target type:

* ``str`` → ``int``    — only if the string is a (possibly negative) digit
  sequence, e.g. ``"30"`` or ``"-5"``.
* ``str`` → ``float``  — only if ``float(value)`` succeeds.
* ``int`` → ``float``  — always safe.
* ``str`` → ``bool``   — only for the exact strings (case-insensitive)
  ``"true"``, ``"false"``, ``"1"``, ``"0"``.
* ``dict``/``list`` → ``str`` — JSON-serialise, for ``STRING`` and
  ``BYTES`` targets, only if ``json.dumps(value)`` succeeds (deterministic
  and round-trippable; repairs harness-side over-parsing of JSON text
  arguments).  ``BYTES`` targets also yield a ``str`` — the framework's
  native validation encodes it (e.g. Pydantic's lax str -> bytes).
* value → ``list``     — wrap-in-list, only when the target is an ``ARRAY``
  with a declared ``item_type`` that the value already satisfies as a
  single element (lossless: ``"x"`` → ``["x"]``).  Bare/untyped arrays and
  values that are already lists are never wrapped.
* value → union        — for ``UNION`` targets, each member is tried with
  the rules above; the coercion is proposed only when exactly one member
  yields the highest-confidence candidate (ties are ambiguous and refused).

All other combinations are left unrepaired by this strategy (no operation
is proposed; ``confidence`` is never fabricated for unsafe casts).

This strategy determines *feasibility and confidence* only.  The actual
cast is performed by the engine when applying the ``COERCE`` operation,
using ``ContractSpec`` to look up the target ``FieldType`` (and, for
``ARRAY``/``UNION`` targets, the ``item_type`` / ``union_members``).
"""

from __future__ import annotations

import json
from typing import Any

from stateguard.core.errors.operations import (
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType, UnionMember, type_matches

__all__ = ["TypeCoercionStrategy"]


# ---------------------------------------------------------------------------
# Round-trip fidelity
# ---------------------------------------------------------------------------
#
# These replace the four hand-picked confidence constants this module used to
# carry (0.95 numeric, 0.85 bool, 0.9 array-wrap, 0.85 JSON).  Those numbers
# described nothing measurable -- they were the author's feeling about each
# cast.  Fidelity is measured instead: convert the value, convert it back, and
# see how much of the original survived.

#: Converting back reproduces the input exactly: ``"5"`` -> ``5`` -> ``"5"``.
_FIDELITY_EXACT = 1.0

#: Converting back reproduces the input modulo surrounding whitespace.
_FIDELITY_WHITESPACE = 0.95

#: Converting back differs from the input, but only in formatting the value
#: itself does not depend on: ``"05"`` -> ``5`` -> ``"5"``.
_FIDELITY_NORMALISED = 0.85

#: ``"1"`` -> ``True`` is a reading of the string, not a re-encoding of it.
_FIDELITY_BOOL_NUMERIC = 0.80

# Strings accepted for str -> bool coercion (case-insensitive).
_BOOL_TRUE_WORDS = {"true"}
_BOOL_FALSE_WORDS = {"false"}
_BOOL_NUMERIC = {"1", "0"}
_BOOL_STRINGS = _BOOL_TRUE_WORDS | _BOOL_FALSE_WORDS | _BOOL_NUMERIC


def _roundtrip_fidelity(original: str, coerced: Any) -> float:
    """
    How much of *original* survives ``str(coerced)``.

    The point of measuring rather than assuming: ``"5"`` and ``"05"`` were
    previously both worth 0.95 simply because both are digit strings, even
    though only one of them round-trips.
    """
    back = str(coerced)
    if back == original:
        return _FIDELITY_EXACT
    if back == original.strip():
        return _FIDELITY_WHITESPACE
    return _FIDELITY_NORMALISED


# ---------------------------------------------------------------------------
# Path helper (private to this module)
# ---------------------------------------------------------------------------


class _NotFound:
    """Sentinel distinguishing 'path does not exist' from a value of None."""

    def __repr__(self) -> str:
        return "NOT_FOUND"


_NOT_FOUND = _NotFound()


def _get_nested_value(data: dict[str, Any], path: str) -> Any:
    """
    Navigate *data* via dot-notation *path* and return the value found.

    Returns the module-level ``_NOT_FOUND`` sentinel if any segment of
    *path* is absent or an intermediate value is not a dict.  This is
    distinct from a present value of ``None``.
    """
    current: Any = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return _NOT_FOUND
        current = current[part]
    return current


def _find_field_spec(contract: ContractSpec, full_path: str) -> FieldSpec | None:
    """
    Locate the ``FieldSpec`` for a dot-notation *full_path* within *contract*,
    recursing into ``nested_spec`` for nested paths.

    Used to look up ``item_type`` / ``union_members`` for ``ARRAY`` and
    ``UNION`` coercion targets (the violation only carries the
    ``expected_type``).
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


# ---------------------------------------------------------------------------
# Coercion feasibility
# ---------------------------------------------------------------------------


def _coercion_evidence(
    value: Any,
    target_type: FieldType,
    item_type: FieldType | None = None,
    union_members: tuple[UnionMember, ...] | None = None,
) -> tuple[RepairEvidence, RepairRisk] | None:
    """
    Measure the evidence for coercing *value* to *target_type*.

    Returns ``(evidence, risk)``, or ``None`` if no safe coercion is defined
    for this (value, target) pair.  ``TrustPolicy`` turns the evidence into a
    score; this function never invents one.

    *item_type* is consulted only for ``ARRAY`` targets and *union_members*
    only for ``UNION`` targets; both come from the field's ``FieldSpec``.
    """
    if target_type in (FieldType.STRING, FieldType.BYTES):
        serialised = json_serialized(value)
        if serialised is None:
            return None
        # json.loads(json.dumps(x)) == x for the containers we accept, so the
        # value survives intact -- but a container rendered as text is no
        # longer a container, which is why the risk is LOSSY rather than
        # REVERSIBLE.
        return (
            RepairEvidence(
                value_preserved=_FIDELITY_EXACT,
                # Rendering a container as text discards its structure, even
                # though every byte of the value survives. Recorded so that a
                # union offering both this and a wrap-in-list prefers the wrap.
                signals=(("structure_preserved", 0.0),),
                notes=(f"{type(value).__name__} serialises to JSON and parses back equal",),
            ),
            RepairRisk.LOSSY,
        )

    if target_type is FieldType.INTEGER:
        if isinstance(value, str) and not isinstance(value, bool) and _is_integer_string(value):
            fidelity = _roundtrip_fidelity(value, int(value))
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    notes=(f"str -> int round-trips at {fidelity:.2f}",),
                ),
                RepairRisk.REVERSIBLE,
            )
        return None

    if target_type is FieldType.FLOAT:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            # int -> float never loses the value.
            return (
                RepairEvidence(
                    value_preserved=_FIDELITY_EXACT,
                    notes=("int is exactly representable as float",),
                ),
                RepairRisk.REVERSIBLE,
            )
        if isinstance(value, str) and _is_float_string(value):
            fidelity = _roundtrip_fidelity(value, float(value))
            return (
                RepairEvidence(
                    value_preserved=fidelity,
                    notes=(f"str -> float round-trips at {fidelity:.2f}",),
                ),
                RepairRisk.REVERSIBLE,
            )
        return None

    if target_type is FieldType.BOOLEAN:
        if not isinstance(value, str):
            return None
        lowered = value.strip().lower()
        if lowered in _BOOL_TRUE_WORDS or lowered in _BOOL_FALSE_WORDS:
            fidelity = _FIDELITY_EXACT if value == lowered else _FIDELITY_WHITESPACE
            note = "string spells the boolean out"
        elif lowered in _BOOL_NUMERIC:
            fidelity = _FIDELITY_BOOL_NUMERIC
            note = "numeric string read as a boolean"
        else:
            return None
        # Reading a string as a boolean is an interpretation, not a
        # re-encoding -- "1" could legitimately have meant the integer 1.
        return RepairEvidence(value_preserved=fidelity, notes=(note,)), RepairRisk.INFERRED

    if target_type is FieldType.ARRAY:
        if _array_wrap_is_safe(value, item_type):
            # The element survives untouched, but cardinality is invented:
            # nothing in the payload said this was a one-element list.
            return (
                RepairEvidence(
                    value_preserved=_FIDELITY_EXACT,
                    # The value itself is untouched inside the list -- a dict
                    # stays a dict. Only cardinality is invented.
                    signals=(("structure_preserved", 1.0),),
                    notes=("value matches the declared item_type; wrapped as a single element",),
                ),
                RepairRisk.LOSSY,
            )
        return None

    if target_type is FieldType.UNION:
        resolved = resolve_union_member(value, union_members)
        if resolved is None:
            return None
        _member, evidence, risk = resolved
        return evidence, risk

    return None


def json_serialized(value: Any) -> str | None:
    """
    Return ``json.dumps(value)`` if *value* is a dict or list that
    serialises cleanly, else ``None``.

    Only containers qualify — scalars where a string is expected are a
    semantic mismatch, not an over-parsed JSON argument.  Containers
    holding non-JSON values (arbitrary objects, NaN under strict dumps,
    circular references) are refused rather than approximated.

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always produce the same serialisation.
    """
    if not isinstance(value, (dict, list)):
        return None
    try:
        return json.dumps(value)
    except (TypeError, ValueError):
        return None


def _array_wrap_is_safe(value: Any, item_type: FieldType | None) -> bool:
    """
    ``True`` if wrapping *value* as a single-element list is a safe repair
    for an ``ARRAY`` target.

    Refused when the value is already a list (that is an item-level
    problem, not a wrapping problem) and when *item_type* is unknown
    (wrapping into an untyped array would be a guess).
    """
    if isinstance(value, list):
        return False
    if item_type is None:
        return False
    return type_matches(value, item_type)


def resolve_union_member(
    value: Any,
    union_members: tuple[UnionMember, ...] | None,
) -> tuple[UnionMember, RepairEvidence, RepairRisk] | None:
    """
    Pick the union member *value* can be safely coerced to.

    Evaluates every member with the same rules as ``_coercion_evidence``
    (scalar casts; wrap-in-list for ``ARRAY`` members) and ranks them by:

    1. **fidelity** — how much of the value survives;
    2. **structure preservation** — whether the value keeps its own shape.
       A ``dict`` accepted by both a ``str`` member and a ``list`` member
       survives byte-for-byte either way, but ``[{...}]`` keeps it a mapping
       while ``'{"...": ...}'`` turns it into text.  Structured beats
       stringified;
    3. **risk** — the least consequential member wins a remaining tie.

    Returns ``None`` when no member is coercible, or when the top two rank
    identically on all three — a genuinely ambiguous union is refused rather
    than guessed at.

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always resolve to the same member.
    """
    if not union_members:
        return None

    candidates: list[tuple[tuple[float, float, int], UnionMember, RepairEvidence, RepairRisk]] = []
    for member in union_members:
        measured = _coercion_evidence(value, member.field_type, item_type=member.item_type)
        if measured is None:
            continue
        evidence, risk = measured
        fidelity = evidence.value_preserved if evidence.value_preserved is not None else 0.0
        structure = dict(evidence.signals).get("structure_preserved", 1.0)
        candidates.append(((fidelity, structure, -int(risk)), member, evidence, risk))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[0], reverse=True)
    if len(candidates) > 1 and candidates[0][0] == candidates[1][0]:
        return None

    _rank, member, evidence, risk = candidates[0]
    return member, evidence, risk


def _is_integer_string(value: str) -> bool:
    """
    ``True`` if *value* is a decimal integer string, optionally negative.

    Uses ``str.isdecimal`` rather than ``str.isdigit``: ``"²".isdigit()`` is
    ``True`` but ``int("²")`` raises, which previously produced a
    high-confidence coercion that silently did nothing when applied.
    """
    if value.isdecimal():
        return True
    return bool(value.startswith("-") and len(value) > 1 and value[1:].isdecimal())


def _is_float_string(value: str) -> bool:
    """
    ``True`` if *value* names a finite float.

    ``float()`` also accepts ``"nan"``, ``"inf"``, ``"infinity"`` and their
    signed forms.  Those are not lossless casts of a numeric string: NaN
    poisons every downstream comparison, and neither survives
    ``json.dumps`` as valid JSON.  They are refused here rather than given a
    high fidelity score.
    """
    try:
        parsed = float(value)
    except ValueError:
        return False
    return parsed == parsed and parsed not in (float("inf"), float("-inf"))


# ---------------------------------------------------------------------------
# TypeCoercionStrategy
# ---------------------------------------------------------------------------


class TypeCoercionStrategy(IRepairStrategy):
    """
    Proposes ``COERCE`` operations for TYPE_MISMATCH violations where a
    safe, lossless cast exists from the received value to the contract's
    declared type.
    """

    @property
    def name(self) -> str:
        return "TypeCoercionStrategy"

    @property
    def priority(self) -> int:
        return 30

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        return any(v.violation_type is ViolationType.TYPE_MISMATCH for v in violations)

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        operations: list[FieldOperation] = []

        for violation in violations:
            if violation.violation_type is not ViolationType.TYPE_MISMATCH:
                continue
            if violation.expected_type is None:
                continue

            value = _get_nested_value(data, violation.field_path)
            if value is _NOT_FOUND:
                continue

            item_type: FieldType | None = None
            union_members: tuple[UnionMember, ...] | None = None
            if violation.expected_type in (FieldType.ARRAY, FieldType.UNION):
                field_spec = _find_field_spec(contract, violation.field_path)
                if field_spec is not None:
                    item_type = field_spec.item_type
                    union_members = field_spec.union_members

            measured = _coercion_evidence(
                value,
                violation.expected_type,
                item_type=item_type,
                union_members=union_members,
            )
            if measured is None:
                continue
            evidence, risk = measured

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.COERCE,
                    target_path=violation.field_path,
                    # No value interpolation: rationale strings end up in log
                    # entries, and RepairConfig.include_values_in_log defaults
                    # to False precisely so runtime values do not.
                    rationale=(
                        f"Coerce {type(value).__name__} at "
                        f"'{violation.field_path}' to {violation.expected_type.value}."
                    ),
                    risk=risk,
                    evidence=evidence,
                )
            )

        return operations

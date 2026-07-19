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

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldType, UnionMember, type_matches

__all__ = ["TypeCoercionStrategy"]


# ---------------------------------------------------------------------------
# Confidence constants
# ---------------------------------------------------------------------------

_NUMERIC_COERCION_CONFIDENCE = 0.95
_BOOL_COERCION_CONFIDENCE = 0.85
_ARRAY_WRAP_CONFIDENCE = 0.9
_JSON_SERIALIZE_CONFIDENCE = 0.85

# Strings accepted for str -> bool coercion (case-insensitive).
_BOOL_STRINGS = {"true", "false", "1", "0"}


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


def _coercion_confidence(
    value: Any,
    target_type: FieldType,
    item_type: FieldType | None = None,
    union_members: tuple[UnionMember, ...] | None = None,
) -> float | None:
    """
    Return the confidence for coercing *value* to *target_type*, or
    ``None`` if no safe coercion is defined for this (value, target) pair.

    *item_type* is consulted only for ``ARRAY`` targets and *union_members*
    only for ``UNION`` targets; both come from the field's ``FieldSpec``.
    """
    if target_type in (FieldType.STRING, FieldType.BYTES):
        if json_serialized(value) is not None:
            return _JSON_SERIALIZE_CONFIDENCE
        return None

    if target_type is FieldType.INTEGER:
        if isinstance(value, str) and not isinstance(value, bool) and _is_integer_string(value):
            return _NUMERIC_COERCION_CONFIDENCE
        return None

    if target_type is FieldType.FLOAT:
        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            # int -> float is always safe.
            return _NUMERIC_COERCION_CONFIDENCE
        if isinstance(value, str) and _is_float_string(value):
            return _NUMERIC_COERCION_CONFIDENCE
        return None

    if target_type is FieldType.BOOLEAN:
        if isinstance(value, str) and value.strip().lower() in _BOOL_STRINGS:
            return _BOOL_COERCION_CONFIDENCE
        return None

    if target_type is FieldType.ARRAY:
        if _array_wrap_is_safe(value, item_type):
            return _ARRAY_WRAP_CONFIDENCE
        return None

    if target_type is FieldType.UNION:
        resolved = resolve_union_member(value, union_members)
        if resolved is None:
            return None
        member, confidence = resolved
        return confidence

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
) -> tuple[UnionMember, float] | None:
    """
    Pick the union member *value* can be safely coerced to.

    Evaluates every member with the same rules as ``_coercion_confidence``
    (scalar casts; wrap-in-list for ``ARRAY`` members) and returns the
    single member with the strictly highest confidence, together with that
    confidence.  Returns ``None`` when no member is coercible or when two
    or more members tie at the top (ambiguous — refusing is the safe
    default).

    Shared with the engine's ``_coerce_value`` so that feasibility and
    application always resolve to the same member.
    """
    if not union_members:
        return None

    candidates: list[tuple[UnionMember, float]] = []
    for member in union_members:
        confidence = _coercion_confidence(value, member.field_type, item_type=member.item_type)
        if confidence is not None:
            candidates.append((member, confidence))

    if not candidates:
        return None

    candidates.sort(key=lambda c: c[1], reverse=True)
    if len(candidates) > 1 and candidates[0][1] == candidates[1][1]:
        return None
    return candidates[0]


def _is_integer_string(value: str) -> bool:
    """``True`` if *value* is a digit string, optionally negative."""
    if value.isdigit():
        return True
    return bool(value.startswith("-") and len(value) > 1 and value[1:].isdigit())


def _is_float_string(value: str) -> bool:
    """``True`` if ``float(value)`` would succeed."""
    try:
        float(value)
    except ValueError:
        return False
    return True


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

            confidence = _coercion_confidence(
                value,
                violation.expected_type,
                item_type=item_type,
                union_members=union_members,
            )
            if confidence is None:
                continue

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.COERCE,
                    target_path=violation.field_path,
                    confidence=confidence,
                    rationale=(
                        f"Coerce {type(value).__name__} value "
                        f"{value!r} to {violation.expected_type.value} "
                        f"for field '{violation.field_path}'."
                    ),
                )
            )

        return operations

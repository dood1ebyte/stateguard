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

All other combinations are left unrepaired by this strategy (no operation
is proposed; ``confidence`` is never fabricated for unsafe casts).

This strategy determines *feasibility and confidence* only.  The actual
cast is performed by the engine when applying the ``COERCE`` operation,
using ``ContractSpec`` to look up the target ``FieldType``.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec
from stateguard.core.models.field_types import FieldType

__all__ = ["TypeCoercionStrategy"]


# ---------------------------------------------------------------------------
# Confidence constants
# ---------------------------------------------------------------------------

_NUMERIC_COERCION_CONFIDENCE = 0.95
_BOOL_COERCION_CONFIDENCE = 0.85

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


# ---------------------------------------------------------------------------
# Coercion feasibility
# ---------------------------------------------------------------------------


def _coercion_confidence(value: Any, target_type: FieldType) -> float | None:
    """
    Return the confidence for coercing *value* to *target_type*, or
    ``None`` if no safe coercion is defined for this (value, target) pair.
    """
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

    return None


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

            confidence = _coercion_confidence(value, violation.expected_type)
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

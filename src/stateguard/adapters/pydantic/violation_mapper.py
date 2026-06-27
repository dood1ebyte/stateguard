"""
PydanticViolationMapper — maps Pydantic ``ValidationError`` to
``List[ContractViolation]``.

This is the second piece of inward translation in the Pydantic adapter:
where ``PydanticContractExtractor`` translates the *schema*,
``PydanticViolationMapper`` translates *validation failures*.

Error-type mapping
-------------------
Pydantic v2's ``ValidationError.errors()`` returns a list of dicts with a
``type`` field identifying the failure kind.  These are mapped to
``ViolationType`` as follows::

    "missing"                                    -> MISSING_REQUIRED_FIELD
    "extra_forbidden"                            -> UNEXPECTED_FIELD
    "none_required"                              -> NULL_NOT_ALLOWED
    "*_type"  (string_type, int_type, ...,
               model_type, list_type, dict_type) -> TYPE_MISMATCH
    "*_parsing" (int_parsing, float_parsing,
                 bool_parsing, ...)               -> TYPE_MISMATCH
    "greater_than[_equal]", "less_than[_equal]",
    "*_too_short", "*_too_long",
    "string_pattern_mismatch", "literal_error",
    "value_error"                                 -> VALUE_CONSTRAINT_VIOLATION
    (anything else, unrecognized)                 -> VALUE_CONSTRAINT_VIOLATION

All Pydantic ``ValidationError`` entries are treated as
``ViolationSeverity.ERROR`` -- Pydantic has no concept of a "warning".

``field_path`` is built by joining the error's ``loc`` tuple with ``"."``,
converting non-string segments (e.g. list indices) to strings.

``expected_type`` is looked up from the ``ContractSpec`` by ``field_path``
(via the same ``FieldSpec`` recursion pattern used by the repair
strategies), so that ``TypeCoercionStrategy`` knows the target type.

``received_value`` is taken from the error's ``input`` field, except for
``"missing"`` errors where ``input`` is the *parent* dict (not the missing
field's value) -- for those, ``received_value`` is left as ``None``.

``expected_value`` (the constraint boundary) is taken from the error's
``ctx`` dict where present (``ge``, ``le``, ``min_length``, ``max_length``,
``pattern``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.models.contract import ContractSpec, FieldSpec

if TYPE_CHECKING:
    from pydantic import ValidationError

__all__ = ["PydanticViolationMapper"]


# ---------------------------------------------------------------------------
# Error-type classification
# ---------------------------------------------------------------------------

# Exact-match error types with a direct ViolationType mapping.
_EXACT_TYPE_MAP: dict[str, ViolationType] = {
    "missing": ViolationType.MISSING_REQUIRED_FIELD,
    "extra_forbidden": ViolationType.UNEXPECTED_FIELD,
    "none_required": ViolationType.NULL_NOT_ALLOWED,
}

# Error types (or suffixes) that indicate a constraint violation rather than
# a structural/type problem.
_CONSTRAINT_SUFFIXES: tuple[str, ...] = (
    "too_short",
    "too_long",
)
_CONSTRAINT_EXACT: frozenset[str] = frozenset({
    "greater_than",
    "greater_than_equal",
    "less_than",
    "less_than_equal",
    "string_pattern_mismatch",
    "literal_error",
    "value_error",
})

# Keys in a Pydantic error's `ctx` dict that represent a constraint boundary,
# checked in priority order.
_CTX_BOUND_KEYS: tuple[str, ...] = (
    "ge", "le", "gt", "lt",
    "min_length", "max_length",
    "pattern", "expected",
)


def _classify(error_type: str) -> ViolationType:
    """Map a Pydantic error ``type`` string to a ``ViolationType``."""
    if error_type in _EXACT_TYPE_MAP:
        return _EXACT_TYPE_MAP[error_type]

    if error_type.endswith("_type") or error_type.endswith("_parsing"):
        return ViolationType.TYPE_MISMATCH

    if error_type in _CONSTRAINT_EXACT:
        return ViolationType.VALUE_CONSTRAINT_VIOLATION

    if any(error_type.endswith(suffix) for suffix in _CONSTRAINT_SUFFIXES):
        return ViolationType.VALUE_CONSTRAINT_VIOLATION

    # Unrecognized error type: treat as a constraint violation. This is the
    # safest default -- it surfaces as an ERROR-severity violation without
    # implying a specific repair strategy should fire (no built-in V1
    # strategy targets VALUE_CONSTRAINT_VIOLATION).
    return ViolationType.VALUE_CONSTRAINT_VIOLATION


# ---------------------------------------------------------------------------
# Field path helpers
# ---------------------------------------------------------------------------


def _loc_to_field_path(loc: tuple[Any, ...]) -> str:
    """Convert a Pydantic error ``loc`` tuple to a dot-notation field path."""
    return ".".join(str(part) for part in loc)


def _find_field_spec(contract: ContractSpec, full_path: str) -> FieldSpec | None:
    """
    Locate the ``FieldSpec`` for a dot-notation *full_path* within *contract*,
    recursing into ``nested_spec`` for nested paths.

    Duplicated from the repair-strategy modules to keep this adapter
    self-contained (no core->adapter or adapter->adapter coupling).
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
# PydanticViolationMapper
# ---------------------------------------------------------------------------


class PydanticViolationMapper:
    """Maps a Pydantic ``ValidationError`` to ``list[ContractViolation]``."""

    @classmethod
    def map(
        cls,
        error: ValidationError,
        contract: ContractSpec,
    ) -> list[ContractViolation]:
        """
        Convert every entry in ``error.errors()`` to a ``ContractViolation``.

        Parameters
        ----------
        error:
            The ``pydantic.ValidationError`` raised by ``model_validate``.
        contract:
            The normalised contract, used to look up ``expected_type`` for
            each violation's ``field_path``.

        Returns
        -------
        list[ContractViolation]
            One violation per entry in ``error.errors()``, in the same
            order.  All entries have ``severity = ViolationSeverity.ERROR``.
        """
        violations: list[ContractViolation] = []
        for err in error.errors():
            violations.append(cls._map_single(err, contract))
        return violations

    @classmethod
    def _map_single(
        cls,
        err: Any,
        contract: ContractSpec,
    ) -> ContractViolation:
        error_type: str = err.get("type", "")
        loc: tuple[Any, ...] = err.get("loc", ())
        field_path = _loc_to_field_path(loc)
        violation_type = _classify(error_type)

        field_spec = _find_field_spec(contract, field_path)
        expected_type = field_spec.field_type if field_spec is not None else None

        received_value: Any = None
        if violation_type is not ViolationType.MISSING_REQUIRED_FIELD:
            received_value = err.get("input")

        expected_value = cls._extract_expected_value(err)

        return ContractViolation(
            field_path=field_path,
            violation_type=violation_type,
            severity=ViolationSeverity.ERROR,
            message=err.get("msg", ""),
            expected_type=expected_type,
            expected_value=expected_value,
            received_value=received_value,
        )

    @staticmethod
    def _extract_expected_value(err: dict[str, Any]) -> Any:
        """
        Extract a constraint boundary from a Pydantic error's ``ctx`` dict,
        if present.

        Checks ``_CTX_BOUND_KEYS`` in priority order and returns the first
        present key's value.  Returns ``None`` if ``ctx`` is absent or
        contains none of these keys.
        """
        ctx = err.get("ctx")
        if not isinstance(ctx, dict):
            return None
        for key in _CTX_BOUND_KEYS:
            if key in ctx:
                return ctx[key]
        return None

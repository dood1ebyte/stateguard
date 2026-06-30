"""
Field type vocabulary and constraint descriptors.

These are the primitive building blocks used by ``ContractSpec`` to describe
what a contract field looks like.  The engine reasons over ``FieldType`` values
instead of raw Python types, keeping it independent of any specific framework's
type system.

Adapters are responsible for mapping their native type representations
(Pydantic annotations, JSON Schema type strings, etc.) into these enumerations.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "FieldConstraint",
    "FieldConstraintType",
    "FieldType",
]


# ---------------------------------------------------------------------------
# FieldType
# ---------------------------------------------------------------------------


class FieldType(StrEnum):
    """
    Abstract type vocabulary for contract fields.

    The engine uses this closed set instead of Python's open type system so
    that repair strategies can reason about types without depending on any
    particular framework.  Adapters translate their native types into these
    values.

    Notes
    -----
    ``NULL`` represents a field whose *declared* type is null/None (e.g.
    JSON Schema ``"type": "null"``).  It is distinct from a field being
    *optional* (``FieldSpec.required = False``) or a field receiving a
    ``None`` value that violates a ``NOT_NULL`` constraint.

    ``ANY`` means the field is explicitly untyped.  The validator skips
    type-checking for ``ANY`` fields; strategies still apply.
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    OBJECT = "object"   # nested dict / sub-schema
    ARRAY = "array"     # list of items (item type tracked separately)
    ANY = "any"         # explicitly untyped; validator skips type checks
    NULL = "null"       # field's declared type is null


# ---------------------------------------------------------------------------
# FieldConstraintType
# ---------------------------------------------------------------------------


class FieldConstraintType(StrEnum):
    """
    Category of a constraint applied to a field's *value*, beyond type-checking.

    These map directly to the validation rules that adapters extract from
    their native schemas (Pydantic ``Field(ge=0)``, JSON Schema ``minimum``,
    etc.).
    """

    MINIMUM = "minimum"         # numeric lower bound (inclusive)
    MAXIMUM = "maximum"         # numeric upper bound (inclusive)
    MIN_LENGTH = "min_length"   # minimum string / array length
    MAX_LENGTH = "max_length"   # maximum string / array length
    PATTERN = "pattern"         # regex pattern the string must match
    ENUM_VALUES = "enum_values" # field value must be one of a fixed set
    NOT_NULL = "not_null"       # field must not be None


# ---------------------------------------------------------------------------
# FieldConstraint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldConstraint:
    """
    A single constraint applied to a field's value, beyond type-checking.

    Immutable by design — constraints are defined once (at contract-extraction
    time) and never mutated.

    Attributes
    ----------
    constraint_type:
        The category of this constraint.
    value:
        The constraint parameter (e.g. ``0`` for ``MINIMUM``, ``r"^\\d+$"``
        for ``PATTERN``, ``("a", "b")`` for ``ENUM_VALUES``).

    Hashability
    -----------
    ``FieldConstraint`` is a frozen dataclass, so its ``__hash__`` is derived
    from all fields including ``value``.  Therefore **``value`` must be
    hashable**.  All V1 constraint values are hashable primitives or tuples of
    primitives (int, float, str, bool, tuple).  Passing a mutable container
    (list, dict) as ``value`` will raise ``TypeError`` when the constraint is
    hashed (e.g. placed in a set).
    """

    constraint_type: FieldConstraintType
    value: Any

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
    "UnionMember",
    "type_matches",
    "union_member_matches",
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

    ``UNION`` means the field accepts more than one type; the accepted
    member types are tracked separately via ``FieldSpec.union_members``.
    A ``UNION`` field with no ``union_members`` behaves like ``ANY``.
    """

    STRING = "string"
    INTEGER = "integer"
    FLOAT = "float"
    BOOLEAN = "boolean"
    OBJECT = "object"  # nested dict / sub-schema
    ARRAY = "array"  # list of items (item type tracked separately)
    ANY = "any"  # explicitly untyped; validator skips type checks
    NULL = "null"  # field's declared type is null
    UNION = "union"  # multiple accepted types (see FieldSpec.union_members)


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

    MINIMUM = "minimum"  # numeric lower bound (inclusive)
    MAXIMUM = "maximum"  # numeric upper bound (inclusive)
    MIN_LENGTH = "min_length"  # minimum string / array length
    MAX_LENGTH = "max_length"  # maximum string / array length
    PATTERN = "pattern"  # regex pattern the string must match
    ENUM_VALUES = "enum_values"  # field value must be one of a fixed set
    NOT_NULL = "not_null"  # field must not be None


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


# ---------------------------------------------------------------------------
# UnionMember
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UnionMember:
    """
    One accepted type of a ``FieldType.UNION`` field.

    Attributes
    ----------
    field_type:
        The abstract type this member accepts.  ``UNION`` itself is not a
        valid member type — adapters must flatten nested unions.
    item_type:
        For ``ARRAY`` members, the element type.  A member whose elements
        are themselves a union collapses to ``item_type=ANY`` (per-element
        union checking is not supported; the framework-native revalidation
        remains the source of truth).  ``None`` for non-array members.
    """

    field_type: FieldType
    item_type: FieldType | None = None


# ---------------------------------------------------------------------------
# Value/type compatibility
# ---------------------------------------------------------------------------


def type_matches(value: Any, field_type: FieldType) -> bool:
    """
    Return ``True`` if *value*'s Python type is compatible with *field_type*.

    Shared by ``ContractValidator``, ``TypeCoercionStrategy``, and the
    engine's coercion applier so that feasibility checks, application, and
    revalidation always agree on type compatibility.

    Rules
    -----
    * ``ANY``     — always ``True``.
    * ``NULL``    — only ``None``.
    * ``BOOLEAN`` — only ``bool`` (checked before INTEGER since
      ``bool`` is a subclass of ``int``).
    * ``INTEGER`` — ``int`` but not ``bool``.
    * ``FLOAT``   — ``int`` or ``float`` but not ``bool``
      (an int value is an acceptable float).
    * ``STRING``  — only ``str``.
    * ``OBJECT``  — only ``dict``.
    * ``ARRAY``   — only ``list``.
    * ``UNION``   — always ``True``.  Members are not available here;
      callers with access to the ``FieldSpec`` should use
      ``union_member_matches`` per member instead.
    """
    if field_type is FieldType.ANY:
        return True
    if field_type is FieldType.NULL:
        return value is None
    if value is None:
        return False
    if field_type is FieldType.BOOLEAN:
        return isinstance(value, bool)
    if field_type is FieldType.INTEGER:
        return isinstance(value, int) and not isinstance(value, bool)
    if field_type is FieldType.FLOAT:
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if field_type is FieldType.STRING:
        return isinstance(value, str)
    if field_type is FieldType.OBJECT:
        return isinstance(value, dict)
    if field_type is FieldType.ARRAY:
        return isinstance(value, list)
    return True


def union_member_matches(value: Any, member: UnionMember) -> bool:
    """
    Return ``True`` if *value* is acceptable for *member*.

    For ``ARRAY`` members the value must be a list whose every element
    matches ``member.item_type`` (an unset ``item_type`` accepts any
    element).  For all other members this is ``type_matches`` on the
    member's ``field_type``.
    """
    if member.field_type is FieldType.ARRAY:
        if not isinstance(value, list):
            return False
        if member.item_type is None:
            return True
        return all(type_matches(item, member.item_type) for item in value)
    return type_matches(value, member.field_type)

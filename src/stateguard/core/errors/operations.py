"""
Field operation types used by repair strategies to describe proposed fixes.

A FieldOperation is the atomic unit of repair.  Strategies propose lists of
FieldOperation objects; the RepairEngine applies those that meet the configured
confidence threshold and records the rest as rejected in the audit trail.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

__all__ = [
    "FieldOperation",
    "FieldOpType",
]


# ---------------------------------------------------------------------------
# FieldOpType
# ---------------------------------------------------------------------------


class FieldOpType(StrEnum):
    """
    The type of atomic change a repair strategy can propose.

    Members
    -------
    RENAME:
        Move a value from ``source_path`` to ``target_path`` and remove the
        source key.  Requires ``source_path`` to be set.
    COERCE:
        Cast the value already present at ``target_path`` to a different type
        (e.g. ``"30"`` → ``30`` for an INTEGER field).  ``source_path`` and
        ``value`` are unused.
    SET_DEFAULT:
        Insert a missing required field at ``target_path`` using its declared
        ``FieldSpec.default`` value.  The value is carried in
        ``FieldOperation.value``.
    REMOVE:
        Delete the key at ``target_path`` from the data.  Used in strict mode
        to remove unexpected fields.  ``source_path`` and ``value`` are unused.
    SET_VALUE:
        Force a specific value at ``target_path``.  Last-resort operation;
        strategies should assign low confidence.  The forced value is carried
        in ``FieldOperation.value``.
    """

    RENAME = "rename"
    COERCE = "coerce"
    SET_DEFAULT = "set_default"
    REMOVE = "remove"
    SET_VALUE = "set_value"


# ---------------------------------------------------------------------------
# FieldOperation
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldOperation:
    """
    An atomic repair operation proposed by a strategy.

    Immutability
    ------------
    ``FieldOperation`` is a **frozen** dataclass.  Once proposed, an operation
    is never modified.  The engine applies or rejects operations but does not
    alter them, ensuring the ``RepairAttempt`` audit trail is a faithful record
    of what was proposed.

    Hashability
    -----------
    Because this is a frozen dataclass, instances are hashable and can be
    placed in sets or used as dict keys.  This requires ``value`` to be
    hashable.  V1 guarantees that all operation values are primitive types
    (``str``, ``int``, ``float``, ``bool``, ``None``) — all hashable.
    Passing a mutable container as ``value`` will raise ``TypeError`` at hash
    time.

    Attributes
    ----------
    op_type:
        The type of operation to perform.
    target_path:
        Dot-notation path to the field being written, coerced, or removed.
    confidence:
        The strategy's confidence in this operation, in ``[0.0, 1.0]``.
        The engine only applies operations where
        ``confidence >= RepairConfig.min_confidence_threshold``.
    rationale:
        Human-readable explanation included in repair log entries and the
        ``RepairAttempt`` audit trail.
    source_path:
        Dot-notation path to read from.  **Required** when
        ``op_type == FieldOpType.RENAME``; ``None`` for all other op types.
    value:
        Value to write.  Used only by ``SET_DEFAULT`` and ``SET_VALUE``;
        ``None`` for ``RENAME``, ``COERCE``, and ``REMOVE``.

        .. note::
           ``value`` accepts ``Any`` including mutable types.  The frozen
           constraint prevents field *reassignment* but does not deep-freeze
           the value itself.  V1 values are always primitives.
    """

    # Required positional fields
    op_type: FieldOpType
    target_path: str
    confidence: float
    rationale: str

    # Optional fields
    source_path: str | None = None
    value: Any = None

    def __post_init__(self) -> None:
        if not (0.0 <= self.confidence <= 1.0):
            raise ValueError(
                f"confidence must be in [0.0, 1.0], got {self.confidence!r}"
            )
        if self.op_type is FieldOpType.RENAME and self.source_path is None:
            raise ValueError(
                "FieldOperation with op_type=RENAME requires source_path to be set."
            )

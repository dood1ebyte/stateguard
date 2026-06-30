"""
Contract violation types and the ContractViolation domain object.

A ContractViolation describes a single way in which actual output deviates
from its contract.  Violations are produced by ContractValidator and consumed
by repair strategies and the RepairEngine.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from stateguard.core.models.field_types import FieldType

__all__ = [
    "ContractViolation",
    "ViolationSeverity",
    "ViolationType",
]


# ---------------------------------------------------------------------------
# ViolationType
# ---------------------------------------------------------------------------


class ViolationType(StrEnum):
    """
    Categories of contract violations the engine can detect and repair.

    Members
    -------
    MISSING_REQUIRED_FIELD:
        A field declared as required in the contract is absent from the data.
    UNEXPECTED_FIELD:
        The data contains a key that has no corresponding FieldSpec in the
        contract.  Severity depends on ``GuardConfig.strict_mode``.
    TYPE_MISMATCH:
        A field is present but its value's Python type does not match the
        declared ``FieldType``.
    VALUE_CONSTRAINT_VIOLATION:
        A field is present and correctly typed, but its value violates a
        ``FieldConstraint`` (e.g. below minimum, exceeds max_length).
    NULL_NOT_ALLOWED:
        A field is present but its value is ``None`` and the field is
        declared non-nullable (``FieldConstraintType.NOT_NULL``).
    STRUCTURAL_MISMATCH:
        The shape of the data is fundamentally wrong — e.g. a list was
        received where a dict was expected.  ``field_path`` is typically
        empty string for root-level structural violations.
    """

    MISSING_REQUIRED_FIELD = "missing_required_field"
    UNEXPECTED_FIELD = "unexpected_field"
    TYPE_MISMATCH = "type_mismatch"
    VALUE_CONSTRAINT_VIOLATION = "value_constraint_violation"
    NULL_NOT_ALLOWED = "null_not_allowed"
    STRUCTURAL_MISMATCH = "structural_mismatch"


# ---------------------------------------------------------------------------
# ViolationSeverity
# ---------------------------------------------------------------------------


class ViolationSeverity(StrEnum):
    """
    Indicates how critical a violation is.

    ERROR:
        Repair is required; the output cannot be trusted or forwarded as-is.
        All MISSING_REQUIRED_FIELD, TYPE_MISMATCH, NULL_NOT_ALLOWED, and
        STRUCTURAL_MISMATCH violations are ERROR by default.
    WARNING:
        The output may still be usable; repair is advisory.
        UNEXPECTED_FIELD is WARNING by default (ERROR in strict_mode).
    """

    ERROR = "error"
    WARNING = "warning"


# ---------------------------------------------------------------------------
# ContractViolation
# ---------------------------------------------------------------------------


@dataclass
class ContractViolation:
    """
    Describes a single deviation between actual output and its contract.

    Mutability
    ----------
    ``ContractViolation`` is intentionally **mutable**.  The correlation step
    in ``RepairEngine`` populates ``related_ids`` after initial detection,
    linking e.g. a ``MISSING_REQUIRED_FIELD`` violation for ``"temperature"``
    with the ``UNEXPECTED_FIELD`` violation for ``"temp_celsius"`` so that
    strategies can treat them as a single rename operation.

    Attributes
    ----------
    field_path:
        Dot-notation path to the offending field (e.g. ``"address.city"``).
        Empty string ``""`` is valid for root-level structural violations.
    violation_type:
        The category of this deviation.
    severity:
        ERROR or WARNING.
    message:
        Human-readable explanation suitable for logs and error reporting.
    expected_type:
        The ``FieldType`` declared in the ``ContractSpec``, when applicable
        (primarily used for TYPE_MISMATCH and MISSING_REQUIRED_FIELD).
    expected_value:
        The constraint boundary that was violated, when applicable
        (e.g. the minimum value for a VALUE_CONSTRAINT_VIOLATION).
    received_value:
        The actual value that triggered the violation.

        .. caution::
           This field may contain sensitive runtime data.  Whether it is
           included in log output is controlled by
           ``RepairConfig.include_values_in_log``.

    related_ids:
        ``violation_id`` values of correlated violations.  Populated by the
        engine's correlation step, not by ``ContractValidator``.
    violation_id:
        Stable identifier for this instance within a single repair session.
        Auto-generated as a UUID4 string.  Can be overridden explicitly (e.g.
        in tests) by passing ``violation_id=`` at construction time.
    """

    # --- Required fields (no defaults) ----------------------------------------
    field_path: str
    violation_type: ViolationType
    severity: ViolationSeverity
    message: str

    # --- Optional contextual fields -------------------------------------------
    expected_type: FieldType | None = None
    expected_value: Any = None
    received_value: Any = None

    # --- Mutable correlation field --------------------------------------------
    related_ids: list[str] = field(default_factory=list)

    # --- Auto-generated identity ----------------------------------------------
    violation_id: str = field(default_factory=lambda: str(uuid.uuid4()))

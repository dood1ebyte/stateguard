"""
DefaultValueFillStrategy — repairs MISSING_REQUIRED_FIELD via declared defaults.

Priority 40 (runs last among the four V1 strategies).  Fires only when the
contract declares an explicit default for the missing field
(``FieldSpec.default is not MISSING``).  Reports
``schema_authority = 1.0`` at ``RepairRisk.DECLARED``: filling a field with
its own declared default is the contract speaking, not an inference, so there
is nothing for a margin to erode.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import (
    FieldOperation,
    FieldOpType,
    RepairEvidence,
    RepairRisk,
)
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import MISSING, ContractSpec, find_field_spec

__all__ = ["DefaultValueFillStrategy"]


# ---------------------------------------------------------------------------
# Path helper (private to this module)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# DefaultValueFillStrategy
# ---------------------------------------------------------------------------


class DefaultValueFillStrategy(IRepairStrategy):
    """
    Fills a MISSING_REQUIRED_FIELD violation with the field's declared default.

    Only fires when ``FieldSpec.default is not MISSING`` (the sentinel).
    A declared default of ``None`` is a valid, distinct value from "no
    default declared" and is filled normally.

    Declares ``RepairRisk.DECLARED`` with ``schema_authority = 1.0`` —
    using a schema-declared default is definitionally correct per the
    contract. ``TrustPolicy`` turns that into the score.
    """

    @property
    def name(self) -> str:
        return "DefaultValueFillStrategy"

    @property
    def priority(self) -> int:
        return 40

    def can_handle(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> bool:
        for violation in violations:
            if violation.violation_type is not ViolationType.MISSING_REQUIRED_FIELD:
                continue
            field_spec = find_field_spec(contract, violation.field_path)
            if field_spec is not None and field_spec.default is not MISSING:
                return True
        return False

    def propose(
        self,
        violations: list[ContractViolation],
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> list[FieldOperation]:
        operations: list[FieldOperation] = []

        for violation in violations:
            if violation.violation_type is not ViolationType.MISSING_REQUIRED_FIELD:
                continue

            field_spec = find_field_spec(contract, violation.field_path)
            if field_spec is None or field_spec.default is MISSING:
                continue

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.SET_DEFAULT,
                    target_path=violation.field_path,
                    rationale=(f"Field '{violation.field_path}' has a declared default value."),
                    value=field_spec.default,
                    # Using a field's own declared default is definitionally
                    # correct per the contract -- the schema is the authority.
                    risk=RepairRisk.DECLARED,
                    evidence=RepairEvidence(
                        schema_authority=1.0,
                        notes=(f"'{violation.field_path}' declares a default in the contract",),
                    ),
                )
            )

        return operations

"""
DefaultValueFillStrategy — repairs MISSING_REQUIRED_FIELD via declared defaults.

Priority 40 (runs last among the four V1 strategies).  Fires only when the
contract declares an explicit default for the missing field
(``FieldSpec.default is not MISSING``).  Confidence is always ``1.0`` —
filling a field with its own declared default is the lowest-risk repair
available.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec

__all__ = ["DefaultValueFillStrategy"]


# ---------------------------------------------------------------------------
# Path helper (private to this module)
# ---------------------------------------------------------------------------


def _find_field_spec(contract: ContractSpec, full_path: str) -> FieldSpec | None:
    """
    Locate the ``FieldSpec`` for a dot-notation *full_path* within *contract*,
    recursing into ``nested_spec`` for nested paths.

    See ``stateguard.core.strategies.alias._find_field_spec`` for the
    rationale; duplicated here to keep each strategy module self-contained.
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
# DefaultValueFillStrategy
# ---------------------------------------------------------------------------


class DefaultValueFillStrategy(IRepairStrategy):
    """
    Fills a MISSING_REQUIRED_FIELD violation with the field's declared default.

    Only fires when ``FieldSpec.default is not MISSING`` (the sentinel).
    A declared default of ``None`` is a valid, distinct value from "no
    default declared" and is filled normally.

    Confidence is always ``1.0`` — using a schema-declared default is
    definitionally correct per the contract.
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
            field_spec = _find_field_spec(contract, violation.field_path)
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

            field_spec = _find_field_spec(contract, violation.field_path)
            if field_spec is None or field_spec.default is MISSING:
                continue

            operations.append(
                FieldOperation(
                    op_type=FieldOpType.SET_DEFAULT,
                    target_path=violation.field_path,
                    confidence=1.0,
                    rationale=(
                        f"Field '{violation.field_path}' has a declared "
                        f"default value of {field_spec.default!r}."
                    ),
                    value=field_spec.default,
                )
            )

        return operations

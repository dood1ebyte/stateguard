"""
ExactAliasStrategy — repairs MISSING_REQUIRED_FIELD via declared aliases.

Priority 10 (runs first).  This is the highest-confidence, lowest-risk
strategy: it only fires when the contract explicitly declares that a field
may appear under an alternative name (e.g. Pydantic ``Field(alias="temp")``),
and only proposes a rename when that exact alias is present in the data.
"""

from __future__ import annotations

from typing import Any

from stateguard.core.errors.operations import FieldOperation, FieldOpType
from stateguard.core.errors.violations import ContractViolation, ViolationType
from stateguard.core.interfaces.strategy import IRepairStrategy
from stateguard.core.models.contract import ContractSpec, FieldSpec

__all__ = ["ExactAliasStrategy"]


# ---------------------------------------------------------------------------
# Path helpers (private to this module)
# ---------------------------------------------------------------------------


def _find_field_spec(contract: ContractSpec, full_path: str) -> FieldSpec | None:
    """
    Locate the ``FieldSpec`` for a dot-notation *full_path* within *contract*,
    recursing into ``nested_spec`` for nested paths.

    ``FieldSpec.path`` for fields inside a nested contract stores only the
    local segment (e.g. ``"zip_code"``), while violation field paths are
    fully qualified (e.g. ``"address.zip_code"``).  This helper bridges
    the two.
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


def _split_path(full_path: str) -> tuple[str, str]:
    """Split *full_path* into ``(parent_path, local_name)``."""
    if "." in full_path:
        parent, local = full_path.rsplit(".", 1)
        return parent, local
    return "", full_path


def _get_dict_at_path(data: dict[str, Any], parent_path: str) -> dict[str, Any] | None:
    """
    Navigate *data* to the dict located at *parent_path* (dot-notation).

    Returns ``None`` if *parent_path* is empty (root), the path does not
    exist, or any intermediate value is not a dict.
    """
    if not parent_path:
        return data
    current: Any = data
    for part in parent_path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current if isinstance(current, dict) else None


# ---------------------------------------------------------------------------
# ExactAliasStrategy
# ---------------------------------------------------------------------------


class ExactAliasStrategy(IRepairStrategy):
    """
    Renames a field to its contract path when an exact declared alias is
    present in the data.

    Confidence is always ``1.0`` — this strategy only fires on an exact
    string match against ``FieldSpec.known_aliases``, which is populated
    by adapters from schema-declared aliases (e.g. Pydantic
    ``Field(alias="temp")`` or ``validation_alias``).

    No fuzzy matching is performed here; see ``FuzzyFieldMatchStrategy``
    for heuristic renames.
    """

    @property
    def name(self) -> str:
        return "ExactAliasStrategy"

    @property
    def priority(self) -> int:
        return 10

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
            if field_spec is not None and field_spec.known_aliases:
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
            if field_spec is None or not field_spec.known_aliases:
                continue

            parent_path, _local_name = _split_path(violation.field_path)
            local_data = _get_dict_at_path(data, parent_path)
            if local_data is None:
                continue

            for alias in field_spec.known_aliases:
                if alias in local_data:
                    source_path = (
                        f"{parent_path}.{alias}" if parent_path else alias
                    )
                    operations.append(
                        FieldOperation(
                            op_type=FieldOpType.RENAME,
                            target_path=violation.field_path,
                            confidence=1.0,
                            rationale=(
                                f"Exact alias match: '{alias}' is a declared "
                                f"alias for '{violation.field_path}'."
                            ),
                            source_path=source_path,
                        )
                    )
                    break  # one rename per missing field; first alias wins

        return operations

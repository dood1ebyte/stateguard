"""
ContractValidator — framework-agnostic detection of contract violations.

This is the engine's own validator, used for initial violation analysis to
guide strategy selection.  It is independent of any adapter's native
validator (e.g. Pydantic's ``model_validate``), which remains the source of
truth for revalidation via ``IContractAdapter.validate``.

Zero external dependencies — part of Layer 3 (depends on Layers 0-2:
field_types, contract, violations).
"""

from __future__ import annotations

import re
from typing import Any

from stateguard.core.errors.results import ValidationResult
from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.models.contract import ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
    type_matches,
    union_member_matches,
)

__all__ = ["ContractValidator"]


# ---------------------------------------------------------------------------
# Numeric / sized type aliases for constraint checks
# ---------------------------------------------------------------------------

_SIZED_TYPES = (str, list, dict)
_NUMERIC_TYPES = (int, float)


class ContractValidator:
    """
    Detects contract violations between a ``ContractSpec`` and a data dict.

    Behaviour
    ---------
    * **Non-halting** — all violations are collected before returning;
      a single ``validate()`` call may return many violations.
    * **Recursive** — nested ``FieldSpec.nested_spec`` (``OBJECT`` fields)
      are validated by recursing with dot-notation path prefixes.
    * **Graceful on missing nested objects** — if a required ``OBJECT``
      field is absent, exactly one ``MISSING_REQUIRED_FIELD`` violation is
      emitted for that field; nested fields are not individually reported.
    * **Constraint-aware** — ``FieldConstraint`` objects are checked only
      when the field is present, non-``None``, and type-correct.

    This validator does not consult any framework-native validator; it is
    used purely for pre-repair violation analysis and strategy selection.
    """

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """
        Validate *data* against *contract*, returning all violations found.

        Parameters
        ----------
        contract:
            The normalised contract to validate against.
        data:
            The data dict to validate.  Not mutated.

        Returns
        -------
        ValidationResult
            ``is_valid`` is ``True`` iff no ``ViolationSeverity.ERROR``
            violations were found (``WARNING`` violations do not affect
            ``is_valid``).
        """
        violations: list[ContractViolation] = []
        self._validate_fields(contract, data, prefix="", violations=violations)
        self._detect_unexpected(contract, data, prefix="", violations=violations)

        is_valid = not any(v.severity is ViolationSeverity.ERROR for v in violations)
        return ValidationResult(
            is_valid=is_valid,
            violations=violations,
            raw_input=dict(data),
            contract_id=contract.contract_id,
        )

    # ------------------------------------------------------------------
    # Field-level validation
    # ------------------------------------------------------------------

    def _validate_fields(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
        prefix: str,
        violations: list[ContractViolation],
    ) -> None:
        """Validate every FieldSpec in *contract* against *data*."""
        for field_spec in contract.fields:
            self._validate_field(field_spec, data, prefix, violations)

    def _validate_field(
        self,
        field_spec: FieldSpec,
        data: dict[str, Any],
        prefix: str,
        violations: list[ContractViolation],
    ) -> None:
        """Validate a single FieldSpec against *data*."""
        local_name = self._local_name(field_spec.path)
        full_path = self._full_path(prefix, local_name)

        if local_name not in data:
            if field_spec.required:
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.MISSING_REQUIRED_FIELD,
                        severity=ViolationSeverity.ERROR,
                        message=f"Required field '{full_path}' is missing.",
                        expected_type=field_spec.field_type,
                    )
                )
            # Optional + absent: nothing to check, nothing to recurse into.
            return

        value = data[local_name]

        # --- None handling -------------------------------------------------
        if value is None:
            if self._has_not_null_constraint(field_spec):
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.NULL_NOT_ALLOWED,
                        severity=ViolationSeverity.ERROR,
                        message=f"Field '{full_path}' must not be null.",
                        expected_type=field_spec.field_type,
                        received_value=None,
                    )
                )
            # None values are not type/constraint checked further, and
            # nested objects cannot be recursed into when None.
            return

        # --- OBJECT fields: structural check + recursion --------------------
        if field_spec.field_type is FieldType.OBJECT:
            if not isinstance(value, dict):
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.STRUCTURAL_MISMATCH,
                        severity=ViolationSeverity.ERROR,
                        message=(
                            f"Field '{full_path}' expected an object, got {type(value).__name__}."
                        ),
                        expected_type=FieldType.OBJECT,
                        received_value=value,
                    )
                )
                return

            if field_spec.nested_spec is not None:
                self._validate_fields(
                    field_spec.nested_spec,
                    value,
                    prefix=full_path,
                    violations=violations,
                )
                self._detect_unexpected(
                    field_spec.nested_spec,
                    value,
                    prefix=full_path,
                    violations=violations,
                )
            # OBJECT with no nested_spec: presence + dict-ness is sufficient.
            return

        # --- ARRAY fields -----------------------------------------------------
        if field_spec.field_type is FieldType.ARRAY:
            if not isinstance(value, list):
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.TYPE_MISMATCH,
                        severity=ViolationSeverity.ERROR,
                        message=(
                            f"Field '{full_path}' expected an array, got {type(value).__name__}."
                        ),
                        expected_type=FieldType.ARRAY,
                        received_value=value,
                    )
                )
                return

            if field_spec.item_type is not None:
                for item in value:
                    if not self._type_matches(item, field_spec.item_type):
                        violations.append(
                            ContractViolation(
                                field_path=full_path,
                                violation_type=ViolationType.TYPE_MISMATCH,
                                severity=ViolationSeverity.ERROR,
                                message=(
                                    f"Field '{full_path}' contains an item "
                                    f"of type {type(item).__name__}, "
                                    f"expected {field_spec.item_type.value}."
                                ),
                                expected_type=field_spec.item_type,
                                received_value=value,
                            )
                        )
                        break

            self._check_constraints(field_spec, value, full_path, violations)
            return

        # --- UNION fields -------------------------------------------------------
        if field_spec.field_type is FieldType.UNION:
            members = field_spec.union_members
            # No member information: nothing to check against (ANY-like).
            if members and not any(union_member_matches(value, m) for m in members):
                accepted = ", ".join(m.field_type.value for m in members)
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.TYPE_MISMATCH,
                        severity=ViolationSeverity.ERROR,
                        message=(
                            f"Field '{full_path}' type mismatch: expected one "
                            f"of ({accepted}), got {type(value).__name__}."
                        ),
                        expected_type=FieldType.UNION,
                        received_value=value,
                    )
                )
                return

            self._check_constraints(field_spec, value, full_path, violations)
            return

        # --- Primitive / ANY / NULL fields -------------------------------------
        if not self._type_matches(value, field_spec.field_type):
            violations.append(
                ContractViolation(
                    field_path=full_path,
                    violation_type=ViolationType.TYPE_MISMATCH,
                    severity=ViolationSeverity.ERROR,
                    message=(
                        f"Field '{full_path}' type mismatch: expected "
                        f"{field_spec.field_type.value}, got "
                        f"{type(value).__name__}."
                    ),
                    expected_type=field_spec.field_type,
                    received_value=value,
                )
            )
            return

        self._check_constraints(field_spec, value, full_path, violations)

    # ------------------------------------------------------------------
    # Unexpected field detection
    # ------------------------------------------------------------------

    def _detect_unexpected(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
        prefix: str,
        violations: list[ContractViolation],
    ) -> None:
        """Emit UNEXPECTED_FIELD for keys in *data* not declared by *contract*."""
        known_local_names = {self._local_name(f.path) for f in contract.fields}
        severity = ViolationSeverity.ERROR if contract.strict_mode else ViolationSeverity.WARNING
        for key in data:
            if key not in known_local_names:
                full_path = self._full_path(prefix, key)
                violations.append(
                    ContractViolation(
                        field_path=full_path,
                        violation_type=ViolationType.UNEXPECTED_FIELD,
                        severity=severity,
                        message=(
                            f"Unexpected field '{full_path}' is not declared in the contract."
                        ),
                        received_value=data[key],
                    )
                )

    # ------------------------------------------------------------------
    # Constraint checking
    # ------------------------------------------------------------------

    def _check_constraints(
        self,
        field_spec: FieldSpec,
        value: Any,
        full_path: str,
        violations: list[ContractViolation],
    ) -> None:
        """Check every FieldConstraint declared on *field_spec* against *value*."""
        for constraint in field_spec.constraints:
            if constraint.constraint_type is FieldConstraintType.NOT_NULL:
                # NOT_NULL is enforced when value is None, handled separately
                # in _validate_field. A non-None value always satisfies it.
                continue

            violation = self._check_single_constraint(constraint, value, full_path, field_spec)
            if violation is not None:
                violations.append(violation)

    def _check_single_constraint(
        self,
        constraint: FieldConstraint,
        value: Any,
        full_path: str,
        field_spec: FieldSpec,
    ) -> ContractViolation | None:
        """Return a violation if *value* fails *constraint*, else ``None``."""
        ctype = constraint.constraint_type
        bound = constraint.value

        if ctype is FieldConstraintType.MINIMUM:
            if isinstance(value, _NUMERIC_TYPES) and value < bound:
                return self._constraint_violation(
                    full_path,
                    f"Field '{full_path}' value {value!r} is below the minimum of {bound!r}.",
                    bound,
                    value,
                    field_spec,
                )

        elif ctype is FieldConstraintType.MAXIMUM:
            if isinstance(value, _NUMERIC_TYPES) and value > bound:
                return self._constraint_violation(
                    full_path,
                    f"Field '{full_path}' value {value!r} exceeds the maximum of {bound!r}.",
                    bound,
                    value,
                    field_spec,
                )

        elif ctype is FieldConstraintType.MIN_LENGTH:
            if isinstance(value, _SIZED_TYPES) and len(value) < bound:
                return self._constraint_violation(
                    full_path,
                    f"Field '{full_path}' length {len(value)} is below the "
                    f"minimum length of {bound!r}.",
                    bound,
                    value,
                    field_spec,
                )

        elif ctype is FieldConstraintType.MAX_LENGTH:
            if isinstance(value, _SIZED_TYPES) and len(value) > bound:
                return self._constraint_violation(
                    full_path,
                    f"Field '{full_path}' length {len(value)} exceeds the "
                    f"maximum length of {bound!r}.",
                    bound,
                    value,
                    field_spec,
                )

        elif ctype is FieldConstraintType.PATTERN:
            if isinstance(value, str) and re.match(bound, value) is None:
                return self._constraint_violation(
                    full_path,
                    f"Field '{full_path}' value {value!r} does not match pattern {bound!r}.",
                    bound,
                    value,
                    field_spec,
                )

        elif ctype is FieldConstraintType.ENUM_VALUES and value not in bound:
            return self._constraint_violation(
                full_path,
                f"Field '{full_path}' value {value!r} is not one of the allowed values {bound!r}.",
                bound,
                value,
                field_spec,
            )

        return None

    @staticmethod
    def _constraint_violation(
        full_path: str,
        message: str,
        expected_value: Any,
        received_value: Any,
        field_spec: FieldSpec,
    ) -> ContractViolation:
        return ContractViolation(
            field_path=full_path,
            violation_type=ViolationType.VALUE_CONSTRAINT_VIOLATION,
            severity=ViolationSeverity.ERROR,
            message=message,
            expected_type=field_spec.field_type,
            expected_value=expected_value,
            received_value=received_value,
        )

    @staticmethod
    def _has_not_null_constraint(field_spec: FieldSpec) -> bool:
        return any(
            c.constraint_type is FieldConstraintType.NOT_NULL and c.value
            for c in field_spec.constraints
        )

    # ------------------------------------------------------------------
    # Type checking
    # ------------------------------------------------------------------

    # Shared implementation lives in stateguard.core.models.field_types so
    # that TypeCoercionStrategy and the engine's coercion applier use the
    # exact same compatibility rules. Kept as a static method for backward
    # compatibility with existing callers and tests.
    _type_matches = staticmethod(type_matches)

    # ------------------------------------------------------------------
    # Path helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _local_name(path: str) -> str:
        """
        Return the final dot-notation segment of *path*.

        ``FieldSpec.path`` for nested fields is stored as a full dot-notation
        path (e.g. ``"address.city"``).  When validating within a recursive
        call for the ``address`` object, *data* is the inner dict keyed by
        ``"city"``, so only the final segment is used to look up the value.

        For top-level fields (e.g. ``"temperature"``), this returns the path
        unchanged.
        """
        return path.rsplit(".", 1)[-1]

    @staticmethod
    def _full_path(prefix: str, local_name: str) -> str:
        """Join *prefix* and *local_name* with a dot, omitting empty prefix."""
        return f"{prefix}.{local_name}" if prefix else local_name

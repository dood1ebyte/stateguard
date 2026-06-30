"""Tests for stateguard.core.errors.violations."""

from __future__ import annotations

import re
import uuid

import pytest

from stateguard.core.errors.violations import (
    ContractViolation,
    ViolationSeverity,
    ViolationType,
)
from stateguard.core.models.field_types import FieldType


# ---------------------------------------------------------------------------
# ViolationType
# ---------------------------------------------------------------------------


class TestViolationType:
    def test_all_expected_values_present(self) -> None:
        expected = {
            "missing_required_field",
            "unexpected_field",
            "type_mismatch",
            "value_constraint_violation",
            "null_not_allowed",
            "structural_mismatch",
        }
        assert {vt.value for vt in ViolationType} == expected

    def test_member_count(self) -> None:
        assert len(ViolationType) == 6

    def test_string_equality_missing_required_field(self) -> None:
        assert ViolationType.MISSING_REQUIRED_FIELD == "missing_required_field"

    def test_string_equality_unexpected_field(self) -> None:
        assert ViolationType.UNEXPECTED_FIELD == "unexpected_field"

    def test_string_equality_type_mismatch(self) -> None:
        assert ViolationType.TYPE_MISMATCH == "type_mismatch"

    def test_string_equality_value_constraint_violation(self) -> None:
        assert ViolationType.VALUE_CONSTRAINT_VIOLATION == "value_constraint_violation"

    def test_string_equality_null_not_allowed(self) -> None:
        assert ViolationType.NULL_NOT_ALLOWED == "null_not_allowed"

    def test_string_equality_structural_mismatch(self) -> None:
        assert ViolationType.STRUCTURAL_MISMATCH == "structural_mismatch"

    @pytest.mark.parametrize("member", list(ViolationType))
    def test_every_member_round_trips_via_value(self, member: ViolationType) -> None:
        assert ViolationType(member.value) is member

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            ViolationType("not_a_violation")


# ---------------------------------------------------------------------------
# ViolationSeverity
# ---------------------------------------------------------------------------


class TestViolationSeverity:
    def test_all_expected_values_present(self) -> None:
        assert {vs.value for vs in ViolationSeverity} == {"error", "warning"}

    def test_member_count(self) -> None:
        assert len(ViolationSeverity) == 2

    def test_string_equality_error(self) -> None:
        assert ViolationSeverity.ERROR == "error"

    def test_string_equality_warning(self) -> None:
        assert ViolationSeverity.WARNING == "warning"

    @pytest.mark.parametrize("member", list(ViolationSeverity))
    def test_every_member_round_trips_via_value(self, member: ViolationSeverity) -> None:
        assert ViolationSeverity(member.value) is member

    def test_error_and_warning_are_not_equal(self) -> None:
        assert ViolationSeverity.ERROR != ViolationSeverity.WARNING


# ---------------------------------------------------------------------------
# ContractViolation — helpers
# ---------------------------------------------------------------------------


def _make(
    field_path: str = "temperature",
    violation_type: ViolationType = ViolationType.MISSING_REQUIRED_FIELD,
    severity: ViolationSeverity = ViolationSeverity.ERROR,
    message: str = "Field required.",
    **kwargs: object,
) -> ContractViolation:
    return ContractViolation(
        field_path=field_path,
        violation_type=violation_type,
        severity=severity,
        message=message,
        **kwargs,  # type: ignore[arg-type]
    )


_UUID4_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# ContractViolation
# ---------------------------------------------------------------------------


class TestContractViolation:
    # --- Minimal construction -------------------------------------------------

    def test_required_fields_are_stored(self) -> None:
        v = _make()
        assert v.field_path == "temperature"
        assert v.violation_type is ViolationType.MISSING_REQUIRED_FIELD
        assert v.severity is ViolationSeverity.ERROR
        assert v.message == "Field required."

    # --- Auto-generated violation_id ------------------------------------------

    def test_violation_id_is_auto_generated(self) -> None:
        v = _make()
        assert v.violation_id is not None
        assert isinstance(v.violation_id, str)
        assert len(v.violation_id) > 0

    def test_violation_id_is_uuid4_format(self) -> None:
        v = _make()
        assert _UUID4_RE.match(v.violation_id), (
            f"violation_id '{v.violation_id}' is not valid UUID4"
        )

    def test_violation_ids_are_unique_per_instance(self) -> None:
        ids = {_make().violation_id for _ in range(20)}
        assert len(ids) == 20

    def test_explicit_violation_id_is_respected(self) -> None:
        fixed = "00000000-0000-4000-8000-000000000042"
        v = _make(violation_id=fixed)
        assert v.violation_id == fixed

    def test_explicit_id_does_not_affect_other_instance(self) -> None:
        fixed = "aaaaaaaa-0000-4000-8000-000000000001"
        v1 = _make(violation_id=fixed)
        v2 = _make()
        assert v2.violation_id != fixed
        assert v1.violation_id == fixed

    # --- Optional fields default to None -------------------------------------

    def test_expected_type_defaults_to_none(self) -> None:
        assert _make().expected_type is None

    def test_expected_value_defaults_to_none(self) -> None:
        assert _make().expected_value is None

    def test_received_value_defaults_to_none(self) -> None:
        assert _make().received_value is None

    # --- related_ids ----------------------------------------------------------

    def test_related_ids_default_to_empty_list(self) -> None:
        assert _make().related_ids == []

    def test_related_ids_type_is_list(self) -> None:
        assert isinstance(_make().related_ids, list)

    def test_related_ids_not_shared_between_instances(self) -> None:
        """
        Confirms default_factory=list creates a new list per instance.
        This is the mutation safety test for the correlation step.
        """
        v1 = _make(field_path="a")
        v2 = _make(field_path="b")
        v1.related_ids.append("some-other-id")
        assert v2.related_ids == [], (
            "related_ids must use default_factory=list — "
            "instances must not share a mutable default."
        )

    def test_related_ids_can_be_appended(self) -> None:
        """ContractViolation must be mutable for the correlation step."""
        v = _make()
        v.related_ids.append("correlated-id-1")
        v.related_ids.append("correlated-id-2")
        assert v.related_ids == ["correlated-id-1", "correlated-id-2"]

    def test_related_ids_passed_explicitly(self) -> None:
        v = _make(related_ids=["id-1", "id-2"])
        assert v.related_ids == ["id-1", "id-2"]

    # --- Full construction ----------------------------------------------------

    def test_full_construction_type_mismatch(self) -> None:
        v = ContractViolation(
            field_path="temperature",
            violation_type=ViolationType.TYPE_MISMATCH,
            severity=ViolationSeverity.ERROR,
            message="Expected float, received str.",
            expected_type=FieldType.FLOAT,
            expected_value=None,
            received_value="31.5",
            related_ids=["other-id"],
            violation_id="test-id-fixed",
        )
        assert v.expected_type is FieldType.FLOAT
        assert v.received_value == "31.5"
        assert v.related_ids == ["other-id"]
        assert v.violation_id == "test-id-fixed"

    def test_full_construction_value_constraint(self) -> None:
        v = ContractViolation(
            field_path="age",
            violation_type=ViolationType.VALUE_CONSTRAINT_VIOLATION,
            severity=ViolationSeverity.ERROR,
            message="age must be >= 0",
            expected_type=FieldType.INTEGER,
            expected_value=0,
            received_value=-5,
        )
        assert v.expected_value == 0
        assert v.received_value == -5

    def test_null_not_allowed_construction(self) -> None:
        v = ContractViolation(
            field_path="user_id",
            violation_type=ViolationType.NULL_NOT_ALLOWED,
            severity=ViolationSeverity.ERROR,
            message="user_id must not be None.",
            received_value=None,
        )
        assert v.received_value is None
        assert v.violation_type is ViolationType.NULL_NOT_ALLOWED

    # --- Field path edge cases -----------------------------------------------

    def test_empty_field_path_allowed(self) -> None:
        """Root-level structural violations have no field path."""
        v = ContractViolation(
            field_path="",
            violation_type=ViolationType.STRUCTURAL_MISMATCH,
            severity=ViolationSeverity.ERROR,
            message="Expected dict, got list.",
        )
        assert v.field_path == ""

    def test_nested_dot_notation_path(self) -> None:
        v = _make(field_path="address.city")
        assert v.field_path == "address.city"

    def test_deeply_nested_path(self) -> None:
        v = _make(field_path="a.b.c.d")
        assert v.field_path == "a.b.c.d"

    # --- Mutability -----------------------------------------------------------

    def test_field_path_is_mutable(self) -> None:
        v = _make()
        v.field_path = "new_path"
        assert v.field_path == "new_path"

    def test_severity_is_mutable(self) -> None:
        v = _make(severity=ViolationSeverity.ERROR)
        v.severity = ViolationSeverity.WARNING
        assert v.severity is ViolationSeverity.WARNING

    def test_message_is_mutable(self) -> None:
        v = _make()
        v.message = "updated message"
        assert v.message == "updated message"

    # --- Parametrised: all violation types ------------------------------------

    @pytest.mark.parametrize("vtype", list(ViolationType))
    def test_every_violation_type_is_constructable(self, vtype: ViolationType) -> None:
        v = _make(violation_type=vtype)
        assert v.violation_type is vtype

    # --- Parametrised: all severities -----------------------------------------

    @pytest.mark.parametrize("severity", list(ViolationSeverity))
    def test_every_severity_is_constructable(self, severity: ViolationSeverity) -> None:
        v = _make(severity=severity)
        assert v.severity is severity

    # --- Parametrised: all FieldType expected_types ---------------------------

    @pytest.mark.parametrize("ftype", list(FieldType))
    def test_every_field_type_accepted_as_expected_type(self, ftype: FieldType) -> None:
        v = _make(
            violation_type=ViolationType.TYPE_MISMATCH,
            expected_type=ftype,
        )
        assert v.expected_type is ftype

    # --- received_value accepts arbitrary types -------------------------------

    @pytest.mark.parametrize(
        "value",
        [0, 3.14, "a string", True, None, [], {"key": "val"}],
    )
    def test_received_value_accepts_any_type(self, value: object) -> None:
        v = _make(received_value=value)
        assert v.received_value == value

    # --- Repr -----------------------------------------------------------------

    def test_repr_contains_class_name(self) -> None:
        assert "ContractViolation" in repr(_make())

    def test_repr_contains_field_path(self) -> None:
        v = _make(field_path="my_field")
        assert "my_field" in repr(v)

    # --- UUID uniqueness at scale ---------------------------------------------

    def test_violation_ids_unique_across_100_instances(self) -> None:
        ids = [_make().violation_id for _ in range(100)]
        assert len(set(ids)) == 100

    def test_all_auto_ids_are_valid_uuids(self) -> None:
        for _ in range(10):
            v = _make()
            try:
                uuid.UUID(v.violation_id, version=4)
            except ValueError:
                pytest.fail(f"violation_id '{v.violation_id}' is not a valid UUID4")

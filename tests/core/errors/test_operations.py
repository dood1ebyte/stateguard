"""Tests for stateguard.core.errors.operations."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from stateguard.core.errors.operations import (
    FieldOperation,
    FieldOpType,
    RepairEvidence,
)


# ---------------------------------------------------------------------------
# FieldOpType
# ---------------------------------------------------------------------------


class TestFieldOpType:
    def test_all_expected_values_present(self) -> None:
        expected = {"rename", "coerce", "set_default", "remove", "set_value"}
        assert {fot.value for fot in FieldOpType} == expected

    def test_member_count(self) -> None:
        assert len(FieldOpType) == 5

    def test_string_equality_rename(self) -> None:
        assert FieldOpType.RENAME == "rename"

    def test_string_equality_coerce(self) -> None:
        assert FieldOpType.COERCE == "coerce"

    def test_string_equality_set_default(self) -> None:
        assert FieldOpType.SET_DEFAULT == "set_default"

    def test_string_equality_remove(self) -> None:
        assert FieldOpType.REMOVE == "remove"

    def test_string_equality_set_value(self) -> None:
        assert FieldOpType.SET_VALUE == "set_value"

    @pytest.mark.parametrize("member", list(FieldOpType))
    def test_every_member_round_trips_via_value(self, member: FieldOpType) -> None:
        assert FieldOpType(member.value) is member

    def test_invalid_value_raises(self) -> None:
        with pytest.raises(ValueError):
            FieldOpType("delete")


# ---------------------------------------------------------------------------
# FieldOperation — helpers
# ---------------------------------------------------------------------------


def _rename(
    source: str = "temp_celsius",
    target: str = "temperature",
    trust: float = 0.85,
    rationale: str = "Fuzzy match",
) -> FieldOperation:
    return FieldOperation(
        op_type=FieldOpType.RENAME,
        target_path=target,
        trust=trust,
        rationale=rationale,
        source_path=source,
    )


def _remove(target: str = "unwanted", trust: float = 1.0) -> FieldOperation:
    return FieldOperation(
        op_type=FieldOpType.REMOVE,
        target_path=target,
        trust=trust,
        rationale="Remove unexpected field",
    )


# ---------------------------------------------------------------------------
# FieldOperation — construction
# ---------------------------------------------------------------------------


class TestFieldOperationConstruction:
    def test_rename_stores_all_fields(self) -> None:
        op = _rename()
        assert op.op_type is FieldOpType.RENAME
        assert op.target_path == "temperature"
        assert op.source_path == "temp_celsius"
        assert op.confidence == 0.85
        assert op.rationale == "Fuzzy match"
        assert op.value is None

    def test_coerce_construction(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.COERCE,
            target_path="count",
            trust=0.95,
            rationale="str->int: value is digit string",
        )
        assert op.op_type is FieldOpType.COERCE
        assert op.source_path is None
        assert op.value is None

    def test_set_default_with_int_value(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="humidity",
            trust=1.0,
            rationale="Field has declared default",
            value=50,
        )
        assert op.op_type is FieldOpType.SET_DEFAULT
        assert op.value == 50
        assert op.source_path is None

    def test_set_default_with_none_value(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="optional_field",
            trust=1.0,
            rationale="Default is None",
            value=None,
        )
        assert op.value is None

    def test_set_default_with_string_value(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="status",
            trust=1.0,
            rationale="Default status",
            value="unknown",
        )
        assert op.value == "unknown"

    def test_set_default_with_float_value(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="temperature",
            trust=1.0,
            rationale="Default temp",
            value=20.0,
        )
        assert op.value == 20.0

    def test_set_default_with_bool_value(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="active",
            trust=1.0,
            rationale="Default active",
            value=False,
        )
        assert op.value is False

    def test_remove_construction(self) -> None:
        op = _remove()
        assert op.op_type is FieldOpType.REMOVE
        assert op.source_path is None
        assert op.value is None

    def test_set_value_construction(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_VALUE,
            target_path="override_field",
            trust=0.5,
            rationale="Last-resort forced value",
            value="forced",
        )
        assert op.op_type is FieldOpType.SET_VALUE
        assert op.value == "forced"

    def test_confidence_zero_is_valid(self) -> None:
        op = _remove(trust=0.0)
        assert op.confidence == 0.0

    def test_confidence_one_is_valid(self) -> None:
        op = _remove(trust=1.0)
        assert op.confidence == 1.0

    def test_confidence_midpoint_is_valid(self) -> None:
        op = _rename(trust=0.5)
        assert op.confidence == 0.5


# ---------------------------------------------------------------------------
# FieldOperation — validation
# ---------------------------------------------------------------------------


class TestFieldOperationValidation:
    def test_confidence_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="trust must be in"):
            _remove(trust=1.001)

    def test_confidence_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="trust must be in"):
            _remove(trust=-0.001)

    def test_confidence_significantly_above_one_raises(self) -> None:
        with pytest.raises(ValueError, match="trust must be in"):
            _remove(trust=2.0)

    def test_confidence_significantly_below_zero_raises(self) -> None:
        with pytest.raises(ValueError, match="trust must be in"):
            _remove(trust=-1.0)

    def test_rename_without_source_path_raises(self) -> None:
        with pytest.raises(ValueError, match="RENAME.*requires source_path"):
            FieldOperation(
                op_type=FieldOpType.RENAME,
                target_path="temperature",
                trust=0.9,
                rationale="missing source",
                # source_path intentionally omitted
            )

    def test_rename_with_empty_string_source_path_is_valid(self) -> None:
        """Empty string is a valid path (root level); None is not."""
        op = FieldOperation(
            op_type=FieldOpType.RENAME,
            target_path="x",
            trust=0.9,
            rationale="root-level rename",
            source_path="",
        )
        assert op.source_path == ""

    @pytest.mark.parametrize(
        "op_type",
        [
            FieldOpType.COERCE,
            FieldOpType.SET_DEFAULT,
            FieldOpType.REMOVE,
            FieldOpType.SET_VALUE,
        ],
    )
    def test_non_rename_ops_do_not_require_source_path(self, op_type: FieldOpType) -> None:
        op = FieldOperation(
            op_type=op_type,
            target_path="some_field",
            trust=0.9,
            rationale="no source needed",
        )
        assert op.source_path is None

    @pytest.mark.parametrize(
        "op_type",
        [
            FieldOpType.COERCE,
            FieldOpType.SET_DEFAULT,
            FieldOpType.REMOVE,
            FieldOpType.SET_VALUE,
        ],
    )
    def test_non_rename_ops_accept_source_path_if_provided(self, op_type: FieldOpType) -> None:
        """source_path is optional for non-RENAME ops; providing it is allowed."""
        op = FieldOperation(
            op_type=op_type,
            target_path="some_field",
            trust=0.9,
            rationale="source provided anyway",
            source_path="original_field",
        )
        assert op.source_path == "original_field"


# ---------------------------------------------------------------------------
# FieldOperation — immutability
# ---------------------------------------------------------------------------


class TestFieldOperationImmutability:
    def test_op_type_is_immutable(self) -> None:
        op = _rename()
        with pytest.raises(FrozenInstanceError):
            op.op_type = FieldOpType.REMOVE  # type: ignore[misc]

    def test_target_path_is_immutable(self) -> None:
        op = _rename()
        with pytest.raises(FrozenInstanceError):
            op.target_path = "other"  # type: ignore[misc]

    def test_confidence_is_immutable(self) -> None:
        op = _rename()
        with pytest.raises(FrozenInstanceError):
            op.confidence = 0.5  # type: ignore[misc]

    def test_rationale_is_immutable(self) -> None:
        op = _rename()
        with pytest.raises(FrozenInstanceError):
            op.rationale = "changed"  # type: ignore[misc]

    def test_source_path_is_immutable(self) -> None:
        op = _rename()
        with pytest.raises(FrozenInstanceError):
            op.source_path = "other_source"  # type: ignore[misc]

    def test_value_is_immutable(self) -> None:
        op = FieldOperation(
            op_type=FieldOpType.SET_DEFAULT,
            target_path="x",
            trust=1.0,
            rationale="r",
            value=42,
        )
        with pytest.raises(FrozenInstanceError):
            op.value = 99  # type: ignore[misc]

    def test_cannot_add_new_attribute(self) -> None:
        op = _rename()
        with pytest.raises((FrozenInstanceError, AttributeError)):
            op.new_attribute = "not allowed"  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FieldOperation — equality
# ---------------------------------------------------------------------------


class TestFieldOperationEquality:
    def test_identical_renames_are_equal(self) -> None:
        assert _rename() == _rename()

    def test_different_confidence_not_equal(self) -> None:
        assert _rename(trust=0.8) != _rename(trust=0.9)

    def test_different_source_not_equal(self) -> None:
        assert _rename(source="a") != _rename(source="b")

    def test_different_target_not_equal(self) -> None:
        assert _rename(target="x") != _rename(target="y")

    def test_different_op_type_not_equal(self) -> None:
        op1 = FieldOperation(FieldOpType.COERCE, "x", "r", trust=0.9)
        op2 = FieldOperation(FieldOpType.REMOVE, "x", "r", trust=0.9)
        assert op1 != op2

    def test_set_default_with_different_values_not_equal(self) -> None:
        op1 = FieldOperation(FieldOpType.SET_DEFAULT, "f", "r", trust=1.0, value=1)
        op2 = FieldOperation(FieldOpType.SET_DEFAULT, "f", "r", trust=1.0, value=2)
        assert op1 != op2

    def test_not_equal_to_none(self) -> None:
        assert _rename() is not None

    def test_not_equal_to_different_type(self) -> None:
        assert _rename() != "not an operation"


# ---------------------------------------------------------------------------
# FieldOperation — hashability
# ---------------------------------------------------------------------------


class TestFieldOperationHashability:
    def test_is_hashable(self) -> None:
        h = hash(_rename())
        assert isinstance(h, int)

    def test_hash_is_stable(self) -> None:
        op = _rename()
        assert hash(op) == hash(op)

    def test_equal_operations_have_equal_hashes(self) -> None:
        op1 = _rename()
        op2 = _rename()
        assert hash(op1) == hash(op2)

    def test_can_be_stored_in_set(self) -> None:
        op1 = _rename()
        op2 = _rename()  # duplicate
        op3 = _rename(trust=0.99)  # different
        s = {op1, op2, op3}
        assert len(s) == 2

    def test_can_be_used_as_dict_key(self) -> None:
        op = _rename()
        d = {op: "repair action"}
        assert d[op] == "repair action"

    def test_set_deduplicates_identical_operations(self) -> None:
        ops = [_rename() for _ in range(5)]
        assert len(set(ops)) == 1


# ---------------------------------------------------------------------------
# FieldOperation — repr
# ---------------------------------------------------------------------------


class TestFieldOperationRepr:
    def test_repr_contains_class_name(self) -> None:
        assert "FieldOperation" in repr(_rename())

    def test_repr_contains_op_type(self) -> None:
        r = repr(_rename())
        assert "RENAME" in r or "rename" in r

    def test_repr_contains_target_path(self) -> None:
        op = _rename(target="temperature")
        assert "temperature" in repr(op)


# ---------------------------------------------------------------------------
# FieldOperation — parametrised: all op types constructable
# ---------------------------------------------------------------------------


class TestFieldOperationAllOpTypes:
    @pytest.mark.parametrize("op_type", list(FieldOpType))
    def test_every_op_type_is_constructable(self, op_type: FieldOpType) -> None:
        source = "src" if op_type is FieldOpType.RENAME else None
        op = FieldOperation(
            op_type=op_type,
            target_path="target_field",
            trust=0.9,
            rationale=f"Testing {op_type.value}",
            source_path=source,
        )
        assert op.op_type is op_type

    @pytest.mark.parametrize(
        "trust",
        [0.0, 0.01, 0.5, 0.7, 0.99, 1.0],
    )
    def test_boundary_and_typical_confidence_values(self, trust: float) -> None:
        op = _remove(trust=trust)
        assert op.confidence == trust

    @pytest.mark.parametrize(
        "trust",
        [-0.001, -1.0, 1.001, 2.0, float("inf")],
    )
    def test_out_of_range_confidence_values_raise(self, trust: float) -> None:
        with pytest.raises(ValueError):
            _remove(trust=trust)


class TestEvidenceRangeValidation:
    """Every evidence score is a [0, 1] quantity; nothing else is meaningful."""

    @pytest.mark.parametrize(
        "label", ["name_match", "value_preserved", "schema_authority", "margin"]
    )
    @pytest.mark.parametrize("score", [-0.01, 1.01, 2.0, -1.0])
    def test_out_of_range_scores_raise(self, label: str, score: float) -> None:
        with pytest.raises(ValueError, match=f"RepairEvidence.{label} must be in"):
            RepairEvidence(**{label: score})

    @pytest.mark.parametrize("score", [0.0, 0.5, 1.0])
    def test_boundary_scores_are_accepted(self, score: float) -> None:
        assert RepairEvidence(name_match=score).name_match == score

    def test_none_means_not_applicable_and_is_never_range_checked(self) -> None:
        assert RepairEvidence().applicable_scores == ()

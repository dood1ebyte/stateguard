"""
Phase 5.1: ``enum.Enum`` is visible to the contract extractor.

A real Enum previously fell through every branch of ``map_annotation`` to
``FieldType.ANY`` with no ``ENUM_VALUES`` constraint, so StateGuard could not
see a restriction that the ``Literal`` spelling of the same thing made
plainly visible. These tests pin the equivalence.
"""

from __future__ import annotations

from enum import Enum, Flag, IntEnum, IntFlag, StrEnum
from typing import Any, Literal, Optional

import pytest

pytest.importorskip("pydantic")

from pydantic import BaseModel  # noqa: E402

from stateguard.adapters.pydantic.extractor import PydanticContractExtractor  # noqa: E402
from stateguard.adapters.pydantic.type_mapper import PydanticTypeMapper  # noqa: E402
from stateguard.core.models.field_types import FieldConstraintType, FieldType  # noqa: E402


# UP042 suggests StrEnum, but the legacy `(str, Enum)` mix is one of the
# three flavours Phase 5.1 has to support and is still the commonest form in
# real schemas -- testing StrEnum instead would not cover it. StrEnum has its
# own case below.
class Status(str, Enum):  # noqa: UP042
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    DONE = "done"


class Level(IntEnum):
    LOW = 1
    HIGH = 2


class Plain(Enum):
    A = "a"
    B = "b"


class Permission(Flag):
    READ = 1
    WRITE = 2


class IntPermission(IntFlag):
    READ = 1
    WRITE = 2


class Modern(StrEnum):
    OPEN = "open"
    DONE = "done"


class Empty(Enum):
    pass


class Aliased(str, Enum):  # noqa: UP042
    OPEN = "open"
    ACTIVE = "open"  # alias for the same value


# ===========================================================================
# map_annotation / get_literal_values
# ===========================================================================


class TestEnumMapsLikeItsLiteral:
    @pytest.mark.parametrize(
        ("annotation", "field_type", "values"),
        [
            (Status, FieldType.STRING, ("open", "in_progress", "done")),
            (Level, FieldType.INTEGER, (1, 2)),
            (Plain, FieldType.STRING, ("a", "b")),
            (Modern, FieldType.STRING, ("open", "done")),
            (Optional[Status], FieldType.STRING, ("open", "in_progress", "done")),
        ],
    )
    def test_member_values_become_the_closed_set(
        self, annotation: Any, field_type: FieldType, values: tuple[Any, ...]
    ) -> None:
        assert PydanticTypeMapper.map_annotation(annotation) is field_type
        assert PydanticTypeMapper.get_literal_values(annotation) == values

    def test_an_enum_and_the_literal_of_its_values_agree(self) -> None:
        """The whole point: the same payload, described two ways."""
        literal = Literal["open", "in_progress", "done"]
        assert PydanticTypeMapper.map_annotation(Status) is PydanticTypeMapper.map_annotation(
            literal
        )
        assert PydanticTypeMapper.get_literal_values(
            Status
        ) == PydanticTypeMapper.get_literal_values(literal)

    def test_member_aliases_collapse_to_one_value(self) -> None:
        """Two names bound to one value are one allowed value, not two."""
        assert PydanticTypeMapper.get_literal_values(Aliased) == ("open",)


class TestExcludedEnums:
    @pytest.mark.parametrize("annotation", [Permission, IntPermission])
    def test_flags_are_not_a_closed_set(self, annotation: Any) -> None:
        """
        Flag members combine (``READ | WRITE``), so a combined value is
        legitimately valid while being no single member. An ENUM_VALUES
        constraint built from the members would reject correct data.
        """
        assert PydanticTypeMapper.get_literal_values(annotation) is None
        assert PydanticTypeMapper.map_annotation(annotation) is FieldType.ANY

    def test_an_empty_enum_declares_no_restriction(self) -> None:
        """An empty set nothing can satisfy would fail every payload."""
        assert PydanticTypeMapper.get_literal_values(Empty) is None
        assert PydanticTypeMapper.map_annotation(Empty) is FieldType.ANY


# ===========================================================================
# Extraction
# ===========================================================================


class TestExtractedConstraint:
    def test_enum_field_carries_an_enum_values_constraint(self) -> None:
        class Task(BaseModel):
            title: str
            status: Status

        contract = PydanticContractExtractor.extract(Task)
        status = next(f for f in contract.fields if f.path == "status")

        assert status.field_type is FieldType.STRING
        enum_constraints = [
            c for c in status.constraints if c.constraint_type is FieldConstraintType.ENUM_VALUES
        ]
        assert len(enum_constraints) == 1
        assert enum_constraints[0].value == ("open", "in_progress", "done")

    def test_a_flag_field_carries_no_enum_constraint(self) -> None:
        class Doc(BaseModel):
            perms: Permission = Permission.READ

        contract = PydanticContractExtractor.extract(Doc)
        perms = next(f for f in contract.fields if f.path == "perms")
        assert not [
            c for c in perms.constraints if c.constraint_type is FieldConstraintType.ENUM_VALUES
        ]

    def test_nested_model_enum_is_extracted(self) -> None:
        class Inner(BaseModel):
            status: Status

        class Outer(BaseModel):
            inner: Inner

        contract = PydanticContractExtractor.extract(Outer)
        inner_spec = contract.fields[0].nested_spec
        assert inner_spec is not None
        status = inner_spec.fields[0]
        assert status.constraints[0].value == ("open", "in_progress", "done")

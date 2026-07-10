"""Tests for stateguard.adapters.pydantic.type_mapper."""

from __future__ import annotations

import datetime
import typing
import uuid
from typing import Annotated, Any, Dict, List, Literal, Optional, Union

import pytest
from pydantic import BaseModel

from stateguard.adapters.pydantic.type_mapper import PydanticTypeMapper
from stateguard.core.models.field_types import FieldType, UnionMember


# ===========================================================================
# strip_annotated
# ===========================================================================


class TestStripAnnotated:
    def test_plain_type_unchanged(self) -> None:
        assert PydanticTypeMapper.strip_annotated(int) is int

    def test_single_annotated_unwrapped(self) -> None:
        assert PydanticTypeMapper.strip_annotated(Annotated[int, "meta"]) is int

    def test_nested_annotated_fully_unwrapped(self) -> None:
        inner = Annotated[int, "a"]
        outer = Annotated[inner, "b"]
        assert PydanticTypeMapper.strip_annotated(outer) is int

    def test_non_annotated_complex_type_unchanged(self) -> None:
        assert PydanticTypeMapper.strip_annotated(List[str]) == List[str]


# ===========================================================================
# unwrap_optional
# ===========================================================================


class TestUnwrapOptional:
    def test_optional_float(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(Optional[float])
        assert inner is float
        assert is_opt is True

    def test_union_int_none(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(Union[int, None])
        assert inner is int
        assert is_opt is True

    def test_plain_type_not_optional(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(int)
        assert inner is int
        assert is_opt is False

    def test_multi_type_union_not_unwrapped(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(Union[int, str])
        assert is_opt is False
        assert inner == Union[int, str]

    def test_three_way_union_with_none_not_unwrapped(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(Union[int, str, None])
        assert is_opt is False

    def test_optional_annotated(self) -> None:
        inner, is_opt = PydanticTypeMapper.unwrap_optional(Optional[Annotated[int, "meta"]])
        assert inner is int
        assert is_opt is True

    def test_optional_basemodel(self) -> None:
        class M(BaseModel):
            x: int

        inner, is_opt = PydanticTypeMapper.unwrap_optional(Optional[M])
        assert inner is M
        assert is_opt is True


# ===========================================================================
# get_literal_values
# ===========================================================================


class TestGetLiteralValues:
    def test_string_literal(self) -> None:
        result = PydanticTypeMapper.get_literal_values(Literal["a", "b", "c"])
        assert result == ("a", "b", "c")

    def test_int_literal(self) -> None:
        result = PydanticTypeMapper.get_literal_values(Literal[1, 2, 3])
        assert result == (1, 2, 3)

    def test_optional_literal(self) -> None:
        result = PydanticTypeMapper.get_literal_values(Optional[Literal["a", "b"]])
        assert result == ("a", "b")

    def test_non_literal_returns_none(self) -> None:
        assert PydanticTypeMapper.get_literal_values(int) is None

    def test_non_literal_optional_returns_none(self) -> None:
        assert PydanticTypeMapper.get_literal_values(Optional[int]) is None

    def test_annotated_literal(self) -> None:
        result = PydanticTypeMapper.get_literal_values(Annotated[Literal["x", "y"], "meta"])
        assert result == ("x", "y")


# ===========================================================================
# map_annotation — primitives
# ===========================================================================


class TestMapAnnotationPrimitives:
    def test_str(self) -> None:
        assert PydanticTypeMapper.map_annotation(str) is FieldType.STRING

    def test_int(self) -> None:
        assert PydanticTypeMapper.map_annotation(int) is FieldType.INTEGER

    def test_float(self) -> None:
        assert PydanticTypeMapper.map_annotation(float) is FieldType.FLOAT

    def test_bool(self) -> None:
        assert PydanticTypeMapper.map_annotation(bool) is FieldType.BOOLEAN

    def test_any(self) -> None:
        assert PydanticTypeMapper.map_annotation(Any) is FieldType.ANY

    def test_none_type(self) -> None:
        assert PydanticTypeMapper.map_annotation(type(None)) is FieldType.NULL

    def test_datetime(self) -> None:
        assert PydanticTypeMapper.map_annotation(datetime.datetime) is FieldType.STRING

    def test_date(self) -> None:
        assert PydanticTypeMapper.map_annotation(datetime.date) is FieldType.STRING

    def test_uuid(self) -> None:
        assert PydanticTypeMapper.map_annotation(uuid.UUID) is FieldType.STRING


# ===========================================================================
# map_annotation — Optional / Union
# ===========================================================================


class TestMapAnnotationOptional:
    def test_optional_float_maps_to_float(self) -> None:
        assert PydanticTypeMapper.map_annotation(Optional[float]) is FieldType.FLOAT

    def test_optional_str_maps_to_string(self) -> None:
        assert PydanticTypeMapper.map_annotation(Optional[str]) is FieldType.STRING

    def test_union_int_none_maps_to_integer(self) -> None:
        assert PydanticTypeMapper.map_annotation(Union[int, None]) is FieldType.INTEGER

    def test_multi_type_union_maps_to_union(self) -> None:
        assert PydanticTypeMapper.map_annotation(Union[int, str]) is FieldType.UNION

    def test_three_way_union_with_none_maps_to_union(self) -> None:
        assert PydanticTypeMapper.map_annotation(Union[int, str, None]) is FieldType.UNION

    def test_pep604_union_maps_to_union(self) -> None:
        assert PydanticTypeMapper.map_annotation(int | str) is FieldType.UNION

    def test_pep604_optional_maps_to_inner_type(self) -> None:
        assert PydanticTypeMapper.map_annotation(str | None) is FieldType.STRING

    def test_set_maps_to_any(self) -> None:
        """set is not list or dict; falls back to ANY."""
        assert PydanticTypeMapper.map_annotation(set) is FieldType.ANY

    def test_tuple_maps_to_any(self) -> None:
        assert PydanticTypeMapper.map_annotation(tuple) is FieldType.ANY

    def test_custom_non_model_class_maps_to_any(self) -> None:
        class NotAModel:
            pass

        assert PydanticTypeMapper.map_annotation(NotAModel) is FieldType.ANY


# ===========================================================================
# map_annotation — list / dict
# ===========================================================================


class TestMapAnnotationContainers:
    def test_bare_list(self) -> None:
        assert PydanticTypeMapper.map_annotation(list) is FieldType.ARRAY

    def test_list_of_str(self) -> None:
        assert PydanticTypeMapper.map_annotation(List[str]) is FieldType.ARRAY

    def test_bare_dict(self) -> None:
        assert PydanticTypeMapper.map_annotation(dict) is FieldType.OBJECT

    def test_dict_str_int(self) -> None:
        assert PydanticTypeMapper.map_annotation(Dict[str, int]) is FieldType.OBJECT

    def test_optional_list(self) -> None:
        assert PydanticTypeMapper.map_annotation(Optional[List[str]]) is FieldType.ARRAY


# ===========================================================================
# map_annotation — Annotated
# ===========================================================================


class TestMapAnnotationAnnotated:
    def test_annotated_int(self) -> None:
        assert PydanticTypeMapper.map_annotation(Annotated[int, "meta"]) is FieldType.INTEGER

    def test_optional_annotated_int(self) -> None:
        result = PydanticTypeMapper.map_annotation(Optional[Annotated[int, "meta"]])
        assert result is FieldType.INTEGER


# ===========================================================================
# map_annotation — Literal
# ===========================================================================


class TestMapAnnotationLiteral:
    def test_string_literal_maps_to_string(self) -> None:
        assert PydanticTypeMapper.map_annotation(Literal["a", "b"]) is FieldType.STRING

    def test_int_literal_maps_to_integer(self) -> None:
        assert PydanticTypeMapper.map_annotation(Literal[1, 2, 3]) is FieldType.INTEGER

    def test_bool_literal_maps_to_boolean(self) -> None:
        """bool is checked before int since bool subclasses int."""
        assert PydanticTypeMapper.map_annotation(Literal[True, False]) is FieldType.BOOLEAN

    def test_float_literal_maps_to_float(self) -> None:
        assert PydanticTypeMapper.map_annotation(Literal[1.0, 2.5]) is FieldType.FLOAT

    def test_mixed_type_literal_maps_to_any(self) -> None:
        assert PydanticTypeMapper.map_annotation(Literal[1, "a"]) is FieldType.ANY

    def test_optional_literal_maps_to_inner_type(self) -> None:
        assert PydanticTypeMapper.map_annotation(Optional[Literal["a", "b"]]) is FieldType.STRING

    def test_single_value_literal(self) -> None:
        assert PydanticTypeMapper.map_annotation(Literal["only"]) is FieldType.STRING

    def test_literal_none_maps_to_any(self) -> None:
        """Literal[None] -- value type (NoneType) is not bool/str/int/float."""
        assert PydanticTypeMapper.map_annotation(Literal[None]) is FieldType.ANY

    def test_literal_bytes_maps_to_any(self) -> None:
        """Literal[bytes value] -- bytes is not bool/str/int/float."""
        assert PydanticTypeMapper.map_annotation(Literal[b"x"]) is FieldType.ANY

    def test_literal_field_type_empty_tuple_maps_to_any(self) -> None:
        """Direct unit test: _literal_field_type(()) -> ANY (defensive branch;
        Literal[] is not constructible via normal syntax)."""
        assert PydanticTypeMapper._literal_field_type(()) is FieldType.ANY


# ===========================================================================
# map_annotation — nested BaseModel
# ===========================================================================


class TestMapAnnotationBaseModel:
    def test_basemodel_maps_to_object(self) -> None:
        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.map_annotation(M) is FieldType.OBJECT

    def test_optional_basemodel_maps_to_object(self) -> None:
        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.map_annotation(Optional[M]) is FieldType.OBJECT


# ===========================================================================
# get_item_type
# ===========================================================================


class TestGetItemType:
    def test_list_of_str(self) -> None:
        assert PydanticTypeMapper.get_item_type(List[str]) is FieldType.STRING

    def test_list_of_int(self) -> None:
        assert PydanticTypeMapper.get_item_type(List[int]) is FieldType.INTEGER

    def test_bare_list_returns_none(self) -> None:
        assert PydanticTypeMapper.get_item_type(list) is None

    def test_non_list_returns_none(self) -> None:
        assert PydanticTypeMapper.get_item_type(str) is None

    def test_dict_returns_none(self) -> None:
        assert PydanticTypeMapper.get_item_type(dict) is None

    def test_optional_list_of_str(self) -> None:
        assert PydanticTypeMapper.get_item_type(Optional[List[str]]) is FieldType.STRING

    def test_list_of_basemodel_returns_object(self) -> None:
        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.get_item_type(List[M]) is FieldType.OBJECT

    def test_bare_typing_list_returns_none(self) -> None:
        """typing.List (unsubscripted) has no type args."""
        assert PydanticTypeMapper.get_item_type(typing.List) is None


# ===========================================================================
# get_nested_model
# ===========================================================================


class TestGetNestedModel:
    def test_basemodel_returns_itself(self) -> None:
        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.get_nested_model(M) is M

    def test_optional_basemodel_returns_model(self) -> None:
        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.get_nested_model(Optional[M]) is M

    def test_primitive_returns_none(self) -> None:
        assert PydanticTypeMapper.get_nested_model(int) is None

    def test_optional_primitive_returns_none(self) -> None:
        assert PydanticTypeMapper.get_nested_model(Optional[int]) is None

    def test_list_of_basemodel_returns_none(self) -> None:
        """Per V1 scope: List[Model] does not produce a nested_spec."""

        class M(BaseModel):
            x: int

        assert PydanticTypeMapper.get_nested_model(List[M]) is None

    def test_dict_returns_none(self) -> None:
        assert PydanticTypeMapper.get_nested_model(dict) is None


# ===========================================================================
# FieldType completeness — every FieldType reachable via some annotation
# ===========================================================================


class TestFieldTypeCompleteness:
    """Every FieldType enum value must be reachable from some annotation."""

    def test_string(self) -> None:
        assert PydanticTypeMapper.map_annotation(str) is FieldType.STRING

    def test_integer(self) -> None:
        assert PydanticTypeMapper.map_annotation(int) is FieldType.INTEGER

    def test_float(self) -> None:
        assert PydanticTypeMapper.map_annotation(float) is FieldType.FLOAT

    def test_boolean(self) -> None:
        assert PydanticTypeMapper.map_annotation(bool) is FieldType.BOOLEAN

    def test_object(self) -> None:
        assert PydanticTypeMapper.map_annotation(dict) is FieldType.OBJECT

    def test_array(self) -> None:
        assert PydanticTypeMapper.map_annotation(list) is FieldType.ARRAY

    def test_any(self) -> None:
        assert PydanticTypeMapper.map_annotation(Any) is FieldType.ANY

    def test_null(self) -> None:
        assert PydanticTypeMapper.map_annotation(type(None)) is FieldType.NULL


# ===========================================================================
# get_union_members
# ===========================================================================


class TestGetUnionMembers:
    def test_non_union_returns_none(self) -> None:
        assert PydanticTypeMapper.get_union_members(int) is None
        assert PydanticTypeMapper.get_union_members(List[str]) is None

    def test_optional_returns_none(self) -> None:
        """Optional[X] is unwrap_optional's job, not a multi-type union."""
        assert PydanticTypeMapper.get_union_members(Optional[str]) is None

    def test_typing_union_members(self) -> None:
        members = PydanticTypeMapper.get_union_members(Union[int, str])
        assert members == (
            UnionMember(FieldType.INTEGER),
            UnionMember(FieldType.STRING),
        )

    def test_pep604_union_members(self) -> None:
        members = PydanticTypeMapper.get_union_members(int | str)
        assert members == (
            UnionMember(FieldType.INTEGER),
            UnionMember(FieldType.STRING),
        )

    def test_none_member_dropped(self) -> None:
        members = PydanticTypeMapper.get_union_members(Union[int, str, None])
        assert members == (
            UnionMember(FieldType.INTEGER),
            UnionMember(FieldType.STRING),
        )

    def test_array_member_carries_item_type(self) -> None:
        members = PydanticTypeMapper.get_union_members(Union[str, List[int]])
        assert members == (
            UnionMember(FieldType.STRING),
            UnionMember(FieldType.ARRAY, item_type=FieldType.INTEGER),
        )

    def test_union_item_type_collapses_to_any(self) -> None:
        """The AIMessage.content shape: str | list[str | dict]."""
        annotation = Union[str, List[Union[str, Dict[str, Any]]]]
        members = PydanticTypeMapper.get_union_members(annotation)
        assert members == (
            UnionMember(FieldType.STRING),
            UnionMember(FieldType.ARRAY, item_type=FieldType.ANY),
        )

    def test_bare_list_member_has_no_item_type(self) -> None:
        members = PydanticTypeMapper.get_union_members(Union[str, list])
        assert members == (
            UnionMember(FieldType.STRING),
            UnionMember(FieldType.ARRAY, item_type=None),
        )

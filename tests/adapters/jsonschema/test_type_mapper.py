"""
JSON Schema type keywords -> ``FieldType``.

The rules under test are not this module's own invention: they mirror
``PydanticTypeMapper`` so that the same field described through either
adapter reaches the engine as the same ``FieldType``. Where a test asserts
something surprising -- ``["string", "null"]`` is a nullable string rather
than a union -- the reason is parity, and ``test_parity_with_pydantic.py``
checks the two adapters against each other directly.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from stateguard.adapters.jsonschema import (
    RefResolver,
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.type_mapper import (
    JSONSchemaTypeMapper,
    MappedType,
)
from stateguard.core.models.field_types import FieldType


@pytest.fixture
def mapper() -> JSONSchemaTypeMapper:
    return JSONSchemaTypeMapper()


def _map(
    mapper: JSONSchemaTypeMapper,
    schema: dict[str, Any],
    root: dict[str, Any] | None = None,
) -> MappedType:
    resolver = RefResolver(root if root is not None else schema)
    return mapper.map_schema(schema, resolver, "field")


# ===========================================================================
# Named types
# ===========================================================================


class TestNamedTypes:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("string", FieldType.STRING),
            ("integer", FieldType.INTEGER),
            ("number", FieldType.FLOAT),
            ("boolean", FieldType.BOOLEAN),
            ("object", FieldType.OBJECT),
            ("array", FieldType.ARRAY),
            ("null", FieldType.NULL),
        ],
    )
    def test_each_json_type_maps(
        self, mapper: JSONSchemaTypeMapper, name: str, expected: FieldType
    ) -> None:
        assert _map(mapper, {"type": name}).field_type is expected

    def test_number_is_float_and_integer_stays_integer(self, mapper: JSONSchemaTypeMapper) -> None:
        """JSON draws the same int/float line StateGuard does."""
        assert _map(mapper, {"type": "number"}).field_type is FieldType.FLOAT
        assert _map(mapper, {"type": "integer"}).field_type is FieldType.INTEGER

    def test_unknown_type_is_an_error_not_any(self, mapper: JSONSchemaTypeMapper) -> None:
        """
        Falling back to ANY would widen the field to "accepts anything" --
        under-validating silently, which is the one thing this adapter must
        never do.
        """
        with pytest.raises(UnsupportedSchemaError, match="unknown type 'str'"):
            _map(mapper, {"type": "str"})

    def test_non_string_non_list_type_is_an_error(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="must be a string or a list"):
            _map(mapper, {"type": 7})

    def test_no_type_at_all_is_any(self, mapper: JSONSchemaTypeMapper) -> None:
        """The one legitimate ANY: the author declared no type."""
        assert _map(mapper, {"description": "anything"}).field_type is FieldType.ANY


# ===========================================================================
# Unions
# ===========================================================================


class TestUnions:
    def test_type_list_with_null_is_a_nullable_scalar_not_a_union(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        """The common optional idiom. Parity with ``unwrap_optional``."""
        mapped = _map(mapper, {"type": ["string", "null"]})
        assert mapped.field_type is FieldType.STRING
        assert mapped.nullable is True
        assert mapped.union_members is None

    def test_anyof_with_null_is_a_nullable_scalar(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"anyOf": [{"type": "integer"}, {"type": "null"}]})
        assert mapped.field_type is FieldType.INTEGER
        assert mapped.nullable is True

    def test_two_real_types_produce_a_union(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"type": ["string", "integer"]})
        assert mapped.field_type is FieldType.UNION
        assert [m.field_type for m in mapped.union_members or ()] == [
            FieldType.STRING,
            FieldType.INTEGER,
        ]

    def test_null_is_dropped_from_union_members(self, mapper: JSONSchemaTypeMapper) -> None:
        """Nullability is reported separately, never as a member."""
        mapped = _map(mapper, {"type": ["string", "integer", "null"]})
        assert mapped.field_type is FieldType.UNION
        assert mapped.nullable is True
        assert FieldType.NULL not in [m.field_type for m in mapped.union_members or ()]

    def test_oneof_behaves_like_anyof(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"oneOf": [{"type": "string"}, {"type": "integer"}]})
        assert mapped.field_type is FieldType.UNION

    def test_all_null_branches_is_a_null_field(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"anyOf": [{"type": "null"}, {"type": "null"}]})
        assert mapped.field_type is FieldType.NULL
        assert mapped.nullable is True

    def test_sibling_keywords_travel_with_the_collapsed_branch(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        """
        ``{"type": ["string","null"], "minLength": 2}`` means *a string of at
        least 2 chars, or null*. Dropping ``minLength`` while collapsing
        would silently discard a constraint the extractor still has to read.
        """
        mapped = _map(mapper, {"type": ["string", "null"], "minLength": 2})
        assert mapped.field_type is FieldType.STRING
        assert mapped.effective_schema.get("minLength") == 2

    def test_union_branch_through_a_ref_resolves(self, mapper: JSONSchemaTypeMapper) -> None:
        root = {"$defs": {"S": {"type": "string"}}}
        mapped = _map(
            mapper,
            {"anyOf": [{"$ref": "#/$defs/S"}, {"type": "null"}]},
            root=root,
        )
        assert mapped.field_type is FieldType.STRING
        assert mapped.nullable is True

    def test_collapsed_branch_is_returned_unresolved(self, mapper: JSONSchemaTypeMapper) -> None:
        """
        The extractor must descend through the resolver to keep the
        recursion guard armed, so the surviving branch comes back still
        carrying its ``$ref``.
        """
        root = {"$defs": {"S": {"type": "object", "properties": {}}}}
        mapped = _map(
            mapper,
            {"anyOf": [{"$ref": "#/$defs/S"}, {"type": "null"}]},
            root=root,
        )
        assert mapped.effective_schema == {"$ref": "#/$defs/S"}

    def test_anyof_and_oneof_together_is_refused(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="same schema"):
            _map(
                mapper,
                {"anyOf": [{"type": "string"}], "oneOf": [{"type": "integer"}]},
            )

    @pytest.mark.parametrize(
        "schema",
        [{"anyOf": []}, {"oneOf": []}, {"type": []}],
        ids=["anyOf", "oneOf", "type"],
    )
    def test_empty_combinator_is_refused(
        self, mapper: JSONSchemaTypeMapper, schema: dict[str, Any]
    ) -> None:
        with pytest.raises(UnsupportedSchemaError, match="accepts nothing"):
            _map(mapper, schema)

    def test_non_list_combinator_is_refused(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="must be a list"):
            _map(mapper, {"anyOf": {"type": "string"}})


# ===========================================================================
# Arrays
# ===========================================================================


class TestArrays:
    def test_item_type_comes_from_items(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"type": "array", "items": {"type": "integer"}})
        assert mapped.field_type is FieldType.ARRAY
        assert mapped.item_type is FieldType.INTEGER

    def test_array_without_items_accepts_any_element(self, mapper: JSONSchemaTypeMapper) -> None:
        assert _map(mapper, {"type": "array"}).item_type is FieldType.ANY

    def test_tuple_form_items_collapses_to_any_with_a_warning(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        """
        Positional typing has no representation in a single ``item_type``.
        ANY is honest; taking the first position's type would claim coverage
        the contract does not have.

        It warns on the way past, though: widening to ANY validates more
        loosely than the schema asks, which is the same situation
        ``exclusiveMinimum`` is in, and that one has always warned.
        """
        schema = {"type": "array", "items": [{"type": "string"}, {"type": "integer"}]}
        with pytest.warns(SchemaFeatureWarning, match="widened to ANY"):
            assert _map(mapper, schema).item_type is FieldType.ANY

    def test_absent_items_collapses_to_any_silently(self, mapper: JSONSchemaTypeMapper) -> None:
        """
        An array with no ``items`` genuinely accepts any element, so there
        is nothing being validated more loosely and nothing to warn about.
        """
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            assert _map(mapper, {"type": "array"}).item_type is FieldType.ANY

    def test_union_element_type_collapses_to_any(self, mapper: JSONSchemaTypeMapper) -> None:
        """Matches ``PydanticTypeMapper.get_item_type``."""
        schema = {"type": "array", "items": {"type": ["string", "integer"]}}
        assert _map(mapper, schema).item_type is FieldType.ANY

    def test_items_through_a_ref_resolves(self, mapper: JSONSchemaTypeMapper) -> None:
        root = {"$defs": {"I": {"type": "integer"}}}
        schema = {"type": "array", "items": {"$ref": "#/$defs/I"}}
        assert _map(mapper, schema, root=root).item_type is FieldType.INTEGER

    def test_array_member_of_a_union_carries_its_item_type(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        mapped = _map(
            mapper,
            {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}},
                    {"type": "integer"},
                ]
            },
        )
        array_member = next(
            m for m in mapped.union_members or () if m.field_type is FieldType.ARRAY
        )
        assert array_member.item_type is FieldType.STRING


# ===========================================================================
# enum / const
# ===========================================================================


class TestEnumAndConst:
    def test_enum_values_are_captured(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"type": "string", "enum": ["celsius", "fahrenheit"]})
        assert mapped.enum_values == ("celsius", "fahrenheit")

    def test_const_normalises_to_a_single_member_enum(self, mapper: JSONSchemaTypeMapper) -> None:
        assert _map(mapper, {"const": "fixed"}).enum_values == ("fixed",)

    def test_type_is_inferred_from_enum_members_when_absent(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        assert _map(mapper, {"enum": ["a", "b"]}).field_type is FieldType.STRING
        assert _map(mapper, {"enum": [1, 2]}).field_type is FieldType.INTEGER

    def test_bool_members_infer_boolean_not_integer(self, mapper: JSONSchemaTypeMapper) -> None:
        """``bool`` is an ``int`` subclass in Python; order of checks matters."""
        assert _map(mapper, {"enum": [True, False]}).field_type is FieldType.BOOLEAN

    def test_null_members_do_not_stop_type_inference(self, mapper: JSONSchemaTypeMapper) -> None:
        assert _map(mapper, {"enum": ["a", None]}).field_type is FieldType.STRING

    def test_mixed_member_types_infer_any(self, mapper: JSONSchemaTypeMapper) -> None:
        """
        The members genuinely do not share a type. ``ENUM_VALUES`` still
        pins the allowed set exactly, so nothing is lost.
        """
        assert _map(mapper, {"enum": ["a", 1]}).field_type is FieldType.ANY

    def test_all_null_enum_infers_null(self, mapper: JSONSchemaTypeMapper) -> None:
        assert _map(mapper, {"enum": [None]}).field_type is FieldType.NULL

    def test_declared_type_wins_over_inference(self, mapper: JSONSchemaTypeMapper) -> None:
        mapped = _map(mapper, {"type": "string", "enum": ["1", "2"]})
        assert mapped.field_type is FieldType.STRING

    def test_duplicate_members_are_dropped_preserving_order(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        mapped = _map(mapper, {"enum": ["b", "a", "b"]})
        assert mapped.enum_values == ("b", "a")

    def test_enum_and_const_together_is_refused(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="ambiguous"):
            _map(mapper, {"enum": ["a"], "const": "a"})

    def test_empty_enum_is_refused(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="accepts nothing"):
            _map(mapper, {"enum": []})

    def test_non_list_enum_is_refused(self, mapper: JSONSchemaTypeMapper) -> None:
        with pytest.raises(UnsupportedSchemaError, match="must be a list"):
            _map(mapper, {"enum": "celsius"})

    def test_const_null_is_captured_not_treated_as_absent(
        self, mapper: JSONSchemaTypeMapper
    ) -> None:
        """``const: null`` is a real constraint, distinct from no const."""
        assert _map(mapper, {"const": None}).enum_values == (None,)

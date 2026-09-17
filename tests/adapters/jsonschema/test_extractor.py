"""
JSON Schema document -> ``ContractSpec``.

Covers structure (properties, required, defaults, nesting), the constraint
translation, and -- most importantly -- the refusals. The refusal tests carry
the weight here: ``ContractValidator`` is the source of truth for this
adapter (ADR-0001), so anything the extractor lets through unvalidated is
unvalidated for good, with no second validator downstream to catch it.
"""

from __future__ import annotations

import warnings
from typing import Any

import pytest

from stateguard.adapters.jsonschema import (
    JSONSchemaExtractor,
    SchemaFeatureWarning,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.core.models.contract import MISSING, ContractSpec
from stateguard.core.models.field_types import FieldConstraintType, FieldType


@pytest.fixture
def extractor() -> JSONSchemaExtractor:
    return JSONSchemaExtractor()


def _field(spec: ContractSpec, name: str) -> Any:
    return next(f for f in spec.fields if f.path == name)


def _constraint(spec: ContractSpec, name: str, kind: FieldConstraintType) -> Any:
    return next(c for c in _field(spec, name).constraints if c.constraint_type is kind)


# ===========================================================================
# Structure
# ===========================================================================


class TestStructure:
    def test_properties_become_fields(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "integer"}},
            }
        )
        assert [f.path for f in spec.fields] == ["a", "b"]
        assert _field(spec, "a").field_type is FieldType.STRING

    def test_required_list_drives_requiredness(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
                "required": ["a"],
            }
        )
        assert _field(spec, "a").required is True
        assert _field(spec, "b").required is False

    def test_absent_required_means_nothing_is_required(
        self, extractor: JSONSchemaExtractor
    ) -> None:
        spec = extractor.extract({"type": "object", "properties": {"a": {"type": "string"}}})
        assert _field(spec, "a").required is False

    def test_default_is_captured(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string", "default": "x"}},
            }
        )
        assert _field(spec, "a").default == "x"

    def test_null_default_is_distinct_from_no_default(self, extractor: JSONSchemaExtractor) -> None:
        """``DefaultValueFillStrategy`` reads exactly this difference."""
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {
                    "has": {"type": ["string", "null"], "default": None},
                    "lacks": {"type": "string"},
                },
            }
        )
        assert _field(spec, "has").default is None
        assert _field(spec, "lacks").default is MISSING

    def test_nested_object_produces_a_nested_spec(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    }
                },
            }
        )
        nested = _field(spec, "address").nested_spec
        assert nested is not None
        assert _field(nested, "city").required is True

    def test_nested_field_paths_are_local_segments(self, extractor: JSONSchemaExtractor) -> None:
        """Adapters set ``FieldSpec.path`` to one segment; the engine joins."""
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {
                    "address": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    }
                },
            }
        )
        nested = _field(spec, "address").nested_spec
        assert [f.path for f in nested.fields] == ["city"]

    def test_ref_to_a_definition_is_resolved(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "$defs": {
                    "Address": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    }
                },
                "type": "object",
                "properties": {"address": {"$ref": "#/$defs/Address"}},
            }
        )
        assert _field(spec, "address").field_type is FieldType.OBJECT
        assert _field(spec, "address").nested_spec is not None

    def test_additional_properties_false_sets_strict_mode(
        self, extractor: JSONSchemaExtractor
    ) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": False,
            }
        )
        assert spec.strict_mode is True

    def test_recursive_schema_raises_instead_of_hanging(
        self, extractor: JSONSchemaExtractor
    ) -> None:
        schema = {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
            "$ref": "#/$defs/Node",
        }
        with pytest.raises(SchemaReferenceError, match="Recursive schema"):
            extractor.extract(schema)

    def test_non_object_root_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="expected an object schema"):
            extractor.extract({"type": "string"})

    def test_non_mapping_schema_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="must be an object"):
            extractor.extract(["not", "a", "schema"])  # type: ignore[arg-type]


# ===========================================================================
# Constraints
# ===========================================================================


class TestConstraints:
    @pytest.mark.parametrize(
        ("keyword", "value", "kind"),
        [
            ("minimum", 1, FieldConstraintType.MINIMUM),
            ("maximum", 14, FieldConstraintType.MAXIMUM),
            ("minLength", 2, FieldConstraintType.MIN_LENGTH),
            ("maxLength", 8, FieldConstraintType.MAX_LENGTH),
            ("pattern", "^a", FieldConstraintType.PATTERN),
        ],
    )
    def test_value_constraints_translate(
        self,
        extractor: JSONSchemaExtractor,
        keyword: str,
        value: Any,
        kind: FieldConstraintType,
    ) -> None:
        spec = extractor.extract(
            {"type": "object", "properties": {"a": {"type": "string", keyword: value}}}
        )
        assert _constraint(spec, "a", kind).value == value

    @pytest.mark.parametrize(
        ("keyword", "kind"),
        [
            ("minItems", FieldConstraintType.MIN_LENGTH),
            ("maxItems", FieldConstraintType.MAX_LENGTH),
        ],
    )
    def test_array_size_constraints_reuse_the_length_checks(
        self,
        extractor: JSONSchemaExtractor,
        keyword: str,
        kind: FieldConstraintType,
    ) -> None:
        spec = extractor.extract(
            {"type": "object", "properties": {"a": {"type": "array", keyword: 3}}}
        )
        assert _constraint(spec, "a", kind).value == 3

    def test_enum_becomes_an_enum_values_constraint(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"u": {"type": "string", "enum": ["c", "f"]}},
            }
        )
        assert _constraint(spec, "u", FieldConstraintType.ENUM_VALUES).value == ("c", "f")

    @pytest.mark.parametrize("keyword", ["exclusiveMinimum", "exclusiveMaximum"])
    def test_exclusive_bounds_are_dropped_with_a_warning(
        self, extractor: JSONSchemaExtractor, keyword: str
    ) -> None:
        """
        Rounding an exclusive bound to an inclusive one would accept a value
        the schema forbids, so it is dropped -- but never silently.
        """
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer", keyword: 0}},
        }
        with pytest.warns(SchemaFeatureWarning, match=keyword):
            spec = extractor.extract(schema)

        kinds = {c.constraint_type for c in _field(spec, "a").constraints}
        assert FieldConstraintType.MINIMUM not in kinds
        assert FieldConstraintType.MAXIMUM not in kinds

    def test_dropped_keyword_warning_can_be_made_fatal(
        self, extractor: JSONSchemaExtractor
    ) -> None:
        """A caller who cannot accept the gap has a supported way to refuse."""
        schema = {
            "type": "object",
            "properties": {"a": {"type": "integer", "exclusiveMinimum": 0}},
        }
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            with pytest.raises(SchemaFeatureWarning):
                extractor.extract(schema)


# ===========================================================================
# Nullability -- the divergence from the Pydantic adapter
# ===========================================================================


class TestNullability:
    """
    ``PydanticExtractor`` never emits ``NOT_NULL`` and is right not to:
    Pydantic's own validator rejects a stray ``None``. This adapter has no
    such backstop, so it must emit the constraint or ``ContractValidator``
    will accept ``None`` for a non-nullable field. See ADR-0001.
    """

    def test_non_nullable_field_gets_not_null(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract({"type": "object", "properties": {"a": {"type": "string"}}})
        kinds = {c.constraint_type for c in _field(spec, "a").constraints}
        assert FieldConstraintType.NOT_NULL in kinds

    def test_nullable_field_does_not(self, extractor: JSONSchemaExtractor) -> None:
        spec = extractor.extract(
            {"type": "object", "properties": {"a": {"type": ["string", "null"]}}}
        )
        kinds = {c.constraint_type for c in _field(spec, "a").constraints}
        assert FieldConstraintType.NOT_NULL not in kinds

    def test_untyped_field_does_not(self, extractor: JSONSchemaExtractor) -> None:
        """No declared type means the field genuinely accepts null too."""
        spec = extractor.extract({"type": "object", "properties": {"a": {}}})
        kinds = {c.constraint_type for c in _field(spec, "a").constraints}
        assert FieldConstraintType.NOT_NULL not in kinds


# ===========================================================================
# Refusals
# ===========================================================================


class TestRefusals:
    @pytest.mark.parametrize(
        "keyword",
        [
            "allOf",
            "not",
            "if",
            "then",
            "else",
            "patternProperties",
            "dependentSchemas",
            "dependentRequired",
            "propertyNames",
            "unevaluatedProperties",
            "unevaluatedItems",
        ],
    )
    def test_rejected_keywords_raise(self, extractor: JSONSchemaExtractor, keyword: str) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string", keyword: {}}},
        }
        with pytest.raises(UnsupportedSchemaError, match=keyword):
            extractor.extract(schema)

    def test_rejected_keyword_at_the_root_raises(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="allOf"):
            extractor.extract({"type": "object", "properties": {}, "allOf": []})

    def test_unrecognised_keyword_raises_rather_than_being_ignored(
        self, extractor: JSONSchemaExtractor
    ) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"type": "string", "frobnicate": 1}},
        }
        with pytest.raises(UnsupportedSchemaError, match="frobnicate"):
            extractor.extract(schema)

    def test_extension_keywords_are_allowed(self, extractor: JSONSchemaExtractor) -> None:
        """``x-`` prefixed keywords are the escape hatch for real servers."""
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string", "x-internal": True}},
            }
        )
        assert _field(spec, "a").field_type is FieldType.STRING

    @pytest.mark.parametrize("keyword", ["title", "description", "examples", "format", "$comment"])
    def test_advisory_keywords_are_ignored_not_refused(
        self, extractor: JSONSchemaExtractor, keyword: str
    ) -> None:
        spec = extractor.extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string", keyword: "whatever"}},
            }
        )
        assert _field(spec, "a").field_type is FieldType.STRING

    def test_dotted_property_name_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        """
        ``{"user.name": ...}`` and ``{"user": {"name": ...}}`` produce the
        same dot-notation path. Accepting it would let a repair write to the
        wrong field -- the failure that damages trust most (§8).
        """
        with pytest.raises(UnsupportedSchemaError, match=r"contains '\.'"):
            extractor.extract({"type": "object", "properties": {"user.name": {"type": "string"}}})

    def test_empty_property_name_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="must not be empty"):
            extractor.extract({"type": "object", "properties": {"": {"type": "string"}}})

    def test_boolean_subschema_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="Boolean schemas"):
            extractor.extract({"type": "object", "properties": {"a": True}})

    def test_non_list_required_is_refused(self, extractor: JSONSchemaExtractor) -> None:
        with pytest.raises(UnsupportedSchemaError, match="'required' must be a list"):
            extractor.extract({"type": "object", "properties": {}, "required": "a"})


# ===========================================================================
# $id
# ===========================================================================


class TestSubschemaId:
    """
    ``$id`` on a subschema rebases reference resolution.

    Inside an ``$id``-bearing subschema, ``#/$defs/A`` means *that
    subschema's* ``$defs``, not the document's. This adapter resolves every
    pointer against the document root, so honouring the keyword is not
    possible without a URI-scoped resolver -- and ignoring it silently
    resolves to the wrong schema, which is the failure the whole adapter is
    built to avoid.
    """

    def test_subschema_id_is_refused(self) -> None:
        schema = {
            "type": "object",
            "properties": {"a": {"$id": "https://example.com/inner", "type": "string"}},
        }
        with pytest.raises(UnsupportedSchemaError, match=r"\$id"):
            JSONSchemaExtractor().extract(schema)

    def test_nested_object_id_is_refused(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "o": {
                    "type": "object",
                    "$id": "https://example.com/inner",
                    "properties": {},
                }
            },
        }
        with pytest.raises(UnsupportedSchemaError, match=r"\$id"):
            JSONSchemaExtractor().extract(schema)

    def test_root_id_is_allowed(self) -> None:
        """Root ``$id`` names the document without changing what ``#`` means."""
        schema = {
            "$id": "https://example.com/schema",
            "type": "object",
            "properties": {"a": {"type": "string"}},
        }
        assert JSONSchemaExtractor().extract(schema).fields[0].path == "a"

    def test_the_misresolution_it_prevents(self) -> None:
        """
        Without the refusal this extracted ``integer`` -- the *root* ``A`` --
        where the spec says the inner ``A`` (``string``) applies.
        """
        schema = {
            "$defs": {"A": {"type": "integer"}},
            "type": "object",
            "properties": {
                "x": {
                    "$id": "https://example.com/inner",
                    "$defs": {"A": {"type": "string"}},
                    "$ref": "#/$defs/A",
                }
            },
        }
        with pytest.raises(UnsupportedSchemaError, match=r"\$id"):
            JSONSchemaExtractor().extract(schema)

"""
Regression tests for the ways this adapter used to validate too loosely.

Every test here corresponds to a schema that previously extracted *cleanly*
while quietly dropping something the schema said. That is the one failure
mode the adapter is least able to tolerate: ``ContractValidator`` is the
source of truth for JSON Schema (``docs/adr/0001-json-schema-source-of-truth
.md``), so there is no second validator downstream to notice what got lost.
A crash is visible, a refusal is visible; a silently-widened contract is not.

They are grouped by the bug that produced them rather than by keyword,
because three of the four groups share a single root cause: the subset screen
used to be a private method on ``JSONSchemaExtractor``, so it only ran where
*that* module happened to walk. ``type_mapper`` walks too -- through
``anyOf``/``oneOf`` branches and through ``items`` -- and everything it
reached went unscreened.
"""

from __future__ import annotations

import warnings
from contextlib import ExitStack
from typing import Any

import pytest

from stateguard.adapters.jsonschema import (
    JSONSchemaExtractor,
    RefResolver,
    SchemaFeatureWarning,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import FieldConstraintType, FieldType


def _extract(schema: dict[str, Any]) -> ContractSpec:
    return JSONSchemaExtractor().extract(schema)


def _field(spec: ContractSpec, name: str) -> FieldSpec:
    for candidate in spec.fields:
        if candidate.path == name:
            return candidate
    raise AssertionError(f"no field {name!r} in {[f.path for f in spec.fields]}")


def _constraints(spec: ContractSpec, name: str) -> dict[FieldConstraintType, Any]:
    return {c.constraint_type: c.value for c in _field(spec, name).constraints}


# ===========================================================================
# Optional[X] -- the shape Pydantic emits for every optional model field
# ===========================================================================


class TestOptionalRefKeepsWhatTheSchemaSaid:
    """
    ``{"anyOf": [{"$ref": ...}, {"type": "null"}]}`` is what Pydantic emits
    for *every* ``Optional[X]``, so it is likely the most common non-trivial
    shape in real MCP tool schemas.

    ``MappedType.effective_schema`` is deliberately left unresolved so that
    descending through it re-arms the recursion guard. The extractor used to
    read constraints, defaults and the keyword screen straight off it -- and
    for this shape it is a bare ``{"$ref": ...}``, which carries none of
    them. Everything the referenced definition said was silently discarded.
    """

    @staticmethod
    def _optional_ref(definition: dict[str, Any]) -> dict[str, Any]:
        return {
            "type": "object",
            "properties": {"name": {"anyOf": [{"$ref": "#/$defs/N"}, {"type": "null"}]}},
            "$defs": {"N": definition},
        }

    def test_constraints_survive(self) -> None:
        spec = _extract(self._optional_ref({"type": "string", "minLength": 3, "maxLength": 9}))
        assert _constraints(spec, "name") == {
            FieldConstraintType.MIN_LENGTH: 3,
            FieldConstraintType.MAX_LENGTH: 9,
        }

    def test_declared_default_survives(self) -> None:
        spec = _extract(self._optional_ref({"type": "string", "default": "bob"}))
        assert _field(spec, "name").default == "bob"

    def test_numeric_bounds_survive(self) -> None:
        spec = _extract(self._optional_ref({"type": "integer", "minimum": 1, "maximum": 14}))
        assert _constraints(spec, "name") == {
            FieldConstraintType.MINIMUM: 1,
            FieldConstraintType.MAXIMUM: 14,
        }

    def test_rejected_keyword_behind_the_ref_is_refused(self) -> None:
        """
        The sharp end of the bug. ``allOf`` was not merely dropped -- with no
        ``type`` left to read, the field became ``ANY``, which accepts
        anything. A schema written to *forbid* a payload ended up permitting
        every payload.
        """
        with pytest.raises(UnsupportedSchemaError, match="allOf"):
            _extract(self._optional_ref({"allOf": [{"type": "string"}]}))

    def test_redos_screen_is_reached_through_the_ref(self) -> None:
        """
        The pattern screen lives in ``_constraints``, so a dropped constraint
        was also an unscreened one. It did not reach ``re`` -- the constraint
        was gone entirely -- but the schema was accepted without anyone
        noticing it asked for a catastrophic pattern.
        """
        with pytest.raises(UnsupportedSchemaError, match="exponential time"):
            _extract(self._optional_ref({"type": "string", "pattern": "(a+)+$"}))

    def test_nullable_field_still_omits_not_null(self) -> None:
        """The fix must not over-correct: this field really does accept null."""
        spec = _extract(self._optional_ref({"type": "string"}))
        assert FieldConstraintType.NOT_NULL not in _constraints(spec, "name")

    def test_optional_nested_model_still_nests(self) -> None:
        """
        This path always worked -- it re-entered the resolver -- and the fix
        generalises that re-entry to every field rather than replacing it.
        """
        spec = _extract(
            self._optional_ref(
                {
                    "type": "object",
                    "properties": {"inner": {"type": "string"}},
                    "required": ["inner"],
                }
            )
        )
        nested = _field(spec, "name").nested_spec
        assert nested is not None
        assert _field(nested, "inner").required is True

    def test_recursion_guard_survives_the_extra_resolution(self) -> None:
        """
        The fix resolves ``effective_schema`` a second time, so the guard has
        to still fire on a genuinely recursive schema rather than the extra
        hop popping the pointer early.
        """
        schema = {
            "type": "object",
            "properties": {"node": {"anyOf": [{"$ref": "#/$defs/N"}, {"type": "null"}]}},
            "$defs": {
                "N": {
                    "type": "object",
                    "properties": {"child": {"anyOf": [{"$ref": "#/$defs/N"}, {"type": "null"}]}},
                }
            },
        }
        with pytest.raises(SchemaReferenceError, match="Recursive schema"):
            _extract(schema)

    def test_sibling_refs_are_not_mistaken_for_recursion(self) -> None:
        """Two optional fields of the same type are legal and must extract."""
        schema = {
            "type": "object",
            "properties": {
                "home": {"anyOf": [{"$ref": "#/$defs/A"}, {"type": "null"}]},
                "work": {"anyOf": [{"$ref": "#/$defs/A"}, {"type": "null"}]},
            },
            "$defs": {"A": {"type": "string", "minLength": 2}},
        }
        spec = _extract(schema)
        assert _constraints(spec, "home") == {FieldConstraintType.MIN_LENGTH: 2}
        assert _constraints(spec, "work") == {FieldConstraintType.MIN_LENGTH: 2}


# ===========================================================================
# The screen must run everywhere a schema is walked, not just in the extractor
# ===========================================================================


class TestScreenReachesEveryWalker:
    """
    ``type_mapper`` descends into union branches and into ``items``. The
    extractor never sees either, so while the screen was private to the
    extractor, both were unscreened.
    """

    def test_rejected_keyword_in_a_union_branch(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="allOf"):
            _extract(
                {
                    "type": "object",
                    "properties": {
                        "m": {
                            "anyOf": [
                                {"type": "string"},
                                {"type": "integer", "allOf": [{}]},
                            ]
                        }
                    },
                }
            )

    def test_rejected_keyword_in_a_union_branch_behind_a_ref(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="'not'"):
            _extract(
                {
                    "type": "object",
                    "properties": {"m": {"anyOf": [{"type": "string"}, {"$ref": "#/$defs/X"}]}},
                    "$defs": {"X": {"type": "integer", "not": {}}},
                }
            )

    def test_rejected_keyword_in_array_items(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="'if'"):
            _extract(
                {
                    "type": "object",
                    "properties": {
                        "m": {
                            "type": "array",
                            "items": {"type": "object", "properties": {}, "if": {}},
                        }
                    },
                }
            )

    def test_unknown_keyword_in_array_items_names_the_element_path(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match=r"m\[\]"):
            _extract(
                {
                    "type": "object",
                    "properties": {"m": {"type": "array", "items": {"type": "string", "bogus": 1}}},
                }
            )

    def test_legal_union_and_array_schemas_still_extract(self) -> None:
        """The screen must not start refusing what it always accepted."""
        spec = _extract(
            {
                "type": "object",
                "properties": {
                    "u": {"anyOf": [{"type": "string"}, {"type": "integer"}]},
                    "a": {"type": "array", "items": {"type": "string"}},
                },
            }
        )
        assert _field(spec, "u").field_type is FieldType.UNION
        assert _field(spec, "a").item_type is FieldType.STRING


# ===========================================================================
# Depth exhaustion -- the third way a walk fails to terminate
# ===========================================================================


class TestDepthIsBounded:
    """
    A finite but very deep schema trips neither cycle guard: there is no
    cycle. It used to exhaust the interpreter stack and surface as
    ``RecursionError`` -- a ``RuntimeError``, so it escaped every caller
    catching ``JSONSchemaError`` and landed as an unhandled crash on input
    the adapter's own documentation calls untrusted.
    """

    @staticmethod
    def _nest(levels: int) -> dict[str, Any]:
        schema: dict[str, Any] = {"type": "string"}
        for _ in range(levels):
            schema = {"type": "object", "properties": {"a": schema}}
        return schema

    def test_deep_schema_raises_a_catchable_error(self) -> None:
        """
        ``SchemaReferenceError`` rather than ``RecursionError`` is the whole
        point: a proxy can catch this one and answer its caller. That the
        guard fires *before* the interpreter's own limit is asserted by this
        test passing at all -- if the stack went first, the exception raised
        would be ``RecursionError``, which is not a ``JSONSchemaError`` and
        would not be caught here.
        """
        with pytest.raises(SchemaReferenceError, match="nests deeper"):
            _extract(self._nest(3000))

    def test_realistic_nesting_is_unaffected(self) -> None:
        """
        The bound exists to stop a depth bomb, not to limit real schemas. No
        MCP tool signature nests anything like this far.
        """
        spec = _extract(self._nest(20))
        assert spec.fields[0].path == "a"

    def test_depth_bomb_through_refs_is_also_bounded(self) -> None:
        """
        Deep nesting spelled with references rather than inline objects
        reaches the same guard.
        """
        defs: dict[str, Any] = {"D0": {"type": "string"}}
        for level in range(1, 300):
            defs[f"D{level}"] = {
                "type": "object",
                "properties": {"a": {"$ref": f"#/$defs/D{level - 1}"}},
            }
        schema = {
            "type": "object",
            "properties": {"a": {"$ref": "#/$defs/D299"}},
            "$defs": defs,
        }
        with pytest.raises(SchemaReferenceError, match="nests deeper"):
            _extract(schema)

    def test_the_bound_is_configurable(self) -> None:
        """
        The default is chosen for CPython's default stack. A host that has
        raised ``setrecursionlimit`` for a genuinely deep schema can raise
        this to match, so the guard is a safety bound rather than a ceiling
        on what the adapter can ever read.
        """
        resolver = RefResolver({"type": "object"}, max_depth=2)
        with ExitStack() as stack:
            stack.enter_context(resolver.resolved({"type": "object"}))
            stack.enter_context(resolver.resolved({"type": "object"}))
            with pytest.raises(SchemaReferenceError, match="deeper than 2"):
                stack.enter_context(resolver.resolved({"type": "object"}))

    def test_depth_is_released_on_exit(self) -> None:
        """
        Sibling fields must not accumulate depth. Without the decrement, a
        wide-but-shallow schema -- 200 scalar properties, which is ordinary
        -- would trip a guard meant for deep ones.
        """
        resolver = RefResolver({"type": "object"}, max_depth=3)
        for _ in range(50):
            with resolver.resolved({"type": "string"}):
                pass


# ===========================================================================
# Nullability, presence, and openness
# ===========================================================================


class TestEnumNullability:
    def test_untyped_enum_with_null_member_is_nullable(self) -> None:
        """
        With no ``type`` to forbid it, a ``null`` member is a value the
        schema genuinely accepts. Emitting NOT_NULL made ``{"m": null}`` a
        *false* violation -- the class of error that can trigger a repair of
        a field that was never broken.
        """
        spec = _extract({"type": "object", "properties": {"m": {"enum": ["a", "b", None]}}})
        assert FieldConstraintType.NOT_NULL not in _constraints(spec, "m")

    def test_typed_enum_with_null_member_stays_not_null(self) -> None:
        """
        The other direction. Here ``type: string`` rules the null member
        out, so the field is not nullable and NOT_NULL is correct.
        """
        spec = _extract(
            {
                "type": "object",
                "properties": {"m": {"type": "string", "enum": ["a", "b", None]}},
            }
        )
        assert _constraints(spec, "m")[FieldConstraintType.NOT_NULL] is True

    def test_untyped_enum_without_null_stays_not_null(self) -> None:
        spec = _extract({"type": "object", "properties": {"m": {"enum": ["a", "b"]}}})
        assert _constraints(spec, "m")[FieldConstraintType.NOT_NULL] is True


class TestRequiredWithoutAProperty:
    """
    ``required`` constrains presence; ``properties`` constrains value.
    Neither implies the other, and JSON Schema allows a name in the first
    without an entry in the second. Dropping those names meant a payload
    missing a required field was reported valid.
    """

    def test_required_name_without_a_schema_becomes_a_field(self) -> None:
        spec = _extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "required": ["a", "ghost"],
            }
        )
        ghost = _field(spec, "ghost")
        assert ghost.required is True
        assert ghost.field_type is FieldType.ANY
        assert ghost.default is MISSING

    def test_its_absence_is_now_reported(self) -> None:
        from stateguard.adapters.jsonschema import JSONSchemaAdapter

        schema = {
            "type": "object",
            "properties": {"a": {"type": "string"}},
            "required": ["a", "ghost"],
        }
        adapter = JSONSchemaAdapter()
        result = adapter.validate(adapter.extract_contract(schema), {"a": "x"})
        assert not result.is_valid
        assert any("ghost" in v.field_path for v in result.violations)

    def test_any_value_satisfies_it(self) -> None:
        """
        There is no subschema to type it from, so presence is enforced and
        the value is not constrained. That is exactly what the schema says.
        """
        from stateguard.adapters.jsonschema import JSONSchemaAdapter

        schema = {"type": "object", "properties": {}, "required": ["ghost"]}
        adapter = JSONSchemaAdapter()
        result = adapter.validate(adapter.extract_contract(schema), {"ghost": 42})
        assert result.is_valid

    def test_a_dotted_required_name_is_still_refused(self) -> None:
        """These go through the same path-addressability check as any other."""
        with pytest.raises(UnsupportedSchemaError, match="dot-notation"):
            _extract({"type": "object", "properties": {}, "required": ["a.b"]})


class TestAdditionalProperties:
    def test_false_is_strict(self) -> None:
        spec = _extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": False,
            }
        )
        assert spec.strict_mode is True

    def test_true_is_open(self) -> None:
        spec = _extract(
            {
                "type": "object",
                "properties": {"a": {"type": "string"}},
                "additionalProperties": True,
            }
        )
        assert spec.strict_mode is False

    def test_absent_is_open(self) -> None:
        spec = _extract({"type": "object", "properties": {"a": {"type": "string"}}})
        assert spec.strict_mode is False

    def test_schema_object_warns_rather_than_passing_silently(self) -> None:
        """
        ``{"additionalProperties": {"type": "string"}}`` types the properties
        it has no name for. ``ContractSpec`` cannot express that, so it is
        validated more loosely than the schema asks -- the same situation
        ``exclusiveMinimum`` is in, and that one has always warned.
        """
        with pytest.warns(SchemaFeatureWarning, match="undeclared properties"):
            spec = _extract(
                {
                    "type": "object",
                    "properties": {"a": {"type": "string"}},
                    "additionalProperties": {"type": "string"},
                }
            )
        assert spec.strict_mode is False

    def test_nonsense_value_is_refused(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="additionalProperties"):
            _extract({"type": "object", "properties": {}, "additionalProperties": "yes"})

    def test_strict_false_does_not_warn(self) -> None:
        with warnings.catch_warnings():
            warnings.simplefilter("error", SchemaFeatureWarning)
            _extract({"type": "object", "properties": {}, "additionalProperties": False})

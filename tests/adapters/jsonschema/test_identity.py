"""
Schema identity -- document and contract fingerprints.

The contract fingerprint must be *insensitive* to what does not change the
contract (key order, annotations, meaningless list order, $ref layout,
equivalent spellings) and *sensitive* to everything that does. Both halves
are tested, because a fingerprint that never changes is as broken as one
that always does.
"""

from __future__ import annotations

import copy
import json
import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stateguard.adapters.jsonschema import (
    MalformedReferenceError,
    MalformedSchemaError,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.identity import (
    document_fingerprint,
    fingerprint_schema,
    normalize_schema,
)

BASE: dict[str, Any] = {
    "type": "object",
    "title": "Search",
    "description": "Search the index.",
    "properties": {
        "query": {"type": "string", "minLength": 1, "description": "What to look for"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 50, "default": 10},
        "sort": {"type": "string", "enum": ["relevance", "date", "title"]},
        "filters": {
            "type": "object",
            "properties": {"lang": {"type": "string"}, "after": {"type": "string"}},
            "required": ["lang"],
        },
        "tags": {"type": "array", "items": {"type": "string"}, "maxItems": 5},
    },
    "required": ["query", "limit"],
    "additionalProperties": False,
}


def _fp(schema: dict[str, Any]) -> Any:
    return fingerprint_schema(schema)


def _with(change: Any) -> dict[str, Any]:
    schema = copy.deepcopy(BASE)
    change(schema)
    return schema


def _reordered(value: Any, rng: random.Random) -> Any:
    """Every object's keys in a random order; lists untouched."""
    if isinstance(value, dict):
        items = list(value.items())
        rng.shuffle(items)
        return {key: _reordered(item, rng) for key, item in items}
    if isinstance(value, list):
        return [_reordered(item, rng) for item in value]
    return value


# ===========================================================================
# Insensitive to what does not change the contract
# ===========================================================================


class TestSameContract:
    def test_identical_schemas(self) -> None:
        assert _fp(BASE) == _fp(copy.deepcopy(BASE))

    @pytest.mark.parametrize("seed", range(10))
    def test_object_key_order(self, seed: int) -> None:
        shuffled = _reordered(BASE, random.Random(seed))
        assert list(shuffled) != list(BASE) or seed  # the shuffle did something somewhere
        assert _fp(shuffled) == _fp(BASE)  # both fingerprints

    @pytest.mark.parametrize(
        ("label", "change"),
        [
            ("required order", lambda s: s.__setitem__("required", ["limit", "query"])),
            (
                "required duplicate",
                lambda s: s.__setitem__("required", ["query", "limit", "query"]),
            ),
            (
                "enum order",
                lambda s: s["properties"]["sort"].__setitem__(
                    "enum", ["title", "date", "relevance"]
                ),
            ),
            ("description", lambda s: s.__setitem__("description", "Find things.")),
            ("title", lambda s: s["properties"]["query"].__setitem__("title", "Query")),
            ("examples", lambda s: s["properties"]["query"].__setitem__("examples", ["cats"])),
            ("$comment", lambda s: s.__setitem__("$comment", "v2")),
            ("deprecated", lambda s: s["properties"]["sort"].__setitem__("deprecated", True)),
            ("x- extension", lambda s: s.__setitem__("x-owner", "search-team")),
            (
                "$schema",
                lambda s: s.__setitem__("$schema", "https://json-schema.org/draft/2020-12/schema"),
            ),
            (
                "integral float bound",
                lambda s: s["properties"]["limit"].__setitem__("maximum", 50.0),
            ),
            (
                "type as one-element list",
                lambda s: s["properties"]["query"].__setitem__("type", ["string"]),
            ),
            (
                "explicit empty required",
                lambda s: s["properties"]["tags"].__setitem__("required", []),
            ),
        ],
    )
    def test_contract_unchanged_document_changed(self, label: str, change: Any) -> None:
        before, after = _fp(BASE), _fp(_with(change))
        assert after.contract == before.contract, label
        assert after.document != before.document, label

    def test_moving_a_definition_behind_a_ref(self) -> None:
        refd = copy.deepcopy(BASE)
        refd["$defs"] = {"Filters": refd["properties"]["filters"]}
        refd["properties"]["filters"] = {"$ref": "#/$defs/Filters"}
        assert _fp(refd).contract == _fp(BASE).contract

    def test_additional_properties_true_is_the_default(self) -> None:
        open_schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        explicit = {**open_schema, "additionalProperties": True}
        assert _fp(open_schema).contract == _fp(explicit).contract

    def test_draft4_inclusive_flag_is_the_default(self) -> None:
        bound = {"type": "object", "properties": {"n": {"type": "integer", "minimum": 1}}}
        explicit = copy.deepcopy(bound)
        explicit["properties"]["n"]["exclusiveMinimum"] = False
        assert _fp(bound).contract == _fp(explicit).contract
        explicit["properties"]["n"]["exclusiveMinimum"] = True
        assert _fp(bound).contract != _fp(explicit).contract


# ===========================================================================
# Sensitive to everything that does
# ===========================================================================


class TestDifferentContract:
    @pytest.mark.parametrize(
        ("label", "change"),
        [
            ("type", lambda s: s["properties"]["limit"].__setitem__("type", "number")),
            ("required added", lambda s: s["required"].append("sort")),
            ("required removed", lambda s: s["required"].remove("limit")),
            ("field added", lambda s: s["properties"].__setitem__("page", {"type": "integer"})),
            ("field removed", lambda s: s["properties"].pop("sort")),
            ("enum member added", lambda s: s["properties"]["sort"]["enum"].append("popularity")),
            ("enum member removed", lambda s: s["properties"]["sort"]["enum"].remove("date")),
            ("additionalProperties opened", lambda s: s.pop("additionalProperties")),
            (
                "nested field type",
                lambda s: s["properties"]["filters"]["properties"]["lang"].__setitem__(
                    "type", "integer"
                ),
            ),
            ("nested required", lambda s: s["properties"]["filters"]["required"].append("after")),
            (
                "array item type",
                lambda s: s["properties"]["tags"]["items"].__setitem__("type", "integer"),
            ),
            ("default changed", lambda s: s["properties"]["limit"].__setitem__("default", 20)),
            ("default removed", lambda s: s["properties"]["limit"].pop("default")),
            ("minimum changed", lambda s: s["properties"]["limit"].__setitem__("minimum", 0)),
            ("maxItems changed", lambda s: s["properties"]["tags"].__setitem__("maxItems", 6)),
            (
                "pattern added",
                lambda s: s["properties"]["query"].__setitem__("pattern", "^[a-z]+$"),
            ),
            ("format added", lambda s: s["properties"]["query"].__setitem__("format", "uri")),
            ("non-integral bound", lambda s: s["properties"]["limit"].__setitem__("maximum", 50.5)),
        ],
    )
    def test_contract_changed(self, label: str, change: Any) -> None:
        assert _fp(_with(change)).contract != _fp(BASE).contract, label

    def test_a_default_of_one_differs_from_true(self) -> None:
        """``True == 1`` in Python; JSON -- and the contract -- disagree."""
        a = {"type": "object", "properties": {"x": {"default": 1}}}
        b = {"type": "object", "properties": {"x": {"default": True}}}
        assert _fp(a).contract != _fp(b).contract

    def test_tuple_items_order_matters(self) -> None:
        a = {
            "type": "object",
            "properties": {"p": {"items": [{"type": "string"}, {"type": "integer"}]}},
        }
        b = {
            "type": "object",
            "properties": {"p": {"items": [{"type": "integer"}, {"type": "string"}]}},
        }
        assert _fp(a).contract != _fp(b).contract

    def test_different_tools_with_identical_schemas_share_a_fingerprint(self) -> None:
        """Identity of a *schema*; tool identity is the registry's business."""
        assert _fp(BASE) == _fp(copy.deepcopy(BASE))


# ===========================================================================
# Normal form
# ===========================================================================


class TestNormalForm:
    def test_annotations_are_gone_and_refs_inlined(self) -> None:
        refd = copy.deepcopy(BASE)
        refd["$defs"] = {"Filters": refd["properties"]["filters"]}
        refd["properties"]["filters"] = {"$ref": "#/$defs/Filters", "description": "x"}
        normal = normalize_schema(refd)

        def keys(node: Any) -> set[str]:
            if isinstance(node, dict):
                return set(node) | {k for value in node.values() for k in keys(value)}
            if isinstance(node, list):
                return {k for item in node for k in keys(item)}
            return set()

        # Keys, not text: "title" is also one of this fixture's enum *values*.
        assert not keys(normal) & {"$ref", "$defs", "description", "title"}
        assert normal["properties"]["filters"]["required"] == ["lang"]

    def test_it_is_idempotent(self) -> None:
        once = normalize_schema(BASE)
        assert normalize_schema(once) == once

    def test_branches_are_order_free(self) -> None:
        a = {
            "type": "object",
            "properties": {"v": {"anyOf": [{"type": "string"}, {"type": "null"}]}},
        }
        b = {
            "type": "object",
            "properties": {"v": {"anyOf": [{"type": "null"}, {"type": "string"}]}},
        }
        assert _fp(a).contract == _fp(b).contract

    def test_unsupported_keywords_are_kept_not_refused(self) -> None:
        """Comparison must work on schemas extraction refuses."""
        a = {"type": "object", "allOf": [{"required": ["a"]}]}
        b = {"type": "object", "allOf": [{"required": ["b"]}]}
        assert _fp(a).contract != _fp(b).contract


# ===========================================================================
# Untrusted input
# ===========================================================================


class TestHostileInput:
    def test_not_json(self) -> None:
        with pytest.raises(MalformedSchemaError):
            document_fingerprint({"type": "object", "default": float("nan")})
        with pytest.raises(MalformedSchemaError):
            document_fingerprint({1: "a", "b": 2})
        with pytest.raises(MalformedSchemaError):
            fingerprint_schema(["not", "a", "schema"])  # type: ignore[arg-type]

    def test_deep_nesting_is_refused_not_crashed(self) -> None:
        root: dict[str, Any] = {"type": "object", "properties": {}}
        current = root
        for _ in range(5000):
            child: dict[str, Any] = {"type": "object", "properties": {}}
            current["properties"]["x"] = child
            current = child
        with pytest.raises((SchemaReferenceError, UnsupportedSchemaError)):
            fingerprint_schema(root)

    def test_deep_non_schema_value_is_refused_not_crashed(self) -> None:
        value: Any = 0
        for _ in range(100_000):
            value = [value]
        with pytest.raises(UnsupportedSchemaError):
            fingerprint_schema({"type": "object", "properties": {"x": {"default": value}}})

    def test_exponential_refs_are_refused(self) -> None:
        defs = {
            f"D{i}": {
                "type": "object",
                "properties": {
                    "a": {"$ref": f"#/$defs/D{i + 1}"},
                    "b": {"$ref": f"#/$defs/D{i + 1}"},
                },
            }
            for i in range(40)
        }
        defs["D40"] = {"type": "string"}
        with pytest.raises(SchemaReferenceError, match="expands"):
            fingerprint_schema(
                {"type": "object", "properties": {"r": {"$ref": "#/$defs/D0"}}, "$defs": defs}
            )

    def test_dangling_ref(self) -> None:
        with pytest.raises(MalformedReferenceError):
            fingerprint_schema({"type": "object", "properties": {"a": {"$ref": "#/$defs/Nope"}}})

    def test_huge_property_count_is_linear_and_stable(self) -> None:
        import time

        schema = {
            "type": "object",
            "properties": {f"p{i}": {"type": "string"} for i in range(5000)},
        }
        start = time.perf_counter()
        first = fingerprint_schema(schema)
        assert time.perf_counter() - start < 2.0
        assert fingerprint_schema(json.loads(json.dumps(schema))) == first


# ===========================================================================
# Property: key order never matters
# ===========================================================================

_leaf = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(-5, 5),
    st.text(max_size=3),
    st.floats(allow_nan=False, allow_infinity=False),
)
_json = st.recursive(
    _leaf,
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=4), children, max_size=4)
    ),
    max_leaves=12,
)
_schema = st.fixed_dictionaries(
    {"type": st.just("object")},
    optional={
        "properties": st.dictionaries(
            st.text("abcxyz", min_size=1, max_size=4),
            st.fixed_dictionaries(
                {},
                optional={
                    "type": st.sampled_from(["string", "integer", ["string", "null"]]),
                    "default": _json,
                    "enum": st.lists(_leaf, min_size=1, max_size=4),
                    "description": st.text(max_size=5),
                    "minimum": st.integers(0, 3),
                },
            ),
            max_size=5,
        ),
        "required": st.lists(st.text("abcxyz", min_size=1, max_size=4), max_size=3),
        "x-meta": _json,
    },
)


@settings(max_examples=200, deadline=None)
@given(schema=_schema, seed=st.integers(0, 2**16))
def test_property_key_order_never_changes_either_fingerprint(
    schema: dict[str, Any], seed: int
) -> None:
    shuffled = _reordered(schema, random.Random(seed))
    assert fingerprint_schema(shuffled) == fingerprint_schema(schema)


@settings(max_examples=200, deadline=None)
@given(schema=_schema)
def test_property_normal_form_is_a_fixed_point(schema: dict[str, Any]) -> None:
    once = normalize_schema(schema)
    assert normalize_schema(once) == once

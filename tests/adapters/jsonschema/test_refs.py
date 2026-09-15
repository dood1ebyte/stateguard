"""
``$ref`` resolution and cycle detection.

The headline guarantee is that reference resolution **terminates**.  A schema
arrives from an MCP server as untrusted data and may legally describe a
recursive structure, which has no finite inline expansion; an unguarded
resolver does not return a wrong answer there, it hangs the process.

Two cycle shapes are tested separately because they are caught by different
mechanisms: a ``$ref``-to-``$ref`` chain spins inside a single resolution,
while structural recursion resolves fine one hop at a time and only diverges
when the *extractor* descends.  See ``refs.py`` for why that forces the
context-manager API.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.adapters.jsonschema import (
    RefResolver,
    SchemaReferenceError,
    UnsupportedSchemaError,
)


def _walk(resolver: RefResolver, schema: dict[str, Any]) -> None:
    """
    Descend a schema the way the extractor will.

    Follows ``properties`` and ``anyOf``/``oneOf`` branches, which is the
    minimum needed to reach a recursive ``$ref`` -- Pydantic emits an
    optional self-reference as ``anyOf: [{$ref}, {type: null}]``, so a walk
    that only followed ``properties`` would miss the cycle entirely and
    report a false pass.
    """
    with resolver.resolved(schema) as target:
        for prop in (target.get("properties") or {}).values():
            if not isinstance(prop, dict):
                continue
            for branch in prop.get("anyOf") or prop.get("oneOf") or [prop]:
                _walk(resolver, branch)


# ===========================================================================
# Resolution
# ===========================================================================


class TestResolution:
    def test_schema_without_ref_is_yielded_unchanged(self) -> None:
        schema = {"type": "object", "properties": {"a": {"type": "string"}}}
        with RefResolver(schema).resolved(schema) as target:
            assert target is schema

    def test_local_defs_pointer_resolves(self) -> None:
        root = {"$defs": {"Address": {"type": "object"}}}
        with RefResolver(root).resolved({"$ref": "#/$defs/Address"}) as target:
            assert target == {"type": "object"}

    def test_legacy_definitions_pointer_resolves(self) -> None:
        """Draft-07 servers emit ``definitions`` rather than ``$defs``."""
        root = {"definitions": {"Address": {"type": "object"}}}
        with RefResolver(root).resolved({"$ref": "#/definitions/Address"}) as target:
            assert target == {"type": "object"}

    def test_whole_document_reference_resolves_to_root(self) -> None:
        root = {"type": "object", "properties": {}}
        with RefResolver(root).resolved({"$ref": "#"}) as target:
            assert target is root

    def test_chained_refs_are_followed_to_the_end(self) -> None:
        root = {"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"type": "integer"}}}
        with RefResolver(root).resolved({"$ref": "#/$defs/A"}) as target:
            assert target == {"type": "integer"}

    def test_array_index_pointer_resolves(self) -> None:
        root = {"prefixItems": [{"type": "string"}, {"type": "integer"}]}
        with RefResolver(root).resolved({"$ref": "#/prefixItems/1"}) as target:
            assert target == {"type": "integer"}

    @pytest.mark.parametrize(
        ("token", "key"),
        [
            ("a~1b", "a/b"),
            ("a~0b", "a~b"),
            # The order-sensitive one: unescaping '~0' first would turn this
            # into 'a/b' rather than the literal 'a~1b' its author wrote.
            ("a~01b", "a~1b"),
        ],
        ids=["slash", "tilde", "tilde-then-one"],
    )
    def test_rfc6901_escapes_are_unescaped_in_the_right_order(self, token: str, key: str) -> None:
        """RFC 6901: ``~1`` becomes ``/`` first, then ``~0`` becomes ``~``."""
        root = {"$defs": {key: {"type": "boolean"}}}
        with RefResolver(root).resolved({"$ref": f"#/$defs/{token}"}) as target:
            assert target == {"type": "boolean"}


# ===========================================================================
# Sibling merging
# ===========================================================================


class TestSiblingKeywords:
    def test_siblings_are_merged_over_the_target(self) -> None:
        root = {"$defs": {"A": {"type": "integer"}}}
        ref = {"$ref": "#/$defs/A", "default": 3, "description": "how many"}
        with RefResolver(root).resolved(ref) as target:
            assert target == {"type": "integer", "default": 3, "description": "how many"}

    def test_sibling_wins_over_target_on_conflict(self) -> None:
        root = {"$defs": {"A": {"type": "integer", "default": 1}}}
        with RefResolver(root).resolved({"$ref": "#/$defs/A", "default": 9}) as target:
            assert target["default"] == 9

    def test_target_is_not_mutated_by_merging(self) -> None:
        """The merge must not write the sibling back into the shared ``$defs``."""
        root = {"$defs": {"A": {"type": "integer"}}}
        resolver = RefResolver(root)
        with resolver.resolved({"$ref": "#/$defs/A", "default": 9}):
            pass
        assert root["$defs"]["A"] == {"type": "integer"}


# ===========================================================================
# Cycles
# ===========================================================================


class TestCycles:
    def test_direct_self_reference_chain_raises(self) -> None:
        root = {"$defs": {"A": {"$ref": "#/$defs/A"}}}
        with (
            pytest.raises(SchemaReferenceError, match="Circular"),
            RefResolver(root).resolved({"$ref": "#/$defs/A"}),
        ):
            pass

    def test_mutual_reference_chain_raises(self) -> None:
        root = {"$defs": {"A": {"$ref": "#/$defs/B"}, "B": {"$ref": "#/$defs/A"}}}
        with (
            pytest.raises(SchemaReferenceError, match="Circular"),
            RefResolver(root).resolved({"$ref": "#/$defs/A"}),
        ):
            pass

    def test_structural_recursion_raises_on_descent(self) -> None:
        """
        Each hop resolves fine; it is the descent that diverges.  This is the
        shape Pydantic emits for ``child: Optional['Node']``.
        """
        root = {
            "$defs": {
                "Node": {
                    "type": "object",
                    "properties": {"child": {"$ref": "#/$defs/Node"}},
                }
            },
            "$ref": "#/$defs/Node",
        }
        with pytest.raises(SchemaReferenceError, match="Recursive schema"):
            _walk(RefResolver(root), root)

    def test_recursion_error_names_the_expansion_chain(self) -> None:
        root = {
            "$defs": {
                "A": {"type": "object", "properties": {"b": {"$ref": "#/$defs/B"}}},
                "B": {"type": "object", "properties": {"a": {"$ref": "#/$defs/A"}}},
            },
            "$ref": "#/$defs/A",
        }
        with pytest.raises(SchemaReferenceError) as exc:
            _walk(RefResolver(root), root)
        assert "#/$defs/A -> #/$defs/B -> #/$defs/A" in str(exc.value)

    def test_same_definition_twice_as_siblings_is_not_a_cycle(self) -> None:
        """
        Two fields of the same type is the common case and must not be
        mistaken for recursion -- the guard pops on exit, not at the end.
        """
        root = {
            "$defs": {"Address": {"type": "object", "properties": {}}},
            "type": "object",
            "properties": {
                "home": {"$ref": "#/$defs/Address"},
                "work": {"$ref": "#/$defs/Address"},
            },
        }
        _walk(RefResolver(root), root)  # must not raise

    def test_same_definition_nested_twice_in_sequence_is_not_a_cycle(self) -> None:
        root = {
            "$defs": {"Leaf": {"type": "object", "properties": {}}},
            "type": "object",
            "properties": {
                "outer": {
                    "type": "object",
                    "properties": {"inner": {"$ref": "#/$defs/Leaf"}},
                },
                "other": {"$ref": "#/$defs/Leaf"},
            },
        }
        _walk(RefResolver(root), root)  # must not raise


# ===========================================================================
# Refusals
# ===========================================================================


class TestRefusals:
    @pytest.mark.parametrize(
        "ref",
        [
            "http://example.com/schema.json",
            "https://example.com/schema.json#/$defs/A",
            "file:///etc/passwd",
            "other.json#/$defs/A",
        ],
        ids=["http", "https", "file", "relative-file"],
    )
    def test_remote_reference_is_refused_not_fetched(self, ref: str) -> None:
        """
        Following one of these would make extraction issue a request to a URL
        chosen by whoever supplied the schema.
        """
        with (
            pytest.raises(SchemaReferenceError, match="same-document"),
            RefResolver({}).resolved({"$ref": ref}),
        ):
            pass

    def test_anchor_fragment_is_refused_clearly(self) -> None:
        with (
            pytest.raises(SchemaReferenceError, match=r"\$anchor"),
            RefResolver({}).resolved({"$ref": "#Address"}),
        ):
            pass

    def test_unresolvable_pointer_names_the_missing_token(self) -> None:
        root = {"$defs": {"A": {"type": "integer"}}}
        with (
            pytest.raises(SchemaReferenceError, match="no 'B'"),
            RefResolver(root).resolved({"$ref": "#/$defs/B"}),
        ):
            pass

    def test_non_string_ref_is_refused(self) -> None:
        with (
            pytest.raises(SchemaReferenceError, match="must be a string"),
            RefResolver({}).resolved({"$ref": ["#/$defs/A"]}),
        ):
            pass

    def test_pointer_into_a_scalar_is_refused(self) -> None:
        root = {"$defs": {"A": 5}}
        with (
            pytest.raises(SchemaReferenceError, match="cannot descend"),
            RefResolver(root).resolved({"$ref": "#/$defs/A/deeper"}),
        ):
            pass

    def test_reference_to_a_non_object_is_refused(self) -> None:
        """A ``$ref`` must land on a schema object, not a bare value."""
        root = {"$defs": {"A": 5}}
        with (
            pytest.raises(SchemaReferenceError, match="not a schema object"),
            RefResolver(root).resolved({"$ref": "#/$defs/A"}),
        ):
            pass

    def test_non_numeric_array_index_is_refused(self) -> None:
        root = {"prefixItems": [{"type": "string"}]}
        with (
            pytest.raises(SchemaReferenceError, match="not a valid index"),
            RefResolver(root).resolved({"$ref": "#/prefixItems/abc"}),
        ):
            pass

    def test_array_index_out_of_range_is_refused(self) -> None:
        root = {"prefixItems": [{"type": "string"}]}
        with (
            pytest.raises(SchemaReferenceError, match="out of range"),
            RefResolver(root).resolved({"$ref": "#/prefixItems/7"}),
        ):
            pass

    def test_boolean_schema_is_refused_clearly(self) -> None:
        with (
            pytest.raises(UnsupportedSchemaError, match="Boolean schemas"),
            RefResolver({}).resolved(True),  # type: ignore[arg-type]
        ):
            pass

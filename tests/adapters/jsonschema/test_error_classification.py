"""
Malformed versus unsupported -- the two ways a schema can be unreadable.

``MalformedSchemaError`` means the document is not valid JSON Schema (the
author's error; no validator would accept it). Plain
``UnsupportedSchemaError`` means valid JSON Schema outside StateGuard's
subset (StateGuard's limitation). Both stay catchable as
``UnsupportedSchemaError`` / ``JSONSchemaError``, so existing callers see no
change; schema-change classification uses the distinction.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.adapters.jsonschema import (
    JSONSchemaAdapter,
    JSONSchemaError,
    MalformedReferenceError,
    MalformedSchemaError,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.adapters.mcp import InvalidToolDefinitionError, MCPToolAdapter


def _obj(**properties: Any) -> dict[str, Any]:
    return {"type": "object", "properties": properties}


MALFORMED: list[tuple[str, Any]] = [
    ("not an object", ["type", "object"]),
    ("properties not an object", {"type": "object", "properties": []}),
    ("required not a list", {"type": "object", "required": "a"}),
    ("required holds a non-string", {"type": "object", "required": [1]}),
    ("additionalProperties not bool/object", {"type": "object", "additionalProperties": 3}),
    ("unknown type name", _obj(a={"type": "strng"})),
    ("type not a string or list", _obj(a={"type": 5})),
    ("enum not a list", _obj(a={"enum": "abc"})),
    ("anyOf not a list", _obj(a={"anyOf": {"type": "string"}})),
    ("anyOf empty", _obj(a={"anyOf": []})),
    ("pattern not a string", _obj(a={"type": "string", "pattern": 5})),
    ("$ref not a string", _obj(a={"$ref": 5})),
    ("$ref resolves to nothing", _obj(a={"$ref": "#/$defs/Missing"})),
]

UNSUPPORTED: list[tuple[str, Any]] = [
    ("allOf", {"type": "object", "allOf": [{}]}),
    ("not", _obj(a={"not": {"type": "string"}})),
    ("boolean subschema false", _obj(a=False)),
    ("dotted property name", _obj(**{"a.b": {"type": "string"}})),
    ("remote $ref", _obj(a={"$ref": "https://example.test/s.json"})),
    (
        "recursive",
        {
            "type": "object",
            "properties": {"n": {"$ref": "#/$defs/N"}},
            "$defs": {"N": {"type": "object", "properties": {"c": {"$ref": "#/$defs/N"}}}},
        },
    ),
    ("non-object root", {"type": "string"}),
    ("unrecognised keyword", _obj(a={"type": "integer", "divisibleBy": 3})),
]


@pytest.mark.parametrize(("label", "schema"), MALFORMED, ids=[m[0] for m in MALFORMED])
def test_malformed_schemas_say_so(label: str, schema: Any) -> None:
    with pytest.raises(MalformedSchemaError):
        JSONSchemaAdapter().extract_contract(schema)


@pytest.mark.parametrize(("label", "schema"), UNSUPPORTED, ids=[u[0] for u in UNSUPPORTED])
def test_unsupported_schemas_are_not_called_malformed(label: str, schema: Any) -> None:
    with pytest.raises(JSONSchemaError) as info:
        JSONSchemaAdapter().extract_contract(schema)
    assert not isinstance(info.value, MalformedSchemaError)


def test_an_unresolvable_reference_is_both_kinds() -> None:
    with pytest.raises(MalformedReferenceError) as info:
        JSONSchemaAdapter().extract_contract(_obj(a={"$ref": "#/$defs/Missing"}))
    assert isinstance(info.value, SchemaReferenceError)
    assert isinstance(info.value, UnsupportedSchemaError)


@pytest.mark.parametrize(
    "tool",
    [
        {"name": "t"},
        {"name": "t", "inputSchema": []},
        {"name": "t", "inputSchema": {"type": "object"}, "input_schema": {"type": "string"}},
        "not a tool",
    ],
    ids=["no inputSchema", "inputSchema not object", "spellings disagree", "not an object"],
)
def test_a_malformed_tool_definition_is_an_invalid_tool_definition(tool: Any) -> None:
    with pytest.raises(InvalidToolDefinitionError) as info:
        MCPToolAdapter().extract_contract(tool)
    # Still what it was before the class existed.
    assert isinstance(info.value, UnsupportedSchemaError)
    assert isinstance(info.value, MalformedSchemaError)

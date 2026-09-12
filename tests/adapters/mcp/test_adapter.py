"""
``MCPToolAdapter`` -- tool definition in, ``ContractSpec`` out.

The adapter is deliberately thin: ``inputSchema`` is JSON Schema, so
everything about *reading* a schema is tested in
``tests/adapters/jsonschema/``. What is tested here is the part that is
actually MCP's -- finding the schema inside a tool definition, and not
re-walking it on every call.
"""

from __future__ import annotations

from typing import Any

import pytest

from stateguard.adapters.jsonschema.errors import UnsupportedSchemaError
from stateguard.adapters.mcp import MCPToolAdapter, split_tool_definition
from stateguard.core.models.field_types import FieldType

TOOL: dict[str, Any] = {
    "name": "get_forecast",
    "description": "Get a weather forecast",
    "inputSchema": {
        "type": "object",
        "properties": {
            "location": {"type": "string"},
            "days": {"type": "integer", "minimum": 1, "maximum": 14},
        },
        "required": ["location", "days"],
    },
}


# ===========================================================================
# Finding the schema
# ===========================================================================


class TestSplitToolDefinition:
    def test_full_tool_definition_yields_name_and_schema(self) -> None:
        name, schema = split_tool_definition(TOOL)
        assert name == "get_forecast"
        assert schema == TOOL["inputSchema"]

    def test_bare_input_schema_yields_no_name(self) -> None:
        """
        Both shapes are things a caller reasonably holds: a proxy that
        cached ``tools/list`` has whole definitions, someone testing one
        tool has just its schema.
        """
        name, schema = split_tool_definition(TOOL["inputSchema"])
        assert name is None
        assert schema == TOOL["inputSchema"]

    def test_the_two_are_distinguished_by_a_non_schema_keyword(self) -> None:
        """
        ``inputSchema`` is not a JSON Schema keyword, so a document cannot
        legitimately be both shapes at once.
        """
        assert "inputSchema" not in TOOL["inputSchema"]

    def test_unnamed_tool_is_accepted(self) -> None:
        name, _ = split_tool_definition({"inputSchema": {"type": "object", "properties": {}}})
        assert name is None

    def test_non_mapping_is_refused(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="got list"):
            split_tool_definition([])

    def test_non_mapping_input_schema_is_refused_by_tool_name(self) -> None:
        with pytest.raises(UnsupportedSchemaError, match="get_forecast"):
            split_tool_definition({"name": "get_forecast", "inputSchema": "nope"})

    def test_snake_case_key_is_accepted(self) -> None:
        """
        ``inputSchema`` is the wire spelling; ``input_schema`` is the same
        field as the Python SDK names it. On the SDK's ``Tool`` model the
        camelCase form is a Pydantic alias, so ``tool.model_dump()`` -- an
        entirely ordinary thing to write -- emits snake_case. Refusing it
        would reject a correct caller.
        """
        name, schema = split_tool_definition(
            {"name": "t", "input_schema": {"type": "object", "properties": {}}}
        )
        assert name == "t"
        assert schema == {"type": "object", "properties": {}}

    def test_both_spellings_agreeing_is_fine(self) -> None:
        inner = {"type": "object", "properties": {}}
        _, schema = split_tool_definition(
            {"name": "t", "inputSchema": inner, "input_schema": inner}
        )
        assert schema == inner

    def test_both_spellings_disagreeing_is_refused(self) -> None:
        """Two spellings of one field cannot hold two different values."""
        with pytest.raises(UnsupportedSchemaError, match="they differ"):
            split_tool_definition(
                {
                    "name": "t",
                    "inputSchema": {"type": "object", "properties": {"a": {}}},
                    "input_schema": {"type": "object", "properties": {"b": {}}},
                }
            )

    def test_the_sdk_tool_model_is_why_both_are_accepted(self) -> None:
        """
        Pins the premise rather than trusting the docstring: if a future SDK
        stops aliasing the field, this fails and the reasoning gets revisited.
        """
        mcp = pytest.importorskip("mcp")
        assert mcp.Tool.model_fields["input_schema"].alias == "inputSchema"

        tool = mcp.Tool(name="t", inputSchema={"type": "object", "properties": {}})
        assert "inputSchema" in tool.model_dump(by_alias=True)
        assert "input_schema" in tool.model_dump()

        for dumped in (tool.model_dump(by_alias=True), tool.model_dump()):
            name, schema = split_tool_definition(dumped)
            assert name == "t"
            assert schema == {"type": "object", "properties": {}}


# ===========================================================================
# Extraction
# ===========================================================================


class TestExtraction:
    def test_extracts_the_same_contract_from_either_shape(self) -> None:
        adapter = MCPToolAdapter()
        from_tool = adapter.extract_contract(TOOL)
        from_schema = adapter.extract_contract(TOOL["inputSchema"])
        assert from_tool.contract_id == from_schema.contract_id

    def test_fields_come_through(self) -> None:
        spec = MCPToolAdapter().extract_contract(TOOL)
        by_path = {f.path: f for f in spec.fields}
        assert by_path["location"].field_type is FieldType.STRING
        assert by_path["days"].field_type is FieldType.INTEGER
        assert by_path["days"].required is True

    def test_source_ref_is_the_input_schema_not_the_tool(self) -> None:
        """
        ``source_ref`` is what the contract was extracted *from*, and the
        extractor read the ``inputSchema``. Recording the whole tool
        definition would describe something that was never walked.
        """
        spec = MCPToolAdapter().extract_contract(TOOL)
        assert spec.source_ref == TOOL["inputSchema"]

    def test_an_unsupported_schema_still_raises_through_the_wrapper(self) -> None:
        """The MCP layer must not soften the JSON Schema layer's refusals."""
        tool = {"name": "t", "inputSchema": {"type": "object", "properties": {"a": {"not": {}}}}}
        with pytest.raises(UnsupportedSchemaError, match="'not'"):
            MCPToolAdapter().extract_contract(tool)

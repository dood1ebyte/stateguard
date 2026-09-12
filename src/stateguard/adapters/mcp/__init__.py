"""
MCP adapter -- repair the arguments an agent sends to a tool.

An MCP tool declares its parameters in ``inputSchema``, and ``inputSchema``
is JSON Schema, so this package is deliberately thin: it knows where in a
tool definition to look, caches the extracted contract so a proxy is not
re-walking the same schema on every call, and maps the engine's answer onto
a decision a proxy can act on. Everything about reading the schema itself
belongs to ``stateguard.adapters.jsonschema``.

Zero external dependencies -- a tool definition arriving over the wire is
just a ``dict``. Only a runnable proxy needs the MCP SDK, and that goes
behind an extra.

See ``MCP_ADAPTER_PLAN.md`` for scope and
``docs/adr/0001-json-schema-source-of-truth.md`` for what a ``SUCCESS``
does and does not claim.
"""

from __future__ import annotations

from stateguard.adapters.mcp.adapter import MCPToolAdapter, split_tool_definition
from stateguard.adapters.mcp.cache import DEFAULT_CACHE_SIZE, SchemaCache
from stateguard.adapters.mcp.outcomes import MCPAction, ToolCallOutcome, outcome_for

__all__ = [
    "DEFAULT_CACHE_SIZE",
    "MCPAction",
    "MCPToolAdapter",
    "SchemaCache",
    "ToolCallOutcome",
    "outcome_for",
    "split_tool_definition",
]

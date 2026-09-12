"""
``MCPToolAdapter`` -- ``IContractAdapter`` over an MCP tool definition.

This is the thin part. An MCP tool declares its parameters in
``inputSchema``, and ``inputSchema`` *is* JSON Schema, so nearly all the work
belongs to ``JSONSchemaAdapter``; what is left is knowing where in a tool
definition to look, and not re-walking the same schema on every call.

Spec revision
-------------
Written against the MCP tool-definition shape in which a tool is
``{"name": str, "description": str, "inputSchema": <JSON Schema>}``.
``outputSchema`` / ``structuredContent`` -- repairing a tool's *results*
rather than its arguments -- are deliberately out of scope for v1
(``MCP_ADAPTER_PLAN.md`` §2): argument drift is where the model is, and the
model is what drifts.

Direction of repair
-------------------
This repairs the **arguments an agent sends to a tool**, against the
**server's declared schema**. The server is the authority; the model's
payload is what gets corrected. That asymmetry is the whole design -- see §2
of the plan for why the opposite direction was not built first.

Zero external dependencies. A tool definition arriving over the wire is just
a ``dict``, so nothing here needs the MCP SDK; only a runnable proxy does,
and that goes behind an extra.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from stateguard.adapters.jsonschema.adapter import JSONSchemaAdapter
from stateguard.adapters.jsonschema.errors import UnsupportedSchemaError
from stateguard.adapters.mcp.cache import DEFAULT_CACHE_SIZE, SchemaCache
from stateguard.core.errors.results import ValidationResult
from stateguard.core.interfaces.adapter import IContractAdapter
from stateguard.core.models.contract import ContractSpec

__all__ = ["MCPToolAdapter", "split_tool_definition"]


#: The key an MCP tool definition carries on the wire.
INPUT_SCHEMA_KEY = "inputSchema"

#: The same field as the Python SDK spells it. Both are accepted -- see
#: ``split_tool_definition``.
SNAKE_CASE_KEY = "input_schema"

#: Both spellings, in the order they are looked for.
_SCHEMA_KEYS = (INPUT_SCHEMA_KEY, SNAKE_CASE_KEY)


def split_tool_definition(
    schema: Any,
) -> tuple[str | None, Mapping[str, Any]]:
    """
    Separate a tool definition into ``(tool name, input schema)``.

    Accepts either a full tool definition or a bare input schema, because
    both are things a caller reasonably has in hand: a proxy that cached
    ``tools/list`` holds whole definitions, while someone testing one tool
    holds just its schema.

    Both spellings of the key
    -------------------------
    ``inputSchema`` is what MCP puts on the wire. ``input_schema`` is the
    same field as the Python SDK names it in Python -- on the SDK's ``Tool``
    model the camelCase form is a Pydantic *alias*, so ``tool.input_schema``
    is the attribute and ``tool.model_dump()`` emits the snake_case key
    unless the caller remembers ``by_alias=True``.

    Verified against the installed SDK rather than assumed::

        Tool.model_fields["input_schema"].alias        -> "inputSchema"
        Tool(...).model_dump(by_alias=True)            -> {"inputSchema": ...}
        Tool(...).model_dump()                         -> {"input_schema": ...}

    So both spellings reach this function through entirely ordinary code,
    and refusing either would reject a correct caller. If both are present
    and disagree, that is genuinely ambiguous and raises.

    The tool definition is told apart from a bare schema by these keys,
    neither of which is a JSON Schema keyword, so a document cannot be both.
    A bare schema yields a name of ``None``.
    """
    if not isinstance(schema, Mapping):
        raise UnsupportedSchemaError(
            f"MCPToolAdapter expects an MCP tool definition or a JSON Schema "
            f"object (a dict), got {type(schema).__name__}."
        )

    present = [key for key in _SCHEMA_KEYS if key in schema]

    if len(present) == 2 and schema[INPUT_SCHEMA_KEY] != schema[SNAKE_CASE_KEY]:
        raise UnsupportedSchemaError(
            f"Tool {_describe(schema)} carries both '{INPUT_SCHEMA_KEY}' and "
            f"'{SNAKE_CASE_KEY}', and they differ. They are two spellings of one "
            f"field, so there is no way to tell which the caller meant. Pass one."
        )

    if present:
        key = present[0]
        input_schema = schema[key]
        if not isinstance(input_schema, Mapping):
            raise UnsupportedSchemaError(
                f"Tool {_describe(schema)}: '{key}' must be a JSON Schema object, "
                f"got {type(input_schema).__name__}."
            )
        return _tool_name(schema), input_schema

    # A bare input schema. Anything wrong with it is the JSON Schema
    # adapter's to report, in its own vocabulary.
    return None, schema


def _tool_name(tool: Mapping[str, Any]) -> str | None:
    name = tool.get("name")
    return name if isinstance(name, str) and name else None


def _describe(tool: Mapping[str, Any]) -> str:
    name = _tool_name(tool)
    return repr(name) if name is not None else "<unnamed>"


class MCPToolAdapter(IContractAdapter):
    """
    Adapts an MCP tool definition to StateGuard's contract model.

    ``repair`` takes either shape::

        guard = ContractGuard.with_mcp()
        guard.repair(tool, arguments)                  # full definition
        guard.repair(tool["inputSchema"], arguments)   # bare schema

    Extracted contracts are cached (see ``cache``), which is what makes this
    usable in a proxy: ``tools/list`` is fetched once and every subsequent
    ``tools/call`` reuses the walk rather than repeating it per request.

    Because ``inputSchema`` is JSON Schema, everything ``JSONSchemaAdapter``
    documents applies unchanged -- including that a ``SUCCESS`` is not a
    claim of JSON Schema compliance, and that keywords outside the supported
    subset raise rather than being ignored. See
    ``docs/adr/0001-json-schema-source-of-truth.md``.
    """

    def __init__(
        self,
        jsonschema_adapter: JSONSchemaAdapter | None = None,
        cache_size: int = DEFAULT_CACHE_SIZE,
    ) -> None:
        self._inner = jsonschema_adapter if jsonschema_adapter is not None else JSONSchemaAdapter()
        self._cache = SchemaCache(cache_size)

    @property
    def cache(self) -> SchemaCache:
        """The contract cache, exposed so a proxy can clear it on reconnect."""
        return self._cache

    # ------------------------------------------------------------------
    # IContractAdapter
    # ------------------------------------------------------------------

    def extract_contract(self, schema: Any) -> ContractSpec:
        """
        Build a ``ContractSpec`` from a tool definition or a bare schema.

        Cached on ``(tool name, schema content)``. Including the content is
        what keeps the cache from becoming the very bug this adapter exists
        to catch: a tool that changes its schema keeps its name, so keying on
        the name alone would pin the first version seen forever.
        """
        tool_name, input_schema = split_tool_definition(schema)

        cached = self._cache.get(tool_name, input_schema)
        if cached is not None:
            return cached

        contract = self._inner.extract_contract(input_schema)
        self._cache.put(tool_name, input_schema, contract)
        return contract

    def validate(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> ValidationResult:
        """Delegate to the JSON Schema adapter -- the schema is JSON Schema."""
        return self._inner.validate(contract, data)

    def wrap(
        self,
        contract: ContractSpec,
        data: dict[str, Any],
    ) -> dict[str, Any]:
        """
        Return the arguments dict, with the schema's declared defaults filled.

        A plain ``dict`` is the framework-native type here: it is what goes
        into a ``tools/call`` request's ``arguments``.
        """
        return self._inner.wrap(contract, data)

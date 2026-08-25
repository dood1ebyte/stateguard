"""
JSON Schema adapter.

Translates the subset of JSON Schema that MCP tool definitions actually emit
into a ``ContractSpec`` (``MCP_ADAPTER_PLAN.md`` §5).  This is not a JSON
Schema implementation and does not aim to become one: keywords outside the
supported subset are rejected with a clear error rather than ignored, because
``ContractValidator`` is the source of truth for this adapter and there is no
second validator downstream to catch what gets dropped.  See
``docs/adr/0001-json-schema-source-of-truth.md``.

Zero external dependencies.
"""

from __future__ import annotations

from stateguard.adapters.jsonschema.adapter import JSONSchemaAdapter
from stateguard.adapters.jsonschema.errors import (
    JSONSchemaError,
    SchemaFeatureWarning,
    SchemaReferenceError,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.extractor import JSONSchemaExtractor
from stateguard.adapters.jsonschema.refs import RefResolver
from stateguard.adapters.jsonschema.type_mapper import JSONSchemaTypeMapper

__all__ = [
    "JSONSchemaAdapter",
    "JSONSchemaError",
    "JSONSchemaExtractor",
    "JSONSchemaTypeMapper",
    "RefResolver",
    "SchemaFeatureWarning",
    "SchemaReferenceError",
    "UnsupportedSchemaError",
]

"""
The supported-keyword screen -- one gate, applied at every descent.

``MCP_ADAPTER_PLAN.md`` §5's reject list is the adapter's contract with its
callers: a keyword this adapter cannot represent must raise, never be
skipped, because ``ContractValidator`` is the source of truth here
(``docs/adr/0001-json-schema-source-of-truth.md``) and nothing downstream
would catch what got dropped.

Why this is its own module
--------------------------
It used to be a private method on ``JSONSchemaExtractor``, and that placement
is what let the guarantee leak.  Two modules walk a schema document --
``extractor`` descends through ``properties``, ``type_mapper`` descends
through ``anyOf``/``oneOf`` branches and ``items`` -- and only the first one
could reach the check.  So::

    {"anyOf": [{"type": "string"}, {"type": "integer", "allOf": [...]}]}
    {"type": "array", "items": {"type": "object", "if": {...}}}

both extracted cleanly, with the rejected keyword silently ignored: exactly
the under-validation the reject list exists to prevent.  Moving the screen
here lets both walkers call the same gate, so "every subschema is screened"
is a property of the design rather than of remembering.

Screening is idempotent and cheap -- a few dict membership tests -- so a
subschema reached by two paths being screened twice costs nothing and is
much safer than reasoning about which path got there first.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)

__all__ = [
    "DROPPED_KEYWORDS",
    "IGNORED_KEYWORDS",
    "REJECTED_KEYWORDS",
    "screen_keywords",
    "warn_dropped_keywords",
]


#: Keywords refused outright. Each one changes what "valid" means in a way
#: the contract model cannot express, so honouring it is impossible and
#: ignoring it would under-validate silently.
REJECTED_KEYWORDS: dict[str, str] = {
    "allOf": "conjunction of subschemas has no single contract shape",
    "not": "negation cannot be expressed as a field contract",
    "if": "conditional subschemas have no static contract shape",
    "then": "conditional subschemas have no static contract shape",
    "else": "conditional subschemas have no static contract shape",
    "patternProperties": "property sets defined by regex are not addressable as paths",
    "dependentSchemas": "conditional requirements have no static contract shape",
    "dependentRequired": "conditional requirements have no static contract shape",
    "propertyNames": "constraints on key names are not expressible",
    "unevaluatedProperties": "depends on evaluation order this adapter does not model",
    "unevaluatedItems": "depends on evaluation order this adapter does not model",
}

#: Understood, not representable, dropped with a warning.
#: An exclusive bound is not an inclusive one -- rounding it would accept a
#: value the schema forbids -- and there is no ``FieldConstraintType`` for it.
DROPPED_KEYWORDS: dict[str, str] = {
    "exclusiveMinimum": "no exclusive-bound constraint type exists",
    "exclusiveMaximum": "no exclusive-bound constraint type exists",
}

#: Advisory or already consumed elsewhere. Listed explicitly so that an
#: unrecognised keyword is still noticed rather than silently tolerated.
IGNORED_KEYWORDS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "format",
        "readOnly",
        "title",
        "writeOnly",
        # Consumed by the extractor or the type mapper.
        "additionalProperties",
        "anyOf",
        "const",
        "default",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "type",
    }
)


def screen_keywords(schema: Mapping[str, Any], where: str) -> None:
    """
    Refuse §5's reject list, and any keyword not accounted for.

    *where* names the field being screened, for the error message;
    ``"<root>"`` marks the document root, which is the only place a
    top-level ``$id`` is permitted.

    Call this on every subschema **after** it has been ``$ref``-resolved.
    Screening the unresolved ``{"$ref": ...}`` wrapper is worse than not
    screening at all: it always passes, because a bare reference carries none
    of the keywords being looked for.
    """
    # ``$id`` on a *subschema* rebases reference resolution: inside it,
    # '#/$defs/A' means that subschema's '$defs', not the document's.
    # This adapter resolves every pointer against the document root, so
    # honouring the keyword is not possible without a URI-scoped resolver
    # -- and ignoring it silently resolves to the wrong target, which is
    # worse than refusing. Root-level '$id' is harmless and stays allowed:
    # it names the document without changing what '#' points at.
    if where != "<root>" and "$id" in schema:
        raise UnsupportedSchemaError(
            f"{where}: '$id' on a subschema is not supported. It establishes "
            f"a new base URI, so a '$ref' inside this subschema would resolve "
            f"against it rather than against the document root. This adapter "
            f"resolves against the root only, so honouring '$id' is not "
            f"possible and ignoring it would silently resolve to the wrong "
            f"schema. Inline the definition, or hoist it to the root."
        )

    for keyword, reason in REJECTED_KEYWORDS.items():
        if keyword in schema:
            raise UnsupportedSchemaError(
                f"{where}: '{keyword}' is not supported -- {reason}. "
                f"It is refused rather than ignored: skipping it would mean "
                f"reporting a payload valid that the schema forbids."
            )

    unknown = sorted(
        key
        for key in schema
        if key not in IGNORED_KEYWORDS
        and key not in DROPPED_KEYWORDS
        and not key.startswith("x-")
        and key != "$ref"
    )
    if unknown:
        raise UnsupportedSchemaError(
            f"{where}: unrecognised keyword(s) {unknown}. This adapter "
            f"implements the subset MCP tool schemas use "
            f"(MCP_ADAPTER_PLAN.md §5) and refuses what it does not "
            f"understand rather than validating too loosely. Use an "
            f"'x-' prefix for extension keywords."
        )


def warn_dropped_keywords(schema: Mapping[str, Any], where: str) -> None:
    """
    Warn for each ``DROPPED_KEYWORDS`` entry present in *schema*.

    Separate from ``screen_keywords`` because a dropped keyword loosens
    validation for one *field*, so it should be reported once, where the
    field's constraints are built -- not once per path that happens to walk
    past the same subschema.
    """
    for keyword, reason in DROPPED_KEYWORDS.items():
        if keyword in schema:
            warnings.warn(
                f"Field '{where}': '{keyword}' was dropped -- {reason}. "
                f"StateGuard will not enforce it, so this field is validated "
                f"slightly more loosely than the schema specifies.",
                SchemaFeatureWarning,
                stacklevel=2,
            )

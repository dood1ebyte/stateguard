"""
JSON Schema document -> ``ContractSpec``.

Walks the supported subset (``MCP_ADAPTER_PLAN.md`` §5) and produces the
normalised contract the engine reasons over. Types come from
``JSONSchemaTypeMapper``; this module owns structure -- properties, required,
defaults, constraints, nesting -- and owns the refusals.

Why this adapter emits ``NOT_NULL`` when the Pydantic one does not
---------------------------------------------------------------------
``ContractValidator`` only rejects a ``None`` value when the field carries a
``NOT_NULL`` constraint; without one, ``{"a": None}`` validates cleanly
against a ``STRING`` field. ``PydanticExtractor`` never emits ``NOT_NULL``
and is right not to, because Pydantic's own validator is the source of truth
there and rejects the ``None`` itself.

This adapter has no such backstop. ``ContractValidator`` *is* the source of
truth for JSON Schema (``docs/adr/0001-json-schema-source-of-truth.md``), so
a non-nullable field that does not carry the constraint would let
``{"location": null}`` pass against ``{"location": {"type": "string"}}`` --
which JSON Schema plainly forbids. Emitting it is what keeps the ADR's
promise that the gap between "StateGuard says valid" and "the spec says
valid" is bounded and known rather than accidental.

Refusing rather than ignoring
-----------------------------
Unknown keywords are not ignored. §5's reject list exists because silently
skipping ``allOf`` or ``not`` means reporting ``SUCCESS`` on a payload the
schema's author intended to forbid, and with no second validator downstream
nothing would ever catch it. Keywords that are understood but not
representable warn instead (``SchemaFeatureWarning``); keywords that are
purely advisory are ignored by name, never by falling through.

Zero external dependencies.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.patterns import screen_pattern
from stateguard.adapters.jsonschema.refs import RefResolver
from stateguard.adapters.jsonschema.type_mapper import JSONSchemaTypeMapper, MappedType
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)

__all__ = ["JSONSchemaExtractor"]


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
_IGNORED_KEYWORDS = frozenset(
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


class JSONSchemaExtractor:
    """Converts a JSON Schema document into a ``ContractSpec``. Stateless."""

    def __init__(self, type_mapper: JSONSchemaTypeMapper | None = None) -> None:
        self._types = type_mapper if type_mapper is not None else JSONSchemaTypeMapper()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def extract(self, schema: Mapping[str, Any]) -> ContractSpec:
        """
        Build a ``ContractSpec`` from a complete JSON Schema document.

        *schema* must describe an object -- a contract is a set of named
        fields, so a root of any other type has nothing to map.
        """
        if not isinstance(schema, Mapping):
            raise UnsupportedSchemaError(
                f"A JSON Schema must be an object, got {type(schema).__name__}."
            )

        resolver = RefResolver(schema)
        # The root itself may be a $ref -- Pydantic emits exactly that for a
        # self-referential model.
        with resolver.resolved(schema) as root:
            return self._spec(root, resolver, prefix="")

    # ------------------------------------------------------------------
    # Structure
    # ------------------------------------------------------------------

    def _spec(
        self,
        schema: Mapping[str, Any],
        resolver: RefResolver,
        prefix: str,
    ) -> ContractSpec:
        """Build one level of contract from an object schema."""
        where = prefix or "<root>"
        self._reject_unsupported(schema, where)

        declared = schema.get("type")
        if declared is not None and declared != "object":
            if isinstance(declared, Sequence) and not isinstance(declared, str):
                if "object" not in declared:
                    raise UnsupportedSchemaError(
                        f"{where}: expected an object schema, got type {declared!r}."
                    )
            else:
                raise UnsupportedSchemaError(
                    f"{where}: expected an object schema, got type {declared!r}."
                )

        properties = schema.get("properties") or {}
        if not isinstance(properties, Mapping):
            raise UnsupportedSchemaError(
                f"{where}: 'properties' must be an object, got {type(properties).__name__}."
            )

        required = self._required_names(schema, where)

        fields = [
            self._field(name, subschema, name in required, resolver, prefix)
            for name, subschema in properties.items()
        ]

        return ContractSpec(
            fields=fields,
            strict_mode=schema.get("additionalProperties") is False,
        )

    @staticmethod
    def _required_names(schema: Mapping[str, Any], where: str) -> frozenset[str]:
        required = schema.get("required")
        if required is None:
            return frozenset()
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise UnsupportedSchemaError(
                f"{where}: 'required' must be a list, got {type(required).__name__}."
            )
        return frozenset(str(name) for name in required)

    def _field(
        self,
        name: str,
        subschema: Any,
        required: bool,
        resolver: RefResolver,
        prefix: str,
    ) -> FieldSpec:
        """Build the ``FieldSpec`` for one property."""
        path = f"{prefix}.{name}" if prefix else name
        self._check_property_name(name, path)

        if not isinstance(subschema, Mapping):
            raise UnsupportedSchemaError(
                f"Field '{path}': expected a schema object, "
                f"got {type(subschema).__name__}. Boolean schemas are not supported."
            )

        with resolver.resolved(subschema) as resolved:
            mapped = self._types.map_schema(resolved, resolver, path)

            # Descend through the *unresolved* effective branch so the
            # recursion guard stays armed -- see type_mapper's docstring.
            nested_spec = None
            if mapped.field_type is FieldType.OBJECT:
                with resolver.resolved(mapped.effective_schema) as target:
                    nested_spec = self._spec(target, resolver, path)
            else:
                self._reject_unsupported(mapped.effective_schema, path)

            return FieldSpec(
                path=name,
                field_type=mapped.field_type,
                required=required,
                default=self._default(mapped.effective_schema),
                constraints=self._constraints(mapped, path),
                item_type=mapped.item_type,
                nested_spec=nested_spec,
                union_members=mapped.union_members,
            )

    @staticmethod
    def _check_property_name(name: str, path: str) -> None:
        """
        Refuse property names StateGuard's paths cannot address.

        Field paths are dot-separated, so a property whose own name contains
        a dot is indistinguishable from a nested path: ``{"user.name": ...}``
        and ``{"user": {"name": ...}}`` produce the same string. JSON Schema
        allows the former. Failing loudly is the only safe option -- silently
        accepting it would let a repair write to the wrong field, which is
        the failure mode that damages trust most (§8).
        """
        if not isinstance(name, str):
            raise UnsupportedSchemaError(
                f"Property names must be strings, got {type(name).__name__}: {name!r}"
            )
        if "." in name:
            raise UnsupportedSchemaError(
                f"Property name {name!r} contains '.', which StateGuard's "
                f"dot-notation field paths cannot address unambiguously -- it is "
                f"indistinguishable from the nested path '{path}'. Rename the "
                f"property, or use a schema without dotted names."
            )
        if not name:
            raise UnsupportedSchemaError("Property names must not be empty.")

    @staticmethod
    def _default(schema: Mapping[str, Any]) -> Any:
        """
        The declared default, or ``MISSING``.

        ``MISSING`` rather than ``None`` as the fallback is what keeps
        ``default: null`` -- a real, declared default -- distinguishable from
        no default at all. ``DefaultValueFillStrategy`` reads exactly that
        difference to decide whether it may fill the field.
        """
        return schema.get("default", MISSING)

    # ------------------------------------------------------------------
    # Constraints
    # ------------------------------------------------------------------

    def _constraints(self, mapped: MappedType, path: str) -> list[FieldConstraint]:
        """Translate the value-constraint keywords §5 supports."""
        schema = mapped.effective_schema
        constraints: list[FieldConstraint] = []

        for keyword, constraint_type in (
            ("minimum", FieldConstraintType.MINIMUM),
            ("maximum", FieldConstraintType.MAXIMUM),
            ("minLength", FieldConstraintType.MIN_LENGTH),
            ("maxLength", FieldConstraintType.MAX_LENGTH),
            # minItems/maxItems address the same sized-value check as
            # minLength/maxLength; ContractValidator already applies it to
            # lists as well as strings.
            ("minItems", FieldConstraintType.MIN_LENGTH),
            ("maxItems", FieldConstraintType.MAX_LENGTH),
            ("pattern", FieldConstraintType.PATTERN),
        ):
            if keyword in schema:
                bound = schema[keyword]
                if constraint_type is FieldConstraintType.PATTERN:
                    # The only keyword whose value gets *executed*, and it
                    # comes from an untrusted schema. See patterns.py.
                    bound = screen_pattern(bound, path)
                constraints.append(FieldConstraint(constraint_type, bound))

        for keyword, reason in DROPPED_KEYWORDS.items():
            if keyword in schema:
                warnings.warn(
                    f"Field '{path}': '{keyword}' was dropped -- {reason}. "
                    f"StateGuard will not enforce it, so this field is validated "
                    f"slightly more loosely than the schema specifies.",
                    SchemaFeatureWarning,
                    stacklevel=2,
                )

        if mapped.enum_values is not None:
            constraints.append(FieldConstraint(FieldConstraintType.ENUM_VALUES, mapped.enum_values))

        # See the module docstring: this adapter is its own source of truth,
        # so a non-nullable field must say so or null slips through.
        if not mapped.nullable and mapped.field_type not in (
            FieldType.ANY,
            FieldType.NULL,
        ):
            constraints.append(FieldConstraint(FieldConstraintType.NOT_NULL, True))

        return constraints

    # ------------------------------------------------------------------
    # Refusals
    # ------------------------------------------------------------------

    @staticmethod
    def _reject_unsupported(schema: Mapping[str, Any], where: str) -> None:
        """Refuse §5's reject list, and any keyword not accounted for."""
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
            if key not in _IGNORED_KEYWORDS
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

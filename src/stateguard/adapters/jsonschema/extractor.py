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

The screen itself lives in ``keywords.py`` rather than here, because this
module is not the only one that walks a schema -- see that module for what
went wrong while it was private to this one.

Zero external dependencies.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.keywords import screen_keywords, warn_dropped_keywords
from stateguard.adapters.jsonschema.patterns import screen_pattern
from stateguard.adapters.jsonschema.refs import RefResolver
from stateguard.adapters.jsonschema.type_mapper import JSONSchemaTypeMapper, MappedType
from stateguard.core.models.contract import MISSING, ContractSpec, FieldSpec
from stateguard.core.models.field_types import (
    FieldConstraint,
    FieldConstraintType,
    FieldType,
)
from stateguard.core.validator import ContractValidator

__all__ = ["JSONSchemaExtractor"]


class JSONSchemaExtractor:
    """Converts a JSON Schema document into a ``ContractSpec``. Stateless."""

    def __init__(self, type_mapper: JSONSchemaTypeMapper | None = None) -> None:
        self._types = type_mapper if type_mapper is not None else JSONSchemaTypeMapper()
        # Used only to screen declared defaults -- see ``_screened_default``.
        self._validator = ContractValidator()

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
        screen_keywords(schema, where)

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
        fields.extend(self._undeclared_required(required, properties, prefix))

        return ContractSpec(
            fields=fields,
            strict_mode=self._strict_mode(schema, where),
        )

    @staticmethod
    def _strict_mode(schema: Mapping[str, Any], where: str) -> bool:
        """
        Whether ``additionalProperties`` forbids properties not declared.

        Only ``false`` maps onto ``strict_mode``. A *schema object* --
        ``{"additionalProperties": {"type": "string"}}`` -- means extra
        properties are allowed but must match that schema, which
        ``ContractSpec`` has no way to express: it can require that a
        declared set is exhaustive, not that undeclared members share a
        type. That is looser than the schema asks, so it warns rather than
        passing silently, which is what it used to do.
        """
        if "additionalProperties" not in schema:
            return False

        value = schema["additionalProperties"]
        if value is False:
            return True
        if value is True:
            return False

        if isinstance(value, Mapping):
            warnings.warn(
                f"{where}: 'additionalProperties' is a schema object, and the "
                f"constraint it places on undeclared properties is not enforced. "
                f"StateGuard can require that the declared properties are the only "
                f"ones ('additionalProperties: false'), but cannot type the ones it "
                f"has no name for, so an undeclared property of any type is accepted.",
                SchemaFeatureWarning,
                stacklevel=2,
            )
            return False

        raise UnsupportedSchemaError(
            f"{where}: 'additionalProperties' must be a boolean or a schema object, "
            f"got {type(value).__name__}: {value!r}"
        )

    def _undeclared_required(
        self,
        required: frozenset[str],
        properties: Mapping[str, Any],
        prefix: str,
    ) -> list[FieldSpec]:
        """
        Fields named in ``required`` but absent from ``properties``.

        JSON Schema allows this, and it still means the property must be
        present: ``required`` constrains presence, ``properties`` constrains
        value, and neither implies the other. Dropping these -- which this
        extractor used to do -- meant a payload missing such a field was
        reported ``ALREADY_VALID``.

        There is no subschema to type them from, so they are ``ANY``:
        presence is enforced, the value is not constrained. That is exactly
        what the schema says.
        """
        specs: list[FieldSpec] = []
        for name in sorted(required - set(properties)):
            self._check_property_name(name, f"{prefix}.{name}" if prefix else name)
            specs.append(
                FieldSpec(
                    path=name,
                    field_type=FieldType.ANY,
                    required=True,
                )
            )
        return specs

    @staticmethod
    def _required_names(schema: Mapping[str, Any], where: str) -> frozenset[str]:
        """
        The names ``required`` lists, refusing anything that is not a string.

        JSON Schema specifies ``required`` as an array of strings. These used
        to be ``str()``-coerced, which was invisible while a name with no
        matching property was silently dropped -- but such names now become
        real ``ANY`` fields, so ``required: [123]`` would materialise a field
        called ``"123"`` that no payload can satisfy on purpose. Guessing
        what a non-string entry meant is worse than saying it is malformed.
        """
        required = schema.get("required")
        if required is None:
            return frozenset()
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise UnsupportedSchemaError(
                f"{where}: 'required' must be a list, got {type(required).__name__}."
            )

        for name in required:
            if not isinstance(name, str):
                raise UnsupportedSchemaError(
                    f"{where}: 'required' must contain strings, got "
                    f"{type(name).__name__}: {name!r}. A property name is a string; "
                    f"coercing this one would invent a field the schema never declared."
                )
        return frozenset(required)

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

            # ``MappedType.effective_schema`` is deliberately left
            # *unresolved* so that descending through it re-arms the
            # recursion guard (see type_mapper's docstring). That makes it
            # unsafe to read keywords off directly: for the ``anyOf: [{$ref},
            # {null}]`` shape Pydantic emits for every ``Optional[X]``, it is
            # a bare ``{"$ref": ...}``, which carries no constraints, no
            # default, and none of the keywords the screen looks for. Reading
            # it as-is silently dropped all three -- a schema's `minLength`
            # vanished, and an `allOf` behind the reference was accepted as
            # ANY rather than refused.
            #
            # Resolving it here, once, in a context that stays open for the
            # whole field, gives the keyword-bearing schema *and* keeps the
            # guard armed for anything nested below.
            with resolver.resolved(mapped.effective_schema) as effective:
                nested_spec = None
                if mapped.field_type is FieldType.OBJECT:
                    nested_spec = self._spec(effective, resolver, path)
                else:
                    screen_keywords(effective, path)

                spec = FieldSpec(
                    path=name,
                    field_type=mapped.field_type,
                    required=required,
                    default=self._default(effective),
                    constraints=self._constraints(effective, mapped, path),
                    item_type=mapped.item_type,
                    nested_spec=nested_spec,
                    union_members=mapped.union_members,
                )
                spec.default = self._screened_default(spec, path)
                return spec

    def _screened_default(self, spec: FieldSpec, path: str) -> Any:
        """
        The declared default if it satisfies its own field, else ``MISSING``.

        A default is the one value in a schema that StateGuard *writes*
        rather than merely checks: ``JSONSchemaAdapter.wrap`` materialises it
        into the payload, and it does so after the engine has finished, so
        nothing downstream re-checks it. A schema that declares
        ``{"type": "integer", "default": "abc"}`` -- or a default that fell
        out of an ``enum`` when the server's vocabulary changed, which is
        exactly the drift this adapter exists for -- would otherwise have
        StateGuard inject a value that fails the contract it just certified.
        Confirmed before this screen existed: ``repair`` returned
        ``ALREADY_VALID`` and ``validate`` then rejected its own output.

        The engine already refuses to do this on the path it controls:
        ``DefaultValueFillStrategy`` fills, revalidates, and fails the repair
        when the filled value does not hold up. This restores the same
        guarantee on the path that bypasses it.

        Dropped with a warning rather than refused, because an unusable
        default is not a schema this adapter cannot *read* -- validation is
        unaffected, and every other field still repairs. Refusing the whole
        document would fail hardest against precisely the drifted servers
        StateGuard is for. The field simply stops being auto-filled.
        """
        if spec.default is MISSING:
            return MISSING

        probe = ContractSpec(
            fields=[replace(spec, required=False, default=MISSING)],
            strict_mode=False,
        )
        result = self._validator.validate(probe, {spec.path: spec.default})
        if result.is_valid:
            return spec.default

        reasons = "; ".join(violation.message for violation in result.violations)
        warnings.warn(
            f"Field '{path}': the declared default {spec.default!r} does not satisfy "
            f"this field's own schema ({reasons}). It was dropped -- StateGuard will "
            f"not fill this field, because writing a value that fails the contract "
            f"would hand back a payload it had just reported valid.",
            SchemaFeatureWarning,
            stacklevel=2,
        )
        return MISSING

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

    def _constraints(
        self,
        schema: Mapping[str, Any],
        mapped: MappedType,
        path: str,
    ) -> list[FieldConstraint]:
        """
        Translate the value-constraint keywords §5 supports.

        *schema* is the resolved effective schema -- the one that actually
        carries the keywords. See ``_field`` for why it is passed in rather
        than read off *mapped*.
        """
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

        warn_dropped_keywords(schema, path)

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

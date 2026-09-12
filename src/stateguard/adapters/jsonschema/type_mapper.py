"""
JSON Schema type keywords -> ``FieldType``.

Implements the type half of the supported subset (``MCP_ADAPTER_PLAN.md``
§5).  Constraints, defaults and nesting belong to the extractor; this module
answers exactly one question -- *what shape does this subschema accept* --
and answers it for the four ways JSON Schema can say so: ``type`` as a
string, ``type`` as a list, ``anyOf``/``oneOf``, and (when nothing else says)
the member types of ``enum`` / ``const``.

Parity with the Pydantic adapter
--------------------------------
The two adapters must agree, because the same repair strategies consume both
and a field that reads as ``UNION`` through one path and ``STRING`` through
the other would be priced differently by the trust model for no reason the
user could see.  ``PydanticTypeMapper`` sets the rules and this mirrors them:

* ``null`` members are **dropped**, never carried as union members.
  Nullability is reported separately, exactly as ``unwrap_optional`` reports
  optionality -- ``["string", "null"]`` is a nullable string, not a union.
* **One** surviving member is unwrapped to that member's type.
* **Two or more** surviving members produce ``FieldType.UNION`` with
  ``union_members``; array members carry their element type, and elements
  that are themselves unions collapse to ``ANY``.

Why the effective schema is returned unresolved
-----------------------------------------------
``MappedType.effective_schema`` is the branch that survived the optional
collapse, **as written** -- still a ``$ref`` if that is what it was.  The
extractor descends through it with ``RefResolver.resolved``, which is what
keeps the recursion guard armed: handing back an already-dereferenced dict
would pop the pointer off the active stack before the extractor recursed
into it, and a recursive schema would then walk forever.  The guard only
works if every descent goes through the resolver.

Zero external dependencies.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    SchemaFeatureWarning,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.keywords import screen_keywords
from stateguard.adapters.jsonschema.refs import RefResolver
from stateguard.core.models.field_types import FieldType, UnionMember

__all__ = [
    "JSON_TYPE_NAMES",
    "JSONSchemaTypeMapper",
    "MappedType",
]


#: JSON Schema's ``type`` vocabulary, mapped to StateGuard's.
#:
#: ``number`` becomes ``FLOAT`` while ``integer`` stays ``INTEGER``: JSON
#: draws the same distinction StateGuard does, so nothing is lost. There is
#: deliberately no entry for anything else -- an unknown type name is an
#: error, not an ``ANY`` fallback, because silently widening a field to
#: "accepts anything" is precisely the under-validation this adapter must not
#: do (see ``errors`` module docstring).
JSON_TYPE_NAMES: dict[str, FieldType] = {
    "string": FieldType.STRING,
    "integer": FieldType.INTEGER,
    "number": FieldType.FLOAT,
    "boolean": FieldType.BOOLEAN,
    "object": FieldType.OBJECT,
    "array": FieldType.ARRAY,
    "null": FieldType.NULL,
}

# Python type of an ``enum`` / ``const`` value -> FieldType, used only when
# the schema declares no ``type`` of its own. ``bool`` is checked before
# ``int`` because ``bool`` is an ``int`` subclass in Python and would
# otherwise be reported as INTEGER.
_VALUE_TYPE_NAMES: tuple[tuple[type, FieldType], ...] = (
    (bool, FieldType.BOOLEAN),
    (int, FieldType.INTEGER),
    (float, FieldType.FLOAT),
    (str, FieldType.STRING),
)


@dataclass
class MappedType:
    """
    What one subschema accepts.

    Attributes
    ----------
    field_type:
        The abstract type of the field.
    effective_schema:
        The branch that survived the optional collapse, **unresolved** --
        what the extractor should descend through. For a plain subschema
        this is the input unchanged.
    item_type:
        Element type for ``ARRAY``; ``None`` otherwise.
    union_members:
        Accepted members for ``UNION``; ``None`` otherwise.
    nullable:
        Whether ``null`` was among the accepted types. Reported rather than
        folded into the type, mirroring ``PydanticTypeMapper.unwrap_optional``.
    enum_values:
        Values from ``enum`` / ``const``, in declaration order with
        duplicates removed; ``None`` when neither keyword is present. The
        extractor turns these into an ``ENUM_VALUES`` constraint.
    """

    field_type: FieldType
    effective_schema: Mapping[str, Any] = field(default_factory=dict)
    item_type: FieldType | None = None
    union_members: tuple[UnionMember, ...] | None = None
    nullable: bool = False
    enum_values: tuple[Any, ...] | None = None


class JSONSchemaTypeMapper:
    """Maps JSON Schema type keywords onto ``FieldType``. Stateless."""

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def map_schema(
        self,
        schema: Mapping[str, Any],
        resolver: RefResolver,
        path: str,
    ) -> MappedType:
        """
        Map *schema* (already ``$ref``-resolved by the caller).

        *path* is used only to make errors say which field they are about.
        """
        branches = self._branches(schema, path)
        if branches is not None:
            return self._map_union(branches, schema, resolver, path)

        declared = schema.get("type")
        if isinstance(declared, str):
            return self._map_named_type(declared, schema, resolver, path)
        if declared is not None:
            raise UnsupportedSchemaError(
                f"Field '{path}': 'type' must be a string or a list of strings, "
                f"got {type(declared).__name__}: {declared!r}"
            )

        # No 'type' at all. An enum or const still pins the type down.
        enum_values = self._enum_values(schema, path)
        if enum_values is not None:
            return MappedType(
                field_type=self._infer_from_values(enum_values),
                effective_schema=schema,
                enum_values=enum_values,
                # With no 'type' to forbid it, a ``null`` member of the enum
                # is a value the schema genuinely accepts, so the field is
                # nullable and must not pick up a NOT_NULL constraint.
                # (``{"type": "string", "enum": [..., null]}`` is the other
                # case: there the type rules the null member out, so
                # NOT_NULL stays correct -- which is why this lives on the
                # untyped path only.)
                nullable=any(value is None for value in enum_values),
            )

        # Genuinely untyped. This is the one legitimate ANY: the schema's
        # author declared no type, so the field really does accept anything.
        return MappedType(field_type=FieldType.ANY, effective_schema=schema)

    # ------------------------------------------------------------------
    # Unions
    # ------------------------------------------------------------------

    @staticmethod
    def _branches(schema: Mapping[str, Any], path: str) -> list[Mapping[str, Any]] | None:
        """
        Return the union branches of *schema*, or ``None`` if it is not one.

        Normalises the two spellings into one shape. ``type: ["string",
        "null"]`` is rewritten into real subschemas so that sibling keywords
        travel with the branch they constrain -- ``{"type": ["string",
        "null"], "minLength": 2}`` means a string of at least 2 characters
        *or* null, and dropping ``minLength`` while collapsing would quietly
        discard a constraint.
        """
        any_of = schema.get("anyOf")
        one_of = schema.get("oneOf")
        if any_of is not None and one_of is not None:
            raise UnsupportedSchemaError(
                f"Field '{path}': 'anyOf' and 'oneOf' on the same schema are not "
                f"supported -- their intersection is not expressible as a contract."
            )

        combinator = any_of if any_of is not None else one_of
        if combinator is not None:
            if not isinstance(combinator, Sequence) or isinstance(combinator, (str, bytes)):
                raise UnsupportedSchemaError(
                    f"Field '{path}': 'anyOf'/'oneOf' must be a list, "
                    f"got {type(combinator).__name__}."
                )
            if not combinator:
                raise UnsupportedSchemaError(
                    f"Field '{path}': 'anyOf'/'oneOf' is empty, which accepts nothing."
                )
            return list(combinator)

        declared = schema.get("type")
        if isinstance(declared, Sequence) and not isinstance(declared, (str, bytes)):
            if not declared:
                raise UnsupportedSchemaError(
                    f"Field '{path}': 'type' is an empty list, which accepts nothing."
                )
            rest = {key: value for key, value in schema.items() if key != "type"}
            return [{**rest, "type": name} for name in declared]

        return None

    def _map_union(
        self,
        branches: list[Mapping[str, Any]],
        original: Mapping[str, Any],
        resolver: RefResolver,
        path: str,
    ) -> MappedType:
        """Collapse *branches* per the parity rules in the module docstring."""
        surviving: list[Mapping[str, Any]] = []
        nullable = False

        for branch in branches:
            with resolver.resolved(branch) as target:
                # Screened here because the extractor never sees a branch --
                # it only ever screens the schema that *carries* the
                # combinator. Without this, a rejected keyword inside one
                # branch was silently ignored (see ``keywords`` module).
                screen_keywords(target, path)
                if target.get("type") == "null":
                    nullable = True
                    continue
            surviving.append(branch)

        if not surviving:
            # Every branch was null -- the field's declared type really is null.
            return MappedType(field_type=FieldType.NULL, effective_schema=original, nullable=True)

        if len(surviving) == 1:
            # Optional[X]: unwrap to X and report nullability separately.
            branch = surviving[0]
            with resolver.resolved(branch) as target:
                mapped = self.map_schema(target, resolver, path)

            # Keep the *unresolved* branch so the extractor's descent stays
            # guarded -- see the module docstring -- but only when the
            # recursive call has not already narrowed further.
            #
            # ``target`` is what was handed to ``map_schema``, so identity
            # here means "it mapped the schema as given". If it came back
            # pointing somewhere else, this branch was itself a union and the
            # inner collapse found the schema that actually carries the type,
            # constraints and default; overwriting would throw that away and
            # leave the extractor resolving to an ``anyOf`` that carries
            # none of them. That is what used to happen to a chained
            # ``Optional[Optional[X]]``: the reject-list screen still fired
            # per branch, but X's ``minLength`` and ``default`` vanished.
            #
            # The inner result is unresolved too, so the guard stays armed
            # either way, and a genuine A -> B -> A cycle is caught while
            # mapping rather than while descending.
            if mapped.effective_schema is target:
                mapped.effective_schema = branch
            mapped.nullable = mapped.nullable or nullable
            return mapped

        members: list[UnionMember] = []
        for branch in surviving:
            with resolver.resolved(branch) as target:
                mapped = self.map_schema(target, resolver, path)
            members.append(
                UnionMember(
                    field_type=mapped.field_type,
                    item_type=mapped.item_type,
                )
            )

        return MappedType(
            field_type=FieldType.UNION,
            effective_schema=original,
            union_members=tuple(members),
            nullable=nullable,
        )

    # ------------------------------------------------------------------
    # Single named type
    # ------------------------------------------------------------------

    def _map_named_type(
        self,
        declared: str,
        schema: Mapping[str, Any],
        resolver: RefResolver,
        path: str,
    ) -> MappedType:
        try:
            field_type = JSON_TYPE_NAMES[declared]
        except KeyError:
            raise UnsupportedSchemaError(
                f"Field '{path}': unknown type {declared!r}. "
                f"Supported types are {sorted(JSON_TYPE_NAMES)}."
            ) from None

        item_type = None
        if field_type is FieldType.ARRAY:
            item_type = self._item_type(schema, resolver, path)

        return MappedType(
            field_type=field_type,
            effective_schema=schema,
            item_type=item_type,
            nullable=field_type is FieldType.NULL,
            enum_values=self._enum_values(schema, path),
        )

    def _item_type(
        self,
        schema: Mapping[str, Any],
        resolver: RefResolver,
        path: str,
    ) -> FieldType:
        """
        Element type for an array, from ``items``.

        An array with no ``items`` accepts anything, and so does a tuple-form
        ``items`` (draft-4's positional list): StateGuard's ``item_type`` is a
        single type applied to every element, so per-position typing has no
        representation and ``ANY`` is the honest answer rather than picking
        the first position's type and pretending it covers the rest.

        The tuple form warns on the way past, though. Widening to ``ANY``
        validates more loosely than the schema asks, which is the same
        situation ``exclusiveMinimum`` is in, and that one has always warned.
        Staying silent here made the two inconsistent for no reason a caller
        could see.
        """
        if "items" not in schema:
            return FieldType.ANY

        items = schema["items"]
        if not isinstance(items, Mapping):
            warnings.warn(
                f"Field '{path}': 'items' is a {type(items).__name__}, not a schema "
                f"object -- element typing was widened to ANY. StateGuard applies "
                f"one element type to a whole array, so a positional (tuple-form) "
                f"'items' has no representation, and elements are validated more "
                f"loosely than the schema specifies.",
                SchemaFeatureWarning,
                stacklevel=2,
            )
            return FieldType.ANY

        with resolver.resolved(items) as target:
            # Same reasoning as the union branches: the extractor descends
            # through ``properties``, never through ``items``, so this is the
            # only place an element schema can be screened.
            screen_keywords(target, f"{path}[]")
            mapped = self.map_schema(target, resolver, f"{path}[]")

        # A union of element types has no representation in ``item_type``,
        # matching PydanticTypeMapper.get_item_type.
        if mapped.field_type is FieldType.UNION:
            return FieldType.ANY
        return mapped.field_type

    # ------------------------------------------------------------------
    # enum / const
    # ------------------------------------------------------------------

    @staticmethod
    def _enum_values(schema: Mapping[str, Any], path: str) -> tuple[Any, ...] | None:
        """
        Values from ``enum`` or ``const``, or ``None`` if neither is present.

        ``const`` is the single-member case of ``enum`` and is normalised
        into one, which is what §5 specifies and what lets the enum repair
        strategy treat both identically.
        """
        has_enum = "enum" in schema
        has_const = "const" in schema
        if has_enum and has_const:
            raise UnsupportedSchemaError(
                f"Field '{path}': 'enum' and 'const' on the same schema are ambiguous; use one."
            )

        if has_const:
            return (schema["const"],)

        if not has_enum:
            return None

        values = schema["enum"]
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise UnsupportedSchemaError(
                f"Field '{path}': 'enum' must be a list, got {type(values).__name__}."
            )
        if not values:
            raise UnsupportedSchemaError(f"Field '{path}': 'enum' is empty, which accepts nothing.")

        # Preserve declaration order; drop duplicates. Order is what the
        # repair strategy reports back to a caller, so it should read the way
        # the schema's author wrote it.
        seen: list[Any] = []
        for value in values:
            if value not in seen:
                seen.append(value)
        return tuple(seen)

    @staticmethod
    def _infer_from_values(values: tuple[Any, ...]) -> FieldType:
        """
        Infer a field type from enum members, when no ``type`` is declared.

        ``null`` members are ignored for the purposes of inference -- an enum
        of ``["a", "b", null]`` is still a string field. If what remains is
        not all one type, ``ANY`` is correct: the members genuinely do not
        share a type, and the ``ENUM_VALUES`` constraint still pins the
        allowed set exactly.
        """
        non_null = [value for value in values if value is not None]
        if not non_null:
            return FieldType.NULL

        found: set[FieldType] = set()
        for value in non_null:
            for python_type, field_type in _VALUE_TYPE_NAMES:
                if isinstance(value, python_type):
                    found.add(field_type)
                    break
            else:
                return FieldType.ANY

        return found.pop() if len(found) == 1 else FieldType.ANY

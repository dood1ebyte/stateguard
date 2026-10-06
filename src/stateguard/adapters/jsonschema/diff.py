"""
Structural schema diff and change classification.

``diff_schemas(before, after)`` answers "what changed between these two
versions of a schema, and could a call that worked before stop working
now?" -- deterministically, from structure alone.

What "compatible" means here
----------------------------
One thing, precisely: **every argument payload the old schema accepted, the
new schema accepts too.** That is the question a proxy or an agent holding
cached tool definitions needs answered, and it is decidable structurally for
the changes MCP tools actually make: an optional parameter added, a bound
loosened, an enum member added, a type widened from ``integer`` to
``number``. Each ``SchemaChange`` carries ``breaking``, and a change is
non-breaking only when a rule here *proves* it cannot reject a previously
valid call. Everything else -- a ``pattern`` edited, a ``oneOf`` branch
added, a keyword this module has no rule for -- is breaking, because "might
reject" is the honest answer. So ``COMPATIBLE`` is a guarantee and
``INCOMPATIBLE`` means "possibly incompatible; here is what changed".

"Compatible" says nothing about behaviour. A changed ``default`` accepts the
same calls and changes what omitted arguments mean; a removed optional
parameter in an open schema accepts the same calls and starts ignoring a
value. Both are reported, non-breaking, with a ``detail`` that says so.

What it does not do
-------------------
No semantic inference. A removed ``city`` and an added ``location`` are a
removal and an addition, not a rename -- whether they mean the same thing is
exactly the judgement StateGuard's repair engine makes per call, with
evidence, and declines to make when the evidence is weak. No LLM, no
heuristics, no network.

Statuses
--------
=================  ==========================================================
``UNCHANGED``      The documents are identical (up to key order).
``EQUIVALENT``     The documents differ, the contracts do not: annotations,
                   ordering, ``$ref`` layout. ``changes`` is empty.
``COMPATIBLE``     The contract changed and every change is non-breaking.
``INCOMPATIBLE``   At least one change is (possibly) breaking.
``INVALID``        The new schema is not valid JSON Schema.
``UNSUPPORTED``    The new schema is valid JSON Schema StateGuard cannot
                   represent, so no call to it could be validated.
=================  ==========================================================

``changes`` is empty exactly when the contract fingerprints are equal; a
property test holds the module to that.

Zero external dependencies.
"""

from __future__ import annotations

import warnings
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Final

from stateguard.adapters.jsonschema.errors import (
    JSONSchemaError,
    MalformedSchemaError,
    SchemaFeatureWarning,
)
from stateguard.adapters.jsonschema.extractor import JSONSchemaExtractor
from stateguard.adapters.jsonschema.identity import (
    SchemaFingerprint,
    canonical_json,
    fingerprint_schema,
    normalize_schema,
)

__all__ = [
    "ABSENT",
    "ChangeKind",
    "SchemaChange",
    "SchemaChangeStatus",
    "SchemaDiff",
    "diff_schemas",
]


class _Absent:
    """The keyword or field is not there -- distinct from a value of ``None``."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


#: Marks a ``SchemaChange.before`` / ``after`` that did not exist.
ABSENT: Final[_Absent] = _Absent()


class SchemaChangeStatus(StrEnum):
    """Overall verdict on a schema change. See the module docstring."""

    UNCHANGED = "unchanged"
    EQUIVALENT = "equivalent"
    COMPATIBLE = "compatible"
    INCOMPATIBLE = "incompatible"
    INVALID = "invalid"
    UNSUPPORTED = "unsupported"


class ChangeKind(StrEnum):
    """What kind of structural change one ``SchemaChange`` records."""

    FIELD_ADDED = "field_added"
    FIELD_REMOVED = "field_removed"
    REQUIRED_ADDED = "required_added"
    REQUIRED_REMOVED = "required_removed"
    TYPE_CHANGED = "type_changed"
    ENUM_CHANGED = "enum_changed"
    CONST_CHANGED = "const_changed"
    CONSTRAINT_CHANGED = "constraint_changed"
    DEFAULT_CHANGED = "default_changed"
    ITEMS_CHANGED = "items_changed"
    ADDITIONAL_PROPERTIES_CHANGED = "additional_properties_changed"
    UNION_CHANGED = "union_changed"
    KEYWORD_CHANGED = "keyword_changed"
    SCHEMA_REPLACED = "schema_replaced"


@dataclass(frozen=True)
class SchemaChange:
    """
    One structural difference.

    Attributes
    ----------
    kind:
        See ``ChangeKind``.
    path:
        Where: dotted field path (``filters.lang``), ``[]`` for array items
        (``tags[]``), ``*`` for additional properties, ``""`` for the root.
    keyword:
        The JSON Schema keyword involved, when there is one.
    before / after:
        The old and new value, in normal form; ``ABSENT`` when it did not
        exist on that side.
    breaking:
        ``True`` unless a rule proves no previously valid call is rejected.
    detail:
        One deterministic sentence. Contains schema content only -- never
        argument values.
    """

    kind: ChangeKind
    path: str
    breaking: bool
    detail: str
    keyword: str | None = None
    before: Any = ABSENT
    after: Any = ABSENT

    def to_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {
            "kind": self.kind.value,
            "path": self.path,
            "keyword": self.keyword,
            "breaking": self.breaking,
            "detail": self.detail,
        }
        if self.before is not ABSENT:
            record["before"] = self.before
        if self.after is not ABSENT:
            record["after"] = self.after
        return record


@dataclass(frozen=True)
class SchemaDiff:
    """
    The verdict and the changes behind it.

    Attributes
    ----------
    status:
        See ``SchemaChangeStatus``.
    changes:
        Every structural change, in a deterministic order (depth-first,
        keyword order fixed, properties sorted).
    before / after:
        Fingerprints of the two schemas, or ``None`` for one that could not
        be fingerprinted at all.
    error:
        Why the new schema is ``INVALID`` / ``UNSUPPORTED``; ``None``
        otherwise.
    """

    status: SchemaChangeStatus
    changes: tuple[SchemaChange, ...] = ()
    before: SchemaFingerprint | None = None
    after: SchemaFingerprint | None = None
    error: str | None = None

    @property
    def breaking_changes(self) -> tuple[SchemaChange, ...]:
        return tuple(change for change in self.changes if change.breaking)

    @property
    def is_compatible(self) -> bool:
        """Every call valid against the old schema is valid against the new."""
        return self.status in (
            SchemaChangeStatus.UNCHANGED,
            SchemaChangeStatus.EQUIVALENT,
            SchemaChangeStatus.COMPATIBLE,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "compatible": self.is_compatible,
            "changes": [change.to_dict() for change in self.changes],
            "before": None if self.before is None else _fp_dict(self.before),
            "after": None if self.after is None else _fp_dict(self.after),
            "error": self.error,
        }

    def explain(self) -> str:
        """Deterministic, human-readable summary: the status, then one line per change."""
        lines = [f"{self.status.value.upper()}"]
        if self.error:
            lines.append(f"  {self.error}")
        for change in self.changes:
            mark = "!" if change.breaking else "~"
            where = change.path or "<root>"
            lines.append(f"  {mark} {change.kind.value:<30} {where}: {change.detail}")
        return "\n".join(lines)


def _fp_dict(fp: SchemaFingerprint) -> dict[str, str]:
    return {"document": fp.document, "contract": fp.contract}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def diff_schemas(before: Any, after: Any) -> SchemaDiff:
    """
    Compare two JSON Schema documents. Never raises for schema content.

    *before* may itself be unreadable (unsupported, or even malformed): a
    tool that moves from an unreadable schema to a readable one is a change
    worth reporting. When *before* cannot be normalised at all, the result
    is ``INCOMPATIBLE`` with one ``SCHEMA_REPLACED`` change -- nothing can be
    proven about it.
    """
    after_fp, after_error = _readable(after)
    before_fp = _fingerprint_or_none(before)

    if after_error is not None:
        status = (
            SchemaChangeStatus.INVALID
            if isinstance(after_error, MalformedSchemaError)
            else SchemaChangeStatus.UNSUPPORTED
        )
        return SchemaDiff(status, before=before_fp, after=after_fp, error=str(after_error))

    assert after_fp is not None  # readable implies fingerprintable
    if before_fp is not None and before_fp.same_document(after_fp):
        return SchemaDiff(SchemaChangeStatus.UNCHANGED, before=before_fp, after=after_fp)
    if before_fp is not None and before_fp.same_contract(after_fp):
        return SchemaDiff(SchemaChangeStatus.EQUIVALENT, before=before_fp, after=after_fp)
    if before_fp is None:
        return SchemaDiff(
            SchemaChangeStatus.INCOMPATIBLE,
            changes=(
                SchemaChange(
                    ChangeKind.SCHEMA_REPLACED,
                    "",
                    breaking=True,
                    detail="the previous schema could not be read, so nothing about the "
                    "change can be proven",
                ),
            ),
            before=None,
            after=after_fp,
        )

    changes = compare_normalized(normalize_schema(before), normalize_schema(after))
    status = (
        SchemaChangeStatus.INCOMPATIBLE
        if any(change.breaking for change in changes)
        else SchemaChangeStatus.COMPATIBLE
    )
    return SchemaDiff(status, changes=tuple(changes), before=before_fp, after=after_fp)


def _readable(schema: Any) -> tuple[SchemaFingerprint | None, JSONSchemaError | None]:
    """Fingerprint *schema* and check StateGuard can extract a contract from it."""
    try:
        fp = fingerprint_schema(schema)
    except JSONSchemaError as exc:
        return None, exc
    try:
        with warnings.catch_warnings():
            # Loosening warnings are about validation, not about readability.
            warnings.simplefilter("ignore", SchemaFeatureWarning)
            JSONSchemaExtractor().extract(schema)
    except JSONSchemaError as exc:
        return fp, exc
    return fp, None


def _fingerprint_or_none(schema: Any) -> SchemaFingerprint | None:
    try:
        return fingerprint_schema(schema)
    except JSONSchemaError:
        return None


# ---------------------------------------------------------------------------
# Structural comparison
# ---------------------------------------------------------------------------

_Node = Mapping[str, Any]
_Out = list[SchemaChange]

#: Keywords that set a lower bound: raising one can reject a valid call.
_LOWER_BOUNDS = ("minimum", "exclusiveMinimum", "minLength", "minItems")
#: Keywords that set an upper bound: lowering one can reject a valid call.
_UPPER_BOUNDS = ("maximum", "exclusiveMaximum", "maxLength", "maxItems")
#: Keywords whose any change, or addition, can reject a valid call.
_OPAQUE_CONSTRAINTS = ("pattern", "format")

#: Keywords whose *removal* can reject a value: they decide which values a
#: sibling keyword applies to, and without them that stricter sibling
#: ('additionalProperties', 'items') covers more.
_REMOVAL_CAN_RESTRICT = frozenset({"patternProperties", "prefixItems"})

#: Keywords handled by a dedicated rule; everything else is generic.
_HANDLED = frozenset(
    {
        "type",
        "properties",
        "required",
        "items",
        "additionalProperties",
        "enum",
        "const",
        "default",
        "anyOf",
        "oneOf",
        *_LOWER_BOUNDS,
        *_UPPER_BOUNDS,
        *_OPAQUE_CONSTRAINTS,
    }
)


def compare_normalized(before: Any, after: Any, path: str = "") -> list[SchemaChange]:
    """
    Changes between two schemas already in normal form (``normalize_schema``).

    Exposed for callers that hold normalised documents; ``diff_schemas`` is
    the entry point for raw ones.
    """
    out: list[SchemaChange] = []
    _compare(before, after, path, out)
    return out


def _compare(a: Any, b: Any, path: str, out: list[SchemaChange]) -> None:
    if a == b and _same_json(a, b):
        return
    if not isinstance(a, Mapping) or not isinstance(b, Mapping):
        out.append(
            SchemaChange(
                ChangeKind.SCHEMA_REPLACED,
                path,
                breaking=True,
                detail="a subschema was replaced by a non-object (boolean) schema or vice versa",
                before=a,
                after=b,
            )
        )
        return

    if _widened_to_union(a, b, path, out) or _narrowed_from_union(a, b, path, out):
        _compare_default(a, b, path, out)
        return

    _compare_type(a, b, path, out)
    _compare_properties(a, b, path, out)
    _compare_additional(a, b, path, out)
    _compare_items(a, b, path, out)
    _compare_enum(a, b, path, out)
    _compare_const(a, b, path, out)
    for keyword in _LOWER_BOUNDS:
        _compare_bound(a, b, keyword, path, out, lower=True)
    for keyword in _UPPER_BOUNDS:
        _compare_bound(a, b, keyword, path, out, lower=False)
    for keyword in _OPAQUE_CONSTRAINTS:
        _compare_opaque(a, b, keyword, path, out)
    _compare_default(a, b, path, out)
    _compare_branches(a, b, "anyOf", path, out)
    _compare_branches(a, b, "oneOf", path, out)
    for keyword in sorted((set(a) | set(b)) - _HANDLED):
        before, after = a.get(keyword, ABSENT), b.get(keyword, ABSENT)
        if _same_json(before, after):
            continue
        # JSON Schema assertions only ever restrict, so dropping one can only
        # accept more -- except the few whose absence hands their values to a
        # stricter sibling ('additionalProperties', 'items').
        loosened = after is ABSENT and keyword not in _REMOVAL_CAN_RESTRICT
        out.append(
            SchemaChange(
                ChangeKind.KEYWORD_CHANGED,
                path,
                breaking=not loosened,
                keyword=keyword,
                before=before,
                after=after,
                detail=(
                    f"'{keyword}' removed; it only ever restricted what was accepted"
                    if loosened
                    else f"'{keyword}' changed, and there is no rule proving it accepts "
                    f"every call it used to"
                ),
            )
        )


def _same_json(a: Any, b: Any) -> bool:
    """Equality as JSON sees it: ``True`` is not ``1``, ``ABSENT`` is only itself."""
    if a is ABSENT or b is ABSENT:
        return a is b
    return canonical_json(a) == canonical_json(b)


def _join(path: str, name: str) -> str:
    return f"{path}.{name}" if path else name


# --- type -------------------------------------------------------------------


def _types(schema: Mapping[str, Any]) -> frozenset[str] | None:
    """The declared type set, or ``None`` when ``type`` is absent (any type)."""
    declared = schema.get("type", ABSENT)
    if declared is ABSENT:
        return None
    if isinstance(declared, list):
        return frozenset(str(t) for t in declared)
    return frozenset({str(declared)})


def _accepts_type(new: frozenset[str] | None, old_type: str) -> bool:
    if new is None:
        return True
    return old_type in new or (old_type == "integer" and "number" in new)


def _compare_type(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = _types(a), _types(b)
    if before == after:
        return
    # ``before is None`` was "any type": any declared type now restricts it.
    breaking = True if before is None else not all(_accepts_type(after, t) for t in before)
    out.append(
        SchemaChange(
            ChangeKind.TYPE_CHANGED,
            path,
            breaking=breaking,
            keyword="type",
            before=a.get("type", ABSENT),
            after=b.get("type", ABSENT),
            detail=(
                f"type {_render_types(before)} -> {_render_types(after)}"
                + ("" if breaking else " (widened: every value accepted before still is)")
            ),
        )
    )


def _render_types(types: frozenset[str] | None) -> str:
    return "any" if types is None else "|".join(sorted(types))


# --- properties / required ----------------------------------------------------


def _closed(schema: Mapping[str, Any]) -> bool:
    """Undeclared keys are rejected outright."""
    return schema.get("additionalProperties", True) is False


def _open(schema: Mapping[str, Any]) -> bool:
    """Undeclared keys are accepted with any value."""
    return schema.get("additionalProperties", True) is True


def _compare_properties(a: _Node, b: _Node, path: str, out: _Out) -> None:
    props_a: Mapping[str, Any] = a.get("properties", {})
    props_b: Mapping[str, Any] = b.get("properties", {})
    req_a, req_b = set(a.get("required", [])), set(b.get("required", []))

    for name in sorted(set(props_a) | set(props_b) | req_a | req_b):
        where = _join(path, name)
        in_a, in_b = name in props_a, name in props_b

        if in_b and not in_a and name not in req_a:
            required = name in req_b
            # An open schema already accepted this key, with any value. A
            # schema for it now can reject a call that sent one -- unless the
            # old schema was closed (no valid call can have sent it) or the
            # new subschema accepts everything.
            shadowed = not _closed(a) and props_b[name] != {}
            if required:
                detail = "new required parameter: calls that omit it are now rejected"
            elif shadowed:
                detail = (
                    "new optional parameter; the schema was open, so a call that "
                    "already sent this key with a value the new schema does not "
                    "accept is now rejected"
                )
            else:
                detail = "new optional parameter"
            out.append(
                SchemaChange(
                    ChangeKind.FIELD_ADDED,
                    where,
                    breaking=required or shadowed,
                    after=props_b[name],
                    detail=detail,
                )
            )
            continue

        if in_a and not in_b and name not in req_b:
            # Without its declaration the key falls to 'additionalProperties':
            # rejected when closed, checked against it when it is a schema,
            # accepted with any value only when it is open.
            accepted = _open(b)
            out.append(
                SchemaChange(
                    ChangeKind.FIELD_REMOVED,
                    where,
                    breaking=not accepted,
                    before=props_a[name],
                    detail=(
                        "parameter removed; the schema is open, so calls that still "
                        "send it are accepted and the value is ignored"
                        if accepted
                        else "parameter removed and undeclared keys are restricted: "
                        "calls that still send it can be rejected"
                    ),
                )
            )
            if name in req_a:
                out.append(
                    SchemaChange(
                        ChangeKind.REQUIRED_REMOVED,
                        where,
                        breaking=False,
                        keyword="required",
                        detail="no longer required",
                    )
                )
            continue

        if in_a and in_b:
            _compare(props_a[name], props_b[name], where, out)
        elif in_a != in_b:
            # Declared on one side only, but required on both: the value was
            # (or becomes) constrained by a subschema; required-without-a-
            # subschema means "any value". Gaining a subschema can reject.
            out.append(
                SchemaChange(
                    ChangeKind.KEYWORD_CHANGED,
                    where,
                    breaking=in_b,
                    keyword="properties",
                    before=props_a.get(name, ABSENT),
                    after=props_b.get(name, ABSENT),
                    detail=(
                        "required parameter gained a schema for its value"
                        if in_b
                        else "required parameter lost the schema for its value (any value now)"
                    ),
                )
            )

        if name in req_b and name not in req_a:
            out.append(
                SchemaChange(
                    ChangeKind.REQUIRED_ADDED,
                    where,
                    breaking=True,
                    keyword="required",
                    detail="now required: calls that omit it are rejected",
                )
            )
        elif name in req_a and name not in req_b:
            # A required name with no subschema is still a *declared* key (as
            # StateGuard's extractor reads it: present, any value). Dropping
            # it from 'required' undeclares it, and a schema that restricts
            # undeclared keys can then reject a call that sends it.
            undeclared = name not in props_b and not _open(b)
            out.append(
                SchemaChange(
                    ChangeKind.REQUIRED_REMOVED,
                    where,
                    breaking=undeclared,
                    keyword="required",
                    detail=(
                        "no longer required, and no longer declared: undeclared keys are "
                        "restricted, so a call that still sends it can be rejected"
                        if undeclared
                        else "no longer required"
                    ),
                )
            )


def _compare_additional(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = a.get("additionalProperties", True), b.get("additionalProperties", True)
    if _same_json(before, after):
        return
    if after is True:
        breaking, detail = False, "undeclared parameters are now accepted"
    elif after is False:
        breaking, detail = True, "undeclared parameters are now rejected"
    else:
        breaking, detail = True, "the schema for undeclared parameters changed"
    out.append(
        SchemaChange(
            ChangeKind.ADDITIONAL_PROPERTIES_CHANGED,
            _join(path, "*"),
            breaking=breaking,
            keyword="additionalProperties",
            before=before,
            after=after,
            detail=detail,
        )
    )


# --- items --------------------------------------------------------------------


def _compare_items(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = a.get("items", ABSENT), b.get("items", ABSENT)
    if _same_json(before, after):
        return
    where = f"{path}[]"
    if isinstance(before, Mapping) and isinstance(after, Mapping):
        _compare(before, after, where, out)
        return
    if after is ABSENT:
        breaking, detail = False, "array items are no longer constrained"
    else:
        breaking, detail = True, "the array item schema changed shape"
    out.append(
        SchemaChange(
            ChangeKind.ITEMS_CHANGED,
            where,
            breaking=breaking,
            keyword="items",
            before=before,
            after=after,
            detail=detail,
        )
    )


# --- enum / const / default ---------------------------------------------------


def _compare_enum(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = a.get("enum", ABSENT), b.get("enum", ABSENT)
    if _same_json(before, after):
        return
    if before is ABSENT:
        breaking, detail = True, f"now restricted to {len(after)} value(s)"
    elif after is ABSENT:
        breaking, detail = False, "no longer restricted to a fixed set of values"
    else:
        old = {canonical_json(v): v for v in before}
        new = {canonical_json(v): v for v in after}
        added = [new[k] for k in sorted(set(new) - set(old))]
        removed = [old[k] for k in sorted(set(old) - set(new))]
        breaking = bool(removed)
        parts = []
        if added:
            parts.append(f"added {added!r}")
        if removed:
            parts.append(f"removed {removed!r} (calls using them are now rejected)")
        detail = "; ".join(parts)
    out.append(
        SchemaChange(
            ChangeKind.ENUM_CHANGED,
            path,
            breaking=breaking,
            keyword="enum",
            before=before,
            after=after,
            detail=detail,
        )
    )


def _compare_const(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = a.get("const", ABSENT), b.get("const", ABSENT)
    if _same_json(before, after):
        return
    out.append(
        SchemaChange(
            ChangeKind.CONST_CHANGED,
            path,
            breaking=after is not ABSENT,
            keyword="const",
            before=before,
            after=after,
            detail="no longer fixed to one value" if after is ABSENT else "fixed value changed",
        )
    )


def _compare_default(a: _Node, b: _Node, path: str, out: _Out) -> None:
    before, after = a.get("default", ABSENT), b.get("default", ABSENT)
    if _same_json(before, after):
        return
    if before is ABSENT:
        detail = "default added: an omitted value will now be filled"
    elif after is ABSENT:
        detail = "default removed: an omitted value will no longer be filled"
    else:
        detail = "default changed: an omitted value now means something else"
    out.append(
        SchemaChange(
            ChangeKind.DEFAULT_CHANGED,
            path,
            # Acceptance is unchanged; behaviour is not -- hence the detail.
            breaking=False,
            keyword="default",
            before=before,
            after=after,
            detail=detail,
        )
    )


# --- bounds and opaque constraints --------------------------------------------


def _compare_bound(
    a: Mapping[str, Any],
    b: Mapping[str, Any],
    keyword: str,
    path: str,
    out: list[SchemaChange],
    *,
    lower: bool,
) -> None:
    before, after = a.get(keyword, ABSENT), b.get(keyword, ABSENT)
    if _same_json(before, after):
        return
    if after is ABSENT:
        breaking, verb = False, "removed"
    elif before is ABSENT:
        breaking, verb = True, "added"
    elif _is_number(before) and _is_number(after):
        tightened = after > before if lower else after < before
        breaking, verb = tightened, "tightened" if tightened else "loosened"
    else:
        breaking, verb = True, "changed"
    out.append(
        SchemaChange(
            ChangeKind.CONSTRAINT_CHANGED,
            path,
            breaking=breaking,
            keyword=keyword,
            before=before,
            after=after,
            detail=f"'{keyword}' {verb}: {_render(before)} -> {_render(after)}",
        )
    )


def _compare_opaque(
    a: Mapping[str, Any], b: Mapping[str, Any], keyword: str, path: str, out: list[SchemaChange]
) -> None:
    before, after = a.get(keyword, ABSENT), b.get(keyword, ABSENT)
    if _same_json(before, after):
        return
    removed = after is ABSENT
    out.append(
        SchemaChange(
            ChangeKind.CONSTRAINT_CHANGED,
            path,
            breaking=not removed,
            keyword=keyword,
            before=before,
            after=after,
            detail=(
                f"'{keyword}' removed"
                if removed
                else f"'{keyword}' {'added' if before is ABSENT else 'changed'}; whether it "
                f"accepts every value it used to cannot be decided structurally"
            ),
        )
    )


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _render(value: Any) -> str:
    return "absent" if value is ABSENT else repr(value)


# --- unions -------------------------------------------------------------------


def _compare_branches(
    a: Mapping[str, Any], b: Mapping[str, Any], keyword: str, path: str, out: list[SchemaChange]
) -> None:
    before, after = a.get(keyword, ABSENT), b.get(keyword, ABSENT)
    if _same_json(before, after):
        return
    old = {canonical_json(x) for x in before} if isinstance(before, list) else set()
    new = {canonical_json(x) for x in after} if isinstance(after, list) else set()
    added, removed = len(new - old), len(old - new)
    if keyword == "anyOf" and before is not ABSENT and after is not ABSENT:
        # anyOf accepts what any branch accepts: adding branches only widens.
        breaking = bool(removed)
    elif after is ABSENT and keyword == "anyOf":
        breaking = False  # the constraint is gone
    else:
        # A new anyOf restricts; any oneOf change can make a value match
        # zero branches or two, either of which rejects it.
        breaking = True
    out.append(
        SchemaChange(
            ChangeKind.UNION_CHANGED,
            path,
            breaking=breaking,
            keyword=keyword,
            before=before,
            after=after,
            detail=f"'{keyword}' changed: {added} branch(es) added, {removed} removed",
        )
    )


def _core(schema: Mapping[str, Any]) -> dict[str, Any]:
    """*schema* without ``default``, which never affects acceptance."""
    return {key: value for key, value in schema.items() if key != "default"}


def _widened_to_union(a: _Node, b: _Node, path: str, out: _Out) -> bool:
    """
    ``X`` -> ``anyOf: [X, ...]``: the common way a parameter becomes nullable
    or gains an alternative type. Every value ``X`` accepted, the union does.
    """
    core_a, core_b = _core(a), _core(b)
    if "anyOf" in core_a or set(core_b) != {"anyOf"}:
        return False
    branches = {canonical_json(x) for x in core_b["anyOf"]}
    if canonical_json(core_a) not in branches:
        return False
    out.append(
        SchemaChange(
            ChangeKind.UNION_CHANGED,
            path,
            breaking=False,
            keyword="anyOf",
            before=core_a,
            after=core_b["anyOf"],
            detail=f"widened to anyOf with {len(branches) - 1} additional branch(es); "
            f"every value accepted before still is",
        )
    )
    return True


def _narrowed_from_union(a: _Node, b: _Node, path: str, out: _Out) -> bool:
    """The reverse: ``anyOf: [X, ...]`` -> ``X``. Values only other branches accepted are lost."""
    core_a, core_b = _core(a), _core(b)
    if "anyOf" in core_b or set(core_a) != {"anyOf"}:
        return False
    branches = {canonical_json(x) for x in core_a["anyOf"]}
    if canonical_json(core_b) not in branches:
        return False
    out.append(
        SchemaChange(
            ChangeKind.UNION_CHANGED,
            path,
            breaking=len(branches) > 1,
            keyword="anyOf",
            before=core_a["anyOf"],
            after=core_b,
            detail=f"narrowed from anyOf to one of its branches; {len(branches) - 1} "
            f"branch(es) no longer accepted",
        )
    )
    return True

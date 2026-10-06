"""
Schema identity: canonical form, normalisation, and fingerprints.

Two questions get asked about a schema that arrives over the wire, and they
need different answers:

* **Is this the same document I saw before?** -- byte-for-byte modulo JSON
  object key order. Cheap, exact, and right for a cache: two documents that
  serialise identically can share everything derived from them.
* **Is this the same contract?** -- would it accept and reject exactly the
  same arguments, and repair them the same way? A changed ``description``,
  a reordered ``required`` or ``enum``, or a definition moved behind a
  ``$ref`` changes the document and not the contract. A changed ``type``,
  bound, ``enum`` member or ``default`` changes both.

``SchemaFingerprint`` carries one hash for each. Both are SHA-256 over a
canonical JSON rendering (sorted keys, no insignificant whitespace), so they
are deterministic across processes, platforms and Python versions, and need
nothing outside the standard library.

What normalisation does
-----------------------
``normalize_schema`` produces the contract-relevant form of a document:

* every ``$ref`` is resolved and inlined, through the same ``RefResolver``
  extraction uses -- so the depth, expansion and cycle bounds apply here too;
* annotations are dropped: ``title``, ``description``, ``examples``,
  ``$comment``, ``deprecated``, ``readOnly``, ``writeOnly``, ``$schema``,
  ``$id``, ``$defs`` / ``definitions`` (once inlined) and ``x-*`` extensions;
* order that carries no meaning is fixed: ``required`` and ``type`` lists are
  sorted and de-duplicated, ``enum`` members and ``anyOf`` / ``oneOf``
  branches are sorted by their canonical rendering;
* spellings that mean the same thing are unified: an empty ``required`` or
  ``properties`` is dropped, ``additionalProperties: true`` is dropped,
  ``"type": ["string"]`` becomes ``"type": "string"``, and an integral float
  (``1.0``) becomes the integer it equals, as JSON Schema treats them.

Every other keyword -- including ones StateGuard does not validate, like
``format`` -- is kept, so the contract fingerprint errs towards reporting a
change. It never claims two schemas are the same contract when a keyword it
does not understand differs between them.

What it does not do
-------------------
It does not prove semantic equivalence. ``{"type": ["string", "null"]}`` and
``{"anyOf": [{"type": "string"}, {"type": "null"}]}`` accept the same values
and normalise differently. Normalisation is a structural canonical form, not
a JSON Schema theorem prover: equal fingerprints mean equal contracts, and
different fingerprints mean *possibly* different contracts -- which is what
``stateguard.adapters.jsonschema.diff`` is for.

Zero external dependencies.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    JSONSchemaError,
    MalformedSchemaError,
    UnsupportedSchemaError,
)
from stateguard.adapters.jsonschema.refs import RefResolver

__all__ = [
    "ANNOTATION_KEYWORDS",
    "SchemaFingerprint",
    "canonical_json",
    "document_fingerprint",
    "fingerprint_schema",
    "normalize_schema",
]


#: Keywords that describe a schema without constraining what it accepts.
#: Dropped by normalisation, so changing one changes the document
#: fingerprint and not the contract fingerprint.
ANNOTATION_KEYWORDS = frozenset(
    {
        "$comment",
        "$defs",
        "$id",
        "$schema",
        "definitions",
        "deprecated",
        "description",
        "examples",
        "readOnly",
        "title",
        "writeOnly",
    }
)

#: Keywords whose value is a single subschema.
_SUBSCHEMA_KEYWORDS = frozenset({"additionalProperties", "items"})

#: Keywords whose value is an unordered list of subschemas.
_BRANCH_KEYWORDS = frozenset({"anyOf", "oneOf"})


# ---------------------------------------------------------------------------
# Canonical JSON
# ---------------------------------------------------------------------------


def canonical_json(value: Any) -> str:
    """
    Render *value* as canonical JSON: sorted keys, no spaces, no NaN.

    Raises ``TypeError`` / ``ValueError`` for anything JSON cannot represent
    (a non-string key, a set, NaN) and ``RecursionError`` for nesting deeper
    than the interpreter stack -- callers that face untrusted input use
    ``document_fingerprint``, which maps all three onto schema errors.
    """
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def document_fingerprint(schema: Any) -> str:
    """
    SHA-256 of *schema*'s canonical JSON: identity of the exact document.

    Insensitive to object key order and nothing else.

    Raises
    ------
    MalformedSchemaError
        *schema* is not JSON (a non-string key, NaN, a non-JSON type).
    UnsupportedSchemaError
        *schema* nests too deeply to serialise.
    """
    try:
        return _sha256(canonical_json(schema))
    except RecursionError as exc:
        raise UnsupportedSchemaError(
            "Schema nests too deeply to fingerprint; it is refused rather than walked."
        ) from exc
    except (TypeError, ValueError) as exc:
        raise MalformedSchemaError(f"Schema is not a JSON document: {exc}") from exc


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def normalize_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """
    The contract-relevant canonical form of *schema*. See the module docstring.

    Walks through ``RefResolver``, so a recursive, too-deep or exponentially
    expanding schema raises ``SchemaReferenceError`` here exactly as it does
    in extraction, and a ``$ref`` to nothing raises
    ``MalformedReferenceError``. Does not otherwise validate the document:
    an unsupported keyword is carried through rather than refused, so two
    schemas StateGuard cannot extract can still be compared.
    """
    if not isinstance(schema, Mapping):
        raise MalformedSchemaError(f"A JSON Schema must be an object, got {type(schema).__name__}.")
    resolver = RefResolver(schema)
    try:
        with resolver.resolved(schema) as root:
            return _normalize(root, resolver)
    except RecursionError as exc:  # a deep non-schema value, e.g. a default
        raise UnsupportedSchemaError(
            "Schema nests too deeply to normalise; it is refused rather than walked."
        ) from exc


def _normalize(node: Mapping[str, Any], resolver: RefResolver) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in node.items():
        if not isinstance(key, str):
            raise MalformedSchemaError(f"Schema keys must be strings, got {key!r}.")
        if key in ANNOTATION_KEYWORDS or key.startswith("x-") or key == "$ref":
            continue

        if key == "properties" and isinstance(value, Mapping):
            if value:
                out[key] = {name: _subschema(sub, resolver) for name, sub in value.items()}
        elif key in _SUBSCHEMA_KEYWORDS and isinstance(value, Mapping):
            out[key] = _subschema(value, resolver)
        elif key == "additionalProperties" and value is True:
            continue  # the default
        elif key == "items" and _is_list(value):
            out[key] = [_subschema(item, resolver) for item in value]  # tuple form: ordered
        elif key in _BRANCH_KEYWORDS and _is_list(value):
            out[key] = _sorted_unique([_subschema(branch, resolver) for branch in value])
        elif key == "type" and _is_list(value):
            names = sorted({str(name) for name in value})
            out[key] = names[0] if len(names) == 1 else names
        elif key == "required" and _is_list(value):
            if value:
                out[key] = sorted({str(name) for name in value})
        elif key == "enum" and _is_list(value):
            out[key] = _sorted_unique([_value(member) for member in value])
        else:
            out[key] = _value(value)
    return out


def _subschema(schema: Any, resolver: RefResolver) -> Any:
    if not isinstance(schema, Mapping):
        # A boolean subschema. Kept as-is rather than refused: normalisation
        # compares documents, and extraction is where it gets refused.
        return _value(schema)
    with resolver.resolved(schema) as target:
        return _normalize(target, resolver)


def _value(value: Any) -> Any:
    """A JSON value in canonical form: integral floats as integers, recursively."""
    if isinstance(value, bool) or value is None or isinstance(value, (int, str)):
        return value
    if isinstance(value, float):
        if math.isfinite(value) and value.is_integer():
            return int(value)
        return value
    if isinstance(value, Mapping):
        return {key: _value(item) for key, item in value.items()}
    if _is_list(value):
        return [_value(item) for item in value]
    return value  # canonical_json rejects it if it is not JSON


def _sorted_unique(items: list[Any]) -> list[Any]:
    by_rendering = {canonical_json(item): item for item in items}
    return [by_rendering[rendering] for rendering in sorted(by_rendering)]


def _is_list(value: Any) -> bool:
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray))


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchemaFingerprint:
    """
    Two identities for one schema.

    Attributes
    ----------
    document:
        SHA-256 of the canonical document. Equal exactly when the documents
        are equal up to object key order.
    contract:
        SHA-256 of the canonical *normalised* document. Equal when the
        schemas accept, reject and repair the same arguments as far as
        structure can show -- annotations, meaningless ordering and ``$ref``
        layout aside. Different means *possibly* different; ask
        ``diff_schemas`` what changed.
    """

    document: str
    contract: str

    @property
    def short(self) -> str:
        """The first 12 hex digits of ``contract``, for log lines."""
        return self.contract[:12]

    def same_document(self, other: SchemaFingerprint) -> bool:
        return self.document == other.document

    def same_contract(self, other: SchemaFingerprint) -> bool:
        return self.contract == other.contract


def fingerprint_schema(schema: Mapping[str, Any]) -> SchemaFingerprint:
    """
    Fingerprint *schema* both ways. Deterministic and network-free.

    Raises what ``document_fingerprint`` and ``normalize_schema`` raise: a
    ``JSONSchemaError`` for a document that is not JSON, nests too deeply,
    expands too far, recurses, or references nothing. It does *not* require
    the schema to be within StateGuard's supported subset.
    """
    document = document_fingerprint(schema)
    try:
        contract = _sha256(canonical_json(normalize_schema(schema)))
    except RecursionError as exc:  # pragma: no cover -- normalisation is depth-bounded
        raise UnsupportedSchemaError("Schema nests too deeply to fingerprint.") from exc
    except JSONSchemaError:
        raise
    except (TypeError, ValueError) as exc:
        raise MalformedSchemaError(f"Schema is not a JSON document: {exc}") from exc
    return SchemaFingerprint(document=document, contract=contract)

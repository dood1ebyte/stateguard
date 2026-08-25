"""
``$ref`` / ``$defs`` resolution, with cycle detection that cannot be skipped.

Pydantic-backed MCP servers emit ``$ref`` constantly -- every nested model
becomes a ``#/$defs/Name`` pointer -- so an adapter that does not resolve
references cannot read the majority of real tool schemas.

Why this is its own module
--------------------------
Reference resolution is the one part of extraction that can fail to
*terminate*.  A schema may legally describe a recursive structure::

    {"$defs": {"Node": {"type": "object",
                        "properties": {"child": {"$ref": "#/$defs/Node"}}}}}

Expanding that inline has no fixed point.  Left unguarded it is not a wrong
answer, it is a hung process -- and the schema arrives over the network from
a third party, so the input is untrusted.  Keeping resolution here means the
guard lives at the single choke point every reference passes through, rather
than being re-implemented (and eventually forgotten) at each recursive call
site in the extractor.

Two different cycles
--------------------
Both must be caught, and they are not the same shape:

* **Reference chain cycle** -- ``A -> B -> A`` where each is a bare
  ``$ref``.  Caught inside a single ``_follow`` call by its own ``seen`` set;
  resolution itself would spin.
* **Structural recursion** -- ``Node.child -> Node``.  Each individual
  resolution terminates in one hop; it is the extractor's *descent* that
  never ends.  Caught by ``_active``, a stack of the pointers currently being
  expanded, which is why resolution is exposed as a context manager rather
  than a plain function.

Local references only
---------------------
A ``$ref`` that is not a same-document fragment is refused rather than
fetched.  Following ``http://`` here would turn schema extraction into an
outbound network request against a URL chosen by whoever supplied the
schema -- an SSRF primitive reachable from a tool definition.  Refusing is
not a limitation to be lifted later; it is the correct behaviour.

Zero external dependencies -- part of the adapter's zero-dep guarantee
(``MCP_ADAPTER_PLAN.md`` §4).
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any

from stateguard.adapters.jsonschema.errors import (
    SchemaReferenceError,
    UnsupportedSchemaError,
)

__all__ = ["RefResolver"]


class RefResolver:
    """
    Resolves same-document ``$ref`` pointers against one root schema.

    Parameters
    ----------
    root:
        The complete schema document.  Pointers are resolved against this,
        so it must be the whole thing (the object carrying ``$defs``), not a
        subschema.

    Not thread-safe and not reusable across concurrent extractions: the
    active-pointer stack is per-traversal state.  Construct one per
    ``extract_contract`` call, which is what ``JSONSchemaExtractor`` does.
    """

    def __init__(self, root: Mapping[str, Any]) -> None:
        self._root = root
        self._active: list[str] = []

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @contextmanager
    def resolved(self, schema: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
        """
        Yield *schema* with its ``$ref`` chain followed, guarding recursion.

        Use this around every recursive descent into a subschema::

            with resolver.resolved(subschema) as target:
                ...walk target...

        The pointers traversed are held on the active stack for the duration
        of the ``with`` block, so re-entering any of them further down raises
        ``SchemaReferenceError`` instead of recursing forever.  They are
        popped on exit, which is what allows the *same* definition to be
        referenced twice as siblings -- two fields of type ``Address`` are
        perfectly legal and must not be mistaken for a cycle.

        A schema with no ``$ref`` is yielded unchanged and pushes nothing.
        """
        if not isinstance(schema, Mapping):
            raise UnsupportedSchemaError(
                f"Expected a schema object, got {type(schema).__name__}. "
                f"Boolean schemas (`true` / `false`) are not supported."
            )

        target, pointers = self._follow(schema)

        for pointer in pointers:
            if pointer in self._active:
                chain = " -> ".join("#" + p for p in [*self._active, pointer])
                raise SchemaReferenceError(
                    f"Recursive schema: '#{pointer}' is already being expanded. "
                    f"Expansion chain: {chain}. "
                    f"A recursive schema has no finite expansion, so StateGuard "
                    f"cannot describe it as a contract."
                )

        depth = len(self._active)
        self._active.extend(pointers)
        try:
            yield target
        finally:
            del self._active[depth:]

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _follow(self, schema: Mapping[str, Any]) -> tuple[Mapping[str, Any], list[str]]:
        """
        Follow a ``$ref`` chain to its target.

        Returns the resolved schema and every pointer traversed on the way,
        in order, so the caller can push them all onto the active stack.

        ``$ref`` siblings are merged over the target, with the siblings
        winning.  Draft 2019-09 onward makes ``$ref`` an ordinary keyword
        that composes with its neighbours, and this is how Pydantic attaches
        a ``description`` or a ``default`` to a ``$ref``'d field.  Under
        draft-07 the siblings would be ignored instead; merging is the more
        useful reading and never loses information the stricter one keeps.
        """
        pointers: list[str] = []
        seen: set[str] = set()
        current: Mapping[str, Any] = schema

        while "$ref" in current:
            raw_ref = current["$ref"]
            if not isinstance(raw_ref, str):
                raise SchemaReferenceError(
                    f"'$ref' must be a string, got {type(raw_ref).__name__}: {raw_ref!r}"
                )

            pointer = self._as_local_pointer(raw_ref)
            if pointer in seen:
                raise SchemaReferenceError(
                    f"Circular '$ref' chain: '#{pointer}' is reached from itself via "
                    f"{' -> '.join('#' + p for p in [*pointers, pointer])}."
                )
            seen.add(pointer)
            pointers.append(pointer)

            target = self._dereference(pointer, raw_ref)
            siblings = {key: value for key, value in current.items() if key != "$ref"}
            current = {**target, **siblings} if siblings else target

        return current, pointers

    @staticmethod
    def _as_local_pointer(ref: str) -> str:
        """
        Validate *ref* as a same-document reference and return its pointer.

        ``"#"`` (the whole document) yields ``""``; ``"#/$defs/A"`` yields
        ``"/$defs/A"``.
        """
        if not ref.startswith("#"):
            raise SchemaReferenceError(
                f"Only same-document references are supported, got {ref!r}. "
                f"Remote references are refused rather than fetched: resolving one "
                f"would make schema extraction issue a network request to a URL "
                f"supplied by whoever wrote the schema."
            )

        fragment = ref[1:]
        if fragment and not fragment.startswith("/"):
            raise SchemaReferenceError(
                f"Plain-name fragments ('$anchor') are not supported, got {ref!r}. "
                f"Use a JSON Pointer such as '#/$defs/Name'."
            )
        return fragment

    def _dereference(self, pointer: str, raw_ref: str) -> Mapping[str, Any]:
        """
        Walk *pointer* through the root document (RFC 6901 JSON Pointer).

        *raw_ref* is carried only so failures can quote what the schema
        actually said rather than the normalised form.
        """
        if pointer == "":
            return self._root

        node: Any = self._root
        for raw_token in pointer.split("/")[1:]:
            # RFC 6901: '~1' becomes '/' first, then '~0' becomes '~'.
            # Order matters -- reversing it would turn '~01' into '/'
            # instead of the '~1' the author wrote.
            token = raw_token.replace("~1", "/").replace("~0", "~")
            node = self._descend(node, token, raw_ref)

        if not isinstance(node, Mapping):
            raise SchemaReferenceError(
                f"Reference {raw_ref!r} resolves to a {type(node).__name__}, not a schema object."
            )
        return node

    @staticmethod
    def _descend(node: Any, token: str, raw_ref: str) -> Any:
        """Take one JSON Pointer step through a mapping or a sequence."""
        if isinstance(node, Mapping):
            if token not in node:
                raise SchemaReferenceError(
                    f"Reference {raw_ref!r} does not resolve: no '{token}' in {sorted(node)[:8]!r}."
                )
            return node[token]

        if isinstance(node, Sequence) and not isinstance(node, (str, bytes)):
            try:
                index = int(token)
            except ValueError:
                raise SchemaReferenceError(
                    f"Reference {raw_ref!r} does not resolve: '{token}' is not a "
                    f"valid index into a {len(node)}-element array."
                ) from None
            if not 0 <= index < len(node):
                raise SchemaReferenceError(
                    f"Reference {raw_ref!r} does not resolve: index {index} is out "
                    f"of range for a {len(node)}-element array."
                )
            return node[index]

        raise SchemaReferenceError(
            f"Reference {raw_ref!r} does not resolve: cannot descend into a "
            f"{type(node).__name__} looking for '{token}'."
        )

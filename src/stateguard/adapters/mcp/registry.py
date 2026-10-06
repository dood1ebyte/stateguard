"""
``ToolRegistry`` -- the in-process record of which MCP tools exist and what
their contracts are, and the entry point for validating calls to them.

Why a registry, when ``repair_tool_call`` takes a tool definition directly
---------------------------------------------------------------------------
Because in a running proxy the definition is not something the caller holds
per call -- it is state, learned from ``tools/list`` and changed by later
``tools/list`` responses. Every question that matters at that layer is about
the state, not one call:

* Which definition is current for this tool? (Never a stale one.)
* Did the server just change a tool's contract -- and could calls that
  worked yesterday fail today?
* Did it change a tool's *description* without touching its schema? (An
  agent reads descriptions; a definition that changes after approval is a
  known MCP attack pattern, so it is reported even when the schema is not.)
* What happens when a call names a tool that is not there, or whose schema
  StateGuard cannot read?

Identity
--------
A tool is identified by ``(server, name)``. Two MCP servers can and do expose
tools with the same name, so the name alone is not an identity once a
client talks to more than one server. ``server`` is any string the caller
chooses -- a configured server name, a connection id -- and ``None`` is its
own namespace, the natural one for a proxy in front of a single server.
Lookups never guess across servers: ``get("search")`` is the ``search`` with
no server, never "whichever server has one".

Stale schemas are never used
----------------------------
Registering a new definition replaces the old one, unconditionally -- even
when the new one is unreadable. A tool whose current schema cannot be read
is kept as ``INVALID`` / ``UNSUPPORTED`` and ``process_call`` raises
``ToolSchemaUnusableError`` for it, rather than validating against the
previous schema the server no longer serves.

What this is not
----------------
Not persistent, not shared between processes, not a service. It holds
definitions in memory for one adapter instance; a restart rebuilds it from
``tools/list``. No network, no MCP SDK.

Thread-safe: every read and write of the registry's state happens under one
lock, entries are immutable snapshots, and ``process_call`` validates outside
the lock against the snapshot it took. One caveat: recording extraction
warnings uses ``warnings.catch_warnings``, which on most Python builds
adjusts process-global warning filters for its duration -- a warning raised
by another thread during a ``register`` may be captured here instead of
reported there.

Zero external dependencies.
"""

from __future__ import annotations

import copy
import threading
import warnings
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from stateguard.adapters.jsonschema.diff import SchemaChangeStatus, SchemaDiff, diff_schemas
from stateguard.adapters.jsonschema.errors import (
    JSONSchemaError,
    MalformedSchemaError,
    SchemaFeatureWarning,
)
from stateguard.adapters.jsonschema.identity import (
    SchemaFingerprint,
    canonical_json,
    document_fingerprint,
    fingerprint_schema,
)
from stateguard.adapters.mcp.adapter import MCPToolAdapter, split_tool_definition
from stateguard.adapters.mcp.errors import (
    InvalidToolDefinitionError,
    ToolNotFoundError,
    ToolSchemaUnusableError,
)
from stateguard.adapters.mcp.outcomes import ToolCallOutcome, repair_tool_call
from stateguard.guard import ContractGuard

__all__ = [
    "RegisteredTool",
    "SyncReport",
    "ToolChange",
    "ToolChangeKind",
    "ToolKey",
    "ToolRegistry",
    "ToolState",
]


# ---------------------------------------------------------------------------
# Identity and state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolKey:
    """``(server, name)``. ``server=None`` is the unnamed server's namespace."""

    name: str
    server: str | None = None

    def __str__(self) -> str:
        return self.name if self.server is None else f"{self.server}/{self.name}"

    @property
    def sort_key(self) -> tuple[str, str, str]:
        # None sorts first, and never collides with a server called "".
        return ("0" if self.server is None else "1", self.server or "", self.name)


class ToolState(StrEnum):
    """Whether calls to a registered tool can be validated."""

    READY = "ready"
    """The schema extracted; calls are validated and repaired against it."""

    INVALID = "invalid"
    """The definition or its schema is malformed -- not valid MCP / JSON Schema."""

    UNSUPPORTED = "unsupported"
    """Valid JSON Schema that StateGuard cannot represent (``allOf``, recursion, ...)."""


#: Top-level tool-definition fields an agent reads. A change to one of these
#: is reported even when the input schema is untouched.
_DESCRIPTIVE_FIELDS = ("title", "description")


@dataclass(frozen=True)
class RegisteredTool:
    """
    One tool's current definition and everything derived from it. Immutable.

    Attributes
    ----------
    key:
        ``(server, name)``.
    definition:
        A deep copy of the definition as registered. Treat as read-only.
    input_schema:
        Its ``inputSchema`` (``None`` when the definition has none usable).
    state:
        ``READY``, ``INVALID`` or ``UNSUPPORTED``.
    error:
        Why the tool is not ``READY``; ``None`` when it is.
    fingerprint:
        The input schema's document and contract fingerprints, when the
        schema could be fingerprinted.
    definition_fingerprint:
        Document fingerprint of the *whole* definition -- name, description,
        annotations and all. Changes whenever anything about the tool does.
    warnings:
        ``SchemaFeatureWarning`` messages from extraction: places where
        StateGuard validates this tool more loosely than its schema asks
        (an exclusive bound, a schema-valued ``additionalProperties``, ...).
    revision:
        1 when first registered; incremented each time the definition
        changes (``definition_fingerprint``), unchanged by a re-registration
        of the same definition.
    registered_at / updated_at:
        When the tool first appeared, and when its definition last changed.
    """

    key: ToolKey
    definition: Mapping[str, Any]
    input_schema: Mapping[str, Any] | None
    state: ToolState
    error: JSONSchemaError | None
    fingerprint: SchemaFingerprint | None
    definition_fingerprint: str | None
    warnings: tuple[str, ...]
    revision: int
    registered_at: datetime
    updated_at: datetime

    @property
    def name(self) -> str:
        return self.key.name

    @property
    def server(self) -> str | None:
        return self.key.server

    @property
    def usable(self) -> bool:
        """Calls to this tool can be validated."""
        return self.state is ToolState.READY

    @property
    def description(self) -> str | None:
        value = self.definition.get("description")
        return value if isinstance(value, str) else None

    @property
    def annotations(self) -> Mapping[str, Any] | None:
        """
        The tool's MCP ``annotations`` (``readOnlyHint``, ``destructiveHint``,
        ...), as the server declared them. Hints, not guarantees -- a server
        can claim anything -- so StateGuard records them and decides nothing
        from them.
        """
        value = self.definition.get("annotations")
        return value if isinstance(value, Mapping) else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.key.name,
            "server": self.key.server,
            "state": self.state.value,
            "error": None if self.error is None else str(self.error),
            "fingerprint": None
            if self.fingerprint is None
            else {"document": self.fingerprint.document, "contract": self.fingerprint.contract},
            "definition_fingerprint": self.definition_fingerprint,
            "warnings": list(self.warnings),
            "revision": self.revision,
            "registered_at": self.registered_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }


# ---------------------------------------------------------------------------
# Change records
# ---------------------------------------------------------------------------


class ToolChangeKind(StrEnum):
    """What happened to one tool in a ``register``, ``sync`` or ``remove``."""

    ADDED = "added"
    REMOVED = "removed"
    UPDATED = "updated"
    UNCHANGED = "unchanged"


@dataclass(frozen=True)
class ToolChange:
    """
    What changed about one tool, and whether it could break calls.

    Attributes
    ----------
    key:
        Which tool.
    kind:
        ``ADDED``, ``REMOVED``, ``UPDATED`` or ``UNCHANGED``.
    schema:
        The structural diff of the input schema (``UPDATED`` and
        ``UNCHANGED``), or ``None`` (``ADDED``, ``REMOVED``).
    changed_fields:
        Top-level definition fields that differ, sorted -- ``inputSchema``,
        ``description``, ``annotations``, ``title``, ``outputSchema``, ...
    previous / current:
        The entry before and after; ``None`` on the side where there is none.
    """

    key: ToolKey
    kind: ToolChangeKind
    schema: SchemaDiff | None = None
    changed_fields: tuple[str, ...] = ()
    previous: RegisteredTool | None = None
    current: RegisteredTool | None = None

    @property
    def description_changed(self) -> bool:
        """The text an agent reads about the tool changed (``title`` / ``description``)."""
        return any(name in self.changed_fields for name in _DESCRIPTIVE_FIELDS)

    @property
    def breaking(self) -> bool:
        """
        Calls that worked before could fail now: the tool was removed, its
        schema became unreadable, or its contract changed incompatibly.
        """
        if self.kind is ToolChangeKind.REMOVED:
            return True
        if self.schema is None:
            return False
        return not self.schema.is_compatible

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.key.name,
            "server": self.key.server,
            "kind": self.kind.value,
            "breaking": self.breaking,
            "description_changed": self.description_changed,
            "changed_fields": list(self.changed_fields),
            "schema": None if self.schema is None else self.schema.to_dict(),
            "state": None if self.current is None else self.current.state.value,
            "revision": None if self.current is None else self.current.revision,
        }

    def explain(self) -> str:
        head = f"{self.kind.value.upper():<9} {self.key}"
        if self.current is not None and not self.current.usable:
            head += f"  [{self.current.state.value}]"
        if self.breaking:
            head += "  BREAKING"
        lines = [head]
        if self.description_changed:
            lines.append("  description changed: what an agent reads about this tool differs")
        other = [f for f in self.changed_fields if f not in (*_DESCRIPTIVE_FIELDS, "inputSchema")]
        if other:
            lines.append(f"  other fields changed: {', '.join(other)}")
        if self.schema is not None and self.schema.status is not SchemaChangeStatus.UNCHANGED:
            lines.extend(f"  {line}" for line in self.schema.explain().splitlines())
        return "\n".join(lines)


@dataclass(frozen=True)
class SyncReport:
    """
    The result of ``ToolRegistry.sync``: one ``ToolChange`` per tool touched.

    ``changes`` is sorted by tool key, so the same listing always produces
    the same report.
    """

    server: str | None
    changes: tuple[ToolChange, ...] = field(default_factory=tuple)

    def _of(self, kind: ToolChangeKind) -> tuple[ToolChange, ...]:
        return tuple(c for c in self.changes if c.kind is kind)

    @property
    def added(self) -> tuple[ToolChange, ...]:
        return self._of(ToolChangeKind.ADDED)

    @property
    def removed(self) -> tuple[ToolChange, ...]:
        return self._of(ToolChangeKind.REMOVED)

    @property
    def updated(self) -> tuple[ToolChange, ...]:
        return self._of(ToolChangeKind.UPDATED)

    @property
    def unchanged(self) -> tuple[ToolChange, ...]:
        return self._of(ToolChangeKind.UNCHANGED)

    @property
    def unusable(self) -> tuple[ToolChange, ...]:
        """Tools now registered in a state whose calls cannot be validated."""
        return tuple(c for c in self.changes if c.current is not None and not c.current.usable)

    @property
    def breaking(self) -> tuple[ToolChange, ...]:
        return tuple(c for c in self.changes if c.breaking)

    def to_dict(self) -> dict[str, Any]:
        return {
            "server": self.server,
            "summary": {kind.value: len(self._of(kind)) for kind in ToolChangeKind}
            | {"unusable": len(self.unusable), "breaking": len(self.breaking)},
            "changes": [change.to_dict() for change in self.changes],
        }

    def explain(self) -> str:
        counts = ", ".join(f"{len(self._of(kind))} {kind.value}" for kind in ToolChangeKind)
        where = "" if self.server is None else f" ({self.server})"
        lines = [f"tools/list{where}: {counts}; {len(self.breaking)} breaking"]
        for change in self.changes:
            if change.kind is not ToolChangeKind.UNCHANGED or change.changed_fields:
                lines.extend(f"  {line}" for line in change.explain().splitlines())
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_ALL_SERVERS: Any = object()


class ToolRegistry:
    """
    In-process registry of MCP tool definitions, keyed by ``(server, name)``.

    Parameters
    ----------
    guard:
        The guard calls are validated with. Must be built with
        ``ContractGuard.with_mcp()`` (auto or shadow). Defaults to a new
        auto-mode one. The registry and the guard share the guard's schema
        cache, so registering a tool also warms the cache its calls hit.
    clock:
        Returns "now" as an aware ``datetime``. Defaults to UTC wall time;
        injectable so tests and replays are deterministic.

    Typical use in a proxy::

        registry = ToolRegistry(ContractGuard.with_mcp())
        report = registry.sync(tools_list_result["tools"])     # on tools/list
        outcome = registry.process_call(name, arguments)       # on tools/call
    """

    def __init__(
        self,
        guard: ContractGuard | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._guard = guard if guard is not None else ContractGuard.with_mcp()
        if not isinstance(self._guard.adapter, MCPToolAdapter):
            raise TypeError(
                "ToolRegistry needs a guard built with ContractGuard.with_mcp(); this one "
                f"uses {type(self._guard.adapter).__name__}."
            )
        self._adapter: MCPToolAdapter = self._guard.adapter
        self._clock = clock if clock is not None else (lambda: datetime.now(tz=UTC))
        self._tools: dict[ToolKey, RegisteredTool] = {}
        # What calls are validated against: a private copy per entry, so a
        # caller mutating ``RegisteredTool.definition`` -- a plain dict, for
        # compatibility with everything that reads tool definitions -- cannot
        # change the contract calls are checked against behind the
        # fingerprint's back.
        self._call_definitions: dict[ToolKey, dict[str, Any]] = {}
        self._lock = threading.RLock()

    @property
    def guard(self) -> ContractGuard:
        return self._guard

    # ------------------------------------------------------------------
    # Reading
    # ------------------------------------------------------------------

    def get(self, name: str, *, server: str | None = None) -> RegisteredTool:
        """The registered tool, or ``ToolNotFoundError``."""
        key = ToolKey(name, server)
        with self._lock:
            entry = self._tools.get(key)
            if entry is None:
                raise self._not_found(name, server)
            return entry

    def find(self, name: str, *, server: str | None = None) -> RegisteredTool | None:
        """The registered tool, or ``None``."""
        with self._lock:
            return self._tools.get(ToolKey(name, server))

    def tools(self, *, server: str | None = _ALL_SERVERS) -> list[RegisteredTool]:
        """Registered tools sorted by key; one server's when *server* is given."""
        with self._lock:
            entries = [
                entry
                for key, entry in self._tools.items()
                if server is _ALL_SERVERS or key.server == server
            ]
        return sorted(entries, key=lambda entry: entry.key.sort_key)

    def servers(self) -> list[str | None]:
        """Every server with at least one tool registered, ``None`` first."""
        with self._lock:
            keys = {key.server for key in self._tools}
        return sorted(keys, key=lambda s: ("0", "") if s is None else ("1", s))

    def __len__(self) -> int:
        with self._lock:
            return len(self._tools)

    def __contains__(self, item: object) -> bool:
        if isinstance(item, ToolKey):
            key = item
        elif isinstance(item, str):
            key = ToolKey(item)
        else:
            return False
        with self._lock:
            return key in self._tools

    def _names(self, server: str | None) -> list[str]:
        return sorted(key.name for key in self._tools if key.server == server)

    def _not_found(self, name: str, server: str | None) -> ToolNotFoundError:
        """Caller holds the lock."""
        elsewhere = sorted(
            (key for key in self._tools if key.name == name and key.server != server),
            key=lambda key: key.sort_key,
        )
        return ToolNotFoundError(
            name, server, self._names(server), [key.server for key in elsewhere]
        )

    # ------------------------------------------------------------------
    # Writing
    # ------------------------------------------------------------------

    def register(self, tool: Any, *, server: str | None = None) -> ToolChange:
        """
        Add or replace one tool definition, and say what changed.

        Raises ``InvalidToolDefinitionError`` only when the definition has no
        usable ``name`` -- there is nothing to key it by. Every other problem
        registers the tool in an unusable state, replacing whatever was
        there, so a broken update is never masked by the previous schema.
        """
        built = self._build(tool, server)
        with self._lock:
            return self._store(built)

    def compare(self, tool: Any, *, server: str | None = None) -> ToolChange:
        """What ``register`` would report, without registering anything."""
        built = self._build(tool, server)
        with self._lock:
            return self._change_for(self._tools.get(built.key), built)

    def remove(self, name: str, *, server: str | None = None) -> ToolChange:
        """Remove a tool; ``ToolNotFoundError`` if it is not registered."""
        key = ToolKey(name, server)
        with self._lock:
            previous = self._tools.pop(key, None)
            if previous is None:
                raise self._not_found(name, server)
            self._call_definitions.pop(key, None)
        return ToolChange(key, ToolChangeKind.REMOVED, previous=previous)

    def clear(self, *, server: str | None = _ALL_SERVERS) -> None:
        """Forget every tool, or one server's. For a reconnect."""
        with self._lock:
            if server is _ALL_SERVERS:
                self._tools.clear()
                self._call_definitions.clear()
            else:
                for key in [k for k in self._tools if k.server == server]:
                    del self._tools[key]
                    self._call_definitions.pop(key, None)

    def sync(self, tools: Iterable[Any], *, server: str | None = None) -> SyncReport:
        """
        Make *server*'s registered tools exactly *tools* -- a complete
        ``tools/list`` result -- and report every addition, removal and change.

        Atomic: the new set replaces the old one in a single step, so a
        concurrent ``process_call`` sees either the old listing or the new
        one, never a mixture. Pass the *complete* list: a tool missing from
        it is removed. (With a paginated ``tools/list``, collect every page
        first -- ``ToolListAccumulator`` in ``stateguard.adapters.mcp.jsonrpc``
        does that.)

        A name listed more than once is registered ``INVALID``: two
        definitions for one name leave no way to tell which one a call is
        for. A definition with no usable ``name`` cannot be keyed and raises
        ``InvalidToolDefinitionError`` before anything is changed.
        """
        listed = list(tools)
        names = Counter(self._name_of(tool) for tool in listed)
        built: dict[ToolKey, RegisteredTool] = {}
        for tool in listed:
            name = self._name_of(tool)
            if names[name] > 1:
                entry = self._unusable(
                    ToolKey(name, server),
                    tool,
                    MalformedSchemaError(
                        f"Tool {name!r} appears {names[name]} times in one tools/list; "
                        f"with two definitions for one name there is no way to tell which "
                        f"one a call is for."
                    ),
                )
            else:
                entry = self._build(tool, server)
            built.setdefault(entry.key, entry)

        with self._lock:
            changes: list[ToolChange] = []
            existing = {key for key in self._tools if key.server == server}
            for key in sorted(existing - set(built), key=lambda k: k.sort_key):
                self._call_definitions.pop(key, None)
                changes.append(
                    ToolChange(key, ToolChangeKind.REMOVED, previous=self._tools.pop(key))
                )
            for key in sorted(built, key=lambda k: k.sort_key):
                changes.append(self._store(built[key]))
        changes.sort(key=lambda change: change.key.sort_key)
        return SyncReport(server=server, changes=tuple(changes))

    # ------------------------------------------------------------------
    # Calls
    # ------------------------------------------------------------------

    def process_call(
        self, name: str, arguments: Any, *, server: str | None = None
    ) -> ToolCallOutcome:
        """
        Validate (and, in auto mode, repair) one ``tools/call`` against the
        tool's *current* definition.

        Raises
        ------
        ToolNotFoundError
            No tool *name* is registered for *server*.
        ToolSchemaUnusableError
            It is registered, but its current schema could not be read.
            ``cause`` carries the original error. The previous schema is
            never used instead.
        """
        key = ToolKey(name, server)
        with self._lock:
            entry = self._tools.get(key)
            if entry is None:
                raise self._not_found(name, server)
            definition = self._call_definitions.get(key)
        if not entry.usable or definition is None:
            assert entry.error is not None
            raise ToolSchemaUnusableError(name, server, entry.error)
        return repair_tool_call(self._guard, definition, arguments)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _name_of(tool: Any) -> str:
        if not isinstance(tool, Mapping):
            raise InvalidToolDefinitionError(
                f"A tool definition must be an object, got {type(tool).__name__}."
            )
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            raise InvalidToolDefinitionError(
                f"A tool definition needs a non-empty string 'name' to be registered; "
                f"got {name!r}. Keys present: {sorted(map(str, tool))!r}."
            )
        return name

    def _build(self, tool: Any, server: str | None) -> RegisteredTool:
        """Derive an entry for *tool*. Pure: touches no registry state."""
        key = ToolKey(self._name_of(tool), server)
        try:
            definition_fp = document_fingerprint(tool)
        except JSONSchemaError as exc:
            return self._unusable(key, tool, exc)

        # The same definition as the one registered: nothing to re-derive.
        # A relay re-syncs on every client tools/list, and an unchanged
        # listing is the common case -- measured, rebuilding it cost ~0.4 ms
        # per tool (copy, fingerprint, extraction, and a diff that extracted
        # again). The entry is immutable, so returning it is exact.
        with self._lock:
            existing = self._tools.get(key)
        if existing is not None and existing.definition_fingerprint == definition_fp:
            return existing

        definition: dict[str, Any] = copy.deepcopy(dict(tool))
        try:
            _name, input_schema = split_tool_definition(definition)
        except JSONSchemaError as exc:
            return self._unusable(key, definition, exc, definition_fp=definition_fp)

        try:
            fingerprint = fingerprint_schema(input_schema)
        except JSONSchemaError as exc:
            return self._unusable(
                key, definition, exc, definition_fp=definition_fp, input_schema=input_schema
            )

        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always", SchemaFeatureWarning)
            try:
                self._adapter.compile(definition)
            except JSONSchemaError as exc:
                return self._unusable(
                    key,
                    definition,
                    exc,
                    definition_fp=definition_fp,
                    input_schema=input_schema,
                    fingerprint=fingerprint,
                )
        notes = tuple(
            sorted({str(w.message) for w in caught if issubclass(w.category, SchemaFeatureWarning)})
        )

        now = self._clock()
        return RegisteredTool(
            key=key,
            definition=definition,
            input_schema=input_schema,
            state=ToolState.READY,
            error=None,
            fingerprint=fingerprint,
            definition_fingerprint=definition_fp,
            warnings=notes,
            revision=1,
            registered_at=now,
            updated_at=now,
        )

    def _unusable(
        self,
        key: ToolKey,
        tool: Any,
        error: JSONSchemaError,
        *,
        definition_fp: str | None = None,
        input_schema: Mapping[str, Any] | None = None,
        fingerprint: SchemaFingerprint | None = None,
    ) -> RegisteredTool:
        now = self._clock()
        definition: Mapping[str, Any] = copy.deepcopy(dict(tool))
        return RegisteredTool(
            key=key,
            definition=definition,
            input_schema=input_schema,
            state=(
                ToolState.INVALID
                if isinstance(error, MalformedSchemaError)
                else ToolState.UNSUPPORTED
            ),
            error=error,
            fingerprint=fingerprint,
            definition_fingerprint=definition_fp,
            warnings=(),
            revision=1,
            registered_at=now,
            updated_at=now,
        )

    def _store(self, built: RegisteredTool) -> ToolChange:
        """Commit *built*, carrying its history over. Caller holds the lock."""
        previous = self._tools.get(built.key)
        if previous is not None and built is not previous:
            same = (
                previous.definition_fingerprint is not None
                and previous.definition_fingerprint == built.definition_fingerprint
            )
            built = replace(
                built,
                revision=previous.revision + (0 if same else 1),
                registered_at=previous.registered_at,
                updated_at=previous.updated_at if same else built.updated_at,
            )
        self._tools[built.key] = built
        if built is not previous:
            self._call_definitions[built.key] = copy.deepcopy(dict(built.definition))
        return self._change_for(previous, built)

    @staticmethod
    def _change_for(previous: RegisteredTool | None, current: RegisteredTool) -> ToolChange:
        if previous is None:
            return ToolChange(current.key, ToolChangeKind.ADDED, current=current)
        if current is previous:
            return ToolChange(
                current.key,
                ToolChangeKind.UNCHANGED,
                SchemaDiff(
                    SchemaChangeStatus.UNCHANGED,
                    before=previous.fingerprint,
                    after=current.fingerprint,
                    error=None if current.error is None else str(current.error),
                ),
                (),
                previous,
                current,
            )
        changed = _changed_fields(previous.definition, current.definition)
        schema = diff_schemas(previous.input_schema, current.input_schema)
        kind = ToolChangeKind.UNCHANGED if not changed else ToolChangeKind.UPDATED
        return ToolChange(current.key, kind, schema, changed, previous, current)


def _changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> tuple[str, ...]:
    """Top-level definition fields whose canonical JSON differs, sorted."""
    changed = []
    for name in sorted(set(map(str, before)) | set(map(str, after))):
        a, b = before.get(name, _MISSING_FIELD), after.get(name, _MISSING_FIELD)
        if a is _MISSING_FIELD or b is _MISSING_FIELD:
            if a is not b:
                changed.append(name)
            continue
        try:
            if canonical_json(a) != canonical_json(b):
                changed.append(name)
        except (TypeError, ValueError, RecursionError):
            changed.append(name)
    return tuple(changed)


_MISSING_FIELD: Any = object()

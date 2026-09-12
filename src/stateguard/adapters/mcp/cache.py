"""
Bounded cache of extracted contracts, keyed by tool identity **and** content.

Why not ``contract_id``
----------------------
``ContractSpec.contract_id`` is a hash of ``path:type:required`` per field
plus the strict flag. It is a good identifier for *a contract*, and a bad key
for *a schema*, because two different tools whose parameters happen to have
the same shape produce the same id::

    get_weather(location: str, days: int)
    get_traffic(location: str, days: int)     -> same contract_id

Keying a cache on it would serve one tool's contract for another whenever
their signatures coincided -- which for tool definitions is not a remote
possibility but a common one, since so many take a single ``query`` string.
So the key is the tool name plus a hash of the schema document itself.

Why the schema hash is in the key at all
----------------------------------------
A tool's name is stable across a schema change -- that is the entire failure
this adapter exists for. ``get_forecast`` renaming ``location`` to ``city``
is still ``get_forecast``. Keying on the name alone would pin the first
schema ever seen and keep repairing against it after the server moved on,
which is precisely the drift StateGuard is supposed to detect. Including the
content means a changed schema is simply a different key: the new contract is
extracted, and the stale one ages out.

Bounded on purpose
------------------
A proxy is a long-running process seeing tool definitions from servers it
does not control. An unbounded dict keyed partly on remote content is a slow
memory leak with an external party holding the tap. Least-recently-used
eviction keeps the working set -- the handful of tools actually being called
-- and discards the rest.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import OrderedDict
from collections.abc import Mapping
from typing import Any

from stateguard.core.models.contract import ContractSpec

__all__ = ["DEFAULT_CACHE_SIZE", "SchemaCache"]


#: Tools held before least-recently-used eviction begins. A server exposing
#: more distinct tools than this still works; it just re-extracts the ones
#: that fell out, which costs a schema walk rather than correctness.
DEFAULT_CACHE_SIZE = 128


class SchemaCache:
    """
    Thread-safe LRU mapping ``(tool name, schema content) -> ContractSpec``.

    Safe to share across threads: a proxy typically handles concurrent
    ``tools/call`` requests, and the cached ``ContractSpec`` is never
    mutated by the engine (which deep-copies the *data* it repairs, not the
    contract), so handing the same instance to several callers is fine.
    """

    def __init__(self, maxsize: int = DEFAULT_CACHE_SIZE) -> None:
        if maxsize < 1:
            raise ValueError(f"maxsize must be at least 1, got {maxsize}")
        self._maxsize = maxsize
        self._entries: OrderedDict[tuple[str | None, str], ContractSpec] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, tool_name: str | None, schema: Mapping[str, Any]) -> ContractSpec | None:
        """Return the cached contract, or ``None`` on a miss."""
        key = self._key(tool_name, schema)
        if key is None:
            return None

        with self._lock:
            entry = self._entries.get(key)
            if entry is not None:
                self._entries.move_to_end(key)
            return entry

    def put(
        self,
        tool_name: str | None,
        schema: Mapping[str, Any],
        contract: ContractSpec,
    ) -> None:
        """Store *contract*, evicting the least recently used if full."""
        key = self._key(tool_name, schema)
        if key is None:
            return

        with self._lock:
            self._entries[key] = contract
            self._entries.move_to_end(key)
            while len(self._entries) > self._maxsize:
                self._entries.popitem(last=False)

    def clear(self) -> None:
        """Drop every entry. Mainly for tests and for a proxy reconnecting."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _key(tool_name: str | None, schema: Mapping[str, Any]) -> tuple[str | None, str] | None:
        """
        Build the cache key, or ``None`` if *schema* cannot be hashed.

        A schema that arrived as JSON always serialises. One built in Python
        might hold something that does not, and a cache miss is a much
        better answer there than an exception thrown from inside what the
        caller asked to be a repair.
        """
        try:
            canonical = json.dumps(schema, sort_keys=True, separators=(",", ":"))
        except (TypeError, ValueError):
            return None
        return tool_name, hashlib.sha256(canonical.encode("utf-8")).hexdigest()

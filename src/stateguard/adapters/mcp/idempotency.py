"""
Idempotency keys -- a cooperative contract between the relay and a server.

An unknown outcome (a lost response, a deadline) cannot be resent safely:
the call may have executed. Nothing on the wire changes that. What can is
the **server** promising to recognise a resend: if every mutating call
carries a key, and the server records the key durably *before* it acts, a
resend of the same key is never executed twice. The server answers it from
its record instead.

MCP defines no idempotency key, so the names are StateGuard's, namespaced in
``_meta``:

``io.stateguard/idempotency`` in a tool definition's ``_meta``
    The server's statement, per tool, that it honours keys for that tool:
    ``{"version": 1}``. Evidence comes from the server's own listing, and
    the relay re-lists every restarted server, so it is never carried over
    from a process that is gone. Annotations are not this; nothing else is.

``io.stateguard/idempotencyKey`` in a ``tools/call``'s ``params._meta``
    The key. The relay adds one (a random UUID) to each call to such a tool,
    unless the client sent its own, and keeps it when it resends.

``io.stateguard/idempotencyOutcome``
    What the server did with a keyed call. In a result's ``_meta`` when it
    answered with a result; in an error's ``data`` when it executed nothing.

Outcomes (``Outcome``) -- what a client may conclude
----------------------------------------------------
=========================  ==================================================
``executed``               Executed by this request; its result is recorded.
``executed_unrecorded``    Executed by this request, but recording the result
                           failed: a later resend of the key will be in doubt
                           (never executed again).
``replayed``               Executed earlier; this is its recorded result.
                           Nothing executed now.
``recovered``              An earlier request with this key was cut off; the
                           server's recovery check found *this key's own*
                           effect, and this is the result it recorded from
                           that evidence. Nothing executed now. (See
                           "Recovery of abandoned attempts".)
``rolled_back``            (Transactional tools) the tool failed inside the
                           transaction that would have recorded it: nothing
                           was committed, and the key is free again.
``not_executed``           The handler showed it failed before acting
                           (``NotExecuted``): nothing executed, the key is
                           free again.
``in_progress``            An earlier request with this key is executing in
                           this server process right now. Nothing executed
                           now; its outcome is not known yet.
``in_doubt``               An earlier request with this key started and no
                           result was recorded -- it was cut off (process
                           death, an exception), or it is running in another
                           process. It may or may not have executed.
                           Nothing executed now. Never executed again
                           automatically, unless a recovery check proves its
                           effect absent.
``expired``                Executed earlier; its result is no longer retained
                           (see retention). Nothing executed now.
``rejected_mismatch``      The key was used with another tool or other
                           arguments. Nothing executed.
``rejected_store``         The key store is unavailable or unreadable. The
                           server fails closed: nothing executed.
=========================  ==================================================

``rolled_back`` is a tool error result. The last six are JSON-RPC errors
(``outcome_error``), not tool results: a tool result with ``isError`` would
read as "it ran and failed".

The server side (``IdempotencyStore``, ``KeyedExecutor``)
--------------------------------------------------------
**Two modes.** They differ in exactly one crash window.

* *Two-phase* (``KeyedExecutor.execute``), for any side effect:
  1. record the key as *started* and commit;
  2. execute;
  3. record the result as *done* and commit;
  4. answer.

  Crash windows:

  - **before 1:** nothing recorded, nothing executed. A resend executes. Correct.
  - **between 1 and 3** (before, during or after the effect): the key is
    *started*, and a resend is ``in_doubt``. It is never executed again,
    even when the effect had not happened yet. Conservative, never a
    duplicate. **Not exactly-once:** an effect that happened before the
    crash is not reported as such. It is reported as unknown.
  - **after 3:** a resend replays the result.

* *Transactional* (``IdempotencyStore.transact``), for an effect written to
  the **same SQLite database** as the store. The effect and the *done*
  record commit in one transaction:

  - **any crash before the commit:** neither happened, and a resend executes;
  - **after it:** both happened, and a resend replays.

  Concurrent duplicates are serialised by the database's write lock. This
  is exactly-once *for that effect*, and only for it: anything the tool does
  outside the transaction is in the two-phase position.

**Recovery of abandoned attempts** (two-phase only, opt-in per tool). The
crash window between 1 and 3 can be closed by a server that can answer, for
one key, "did this key's effect happen?" -- and only under two conditions,
both of which the library checks or the server author promises:

1. *The attempt is over.* An attempt is **abandoned** when it can no longer
   act: it was marked in doubt (its handler raised or was cancelled), it
   was started by this instance and is no longer executing here (its
   recording failed), or it was started by another instance that held the
   store **exclusively** (``exclusive=True``: an SQLite ``EXCLUSIVE`` lock
   on ``<db>.<sha256(scope)[:16]>.lock``, held for the instance's lifetime
   and dropped by the kernel when its process dies; the TypeScript adapter
   takes the same lock) and this instance now holds that lock -- so that
   instance is gone. The lock is local to one host: it is not distributed
   coordination. A *started* key of a live or non-exclusive instance is
   never abandoned: it stays ``in_doubt``.
2. *The check is about this key, not a postcondition.* The effect must
   carry its key, written in the same atomic step as the effect (the
   reference server writes the key on the ledger line it appends), and any
   partial effect must be undone or ruled out before the check. "The state
   looks right" is not evidence: another request can produce it.

``adopt`` takes an abandoned key over atomically (one taker; a concurrent
duplicate sees ``in_progress``), the check runs, and the key settles:

- effect found -> recorded as done, answered ``recovered``;
- effect absent -> executed now, once (``executed``);
- cannot tell, or the check fails -> ``in_doubt``, as before.

A crash during recovery leaves the key started by the recovering instance:
abandoned again, and recovered again by the next one. The library cannot
verify the check; like the advertisement, it is the server's promise.

**Key scope.** Keys are unique per ``scope`` (a store parameter, for
example the server's name and tenant). The same key in another scope is
another key.

**Concurrency.**
- Within one store instance, a duplicate of a key still executing waits up
  to ``wait_seconds`` for it, then replays it, or answers ``in_progress``.
- Across processes, a *started* key belongs to its owner (a per-instance
  id). Any other instance sees it as ``in_doubt``: it cannot tell a live
  owner from a dead one.

**Retention.**
- ``done`` results are kept for ``retention_seconds``; after that a resend is
  ``expired``.
- ``purge()`` drops expired results, keeping a tombstone (key and
  fingerprint) for ``tombstone_seconds``, and deletes older tombstones.
- After a tombstone is gone, the key is unknown, and a request with it
  executes as new. **Keys must therefore never be reused across that
  horizon**: the relay's are random UUIDs.
- *started* and *in_doubt* keys never expire. ``release`` (the operator
  says it did not execute), ``resolve`` (the operator records its result)
  and a recovery check are the only ways out of doubt.

**Storage failure.** Every store error -- unavailable, locked past its
timeout, corrupted -- raises ``StoreUnavailable``:
- before execution, the executor answers ``rejected_store`` and executes
  nothing;
- after execution, ``executed_unrecorded``.

**Persistence.** SQLite, WAL journal, ``synchronous=FULL``: a committed
record survives a process crash and a power loss.

Zero dependencies (``sqlite3`` is in the standard library). The Python MCP
SDK adapter is ``stateguard.adapters.mcp.idempotency_sdk``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import sqlite3
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

__all__ = [
    "IDEMPOTENCY_CAPABILITY",
    "IDEMPOTENCY_KEY",
    "IDEMPOTENCY_OUTCOME",
    "NOT_EXECUTED_OUTCOMES",
    "Begin",
    "BeginStatus",
    "Execution",
    "IdempotencyStore",
    "KeyedExecutor",
    "NotExecuted",
    "Outcome",
    "Recovery",
    "StoreUnavailable",
    "advertisement",
    "call_key",
    "error_code",
    "error_data",
    "error_message",
    "execution_for",
    "fingerprint",
    "in_doubt_error",
    "is_in_doubt",
    "new_key",
    "not_executed_outcome",
    "outcome_error",
    "recover_abandoned",
    "tool_supports_keys",
    "with_key",
]

IDEMPOTENCY_CAPABILITY = "io.stateguard/idempotency"
IDEMPOTENCY_KEY = "io.stateguard/idempotencyKey"
IDEMPOTENCY_OUTCOME = "io.stateguard/idempotencyOutcome"
#: The only contract version.
VERSION = 1

T = TypeVar("T")


class Outcome(StrEnum):
    EXECUTED = "executed"
    EXECUTED_UNRECORDED = "executed_unrecorded"
    REPLAYED = "replayed"
    RECOVERED = "recovered"
    ROLLED_BACK = "rolled_back"
    NOT_EXECUTED = "not_executed"
    IN_PROGRESS = "in_progress"
    IN_DOUBT = "in_doubt"
    EXPIRED = "expired"
    REJECTED_MISMATCH = "rejected_mismatch"
    REJECTED_STORE = "rejected_store"


#: Outcomes answered as a JSON-RPC error: this request executed nothing.
NOT_EXECUTED_OUTCOMES = frozenset(
    {
        Outcome.NOT_EXECUTED,
        Outcome.IN_PROGRESS,
        Outcome.IN_DOUBT,
        Outcome.EXPIRED,
        Outcome.REJECTED_MISMATCH,
        Outcome.REJECTED_STORE,
    }
)

#: JSON-RPC error codes (server-error range) per not-executed outcome.
_ERROR_CODES = {
    Outcome.NOT_EXECUTED: -32014,
    Outcome.IN_DOUBT: -32010,
    Outcome.IN_PROGRESS: -32011,
    Outcome.EXPIRED: -32012,
    Outcome.REJECTED_STORE: -32013,
    Outcome.REJECTED_MISMATCH: -32602,
}
_ERROR_MESSAGES = {
    Outcome.NOT_EXECUTED: (
        "The tool failed before acting: nothing was executed, and the idempotency key is free."
    ),
    Outcome.IN_DOUBT: (
        "Idempotency key in doubt: an earlier request with this key started and recorded "
        "no result. Nothing was executed now; whether it executed earlier is unknown."
    ),
    Outcome.IN_PROGRESS: (
        "Idempotency key in progress: an earlier request with this key is still executing. "
        "Nothing was executed now."
    ),
    Outcome.EXPIRED: (
        "Idempotency key expired: a request with this key executed earlier and its result is "
        "no longer retained. Nothing was executed now."
    ),
    Outcome.REJECTED_STORE: (
        "Idempotency store unavailable: the server cannot record keys, so it executed nothing."
    ),
    Outcome.REJECTED_MISMATCH: (
        "Idempotency key reused with another tool or other arguments: nothing was executed."
    ),
}


# -- wire helpers (both sides) ------------------------------------------------


def advertisement() -> dict[str, Any]:
    """The ``_meta`` entry a server puts on a tool definition that honours keys."""
    return {IDEMPOTENCY_CAPABILITY: {"version": VERSION}}


def tool_supports_keys(definition: Mapping[str, Any] | None) -> bool:
    """True if the tool definition's ``_meta`` says the server honours keys (version 1)."""
    if not isinstance(definition, Mapping):
        return False
    meta = definition.get("_meta")
    entry = meta.get(IDEMPOTENCY_CAPABILITY) if isinstance(meta, Mapping) else None
    return isinstance(entry, Mapping) and entry.get("version") == VERSION


def new_key() -> str:
    return uuid.uuid4().hex


def call_key(message: Mapping[str, Any]) -> str | None:
    """The idempotency key a ``tools/call`` message carries, if any."""
    params = message.get("params")
    meta = params.get("_meta") if isinstance(params, Mapping) else None
    key = meta.get(IDEMPOTENCY_KEY) if isinstance(meta, Mapping) else None
    return key if isinstance(key, str) and key else None


def with_key(message: dict[str, Any], key: str) -> dict[str, Any]:
    """A copy of *message* whose ``params._meta`` carries *key* (a client's own key wins)."""
    out = dict(message)
    params = dict(out.get("params") or {})
    meta = dict(params.get("_meta") or {})
    meta.setdefault(IDEMPOTENCY_KEY, key)
    params["_meta"] = meta
    out["params"] = params
    return out


def fingerprint(tool: str, arguments: Any) -> str:
    """
    What a key must keep meaning: the tool and its arguments, as canonical
    JSON (sorted keys). Equivalent means equal after that: ``1`` and ``1.0``
    are different requests.
    """
    canonical = json.dumps([tool, arguments], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def error_code(outcome: Outcome) -> int:
    """The JSON-RPC error code for a not-executed outcome."""
    return _ERROR_CODES[outcome]


def error_message(outcome: Outcome) -> str:
    """The JSON-RPC error message for a not-executed outcome."""
    return _ERROR_MESSAGES[outcome]


def error_data(outcome: Outcome, key: str) -> dict[str, Any]:
    return {IDEMPOTENCY_OUTCOME: outcome.value, IDEMPOTENCY_KEY: key}


def outcome_error(identifier: Any, outcome: Outcome, key: str) -> dict[str, Any]:
    """The JSON-RPC error a server answers for a not-executed outcome."""
    if outcome not in NOT_EXECUTED_OUTCOMES:
        raise ValueError(f"{outcome.value} is answered with a result, not an error")
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "error": {
            "code": _ERROR_CODES[outcome],
            "message": _ERROR_MESSAGES[outcome],
            "data": error_data(outcome, key),
        },
    }


def in_doubt_error(identifier: Any, key: str) -> dict[str, Any]:
    return outcome_error(identifier, Outcome.IN_DOUBT, key)


def not_executed_outcome(message: Mapping[str, Any]) -> Outcome | None:
    """For an error answer that says "this keyed request executed nothing": which outcome."""
    error = message.get("error")
    data = error.get("data") if isinstance(error, Mapping) else None
    value = data.get(IDEMPOTENCY_OUTCOME) if isinstance(data, Mapping) else None
    try:
        outcome = Outcome(value) if isinstance(value, str) else None
    except ValueError:
        return None
    return outcome if outcome in NOT_EXECUTED_OUTCOMES else None


def is_in_doubt(message: Mapping[str, Any]) -> bool:
    """The resend's answer leaves the original's outcome unknown (in doubt, or in progress)."""
    return not_executed_outcome(message) in (Outcome.IN_DOUBT, Outcome.IN_PROGRESS)


# -- the store ----------------------------------------------------------------


class StoreUnavailable(Exception):  # noqa: N818 -- a state, not an error kind
    """The key store cannot be read or written (unavailable, locked, corrupted)."""


class BeginStatus(StrEnum):
    EXECUTE = "execute"  # new key: recorded as started; execute, then complete()
    REPLAY = "replay"  # done before: answer the recorded result
    IN_PROGRESS = "in_progress"  # started by this instance and still executing
    IN_DOUBT = "in_doubt"  # started, no result: execute nothing, ever
    EXPIRED = "expired"  # done before, result no longer retained
    MISMATCH = "mismatch"  # same key, other tool or arguments
    RECOVER = "recover"  # adopt(): an abandoned attempt is now yours -- check before acting


@dataclass(frozen=True)
class Begin:
    status: BeginStatus
    #: The recorded result, for REPLAY.
    result: Any = None


_STATE_STARTED, _STATE_DONE, _STATE_IN_DOUBT, _STATE_EXPIRED = (
    "started",
    "done",
    "in_doubt",
    "expired",
)
#: The owner-id prefix of an exclusive instance.
_EXCLUSIVE_OWNER = "x-"


class IdempotencyStore:
    """
    The server side's durable record of keys: one SQLite file. See the
    module docstring for scope, ownership, retention and failure behaviour.
    """

    def __init__(
        self,
        path: str | Path,
        *,
        scope: str = "default",
        retention_seconds: float = 7 * 24 * 3600.0,
        tombstone_seconds: float = 30 * 24 * 3600.0,
        timeout_seconds: float = 5.0,
        clock: Callable[[], float] = time.time,
        exclusive: bool = False,
    ) -> None:
        if retention_seconds <= 0 or tombstone_seconds < retention_seconds:
            raise ValueError("need 0 < retention_seconds <= tombstone_seconds")
        self.path = str(path)
        self.scope = scope
        self.retention_seconds = retention_seconds
        self.tombstone_seconds = tombstone_seconds
        self._timeout = timeout_seconds
        self._clock = clock
        #: Whether this instance holds the scope exclusively (see "Recovery").
        self.exclusive = exclusive
        self._lock_db: sqlite3.Connection | None = None
        if exclusive:
            self._hold_exclusively()
        #: This instance's identity: whose *started* keys are "in progress", not "in doubt".
        #: An exclusive owner's id says so: once its lock is free, its keys are abandoned.
        self.owner = (_EXCLUSIVE_OWNER if exclusive else "") + uuid.uuid4().hex
        self._lock = threading.Lock()
        self._done = threading.Condition(self._lock)
        self._active: set[str] = set()
        try:
            self._run(
                lambda db: db.execute(
                    "CREATE TABLE IF NOT EXISTS stateguard_keys ("
                    " scope TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,"
                    " tool TEXT, state TEXT NOT NULL, owner TEXT, started_at REAL NOT NULL,"
                    " finished_at REAL, result TEXT, note TEXT, PRIMARY KEY (scope, key))"
                )
            )
        except StoreUnavailable:
            self.close()  # an instance that never existed holds no lock
            raise

    # -- plumbing ----------------------------------------------------------------

    @property
    def lock_path(self) -> str:
        """The lock file an exclusive instance holds (one per store file and scope)."""
        return f"{self.path}.{hashlib.sha256(self.scope.encode()).hexdigest()[:16]}.lock"

    def _hold_exclusively(self) -> None:
        """
        Take the scope's lock, waiting up to the timeout for a dying holder to
        let go: an SQLite ``EXCLUSIVE`` lock on ``lock_path``, held by one
        connection for this instance's lifetime. SQLite takes it with
        ``fcntl`` record locks, which the kernel drops when the process dies,
        and the TypeScript adapter takes the same lock on the same file, so the
        two exclude each other. Local only: one host, a filesystem with working
        POSIX locks -- not distributed coordination.
        """
        deadline = time.monotonic() + self._timeout
        while True:
            try:
                db = sqlite3.connect(
                    self.lock_path, timeout=0.05, isolation_level=None, check_same_thread=False
                )
            except (sqlite3.Error, OSError) as exc:
                raise StoreUnavailable(str(exc)) from exc
            try:
                db.execute("PRAGMA locking_mode=EXCLUSIVE")
                db.execute("BEGIN EXCLUSIVE")
                db.execute("COMMIT")  # the lock stays: exclusive locking mode
            except sqlite3.OperationalError as exc:
                db.close()
                if "locked" not in str(exc) and "busy" not in str(exc):
                    raise StoreUnavailable(str(exc)) from exc
                if time.monotonic() >= deadline:
                    raise StoreUnavailable(
                        f"another live instance holds {self.lock_path} exclusively"
                    ) from None
                time.sleep(0.05)
            except sqlite3.Error as exc:
                db.close()
                raise StoreUnavailable(str(exc)) from exc
            else:
                self._lock_db = db
                return

    def close(self) -> None:
        """Let go of the exclusive lock. Only when nothing is executing here."""
        with self._lock:
            if self._active:
                raise RuntimeError("keys are still executing in this instance")
            if self._lock_db is not None:
                self._lock_db.close()
                self._lock_db = None

    def connect(self) -> sqlite3.Connection:
        """A connection to the store's database (for ``transact`` effects and inspection)."""
        db = sqlite3.connect(self.path, timeout=self._timeout, isolation_level=None)
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        return db

    def _run(self, work: Callable[[sqlite3.Connection], T]) -> T:
        try:
            with contextlib.closing(self.connect()) as db:
                return work(db)
        except (sqlite3.Error, OSError) as exc:
            raise StoreUnavailable(str(exc)) from exc

    def _row(self, db: sqlite3.Connection, key: str) -> Any:
        return db.execute(
            "SELECT fingerprint, state, owner, finished_at, result FROM stateguard_keys"
            " WHERE scope = ? AND key = ?",
            (self.scope, key),
        ).fetchone()

    def _insert_started(
        self, db: sqlite3.Connection, key: str, fingerprint_: str, tool: str | None
    ) -> None:
        db.execute(
            "INSERT INTO stateguard_keys"
            " (scope, key, fingerprint, tool, state, owner, started_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (self.scope, key, fingerprint_, tool, _STATE_STARTED, self.owner, self._clock()),
        )

    def _abandoned(self, key: str, state: str, owner: str | None) -> bool:
        """Caller holds the lock: the attempt behind this row can no longer act."""
        if state == _STATE_IN_DOUBT:
            return True  # marked after its handler raised or was cancelled
        if state != _STATE_STARTED:
            return False
        if owner == self.owner:
            return key not in self._active  # finished here; its recording failed
        # Another instance's: only provably gone if it held the lock we now hold.
        return self.exclusive and isinstance(owner, str) and owner.startswith(_EXCLUSIVE_OWNER)

    def _judge(self, key: str, fingerprint_: str, row: Any) -> Begin:
        recorded, state, owner, finished_at, result = row
        if recorded != fingerprint_:
            return Begin(BeginStatus.MISMATCH)
        if state == _STATE_DONE:
            if finished_at is not None and self._clock() - finished_at > self.retention_seconds:
                return Begin(BeginStatus.EXPIRED)
            return Begin(BeginStatus.REPLAY, json.loads(result))
        if state == _STATE_EXPIRED:
            return Begin(BeginStatus.EXPIRED)
        if state == _STATE_STARTED and owner == self.owner and key in self._active:
            return Begin(BeginStatus.IN_PROGRESS)
        return Begin(BeginStatus.IN_DOUBT)

    # -- two-phase ---------------------------------------------------------------

    def begin(self, key: str, fingerprint_: str, *, tool: str | None = None) -> Begin:
        """Claim *key* (EXECUTE: committed as started, by this instance) or judge it."""

        def work(db: sqlite3.Connection) -> Begin:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(db, key)
                if row is None:
                    self._insert_started(db, key, fingerprint_, tool)
                    db.execute("COMMIT")
                    self._active.add(key)
                    return Begin(BeginStatus.EXECUTE)
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            return self._judge(key, fingerprint_, row)

        with self._lock:
            return self._run(work)

    def adopt(self, key: str, fingerprint_: str, *, tool: str | None = None) -> Begin:
        """
        Take over an **abandoned** attempt of *key* (RECOVER: now started by
        this instance), atomically -- one taker. The caller must find out
        what the attempt did before acting (``recover_abandoned``). A key
        that is not abandoned is judged as ``begin`` judges it; an unknown
        key is claimed (EXECUTE).
        """

        def work(db: sqlite3.Connection) -> Begin:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(db, key)
                if row is None:
                    self._insert_started(db, key, fingerprint_, tool)
                    db.execute("COMMIT")
                    self._active.add(key)
                    return Begin(BeginStatus.EXECUTE)
                recorded, state, owner, _, _ = row
                if recorded == fingerprint_ and self._abandoned(key, state, owner):
                    db.execute(
                        "UPDATE stateguard_keys SET state = ?, owner = ?, started_at = ?,"
                        " note = substr(? || coalesce(note, ''), 1, 500)"
                        " WHERE scope = ? AND key = ?",
                        (
                            _STATE_STARTED,
                            self.owner,
                            self._clock(),
                            f"adopted from {owner} ({state}); ",
                            self.scope,
                            key,
                        ),
                    )
                    db.execute("COMMIT")
                    self._active.add(key)
                    return Begin(BeginStatus.RECOVER)
                db.execute("COMMIT")
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise
            return self._judge(key, fingerprint_, row)

        with self._lock:
            return self._run(work)

    def _finish(self, key: str) -> None:
        """Caller holds the lock: *key* is no longer executing here."""
        self._active.discard(key)
        self._done.notify_all()

    def complete(self, key: str, result: Any) -> None:
        """Record *key*'s result (*done*). ``StoreUnavailable`` leaves the key in doubt."""

        def work(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE stateguard_keys SET state = ?, result = ?, finished_at = ?"
                " WHERE scope = ? AND key = ? AND state = ? AND owner = ?",
                (
                    _STATE_DONE,
                    json.dumps(result),
                    self._clock(),
                    self.scope,
                    key,
                    _STATE_STARTED,
                    self.owner,
                ),
            )

        with self._lock:
            try:
                self._run(work)
            finally:
                self._finish(key)

    def abort(self, key: str) -> None:
        """
        The handler proved it executed nothing (``NotExecuted``): free the key.
        Only this instance's own *started* key is released.
        """

        def work(db: sqlite3.Connection) -> None:
            db.execute(
                "DELETE FROM stateguard_keys WHERE scope = ? AND key = ? AND state = ?"
                " AND owner = ?",
                (self.scope, key, _STATE_STARTED, self.owner),
            )

        with self._lock:
            try:
                self._run(work)
            finally:
                self._finish(key)

    def mark_in_doubt(self, key: str, note: str) -> None:
        """The handler failed after it may have acted: the key stays in doubt, with why."""

        def work(db: sqlite3.Connection) -> None:
            db.execute(
                "UPDATE stateguard_keys SET state = ?, note = ? WHERE scope = ? AND key = ?"
                " AND state = ? AND owner = ?",
                (_STATE_IN_DOUBT, note[:500], self.scope, key, _STATE_STARTED, self.owner),
            )

        with self._lock:
            try:
                self._run(work)
            finally:
                self._finish(key)

    def wait(self, key: str, timeout: float) -> bool:
        """Wait until *key* is no longer executing in this instance. False on timeout."""
        with self._lock:
            return self._done.wait_for(lambda: key not in self._active, timeout=timeout)

    # -- transactional -------------------------------------------------------------

    def transact(
        self,
        key: str,
        fingerprint_: str,
        effect: Callable[[sqlite3.Connection], Any],
        *,
        tool: str | None = None,
    ) -> Execution:
        """
        Run *effect(db)* and record its result in **one** transaction on the
        store's database. EXECUTED (committed), ROLLED_BACK (``effect``
        raised: nothing committed, the exception is in ``error``), or a
        not-executed outcome for a key seen before.
        """

        def work(db: sqlite3.Connection) -> Execution:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = self._row(db, key)
                if row is not None:
                    db.execute("COMMIT")
                    return _from_begin(self._judge(key, fingerprint_, row))
                try:
                    result = effect(db)
                except Exception as exc:  # noqa: BLE001 -- the tool's own failure
                    db.execute("ROLLBACK")
                    return Execution(Outcome.ROLLED_BACK, None, error=exc)
                now = self._clock()
                db.execute(
                    "INSERT INTO stateguard_keys (scope, key, fingerprint, tool, state, owner,"
                    " started_at, finished_at, result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        self.scope,
                        key,
                        fingerprint_,
                        tool,
                        _STATE_DONE,
                        self.owner,
                        now,
                        now,
                        json.dumps(result),
                    ),
                )
                db.execute("COMMIT")
                return Execution(Outcome.EXECUTED, result)
            except BaseException:
                with contextlib.suppress(sqlite3.Error):
                    db.execute("ROLLBACK")
                raise

        with self._lock:
            try:
                return self._run(work)
            except StoreUnavailable:
                # Nothing was committed -- the effect is in the same transaction,
                # so it did not happen either.
                return Execution(Outcome.REJECTED_STORE, None)

    # -- retention and operator actions ------------------------------------------

    def purge(self) -> dict[str, int]:
        """Expire old results (keep a tombstone), delete old tombstones. In-doubt keys stay."""
        now = self._clock()

        def work(db: sqlite3.Connection) -> dict[str, int]:
            expired = db.execute(
                "UPDATE stateguard_keys SET state = ?, result = NULL WHERE scope = ?"
                " AND state = ? AND finished_at < ?",
                (_STATE_EXPIRED, self.scope, _STATE_DONE, now - self.retention_seconds),
            ).rowcount
            deleted = db.execute(
                "DELETE FROM stateguard_keys WHERE scope = ? AND state = ? AND finished_at < ?",
                (self.scope, _STATE_EXPIRED, now - self.tombstone_seconds),
            ).rowcount
            return {"expired": expired, "deleted": deleted}

        with self._lock:
            return self._run(work)

    def release(self, key: str) -> bool:
        """Operator: an in-doubt key did **not** execute -- free it. True if one was released."""

        def work(db: sqlite3.Connection) -> bool:
            return bool(
                db.execute(
                    "DELETE FROM stateguard_keys WHERE scope = ? AND key = ? AND state IN (?, ?)",
                    (self.scope, key, _STATE_IN_DOUBT, _STATE_STARTED),
                ).rowcount
            )

        with self._lock:
            if key in self._active:
                return False  # still executing here: not the operator's to free
            return self._run(work)

    def resolve(self, key: str, result: Any) -> bool:
        """Operator: an in-doubt key **did** execute, with *result* -- record it as done."""

        def work(db: sqlite3.Connection) -> bool:
            return bool(
                db.execute(
                    "UPDATE stateguard_keys SET state = ?, result = ?, finished_at = ?"
                    " WHERE scope = ? AND key = ? AND state IN (?, ?)",
                    (
                        _STATE_DONE,
                        json.dumps(result),
                        self._clock(),
                        self.scope,
                        key,
                        _STATE_IN_DOUBT,
                        _STATE_STARTED,
                    ),
                ).rowcount
            )

        with self._lock:
            if key in self._active:
                return False
            return self._run(work)

    def state_of(self, key: str) -> str | None:
        """The recorded state of *key* (``started``/``done``/``in_doubt``/``expired``) or None."""

        def work(db: sqlite3.Connection) -> str | None:
            row = db.execute(
                "SELECT state FROM stateguard_keys WHERE scope = ? AND key = ?", (self.scope, key)
            ).fetchone()
            return str(row[0]) if row else None

        return self._run(work)


# -- the executor -----------------------------------------------------------------


class NotExecuted(Exception):  # noqa: N818 -- a statement of fact, not an error kind
    """
    Raised by a keyed handler that can show it executed nothing (it failed
    before acting -- a validation error, a missing resource). The key is
    freed; a resend may execute. Anything else a handler raises leaves the
    key in doubt.
    """


@dataclass(frozen=True)
class Execution:
    outcome: Outcome
    #: The result (EXECUTED, EXECUTED_UNRECORDED, REPLAYED).
    result: Any
    #: The handler's exception (ROLLED_BACK, IN_DOUBT after a failure, NotExecuted).
    error: BaseException | None = None


@dataclass(frozen=True)
class Recovery:
    """
    What a server's recovery check found for an abandoned attempt of a key
    (see "Recovery of abandoned attempts"): ``found(result)``, ``absent()``,
    ``rerun(note)`` or ``unknown(note)``.
    """

    #: True: this key's own effect is present. False: run it now -- the effect
    #: is absent and no partial effect remains, or (``rerun``) the operator
    #: declared the tool idempotent, so running it again leaves the same state
    #: whether or not the attempt took effect. None: cannot tell.
    applied: bool | None
    #: For ``found``: the result to record and answer (JSON).
    result: Any = None
    note: str = ""

    @classmethod
    def found(cls, result: Any) -> Recovery:
        return cls(True, result)

    @classmethod
    def absent(cls) -> Recovery:
        return cls(False)

    @classmethod
    def rerun(cls, note: str = "declared idempotent: run again") -> Recovery:
        """
        Cannot tell whether the attempt took effect, and need not: the operator
        declared the tool idempotent (running it twice leaves the state running
        it once would). Its declaration, not a check, is the evidence -- like an
        effect declaration, it is the operator's promise.
        """
        return cls(False, note=note)

    @classmethod
    def unknown(cls, note: str = "") -> Recovery:
        return cls(None, note=note)


def recover_abandoned(
    store: IdempotencyStore, key: str, check: Callable[[], Recovery]
) -> Execution | None:
    """
    Settle a key ``adopt`` returned as RECOVER, from the server's *check*:

    - found -> recorded as done; ``Execution(RECOVERED)``;
    - absent -> None: the caller executes now (it holds the key as started)
      and completes it as usual;
    - cannot tell, the check raised, or it returned no ``Recovery`` -> the
      key is in doubt again; ``Execution(IN_DOUBT)``.
    """
    try:
        found = check()
    except Exception as exc:  # noqa: BLE001 -- a failed check proves nothing
        with contextlib.suppress(StoreUnavailable):
            store.mark_in_doubt(key, f"recovery check failed: {type(exc).__name__}: {exc}")
        return Execution(Outcome.IN_DOUBT, None, exc)
    if isinstance(found, Recovery) and found.applied is False:
        return None
    if isinstance(found, Recovery) and found.applied is True:
        # On a store error the key stays started and abandoned: recovered again next time.
        with contextlib.suppress(StoreUnavailable):
            store.complete(key, found.result)
        return Execution(Outcome.RECOVERED, found.result)
    note = found.note if isinstance(found, Recovery) else f"not a Recovery: {found!r}"
    with contextlib.suppress(StoreUnavailable):
        store.mark_in_doubt(key, f"recovery check could not tell: {note}")
    return Execution(Outcome.IN_DOUBT, None)


def execution_for(begun: Begin) -> Execution:
    """What a judged (not claimed) key means for this request."""
    return _from_begin(begun)


def _from_begin(begun: Begin) -> Execution:
    if begun.status in (BeginStatus.EXECUTE, BeginStatus.RECOVER):
        raise ValueError(f"{begun.status.value}: the key is claimed; execute or recover it")
    return {
        BeginStatus.REPLAY: Execution(Outcome.REPLAYED, begun.result),
        BeginStatus.IN_PROGRESS: Execution(Outcome.IN_PROGRESS, None),
        BeginStatus.IN_DOUBT: Execution(Outcome.IN_DOUBT, None),
        BeginStatus.EXPIRED: Execution(Outcome.EXPIRED, None),
        BeginStatus.MISMATCH: Execution(Outcome.REJECTED_MISMATCH, None),
    }[begun.status]


class KeyedExecutor:
    """
    Two-phase execution of a keyed call over an ``IdempotencyStore`` (see the
    module docstring for the crash windows). Framework-free and synchronous;
    the MCP SDK adapter has the async form.
    """

    def __init__(self, store: IdempotencyStore, *, wait_seconds: float = 5.0) -> None:
        self.store = store
        self.wait_seconds = wait_seconds

    def execute(
        self,
        key: str,
        tool: str,
        arguments: Any,
        run: Callable[[], Any],
        *,
        recover: Callable[[], Recovery] | None = None,
    ) -> Execution:
        """
        *run()* at most once for (*key*, *tool*, *arguments*); its result must
        be JSON. With *recover*, an abandoned attempt of the key is checked
        (see "Recovery of abandoned attempts") instead of staying in doubt.
        """
        fp = fingerprint(tool, arguments)
        try:
            begun = self._claim(key, fp, tool, recover is not None)
        except StoreUnavailable:
            return Execution(Outcome.REJECTED_STORE, None)
        if begun.status is BeginStatus.RECOVER and recover is not None:
            settled = recover_abandoned(self.store, key, recover)
            if settled is not None:
                return settled
        elif begun.status is not BeginStatus.EXECUTE:
            return _from_begin(begun)
        try:
            result = run()
        except NotExecuted as exc:
            with contextlib.suppress(StoreUnavailable):
                self.store.abort(key)
            return Execution(Outcome.NOT_EXECUTED, None, exc)
        except BaseException as exc:
            with contextlib.suppress(StoreUnavailable):
                self.store.mark_in_doubt(key, f"{type(exc).__name__}: {exc}")
            return Execution(Outcome.IN_DOUBT, None, exc)
        try:
            self.store.complete(key, result)
        except StoreUnavailable:
            return Execution(Outcome.EXECUTED_UNRECORDED, result)
        return Execution(Outcome.EXECUTED, result)

    def _claim(self, key: str, fp: str, tool: str, recovers: bool) -> Begin:
        begun = self.store.begin(key, fp, tool=tool)
        if begun.status is BeginStatus.IN_PROGRESS and self.store.wait(key, self.wait_seconds):
            begun = self.store.begin(key, fp, tool=tool)
        if begun.status is BeginStatus.IN_DOUBT and recovers:
            begun = self.store.adopt(key, fp, tool=tool)
            # Another request is recovering it here: wait for its verdict, never a second check.
            if begun.status is BeginStatus.IN_PROGRESS and self.store.wait(key, self.wait_seconds):
                begun = self.store.begin(key, fp, tool=tool)
        return begun

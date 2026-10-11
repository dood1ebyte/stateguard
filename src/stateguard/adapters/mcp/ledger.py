"""
``InvocationLedger`` -- what happened to each request a proxy relayed.

A proxy between an MCP client and server sees every request go out and every
response come back, which makes it the one place that can answer, per
request: *was it sent? was it answered? could it have executed?* The ledger
records exactly that and nothing it cannot know.

Execution states
----------------
=====================  ======================================================
``PENDING``            Received from the client, not yet sent or answered.
``ANSWERED_LOCALLY``   The proxy answered it (a refusal, an escalation) and
                       never sent it. **Not executed** -- certain.
``SENT``               Handed to the server's transport; no answer yet.
``COMPLETED``          The server answered (a result or an error).
``UNKNOWN``            Sent, and no answer is coming: the transport closed,
                       a write failed part-way, or a deadline passed. The
                       request **may or may not have executed**. First-class,
                       never rounded to either side.
=====================  ======================================================

Every transition goes forward. ``UNKNOWN`` never becomes ``COMPLETED``: once
a client has been told the outcome is unknown, a late answer from the server
is recorded (``late_response``) but is not delivered -- the client already
has its one response.

Responses
---------
``response(id)`` returns a ``ResponseDisposition``:

* ``DELIVER`` -- the first answer to a request in flight;
* ``DUPLICATE`` -- the request was already answered;
* ``LATE`` -- the request was already resolved as ``UNKNOWN``;
* ``UNSOLICITED`` -- no request with this id was seen.

Only ``DUPLICATE`` and ``LATE`` say "the client already has its answer". A
proxy that drops those never delivers two responses for one request, and
never drops the only one. ``UNSOLICITED`` is for the proxy to pass through:
the ledger did not see the request, so it cannot know.

Id reuse
--------
MCP requires request ids to be unique within a session. The ledger does not
depend on it. Ids are tracked per invocation. A request that reuses the id of
a finished one starts a new invocation, and a response is then judged
against that new one. So a late duplicate of the old response is delivered
to the new request, exactly as it would be without a proxy -- no worse.
Reusing the id of a request *still in flight* is recorded as an anomaly.

What it does not do
-------------------
It decides nothing on its own: no retries, no timeouts it was not given, no
I/O. It is state, read by whoever owns the transport. Zero dependencies;
thread-safe.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

__all__ = [
    "ExecutionState",
    "Invocation",
    "InvocationLedger",
    "ResponseDisposition",
]

#: Finished invocations kept for duplicate detection, by default.
DEFAULT_RETAIN = 4096


class ExecutionState(StrEnum):
    PENDING = "pending"
    ANSWERED_LOCALLY = "answered_locally"
    SENT = "sent"
    COMPLETED = "completed"
    UNKNOWN = "unknown"

    @property
    def finished(self) -> bool:
        """The client has (or is being given) its one response."""
        return self in (
            ExecutionState.ANSWERED_LOCALLY,
            ExecutionState.COMPLETED,
            ExecutionState.UNKNOWN,
        )


class ResponseDisposition(StrEnum):
    DELIVER = "deliver"
    DUPLICATE = "duplicate"
    LATE = "late"
    UNSOLICITED = "unsolicited"


@dataclass
class Invocation:
    """One request's record. Mutated only by its ``InvocationLedger``."""

    request_id: Any
    method: str
    tool: str | None
    state: ExecutionState = ExecutionState.PENDING
    received_at: float = 0.0
    sent_at: float | None = None
    finished_at: float | None = None
    #: The server's answer was an error (``isError`` or a JSON-RPC error).
    is_error: bool | None = None
    #: Why the state is UNKNOWN: "transport_closed", "send_failed", "deadline".
    unknown_reason: str | None = None
    #: A server answer that arrived after the request was resolved UNKNOWN.
    late_response: bool = False
    late_is_error: bool | None = None
    duplicate_responses: int = 0
    history: list[ExecutionState] = field(default_factory=list)
    anomalies: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_id": self.request_id,
            "method": self.method,
            "tool": self.tool,
            "state": self.state.value,
            "is_error": self.is_error,
            "unknown_reason": self.unknown_reason,
            "late_response": self.late_response,
            "late_is_error": self.late_is_error,
            "duplicate_responses": self.duplicate_responses,
            "history": [state.value for state in self.history],
            "anomalies": list(self.anomalies),
        }


def _key(request_id: Any) -> Hashable:
    # JSON-RPC ids are strings or numbers; 1 and "1" are different ids.
    return (
        type(request_id).__name__,
        request_id if isinstance(request_id, Hashable) else repr(request_id),
    )


class InvocationLedger:
    """
    Execution state per relayed request. See the module docstring.

    *retain* bounds how many finished invocations are remembered for
    duplicate / late detection; the oldest are forgotten first, after which
    a response for one is ``UNSOLICITED`` (passed through, as without a
    ledger).
    """

    def __init__(
        self, *, retain: int = DEFAULT_RETAIN, clock: Callable[[], float] | None = None
    ) -> None:
        if retain < 1:
            raise ValueError(f"retain must be at least 1, got {retain}")
        self._retain = retain
        self._clock = clock if clock is not None else time.monotonic
        self._live: dict[Hashable, Invocation] = {}
        self._finished: OrderedDict[Hashable, Invocation] = OrderedDict()
        self._lock = threading.Lock()

    # -- recording ---------------------------------------------------------

    def received(self, request_id: Any, method: str, tool: str | None = None) -> Invocation:
        """A request arrived from the client."""
        key = _key(request_id)
        with self._lock:
            invocation = Invocation(request_id, method, tool, received_at=self._clock())
            invocation.history.append(ExecutionState.PENDING)
            previous = self._live.pop(key, None)
            if previous is not None:
                previous.anomalies.append("id reused while in flight")
                invocation.anomalies.append("id reused while a request with it was in flight")
                self._remember(key, previous)
            self._finished.pop(key, None)
            self._live[key] = invocation
            return invocation

    def answered_locally(self, request_id: Any) -> None:
        """The proxy answered the request itself; it was never sent."""
        self._finish(request_id, ExecutionState.ANSWERED_LOCALLY, expect=(ExecutionState.PENDING,))

    def sent(self, request_id: Any) -> None:
        """
        The request is about to be written to the server.

        Record this *before* writing: the server can answer before a write
        call returns, and an answer must find the request ``SENT``.
        """
        key = _key(request_id)
        with self._lock:
            invocation = self._live.get(key)
            if invocation is None:
                return
            if invocation.state is not ExecutionState.PENDING:
                invocation.anomalies.append(f"sent while {invocation.state.value}")
                return
            invocation.state = ExecutionState.SENT
            invocation.sent_at = self._clock()
            invocation.history.append(ExecutionState.SENT)

    def send_failed(self, request_id: Any) -> Invocation | None:
        """
        Writing the request failed. Part of it may have reached the server,
        so the outcome is ``UNKNOWN`` -- never "not executed".
        """
        return self._unknown(request_id, "send_failed")

    def response(self, request_id: Any, *, is_error: bool) -> ResponseDisposition:
        """A response with this id came from the server: deliver it or not?"""
        key = _key(request_id)
        with self._lock:
            invocation = self._live.get(key)
            if invocation is not None and invocation.state in (
                ExecutionState.SENT,
                ExecutionState.PENDING,
            ):
                if invocation.state is ExecutionState.PENDING:
                    invocation.anomalies.append("answered before it was recorded as sent")
                invocation.state = ExecutionState.COMPLETED
                invocation.is_error = is_error
                invocation.finished_at = self._clock()
                invocation.history.append(ExecutionState.COMPLETED)
                self._live.pop(key)
                self._remember(key, invocation)
                return ResponseDisposition.DELIVER

            finished = self._finished.get(key)
            if finished is None:
                return ResponseDisposition.UNSOLICITED
            if finished.state is ExecutionState.UNKNOWN:
                finished.late_response = True
                finished.late_is_error = is_error
                return ResponseDisposition.LATE
            finished.duplicate_responses += 1
            return ResponseDisposition.DUPLICATE

    def transport_closed(self) -> list[Invocation]:
        """
        The server's transport closed. Everything ``SENT`` is now ``UNKNOWN``
        and is returned, so the caller can tell each client so. ``PENDING``
        requests are left: they were never written, and the caller decides
        whether to answer them locally.
        """
        with self._lock:
            keys = [k for k, inv in self._live.items() if inv.state is ExecutionState.SENT]
        return [inv for k in keys if (inv := self._unknown_by_key(k, "transport_closed"))]

    def expire(self, deadline: float, now: float | None = None) -> list[Invocation]:
        """``SENT`` requests older than *deadline* seconds become ``UNKNOWN``."""
        at = self._clock() if now is None else now
        with self._lock:
            keys = [
                k
                for k, inv in self._live.items()
                if inv.state is ExecutionState.SENT
                and inv.sent_at is not None
                and at - inv.sent_at >= deadline
            ]
        return [inv for k in keys if (inv := self._unknown_by_key(k, "deadline"))]

    # -- reading -------------------------------------------------------------

    def get(self, request_id: Any) -> Invocation | None:
        key = _key(request_id)
        with self._lock:
            return self._live.get(key) or self._finished.get(key)

    def outstanding(self) -> list[Invocation]:
        with self._lock:
            return list(self._live.values())

    def finished(self) -> list[Invocation]:
        with self._lock:
            return list(self._finished.values())

    # -- internals -----------------------------------------------------------

    def _finish(
        self, request_id: Any, state: ExecutionState, *, expect: tuple[ExecutionState, ...]
    ) -> None:
        key = _key(request_id)
        with self._lock:
            invocation = self._live.get(key)
            if invocation is None:
                return
            if invocation.state not in expect:
                invocation.anomalies.append(f"{state.value} while {invocation.state.value}")
                return
            invocation.state = state
            invocation.finished_at = self._clock()
            invocation.history.append(state)
            self._live.pop(key)
            self._remember(key, invocation)

    def _unknown(self, request_id: Any, reason: str) -> Invocation | None:
        return self._unknown_by_key(_key(request_id), reason)

    def _unknown_by_key(self, key: Hashable, reason: str) -> Invocation | None:
        with self._lock:
            invocation = self._live.get(key)
            if invocation is None or invocation.state not in (
                ExecutionState.SENT,
                ExecutionState.PENDING,
            ):
                return None
            invocation.state = ExecutionState.UNKNOWN
            invocation.unknown_reason = reason
            invocation.finished_at = self._clock()
            invocation.history.append(ExecutionState.UNKNOWN)
            self._live.pop(key)
            self._remember(key, invocation)
            return invocation

    def _remember(self, key: Hashable, invocation: Invocation) -> None:
        self._finished[key] = invocation
        self._finished.move_to_end(key)
        while len(self._finished) > self._retain:
            self._finished.popitem(last=False)

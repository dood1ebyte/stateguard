"""
A StateGuard relay for MCP over stdio -- no MCP SDK required.

Put it where an MCP client expects a server command::

    python examples/mcp/stdio_proxy.py [--shadow] [--audit FILE] -- python my_server.py

It launches the real server, and every message passes through it verbatim,
byte for byte, except three it acts on:

``tools/list`` responses (and their pages)
    Collected with ``ToolListAccumulator`` and synced into a
    ``ToolRegistry``, so the relay always holds the server's current
    definitions. The responses themselves still reach the client untouched.
``notifications/tools/list_changed``
    Passed on, and the relay re-lists the tools itself -- with request ids
    in its own namespace, whose responses it consumes -- so validation never
    continues against a listing the server has replaced. Calls that arrive
    while the refresh is in flight wait for it.
``tools/call`` requests
    Validated against the tool's *current* schema. Before the relay changes
    or blocks a call it re-lists the tools and decides again (see below). ``FORWARD`` sends the
    original request, with only ``params.arguments`` replaced when it was
    repaired; ``HOLD`` (shadow) sends it unchanged; ``ESCALATE`` / ``REFUSE``
    are answered by the relay with a ``CallToolResult`` carrying
    ``isError: true`` and the reason, and never reach the server.

Never acting on a stale listing
-------------------------------
Under protocol 2026-07-28 a server announces ``tools/list_changed`` only on
``subscriptions/listen`` streams, which the relay does not hold -- so the
relay cannot rely on hearing that a schema changed. Validating against a
listing the server has replaced would refuse calls that are now valid, or
"repair" them back into the old shape.

So the relay never *intervenes* (repairs, refuses, escalates -- anything but
forwarding the call unchanged) on a listing it has not just confirmed: it
sends its own ``tools/list``, waits for a listing that was requested after
the call arrived, and decides again on that. A call that passes unchanged
needs no refresh -- the server decides it either way. If no fresh listing
arrives in time, the call is forwarded unchanged. ``--no-listing-refresh``
turns this off (one fewer round trip per intervention, at the risk above).

Execution state
---------------
Every client request is recorded in an ``InvocationLedger``: pending, sent,
completed, answered by the relay, or **unknown** -- sent, and no answer is
coming. The relay uses it, in auto mode, for two things only:

* **Never two answers for one request.** A second response for a request
  that was already answered (or already resolved as unknown) is dropped and
  logged; the first is the client's.
* **Never silence instead of an answer.** If the server's pipe closes, every
  request in flight gets an explicit JSON-RPC error saying the outcome is
  unknown -- it **may or may not have executed** -- instead of no answer at
  all. With ``--call-deadline SECONDS`` the same happens to a request the
  server has not answered in time (off by default: a long-running tool is
  not a failure). The relay never retries and never claims a call failed or
  succeeded when it does not know.

Retry
-----
A call that failed or went unanswered is sent again only when the operator
has declared the tool READ_ONLY or IDEMPOTENT (``--effects FILE``), and only
with ``--retry auto``. The rule is ``decide_retry``: never after an
authorization or protocol error, never past the attempt budget (one retry),
never for a tool not declared safe to repeat -- an unknown outcome on such a
tool is escalated as "outcome unknown". Tool annotations
(``readOnlyHint`` ...) are recorded as hints and never decide anything. The
default, ``--retry shadow``, logs every decision and retries nothing.

Shadow mode logs what it would have done and changes nothing.

Results are never touched, except that the answer to a retried call carries
the client's request id. They are *checked* (``check_tool_result``): a
result that is not a ``CallToolResult``, or whose ``structuredContent``
breaks the tool's ``outputSchema``, is logged as category H -- and delivered
exactly as the server sent it. A response from the server is written to the
client as the exact bytes the server produced.

Policy, stated rather than buried
---------------------------------
The relay sits in the call path, so it **fails open**: whenever StateGuard
cannot decide, the call goes to the server exactly as the client sent it --
the behaviour the client would have had with no relay at all.

* A call to a tool the relay has no definition for is forwarded unvalidated
  -- whether the tool exists is the server's question, and it will answer it.
* A call to a tool whose schema StateGuard cannot read is forwarded
  unvalidated by default (``--unreadable forward``). ``--unreadable refuse``
  blocks such calls instead; that is a deliberate fail-closed choice, and it
  blocks calls the server may well accept.
* An internal StateGuard error -- an exception, or the forwarding gate
  rejecting StateGuard's own payload -- forwards the original call and is
  logged as ``category: "Z"`` (StateGuard-induced). It never turns into an
  error the client sees, and never stops the relay.
* ``--passthrough`` turns StateGuard off entirely: a byte relay, the
  baseline every measurement is compared against.

Audit log
---------
One JSON object per line (``--audit FILE``; stderr by default): every
listing sync (added / removed / updated / breaking, description changes) and
every call decision (action, what auto mode would do, the reason, and the
field decisions -- paths, kinds and trust, never argument values).

Scope
-----
An example of where StateGuard sits on the wire, kept small enough to read:
no reconnection, no timeouts beyond the listing wait, one server per relay.
MCP's stdio transport is newline-delimited JSON-RPC, which is all this
parses.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import subprocess
import sys
import threading
from typing import IO, Any

from stateguard import ContractGuard
from stateguard.adapters.mcp import (
    EffectPolicy,
    ForwardingInvariantError,
    InvocationLedger,
    MalformedMessageError,
    MCPAction,
    Observed,
    ResponseDisposition,
    ToolCallOutcome,
    ToolCallRequest,
    ToolListAccumulator,
    ToolNotFoundError,
    ToolRegistry,
    ToolSchemaUnusableError,
    check_tool_result,
    decide_retry,
    forward_message,
    refusal_response,
)
from stateguard.adapters.mcp.jsonrpc import (
    JSONRPC_VERSION,
    PROTOCOL_VERSION_META_KEY,
    TOOLS_CALL,
    TOOLS_LIST,
    TOOLS_LIST_CHANGED,
    is_notification,
    is_request,
    is_response,
    request_id,
    tool_error_response,
)
from stateguard.core.models.config import GuardConfig, RepairMode

#: Prefix for the ids of the relay's own requests, so their responses can be
#: told apart from the client's. A client is free to choose any id; one that
#: chose this prefix would have its tools/list responses consumed here.
OWN_ID_PREFIX = "stateguard-relay:"
#: Ids of retried tools/call attempts; their answers go to the client under its own id.
RETRY_ID_PREFIX = "stateguard-retry:"

#: How long a tools/call waits for a listing in flight before it is
#: validated against what the relay has (and the audit log says so).
LISTING_WAIT_SECONDS = 10.0


class StdioRelay:
    def __init__(
        self,
        guard: ContractGuard,
        client_in: IO[bytes],
        client_out: IO[bytes],
        server_in: IO[bytes],
        server_out: IO[bytes],
        audit: IO[str],
        *,
        unreadable: str = "forward",
        passthrough: bool = False,
        refresh_before_intervening: bool = True,
        call_deadline: float | None = None,
        effects: EffectPolicy | None = None,
        retry: str = "shadow",
    ) -> None:
        self.registry = ToolRegistry(guard)
        self._client_in, self._client_out = client_in, client_out
        self._server_in, self._server_out = server_in, server_out
        self._audit = audit
        self._unreadable = unreadable
        # The kill switch: relay bytes, decide nothing, learn nothing.
        self._passthrough = passthrough
        self._refresh_before_intervening = refresh_before_intervening
        self.ledger = InvocationLedger()
        self._call_deadline = call_deadline
        self._effects = effects if effects is not None else EffectPolicy()
        if retry not in ("off", "shadow", "auto"):
            raise ValueError(f"retry must be off, shadow or auto, got {retry!r}")
        # Shadow mode never acts, whatever the retry setting.
        self._retry_mode = "shadow" if guard.config.mode is RepairMode.SHADOW else retry
        self._retry_ids = (f"{RETRY_ID_PREFIX}{n}" for n in itertools.count(1))
        self._calls: dict[Any, tuple[bytes, str | None]] = {}  # client id -> (payload, tool)
        self._attempts: dict[Any, int] = {}  # client id -> attempts sent
        self._client_of: dict[Any, Any] = {}  # retry id -> client id
        self._retry_lock = threading.Lock()
        self._stopped = threading.Event()
        # Freshness: every relay-initiated listing gets an epoch. A call may
        # only be intervened on after a listing whose epoch began after the
        # call arrived has completed -- an older listing in flight cannot
        # vouch for the server's state now.
        self._epoch_counter = itertools.count(1)
        self._epoch_of: dict[Any, int] = {}  # own request id -> epoch
        self._first_page_ids: set[Any] = set()
        self._chain_epoch = 0  # epoch of the listing being accumulated
        self._fresh_epoch = 0  # newest epoch whose listing completed
        self._failed_epochs: set[int] = set()
        self._listing_done = threading.Condition()
        self._client_lists = ToolListAccumulator()
        self._own_lists = ToolListAccumulator()
        self._own_ids = (f"{OWN_ID_PREFIX}{n}" for n in itertools.count(1))
        self._listing_ready = threading.Event()
        # Protocol revisions without an initialize handshake (2026-07-28 on)
        # put the version on every request, in params._meta. The relay's own
        # requests must say the same, or the server reads them as another
        # protocol version entirely.
        self._protocol_meta: dict[str, Any] = {}
        self._listed = False
        # True from tools/list_changed until a refresh succeeds: calls made
        # meanwhile are validated against the last listing, and say so.
        self._listing_stale = False
        # Shadow observes; it never blocks. That holds for the paths that are
        # not outcomes too -- an unreadable schema, an internal error.
        self._shadow = guard.config.mode is RepairMode.SHADOW
        self._to_client, self._to_server, self._to_audit = (threading.Lock() for _ in range(3))

    # -- plumbing ------------------------------------------------------------

    def _send(self, stream: IO[bytes], lock: threading.Lock, line: bytes) -> None:
        with lock:
            stream.write(line if line.endswith(b"\n") else line + b"\n")
            stream.flush()

    def _send_json(self, stream: IO[bytes], lock: threading.Lock, message: Any) -> None:
        self._send(stream, lock, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def _log(self, event: str, **fields: Any) -> None:
        with self._to_audit:
            self._audit.write(json.dumps({"event": event, **fields}, default=str) + "\n")
            self._audit.flush()

    def _request_listing(self, cursor: str | None = None, epoch: int | None = None) -> int:
        """Send a relay-owned ``tools/list`` (page); return its listing epoch."""
        self._listed = True
        params: dict[str, Any] = {} if cursor is None else {"cursor": cursor}
        if self._protocol_meta:
            params["_meta"] = dict(self._protocol_meta)
        identifier = next(self._own_ids)
        with self._listing_done:
            epoch = next(self._epoch_counter) if epoch is None else epoch
            self._epoch_of[identifier] = epoch
            if cursor is None:
                self._first_page_ids.add(identifier)
        message = {
            "jsonrpc": JSONRPC_VERSION,
            "id": identifier,
            "method": TOOLS_LIST,
            "params": params,
        }
        self._own_lists.observe_request(message)
        self._send_json(self._server_in, self._to_server, message)
        return epoch

    def _finish_epoch(self, epoch: int, *, ok: bool) -> None:
        with self._listing_done:
            if ok:
                self._fresh_epoch = max(self._fresh_epoch, epoch)
            else:
                self._failed_epochs.add(epoch)
            self._listing_done.notify_all()

    def _refresh_listing(self) -> bool:
        """
        Re-list and wait for a listing requested *now*. True if one completed.

        A later epoch completing also counts -- it was requested later still.
        False on an error response, or after ``LISTING_WAIT_SECONDS``.
        """
        epoch = self._request_listing()
        with self._listing_done:
            self._listing_done.wait_for(
                lambda: self._fresh_epoch >= epoch or epoch in self._failed_epochs,
                timeout=LISTING_WAIT_SECONDS,
            )
            return self._fresh_epoch >= epoch

    def _sync(self, tools: list[Any], source: str) -> None:
        report = self.registry.sync(tools)
        self._listing_stale = False
        self._log(
            "tools_synced",
            source=source,
            summary=report.to_dict()["summary"],
            changes=[
                {
                    "tool": str(c.key),
                    "kind": c.kind.value,
                    "breaking": c.breaking,
                    "description_changed": c.description_changed,
                    "state": c.current.state.value if c.current else None,
                }
                for c in report.changes
                if c.kind.value != "unchanged" or c.changed_fields
            ],
        )
        self._listing_ready.set()

    # -- client -> server ----------------------------------------------------

    def pump_client(self) -> None:
        try:
            self._pump_client()
        except OSError as exc:  # the server's stdin is gone: the server exited
            self._log("transport_closed", category="E", direction="client->server", error=str(exc))
        finally:
            with self._to_server, contextlib.suppress(OSError):
                self._server_in.close()

    def _pump_client(self) -> None:
        for line in self._client_in:
            if self._passthrough:
                self._send(self._server_in, self._to_server, line)
                continue
            message = _parse(line)
            identifier = self._track(message)
            if is_request(message, TOOLS_CALL):
                self._on_call(line, message, identifier)
                continue
            try:
                first_versioned = self._remember_protocol(message)
                if is_request(message, TOOLS_LIST):
                    self._client_lists.observe_request(message)
            except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
                self._internal_error("client message", exc)
                first_versioned = False
            self._send_tracked(identifier, line)
            # Learn the tools before the first call: after the handshake under
            # the initialize-based protocol, at the first versioned request
            # under the stateless one.
            if not self._listed and (
                is_notification(message, "notifications/initialized") or first_versioned
            ):
                self._request_listing()

    def _track(self, message: Any) -> Any:
        """Record a client request in the ledger; its id, or None if not a request."""
        if not isinstance(message, dict) or "method" not in message or "id" not in message:
            return None
        identifier = message["id"]
        if identifier is None:
            return None
        params = message.get("params")
        tool = params.get("name") if isinstance(params, dict) else None
        try:
            self.ledger.received(
                identifier, str(message["method"]), tool if isinstance(tool, str) else None
            )
        except Exception as exc:  # noqa: BLE001 -- fail open
            self._internal_error("ledger", exc)
            return None
        return identifier

    def _send_tracked(self, identifier: Any, line: bytes) -> None:
        """Write a client request to the server, recording it as sent first."""
        if identifier is not None:
            self.ledger.sent(identifier)
        try:
            self._send(self._server_in, self._to_server, line)
        except OSError:
            if identifier is not None:
                self._resolve_unknown(
                    [self.ledger.send_failed(identifier)],
                    Observed.UNKNOWN_TRANSPORT,
                    connection_available=False,
                )
            raise

    def _surface_unknown(self, *invocations: Any) -> None:
        """Tell each client its request's outcome is unknown (auto); log it (both)."""
        for invocation in invocations:
            if invocation is None:
                continue
            reason = invocation.unknown_reason or "unknown"
            client_id = self._client_of.get(invocation.request_id, invocation.request_id)
            self._forget(client_id)
            self._log(
                "outcome_unknown",
                category="I",
                id=client_id,
                method=invocation.method,
                tool=invocation.tool,
                reason=reason,
                surfaced=not self._shadow,
            )
            if self._shadow:
                continue
            with contextlib.suppress(OSError):
                self._send_json(
                    self._client_out,
                    self._to_client,
                    unknown_outcome_response(client_id, reason),
                )

    def run_deadlines(self) -> None:
        """With ``call_deadline`` set: resolve unanswered requests as unknown. Runs until stop."""
        if self._call_deadline is None:
            return
        tick = min(0.05, self._call_deadline / 4)
        while not self._stopped.wait(tick):
            try:
                self._resolve_unknown(
                    self.ledger.expire(self._call_deadline), Observed.UNKNOWN_DEADLINE
                )
            except Exception as exc:  # noqa: BLE001 -- never let the deadline thread die
                self._internal_error("deadline", exc)

    # -- retry ---------------------------------------------------------------

    def _resolve_unknown(
        self,
        invocations: list[Any],
        observed: Observed,
        *,
        connection_available: bool = True,
    ) -> None:
        """Requests whose outcome is unknown: retry the ones that may be, surface the rest."""
        for invocation in invocations:
            if invocation is None:
                continue
            client_id = self._client_of.get(invocation.request_id, invocation.request_id)
            if self._consider_retry(client_id, observed, connection_available=connection_available):
                continue
            self._surface_unknown(invocation)

    def _consider_retry(
        self, client_id: Any, observed: Observed, *, connection_available: bool = True
    ) -> bool:
        """Log the retry decision for a tools/call; send the retry if auto says so."""
        with self._retry_lock:
            call = self._calls.get(client_id)
            attempts = self._attempts.get(client_id, 1)
        if call is None or self._retry_mode == "off":
            return False
        payload, tool = call
        entry = self.registry.find(tool) if tool else None
        declaration = self._effects.of(
            tool or "", annotations=entry.annotations if entry is not None else None
        )
        verdict = decide_retry(
            observed,
            declaration,
            attempts_made=attempts,
            connection_available=connection_available,
        )
        acted = verdict.retry and self._retry_mode == "auto"
        self._log(
            "retry_decision",
            category="F" if observed is Observed.TOOL_ERROR else "I",
            id=client_id,
            tool=tool,
            observed=observed.value,
            attempts=attempts,
            effect=declaration.to_dict(),
            decision=verdict.decision.value,
            reason=verdict.reason,
            acted=acted,
        )
        if not acted:
            return False
        retry_id = next(self._retry_ids)
        message = json.loads(payload)
        message["id"] = retry_id
        with self._retry_lock:
            self._client_of[retry_id] = client_id
            self._attempts[client_id] = attempts + 1
        self.ledger.received(retry_id, TOOLS_CALL, tool)
        with contextlib.suppress(OSError):  # resolved as unknown inside _send_tracked
            self._send_tracked(retry_id, self._encode(message))
        return True

    def _forget(self, client_id: Any) -> None:
        with self._retry_lock:
            self._calls.pop(client_id, None)
            self._attempts.pop(client_id, None)

    def _check_result(self, client_id: Any, message: dict[str, Any]) -> None:
        """Log a tools/call result that breaks its tool's contract. Never alters it."""
        if "result" not in message:
            return
        invocation = self.ledger.get(client_id)
        if invocation is None or invocation.method != TOOLS_CALL:
            return
        entry = self.registry.find(invocation.tool) if invocation.tool else None
        check = check_tool_result(
            entry.definition if entry is not None else None, message["result"]
        )
        if check.issues:
            self._log(
                "result_check",
                category="H" if not check.ok else None,
                id=client_id,
                tool=invocation.tool,
                **check.to_dict(),
            )

    def _deliver_as(self, client_id: Any, message: dict[str, Any]) -> None:
        """Write a retried call's answer to the client, under the client's id."""
        self._forget(client_id)
        self._check_result(client_id, message)
        self._send_json(self._client_out, self._to_client, {**message, "id": client_id})

    def _internal_error(self, where: str, exc: BaseException, **fields: Any) -> None:
        """Record a StateGuard failure (category Z). The caller fails open."""
        self._log(
            "internal_error",
            category="Z",
            where=where,
            error=f"{type(exc).__name__}: {exc}",
            **fields,
        )

    def _remember_protocol(self, message: Any) -> bool:
        """Record the request's protocol _meta; True the first time one is seen."""
        params = message.get("params") if isinstance(message, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict) or PROTOCOL_VERSION_META_KEY not in meta:
            return False
        first = not self._protocol_meta
        self._protocol_meta = {
            key: value for key, value in meta.items() if key.startswith("io.modelcontextprotocol/")
        }
        return first

    def _on_call(self, line: bytes, message: Any, identifier: Any = None) -> None:
        """
        Decide, then send exactly once.

        ``_decide`` only computes where the call goes and with which bytes;
        this method does the one write. So an exception during the decision
        fails open (the original line goes to the server) and an exception
        during the write cannot cause a second send -- a relay that retried
        a write it could not confirm would risk executing the call twice.
        """
        try:
            to_server, payload = self._decide(line, message)
        except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
            self._internal_error("tools/call", exc, id=request_id(message), action="unvalidated")
            to_server, payload = True, line
        if to_server:
            if identifier is not None and self._retry_mode != "off":
                tool = message.get("params", {}).get("name") if isinstance(message, dict) else None
                with self._retry_lock:
                    self._calls[identifier] = (payload, tool if isinstance(tool, str) else None)
                    self._attempts[identifier] = 1
            self._send_tracked(identifier, payload)
        else:
            if identifier is not None:
                self.ledger.answered_locally(identifier)
            self._send(self._client_out, self._to_client, payload)

    @staticmethod
    def _encode(message: Any) -> bytes:
        return json.dumps(message, separators=(",", ":")).encode("utf-8")

    def _decide(self, line: bytes, message: Any) -> tuple[bool, bytes]:
        """``(to_server, bytes)`` for one ``tools/call``. Never writes to a stream."""
        try:
            request = ToolCallRequest.parse(message)
        except MalformedMessageError:
            return True, line  # the server's to reject

        stale = not self._listing_ready.wait(LISTING_WAIT_SECONDS) or self._listing_stale
        evaluated = self._evaluate(line, request)
        if isinstance(evaluated, tuple):
            return evaluated
        outcome, listing = evaluated, "stale" if stale else "current"

        if self._refresh_before_intervening and _intervenes(request, outcome):
            # About to change or block the call: only on a listing that is
            # known to be the server's current one. See the module docstring.
            if not self._refresh_listing():
                self._log(
                    "call",
                    id=request.id,
                    tool=request.name,
                    action="unvalidated",
                    auto_action=(outcome.auto_action or outcome.action).value,
                    reason="could not confirm the tool listing is current; "
                    "forwarded unchanged rather than act on a possibly stale schema",
                    listing="unconfirmed",
                )
                return True, line
            evaluated = self._evaluate(line, request)
            if isinstance(evaluated, tuple):
                return evaluated
            outcome, listing = evaluated, "confirmed"

        self._log(
            "call",
            id=request.id,
            tool=request.name,
            action=outcome.action.value,
            auto_action=(outcome.auto_action or outcome.action).value,
            reason=outcome.reason,
            listing=listing,
            decisions=[
                {
                    "path": d.path,
                    "decision": d.kind.value,
                    "op": d.operation.op_type.value if d.operation else None,
                    "trust": round(d.operation.trust, 3) if d.operation else None,
                }
                for d in outcome.result.field_decisions()
            ],
        )
        forwarded = forward_message(request, outcome)
        if forwarded is None:
            return False, self._encode(refusal_response(request, outcome))
        if forwarded == request.message:
            return True, line  # byte-for-byte
        return True, self._encode(forwarded)

    def _evaluate(
        self, line: bytes, request: ToolCallRequest
    ) -> ToolCallOutcome | tuple[bool, bytes]:
        """The outcome against the current registry, or a final ``(to_server, bytes)``."""
        try:
            return self.registry.process_call(request.name, request.arguments)
        except ToolNotFoundError:
            self._log(
                "call",
                id=request.id,
                tool=request.name,
                action="unvalidated",
                reason="no definition for this tool; forwarded for the server to answer",
            )
            return True, line
        except ToolSchemaUnusableError as exc:
            return self._on_unreadable(line, request, exc)
        except ForwardingInvariantError as exc:
            # The gate rejected StateGuard's *own* payload. The client's
            # arguments are not suspect -- forward those, as if no relay.
            self._internal_error("forwarding gate", exc, id=request.id, action="unvalidated")
            return True, line

    def _on_unreadable(
        self, line: bytes, request: ToolCallRequest, exc: ToolSchemaUnusableError
    ) -> tuple[bool, bytes]:
        if self._unreadable == "forward" or self._shadow:
            self._log(
                "call",
                id=request.id,
                tool=request.name,
                action="unvalidated",
                reason=f"schema unreadable; forwarded ({'shadow' if self._shadow else 'policy'}): "
                f"{exc.cause}",
            )
            return True, line
        self._log(
            "call",
            id=request.id,
            tool=request.name,
            action="refuse",
            reason=f"schema unreadable: {exc.cause}",
        )
        text = (
            f"Not sent: StateGuard cannot read the input schema of {request.name!r}, "
            f"so it cannot check calls to it. {exc.cause}"
        )
        return False, self._encode(tool_error_response(request, text))

    # -- server -> client ----------------------------------------------------

    def pump_server(self) -> None:
        try:
            self._pump_server()
            if not self._passthrough:
                # The server's stdout closed: nothing in flight will be answered.
                self._resolve_unknown(
                    self.ledger.transport_closed(),
                    Observed.UNKNOWN_TRANSPORT,
                    connection_available=False,
                )
        except OSError as exc:  # the client's stdout is gone
            self._log("transport_closed", category="E", direction="server->client", error=str(exc))
        finally:
            self._stopped.set()

    def _pump_server(self) -> None:
        for line in self._server_out:
            if self._passthrough:
                self._send(self._client_out, self._to_client, line)
                continue
            message = _parse(line)
            identifier = request_id(message)
            if (
                is_response(message)
                and isinstance(identifier, str)
                and identifier.startswith(OWN_ID_PREFIX)
            ):
                try:
                    self._on_own_listing(message)
                except Exception as exc:  # noqa: BLE001 -- fail open
                    self._internal_error("relay tools/list", exc)
                    self._finish_epoch(self._epoch_of.get(identifier, self._chain_epoch), ok=False)
                    self._listing_ready.set()  # never hold calls on a listing that failed
                continue  # the client never asked for this one
            if is_response(message) and identifier is not None and not self._deliverable(message):
                continue
            try:
                if is_response(message):
                    listing = self._client_lists.observe_response(message)
                    if listing is not None:
                        self._sync(listing, source="client tools/list")
                elif is_notification(message, TOOLS_LIST_CHANGED):
                    self._listing_stale = True
                    self._listing_ready.clear()
                    self._request_listing()
            except Exception as exc:  # noqa: BLE001 -- fail open
                self._internal_error("server message", exc)
                self._listing_ready.set()
            self._send(self._client_out, self._to_client, line)  # untouched

    def _deliverable(self, message: dict[str, Any]) -> bool:
        """Whether a server response goes to the client: not if it already has its answer."""
        identifier = message["id"]
        try:
            result = message.get("result")
            is_error = "error" in message or (
                isinstance(result, dict) and result.get("isError") is True
            )
            observed = Observed.PROTOCOL_ERROR if "error" in message else Observed.TOOL_ERROR
            disposition = self.ledger.response(identifier, is_error=is_error)
            if isinstance(identifier, str) and identifier.startswith(RETRY_ID_PREFIX):
                # Never delivered raw: the client knows its own id, not ours.
                client_id = self._client_of.get(identifier)
                if disposition is ResponseDisposition.DELIVER and client_id is not None:
                    if not (is_error and self._consider_retry(client_id, observed)):
                        self._deliver_as(client_id, message)
                else:
                    self._log(
                        "response_dropped",
                        category="J",
                        id=identifier,
                        disposition=disposition.value,
                    )
                return False
            if disposition is ResponseDisposition.DELIVER:
                if is_error and self._consider_retry(identifier, observed):
                    return False
                self._forget(identifier)
                self._check_result(identifier, message)
        except Exception as exc:  # noqa: BLE001 -- fail open: deliver
            self._internal_error("ledger", exc)
            return True
        if disposition in (ResponseDisposition.DELIVER, ResponseDisposition.UNSOLICITED):
            return True
        self._log(
            "response_dropped" if not self._shadow else "response_would_drop",
            category="J",
            id=message["id"],
            disposition=disposition.value,
            is_error=is_error,
        )
        return self._shadow  # shadow never changes traffic

    def _on_own_listing(self, message: Any) -> None:
        identifier = request_id(message)
        with self._listing_done:
            epoch = self._epoch_of.pop(identifier, 0)
            if identifier in self._first_page_ids:
                self._first_page_ids.discard(identifier)
                if "result" in message:
                    self._chain_epoch = epoch  # this listing starts accumulating now
        listing = self._own_lists.observe_response(message)
        if listing is not None:
            self._sync(listing, source="relay tools/list")
            self._finish_epoch(self._chain_epoch, ok=True)
            return
        result = message.get("result")
        cursor = result.get("nextCursor") if isinstance(result, dict) else None
        if isinstance(cursor, str) and cursor:
            self._request_listing(cursor, epoch)
        elif "error" in message:
            self._log("tools_list_failed", error=message["error"])
            self._finish_epoch(epoch, ok=False)
            self._listing_ready.set()  # do not hold calls forever


#: JSON-RPC implementation-defined server error: the outcome is unknown.
UNKNOWN_OUTCOME_CODE = -32000
UNKNOWN_OUTCOME_KEY = "io.stateguard/executionState"


def unknown_outcome_response(identifier: Any, reason: str) -> dict[str, Any]:
    """
    The relay's answer for a request whose outcome it cannot know.

    A JSON-RPC error, not a tool result: no tool produced it, and a client
    must not read it as the tool's verdict. The message says plainly that
    the request may have executed, so nobody retries a non-idempotent call
    on the strength of it.
    """
    why = {
        "transport_closed": "the MCP server's connection closed before it answered",
        "deadline": "the MCP server did not answer within the relay's deadline",
        "send_failed": "the request could not be fully written to the MCP server",
    }.get(reason, "the relay cannot determine the outcome")
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": identifier,
        "error": {
            "code": UNKNOWN_OUTCOME_CODE,
            "message": (
                f"Outcome unknown: {why}. The request may or may not have been executed; "
                "do not retry it unless it is safe to run twice."
            ),
            "data": {UNKNOWN_OUTCOME_KEY: "unknown", "io.stateguard/reason": reason},
        },
    }


def _intervenes(request: ToolCallRequest, outcome: ToolCallOutcome) -> bool:
    """
    Whether auto mode would do anything but forward the call as received.

    Judged on ``auto_action`` / ``proposed_arguments``, not on this mode's
    action: in shadow the call is forwarded regardless, but the recorded
    decision is what a shadow rollout is measured on, so it must not come
    from a stale listing either.
    """
    if outcome.auto_action is not MCPAction.FORWARD:
        return True
    received = {} if request.arguments is None else request.arguments
    return outcome.proposed_arguments != received or type(received) is not dict


def _parse(line: bytes) -> Any:
    try:
        return json.loads(line)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None  # not ours to understand: relayed as-is


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1] if __doc__ else None)
    parser.add_argument(
        "--shadow", action="store_true", help="Observe and log; never change a call."
    )
    parser.add_argument("--audit", metavar="FILE", help="Audit log (JSON lines). Default: stderr.")
    parser.add_argument(
        "--unreadable",
        choices=["refuse", "forward"],
        default="forward",
        help="Calls to a tool whose schema StateGuard cannot read (default: forward, unvalidated).",
    )
    parser.add_argument(
        "--no-listing-refresh",
        action="store_true",
        help="Do not re-list tools before repairing or blocking a call (see the module docs).",
    )
    parser.add_argument(
        "--call-deadline",
        type=float,
        metavar="SECONDS",
        help="Answer 'outcome unknown' to a request unanswered after SECONDS (default: off).",
    )
    parser.add_argument(
        "--effects",
        metavar="FILE",
        help='Operator effect declarations: {"tools": [{"name", "server"?, "effect"}]}.',
    )
    parser.add_argument(
        "--retry",
        choices=["off", "shadow", "auto"],
        default="shadow",
        help="Retry tools declared read_only/idempotent (default: shadow, log only).",
    )
    parser.add_argument(
        "--passthrough",
        action="store_true",
        help="Kill switch: relay bytes only, StateGuard off. The measurement baseline.",
    )
    parser.add_argument("server", nargs=argparse.REMAINDER, help="-- then the server command")
    args = parser.parse_args(argv)
    command = args.server[1:] if args.server[:1] == ["--"] else args.server
    if not command:
        parser.error("give the server command after --")

    mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
    audit = open(args.audit, "a", encoding="utf-8") if args.audit else sys.stderr  # noqa: SIM115
    server = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
    assert server.stdin is not None and server.stdout is not None
    relay = StdioRelay(
        ContractGuard.with_mcp(config=GuardConfig(mode=mode)),
        sys.stdin.buffer,
        sys.stdout.buffer,
        server.stdin,
        server.stdout,
        audit,
        unreadable=args.unreadable,
        passthrough=args.passthrough,
        refresh_before_intervening=not args.no_listing_refresh,
        call_deadline=args.call_deadline,
        effects=EffectPolicy.from_file(args.effects) if args.effects else None,
        retry=args.retry,
    )
    relay._log(
        "relay_started",
        mode="passthrough" if args.passthrough else mode.value,
        server=command,
    )
    to_server = threading.Thread(target=relay.pump_client, daemon=True)
    to_server.start()
    threading.Thread(target=relay.run_deadlines, daemon=True).start()
    relay.pump_server()  # until the server closes its stdout
    return server.wait()


if __name__ == "__main__":
    sys.exit(main())

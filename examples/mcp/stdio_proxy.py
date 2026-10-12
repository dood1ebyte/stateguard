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

The relay's own requests respect the session
--------------------------------------------
The relay sends a request of its own (``tools/list``) only once the MCP
session allows it, and it learns that from the *server's* answers, never
from what the client merely tries: an SDK may probe the stateless protocol
(``server/discover``) and fall back to ``initialize`` when the server does not
speak it. Under the initialize protocol the session is usable once the
server has answered the client's ``initialize`` and the client's
``notifications/initialized`` has reached that server process; under the
stateless one, once the server has answered a request carrying a protocol
version. Until then a listing is deferred (``tools_list_deferred``), never
sent. After a restart the same rule holds for the new process: the replayed
handshake comes first.

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
A call that *knowingly* failed (a tool error) is sent again only when the
operator has declared the tool READ_ONLY or IDEMPOTENT (``--effects FILE``),
and only with ``--retry auto``. The rule is ``decide_retry``: never after an
authorization or protocol error, never past the attempt budget (one retry),
never for a tool not declared safe to repeat, and **never after an unknown
outcome** (no answer by the deadline, or the connection lost with the
request in flight) -- whatever the declared effect, that is escalated as
"outcome unknown" and never re-sent. Tool annotations
(``readOnlyHint`` ...) are recorded as hints and never decide anything. The
default, ``--retry shadow``, logs every decision and retries nothing.

Auth failures
-------------
Every error answer to a request is classified by
``stateguard.adapters.mcp.classify_response``, which applies only fixed,
documented rules. These are structured ``error.data`` fields (an HTTP
status of 401 / 403, or an RFC 6749 / 6750 OAuth code) and a few exact text
shapes (``401 Unauthorized``, ``HTTP 403``, ``error="invalid_token"``, a bare
``Unauthorized:``). No JSON-RPC code counts -- MCP has none for auth.

An authentication or authorization failure is logged as category K
(``auth_failure``, naming the rule that fired). It is **never retried**,
whatever the operator declared about the tool, and is delivered exactly as
the server sent it. StateGuard never touches credentials. A late answer to a
request already resolved "outcome unknown" is dropped unread: UNKNOWN stays
UNKNOWN.

Server restart (opt-in)
-----------------------
With ``--restart N`` the relay restarts a server process that exits, at most
N times in its lifetime, and only when it is safe:

* **Idle death** (nothing in flight): the server is restarted, the
  ``initialize`` handshake is replayed on the client's behalf if the session
  used one (the relay checks the new server agrees on the protocol version),
  and the tool list is fetched again -- the old listing is not trusted for the
  new process.
* **Requests in flight** may have executed. Each is answered "outcome
  unknown" and **never re-sent** to the new server. By default
  (``--restart-policy idle-only``) the relay then does *not* restart: an
  exited stdio server may leave work running, and starting a second one
  beside it is not something the relay can prove safe.
  ``--restart-policy after-unknown`` restarts anyway, after every in-flight
  request has been answered "outcome unknown".
* A request that arrives while the server is being restarted waits for it
  (bounded); one that cannot be sent is answered "not sent" -- it certainly
  did not execute. When the restart budget is spent, every later request is
  answered that way, and the audit log says why.
* A ``tools/call`` is decided only against a server whose link is up and --
  after a restart -- whose own tool listing has been synced; until then it
  waits (``call_gated``). If the restarted server cannot list its tools, the
  call is answered "not sent", never forwarded unvalidated. Every decision
  is bound to the server generation it was made against and written only to
  that generation: if the server is lost in between, the call is gated and
  decided again (``call_regated``). Retries likewise never cross a restart.

When the server is gone for good (restart off, refused, or the budget spent)
the relay stops, as a relay without StateGuard would: the client sees its
server process end. Requests waiting for a restart are answered "not sent"
first.

Shadow mode never restarts anything; it logs what it would have done.

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

Recording (opt-in)
------------------
``--record FILE`` writes every line in all four directions, every audit
event and the server generation to a redacted, versioned recording
(``stateguard.adapters.mcp.recording``) that the replay benchmark reads.
Redaction runs before anything is written. Recording observes only: the
taps sit *after* a write succeeded or *after* a line was read, so they
cannot add, drop, delay past the write, or reorder a message. A recorder
that fails stops recording, the audit log says so once
(``recording_stopped``), and the traffic carries on exactly as without it.
``--record-provenance`` (default ``local_development``) is written into the
file; claim ``real_capture`` only for real use. ``real_capture`` is
shadow-only and mask-only: the relay refuses to start otherwise, and refuses
a real-capture recorder in auto or passthrough mode. In shadow mode the
``call`` audit event's ``would_modify`` says whether auto mode would have
changed or refused the call -- a proposal, never what was sent.

Scope
-----
An example of where StateGuard sits on the wire, kept small enough to read:
one server process at a time, no timeouts beyond the listing wait and the
opt-in call deadline. MCP's stdio transport is newline-delimited JSON-RPC,
which is all this parses.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from typing import IO, Any

from stateguard import ContractGuard
from stateguard.adapters.mcp import (
    EffectPolicy,
    FailureClassification,
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
    classify_response,
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
from stateguard.adapters.mcp.payload import same_json
from stateguard.adapters.mcp.recording import Direction, Provenance, Recorder, RedactionPolicy
from stateguard.adapters.mcp.recording import RedactionMode as RecordRedaction
from stateguard.core.explain import FieldDecisionKind, root_normalisation
from stateguard.core.models.config import GuardConfig, RepairMode

#: Prefix for the ids of the relay's own requests, so their responses can be
#: told apart from the client's. A client is free to choose any id; one that
#: chose this prefix would have its tools/list responses consumed here.
OWN_ID_PREFIX = "stateguard-relay:"
#: Ids of retried tools/call attempts; their answers go to the client under its own id.
RETRY_ID_PREFIX = "stateguard-retry:"
#: Id prefix of the ``initialize`` the relay replays to a restarted server.
REINIT_ID_PREFIX = f"{OWN_ID_PREFIX}init-"
#: How long a client request waits for a restarting server before it is
#: answered "not sent".
RECONNECT_WAIT_SECONDS = 10.0

#: Link to the server process: up, being restarted, or gone for good.
LINK_UP, LINK_RESTARTING, LINK_DOWN = "up", "restarting", "down"


class _ServerChangedError(Exception):
    """The server was lost while a call was being decided: decide again."""


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
        restart: Callable[[], tuple[IO[bytes], IO[bytes]]] | None = None,
        max_restarts: int = 0,
        restart_policy: str = "idle-only",
        restart_backoff: float = 0.1,
        recorder: Recorder | None = None,
    ) -> None:
        self.registry = ToolRegistry(guard)
        if (
            recorder is not None
            and getattr(recorder, "provenance", None) is Provenance.REAL_CAPTURE
            and (guard.config.mode is not RepairMode.SHADOW or passthrough)
        ):
            # Real traffic is captured in shadow mode only: StateGuard
            # observes and proposes; the client's payload is what is sent.
            raise ValueError(
                "a real_capture recording requires shadow mode (not auto, not passthrough)"
            )
        # Observes only; see "Recording" in the module docstring.
        self._recorder = recorder
        self._recording_reported = False
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
        # client id -> (payload, tool, server generation it was decided against)
        self._calls: dict[Any, tuple[bytes, str | None, int]] = {}
        self._attempts: dict[Any, int] = {}  # client id -> attempts sent
        self._client_of: dict[Any, Any] = {}  # retry id -> client id
        self._retry_lock = threading.Lock()
        self._stopped = threading.Event()
        if restart_policy not in ("idle-only", "after-unknown"):
            raise ValueError(
                f"restart_policy must be idle-only or after-unknown, got {restart_policy!r}"
            )
        # Restart: a factory for a fresh (server stdin, server stdout) pair.
        self._restart = restart if max_restarts > 0 else None
        self._max_restarts = max_restarts
        self._restart_policy = restart_policy
        self._restart_backoff = restart_backoff
        self._restarts = 0
        self.generation = 0
        self._link = LINK_UP
        self._link_cond = threading.Condition()
        self._link_waiters = 0  # client requests waiting for a restart to finish
        self._client_gone = False  # the client closed its side: nothing to restart for
        # The client's initialize, replayed to a restarted server.
        self._client_init: dict[str, Any] | None = None
        self._client_init_id: Any = None
        self._client_initialized = False
        self._negotiated_version: str | None = None
        self._reinit_id: str | None = None
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
        # Epochs whose listing could never be answered: unsent, or requested
        # from a server that died. Distinct from a live server's error answer.
        self._lost_epochs: set[int] = set()
        # The generation whose tools the registry holds, and the epoch of the
        # listing a restarted server was asked for. A call is decided against
        # a restarted server only once its own listing has been synced.
        self._synced_generation = -1
        self._discovery_epoch: int | None = None
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
        # The session's lifecycle, learnt from the server's answers -- never
        # from what the client merely tries (an SDK may probe the stateless
        # protocol, then fall back to the initialize handshake):
        #   None      unknown: the relay sends nothing of its own;
        #   "legacy"  the server answered the client's initialize;
        #   "modern"  the server answered a request that carried a protocol
        #             version (2026-07-28 on: every request stands alone).
        self._era: str | None = None
        # Legacy only: notifications/initialized reached *this* server process.
        self._handshake_done = False
        # Until the era is known: client request id -> the protocol _meta it carried.
        self._versioned: dict[str, dict[str, Any]] = {}
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
            try:
                stream.write(line if line.endswith(b"\n") else line + b"\n")
                stream.flush()
            except ValueError as exc:  # closed by the other side's shutdown
                raise BrokenPipeError(str(exc)) from exc
            if stream is self._client_out:
                self._tap(Direction.RELAY_TO_CLIENT, line)  # written: now it happened

    def _send_server(self, line: bytes) -> None:
        """Write to the *current* server process (the stream is swapped on restart)."""
        with self._to_server:
            self._write_server(line)

    def _write_server(self, line: bytes) -> None:
        """The write itself. Caller holds ``_to_server``."""
        stream = self._server_in
        try:
            stream.write(line if line.endswith(b"\n") else line + b"\n")
            stream.flush()
        except ValueError as exc:  # closed: the client left, or a restart replaced it
            raise BrokenPipeError(str(exc)) from exc
        self._tap(Direction.RELAY_TO_SERVER, line)

    def _send_json(self, stream: IO[bytes], lock: threading.Lock, message: Any) -> None:
        self._send(stream, lock, json.dumps(message, separators=(",", ":")).encode("utf-8"))

    def _log(self, event: str, **fields: Any) -> None:
        with self._to_audit:
            self._audit.write(json.dumps({"event": event, **fields}, default=str) + "\n")
            self._audit.flush()
        recorder = self._recorder
        if recorder is not None:
            try:
                recorder.audit({"event": event, **fields})
            except Exception:  # noqa: BLE001 -- never; but a recorder never breaks a call
                self._recorder = None
            self._check_recorder(recorder)

    def _tap(self, direction: Direction, line: bytes) -> None:
        """Give one wire line to the recorder. Never raises, never alters *line*."""
        recorder = self._recorder
        if recorder is None:
            return
        try:
            recorder.message(direction, line, generation=self.generation)
        except Exception:  # noqa: BLE001 -- Recorder never raises; belt and braces
            self._recorder = None
        self._check_recorder(recorder)

    def _check_recorder(self, recorder: Recorder) -> None:
        """Say once, in the audit log, that recording stopped. The traffic is unaffected."""
        if self._recording_reported or (recorder.healthy and self._recorder is not None):
            return
        self._recording_reported = True
        self._recorder = None
        with contextlib.suppress(Exception), self._to_audit:
            self._audit.write(
                json.dumps(
                    {
                        "event": "recording_stopped",
                        "failures": list(recorder.failure_types),
                        "impact": "recording only; traffic continues unchanged",
                    }
                )
                + "\n"
            )
            self._audit.flush()

    def _session_ready(self) -> bool:
        """Whether the session allows a relay-owned request (see ``_era``)."""
        if self._era == "modern":
            return True
        return self._era == "legacy" and self._handshake_done

    def _discover_tools(self) -> None:
        """List this server's tools once the session allows it, and only once."""
        if not self._listed and self._session_ready():
            self._discovery_epoch = self._request_listing()

    def _request_listing(self, cursor: str | None = None, epoch: int | None = None) -> int:
        """
        Send a relay-owned ``tools/list`` (page); return its listing epoch.

        Never before the session is ready: a server still waiting for its
        initialize handshake rejects any other request, and the relay must
        not be the one breaking the protocol. Such a listing is deferred --
        its epoch fails at once, which callers already treat as "no fresh
        listing" -- and happens when the session becomes ready.
        """
        if not self._session_ready():
            with self._listing_done:
                epoch = next(self._epoch_counter) if epoch is None else epoch
            self._listed = False
            self._log("tools_list_deferred", category="D", reason="session not initialized")
            self._finish_epoch(epoch, ok=False)
            return epoch
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
        try:
            self._send_server(self._encode(message))
        except OSError as exc:
            self._log("tools_list_unsent", category="E", error=str(exc))
            with self._listing_done:
                self._lost_epochs.add(epoch)
            self._finish_epoch(epoch, ok=False)
        return epoch

    def _finish_epoch(self, epoch: int, *, ok: bool) -> None:
        with self._listing_done:
            if ok:
                self._fresh_epoch = max(self._fresh_epoch, epoch)
            else:
                self._failed_epochs.add(epoch)
            self._listing_done.notify_all()

    def _refresh_listing(self) -> str:
        """
        Re-list and wait for a listing requested *now*.

        ``"fresh"`` if one completed (a later epoch completing also counts --
        it was requested later still); ``"lost"`` if the server could not
        answer it -- the request was unsent, or the server died first;
        ``"failed"`` on a live server's error answer or after
        ``LISTING_WAIT_SECONDS``.
        """
        epoch = self._request_listing()
        with self._listing_done:
            self._listing_done.wait_for(
                lambda: self._fresh_epoch >= epoch or epoch in self._failed_epochs,
                timeout=LISTING_WAIT_SECONDS,
            )
            if self._fresh_epoch >= epoch:
                return "fresh"
            return "lost" if epoch in self._lost_epochs else "failed"

    def _sync(self, tools: list[Any], source: str) -> None:
        report = self.registry.sync(tools)
        self._listing_stale = False
        with self._listing_done:
            # Only the current server's stream is read, so this listing is the
            # current generation's.
            self._synced_generation = self.generation
            self._listing_done.notify_all()
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
            with self._link_cond:
                self._client_gone = True
                self._link = LINK_DOWN  # the client is gone: nothing more to restart for
                self._link_cond.notify_all()
            with self._to_server, contextlib.suppress(OSError):
                self._server_in.close()

    def _pump_client(self) -> None:
        for line in self._client_in:
            self._tap(Direction.CLIENT_TO_RELAY, line)
            if self._passthrough:
                self._send_server(line)
                continue
            message = _parse(line)
            identifier = self._track(message)
            self._remember_session(message)
            try:
                self._note_versioned(message)
            except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
                self._internal_error("client message", exc)
            if is_request(message, TOOLS_CALL):
                self._on_call(line, message, identifier)
                continue
            try:
                if is_request(message, TOOLS_LIST):
                    self._client_lists.observe_request(message)
            except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
                self._internal_error("client message", exc)
            self._send_tracked(identifier, line)
            # Under the initialize protocol, the session is usable once the
            # client's notifications/initialized has reached the server. (The
            # stateless protocol's moment is a server answer: _pump_server.)
            if is_notification(message, "notifications/initialized"):
                self._handshake_done = True
                self._discover_tools()

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
        """
        Write a client message to the server, recording a request as sent first.

        Never writes to a server known to be gone: it waits (bounded) while a
        restart is in progress, and a request that still cannot go out is
        answered "not sent" -- it was never written, so it certainly did not
        execute. A write that *fails* may have delivered part of the request,
        so that one is UNKNOWN, never "not sent".
        """
        if not self._await_link():
            self._answer_not_sent(identifier, "server unavailable")
            return
        if identifier is not None:
            self.ledger.sent(identifier)
        try:
            self._send_server(line)
        except OSError as exc:
            self._log("send_failed", category="E", id=identifier, error=str(exc))
            if identifier is not None:
                self._resolve_unknown(
                    [self.ledger.send_failed(identifier)],
                    Observed.UNKNOWN_TRANSPORT,
                    connection_available=False,
                )

    def _answer_not_sent(self, identifier: Any, reason: str) -> None:
        """A request that was never written: it certainly did not execute. Say so."""
        if identifier is None:
            self._log("notification_dropped", category="E", reason=reason)
            return
        self.ledger.answered_locally(identifier)
        self._forget(identifier)
        self._log("not_sent", category="E", id=identifier, reason=reason)
        with contextlib.suppress(OSError, ValueError):  # the client may be gone too
            self._send_json(self._client_out, self._to_client, not_sent_response(identifier))

    def _send_decided(self, identifier: Any, payload: bytes, generation: int) -> bool:
        """
        Write a request decided against server *generation*, exactly once.

        The check that the link is up and still that generation, the ledger's
        ``SENT`` and the write happen under ``_to_server``, which the link
        also takes to leave ``up`` (``_on_server_lost``) or to swap servers
        (``_connect``). So a request decided against one server process is
        never written to another. False -- nothing written, nothing recorded
        -- when the server changed: the caller decides again.
        """
        failure: OSError | None = None
        with self._to_server:
            if self.generation != generation or self._link != LINK_UP:
                return False
            if identifier is not None:
                self.ledger.sent(identifier)
            try:
                self._write_server(payload)
            except OSError as exc:
                failure = exc
        if failure is not None:
            self._log("send_failed", category="E", id=identifier, error=str(failure))
            if identifier is not None:
                self._resolve_unknown(
                    [self.ledger.send_failed(identifier)],
                    Observed.UNKNOWN_TRANSPORT,
                    connection_available=False,
                )
        return True

    def _gate_call(self, identifier: Any) -> int | None:
        """
        The server generation a ``tools/call`` may be decided against: one
        whose link is up and -- after a restart -- whose own tool listing has
        been synced. Waits for a restart in progress (bounded). If no such
        server comes, the call is answered "not sent" and ``None`` returned:
        it is never decided against a dead server's schemas, never forwarded
        unvalidated because a restart was under way.
        """
        reason = "server unavailable"
        waited = self._link != LINK_UP or self._synced_generation != self.generation
        for _ in range(self._max_restarts + 2):
            if not self._await_link():
                break
            generation = self.generation
            if generation == 0:
                return generation  # never restarted: nothing to re-discover
            with self._link_cond:  # the relay answers waiting calls before it stops
                self._link_waiters += 1
            try:
                state = self._await_discovery(generation)
            finally:
                with self._link_cond:
                    self._link_waiters -= 1
                    self._link_cond.notify_all()
            if state == "valid":
                if waited:
                    self._log("call_gated", category="E", id=identifier, generation=generation)
                return generation
            if state == "failed":
                reason = "the restarted server's tools could not be listed"
                break
            # "changed": that server was lost too; wait for the next one.
        if waited:
            self._log("call_gated", category="E", id=identifier, generation=None)
        self._answer_not_sent(identifier, reason)
        return None

    def _await_discovery(self, generation: int) -> str:
        """
        ``"valid"`` once *generation*'s listing is synced; ``"changed"`` if
        that server is lost first; ``"failed"`` if its listing failed (or did
        not come in ``LISTING_WAIT_SECONDS``) while it stayed up.
        """

        def lost() -> bool:
            return self.generation != generation or self._link != LINK_UP

        def settled() -> bool:
            epoch = self._discovery_epoch
            return (
                self._synced_generation == generation
                or lost()
                or (epoch is not None and epoch in self._failed_epochs)
            )

        with self._listing_done:
            self._listing_done.wait_for(settled, timeout=LISTING_WAIT_SECONDS)
            if self._synced_generation == generation:
                return "valid"
            if lost():
                return "changed"
            if self._discovery_epoch in self._lost_epochs:
                # The listing could not be written: that server is dying. Its
                # loss (or the timeout) decides, never a decision without it.
                self._listing_done.wait_for(lost, timeout=RECONNECT_WAIT_SECONDS)
                return "changed" if lost() else "failed"
            return "failed"

    def _await_link(self) -> bool:
        """True once the server link is up; False if it is (or stays) down."""
        with self._link_cond:
            self._link_waiters += 1
            try:
                self._link_cond.wait_for(
                    lambda: self._link != LINK_RESTARTING, timeout=RECONNECT_WAIT_SECONDS
                )
                return self._link == LINK_UP
            finally:
                self._link_waiters -= 1
                self._link_cond.notify_all()

    def _remember_session(self, message: Any) -> None:
        """What a restarted server needs replayed: the client's initialize."""
        if is_request(message, "initialize"):
            params = message.get("params")
            self._client_init = dict(params) if isinstance(params, dict) else {}
            self._client_init_id = message.get("id")
        elif is_notification(message, "notifications/initialized"):
            self._client_initialized = True

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
        payload, tool, generation = call
        if generation != self.generation:
            return False  # decided against a server that is gone: never re-sent to another
        entry = self.registry.find(tool) if tool else None
        declaration = self._effects.of(
            tool or "", annotations=entry.annotations if entry is not None else None
        )
        verdict = decide_retry(
            observed,
            declaration,
            attempts_made=attempts,
            connection_available=connection_available and self._link == LINK_UP,
        )
        acted = verdict.retry and self._retry_mode == "auto"
        self._log(
            "retry_decision",
            category={Observed.TOOL_ERROR: "F", Observed.AUTH_ERROR: "K"}.get(observed, "I"),
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
        # Only to the server it was decided for (send failures resolve as unknown inside).
        if self._send_decided(retry_id, self._encode(message), generation):
            return True
        # The server changed before the retry went out: it was never written.
        self.ledger.answered_locally(retry_id)
        with self._retry_lock:
            self._client_of.pop(retry_id, None)
            self._attempts[client_id] = attempts
        self._log("retry_not_sent", category="E", id=client_id, reason="server changed")
        return False

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

    def _note_versioned(self, message: Any) -> None:
        """
        A client message's protocol ``_meta``. Before the era is known it is
        only a candidate, kept by request id: the client may be probing.
        The server's answer decides (``_on_era_evidence``).
        """
        params = message.get("params") if isinstance(message, dict) else None
        meta = params.get("_meta") if isinstance(params, dict) else None
        if not isinstance(meta, dict) or PROTOCOL_VERSION_META_KEY not in meta:
            return
        versioned = {
            key: value for key, value in meta.items() if key.startswith("io.modelcontextprotocol/")
        }
        if self._era == "modern":
            self._protocol_meta = versioned
        elif self._era is None and message.get("id") is not None:
            self._versioned[_id_key(message["id"])] = versioned
            while len(self._versioned) > _VERSIONED_LIMIT:
                del self._versioned[next(iter(self._versioned))]

    def _on_era_evidence(self, message: dict[str, Any], identifier: Any) -> None:
        """A server answer to a client request: does it settle the session's era?"""
        if (
            self._client_init_id is not None
            and identifier == self._client_init_id
            and isinstance(message.get("result"), dict)
        ):
            version = message["result"].get("protocolVersion")
            self._negotiated_version = version if isinstance(version, str) else None
            # An initialize session: requests carry no per-request version,
            # so neither do the relay's (a probe's _meta is not this session's).
            self._era = "legacy"
            self._protocol_meta = {}
            self._versioned.clear()
            return
        if self._era is not None or identifier is None:
            return
        candidate = self._versioned.pop(_id_key(identifier), None)
        if candidate is not None and isinstance(message.get("result"), dict):
            self._era = "modern"
            self._protocol_meta = candidate
            self._versioned.clear()
            self._discover_tools()

    def _on_call(self, line: bytes, message: Any, identifier: Any = None) -> None:
        """
        Decide, then send exactly once.

        ``_decide`` only computes where the call goes and with which bytes;
        this method does the one write. So an exception during the decision
        fails open (the original line goes to the server) and an exception
        during the write cannot cause a second send -- a relay that retried
        a write it could not confirm would risk executing the call twice.
        """
        # Every decision is bound to the server generation it was made
        # against, and is written only to that generation (``_send_decided``).
        # If the server changes in between -- it dies, it is restarted -- the
        # call is gated again and decided again against the new server.
        # Bounded: each round needs a lost server, and restarts are bounded.
        for _ in range(self._max_restarts + 2):
            generation = self._gate_call(identifier)
            if generation is None:
                return  # answered "not sent"
            try:
                to_server, payload = self._decide(line, message, generation)
            except _ServerChangedError:
                self._log("call_regated", category="E", id=identifier, reason="server lost")
                continue
            except Exception as exc:  # noqa: BLE001 -- fail open; see the module docstring
                self._internal_error(
                    "tools/call", exc, id=request_id(message), action="unvalidated"
                )
                to_server, payload = True, line
            if not to_server:
                if identifier is not None:
                    self.ledger.answered_locally(identifier)
                self._send(self._client_out, self._to_client, payload)
                return
            if identifier is not None and self._retry_mode != "off":
                tool = message.get("params", {}).get("name") if isinstance(message, dict) else None
                with self._retry_lock:
                    self._calls[identifier] = (
                        payload,
                        tool if isinstance(tool, str) else None,
                        generation,
                    )
                    self._attempts[identifier] = 1
            if self._send_decided(identifier, payload, generation):
                return
            self._log("call_regated", category="E", id=identifier, reason="server changed")
        self._answer_not_sent(identifier, "the server kept changing")

    @staticmethod
    def _encode(message: Any) -> bytes:
        return json.dumps(message, separators=(",", ":")).encode("utf-8")

    def _decide(self, line: bytes, message: Any, generation: int = 0) -> tuple[bool, bytes]:
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
            refreshed = self._refresh_listing()
            if refreshed == "lost" or self.generation != generation or self._link != LINK_UP:
                # The server went away while this call was being decided. Its
                # listing says nothing about the next server: decide again
                # there, never forward on the strength of a dead one.
                raise _ServerChangedError
            if refreshed != "fresh":
                self._log(
                    "call",
                    id=request.id,
                    tool=request.name,
                    action="unvalidated",
                    auto_action=(outcome.auto_action or outcome.action).value,
                    reason="could not confirm the tool listing is current; "
                    "forwarded unchanged rather than act on a possibly stale schema",
                    listing="unconfirmed",
                    would_modify=False,
                    proposal="unvalidated",
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
            # What auto mode would do to this call (change or refuse it):
            # in shadow, a proposal -- the call is forwarded as received.
            would_modify=_intervenes(request, outcome),
            # ...and of which kind. "default_fill" is not a repair: the call
            # was valid, and only omitted optionals would be written out.
            proposal=_proposal(request, outcome),
            decisions=[
                {
                    "path": d.path,
                    "decision": d.kind.value,
                    "op": d.operation.op_type.value if d.operation else None,
                    "trust": round(d.operation.trust, 3) if d.operation else None,
                    # For an unresolved decision: "error" is a violation;
                    # "warning" is an undeclared key an open schema allows,
                    # passed through -- observed, not invalid.
                    "severity": d.violation.severity.value if d.violation else None,
                }
                for d in outcome.result.field_decisions()
            ],
        )
        forwarded = forward_message(request, outcome)
        if forwarded is None:
            return False, self._encode(refusal_response(request, outcome))
        if same_json(forwarded, request.message):
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
                would_modify=False,
                proposal="unvalidated",
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
                would_modify=False,
                proposal="unvalidated",
            )
            return True, line
        self._log(
            "call",
            id=request.id,
            tool=request.name,
            action="refuse",
            reason=f"schema unreadable: {exc.cause}",
            would_modify=True,
            proposal="refuse",
        )
        text = (
            f"Not sent: StateGuard cannot read the input schema of {request.name!r}, "
            f"so it cannot check calls to it. {exc.cause}"
        )
        return False, self._encode(tool_error_response(request, text))

    # -- server -> client ----------------------------------------------------

    def pump_server(self) -> None:
        try:
            while True:
                self._pump_server()
                if self._passthrough or not self._on_server_lost():
                    break
        except OSError as exc:  # the client's stdout is gone
            self._log("transport_closed", category="E", direction="server->client", error=str(exc))
        finally:
            with self._link_cond:
                self._link = LINK_DOWN
                self._link_cond.notify_all()
            with self._listing_done:  # wake calls gated on a listing too
                self._listing_done.notify_all()
            with self._link_cond:
                # Requests that were waiting for a restart are answered "not
                # sent" before the relay stops -- never left without an answer.
                self._link_cond.wait_for(lambda: self._link_waiters == 0, timeout=2.0)
            self._stopped.set()

    def _set_link(self, state: str) -> None:
        with self._link_cond:
            self._link = state
            self._link_cond.notify_all()
        with self._listing_done:  # calls gated on discovery watch the link too
            self._listing_done.notify_all()

    def _on_server_lost(self) -> bool:
        """
        The server's stdout closed. Settle what was in flight; restart if safe.
        True when a new server is connected and pumping should go on.
        """
        will_try = self._restart is not None and not self._shadow and not self._client_gone
        # From here no request is written to the dead process. Taken under
        # _to_server: a call between its link check and its write finishes
        # first (and is then SENT, so settled below), or sees the link down.
        with self._to_server:
            self._set_link(LINK_RESTARTING if will_try else LINK_DOWN)
        # Nothing in flight will be answered: each is UNKNOWN, told so, and
        # never re-sent -- not even to a restarted server.
        unknown = [inv for inv in self.ledger.transport_closed() if inv is not None]
        self._resolve_unknown(unknown, Observed.UNKNOWN_TRANSPORT, connection_available=False)
        self._abandon_own_listings()
        self._log(
            "server_lost",
            category="E",
            generation=self.generation,
            in_flight_unknown=len(unknown),
            restarts_used=self._restarts,
        )
        if self._restart is None or self._client_gone:
            return False
        if self._shadow:
            self._log("restart_would_attempt", category="E", in_flight_unknown=len(unknown))
            return False
        if unknown and self._restart_policy == "idle-only":
            self._log(
                "restart_refused",
                category="E",
                in_flight_unknown=len(unknown),
                reason="requests in flight may have executed; a new server beside work the "
                "old one may still be doing is not provably safe (restart policy idle-only)",
            )
            self._set_link(LINK_DOWN)
            return False
        while self._restarts < self._max_restarts:
            self._restarts += 1
            if self._stopped.wait(self._restart_backoff * (self._restarts - 1)):
                break
            try:
                server_in, server_out = self._restart()
            except Exception as exc:  # noqa: BLE001 -- a failed restart is counted, not fatal
                self._log("restart_failed", category="E", attempt=self._restarts, error=str(exc))
                continue
            self._connect(server_in, server_out)
            return True
        self._log("restart_exhausted", category="E", restarts_used=self._restarts)
        self._set_link(LINK_DOWN)
        return False

    def _connect(self, server_in: IO[bytes], server_out: IO[bytes]) -> None:
        """Swap in a restarted server; re-initialize and re-list before trusting it."""
        with self._to_server:
            old = self._server_in
            self._server_in = server_in
            # With the stream, under the same lock: a call decided against the
            # old generation can never be written to the new stream.
            self.generation += 1
            self._discovery_epoch = None
        with contextlib.suppress(OSError):
            old.close()
        self._server_out = server_out
        # The old listing describes a process that no longer exists: forget it,
        # so nothing is validated -- let alone repaired -- against it. Until
        # the new process lists its tools, calls wait for that listing, and if
        # it never comes they are forwarded unvalidated (logged), never checked
        # against the dead process's schemas.
        try:
            self.registry.clear()
        except Exception as exc:  # noqa: BLE001 -- fail open
            self._internal_error("registry reset", exc)
        self._listing_stale = True
        self._listing_ready.clear()
        self._listed = False
        self._handshake_done = False  # a new process has had no handshake
        self._log(
            "server_restarted", category="E", generation=self.generation, attempt=self._restarts
        )
        if self._client_init is not None:
            # initialize-era session: replay the handshake before anything else.
            self._reinit_id = f"{REINIT_ID_PREFIX}{self.generation}"
            message = {
                "jsonrpc": JSONRPC_VERSION,
                "id": self._reinit_id,
                "method": "initialize",
                "params": self._client_init,
            }
            try:
                self._send_server(self._encode(message))
            except OSError as exc:
                self._log("reinit_unsent", category="E", error=str(exc))
            return  # the link comes up when the server answers
        # Ask for the tools before the link comes up: calls gated on this
        # generation wait for exactly this listing. Only in a session known
        # to be stateless; with the era still unknown the relay sends nothing
        # of its own, and lists when the server's first answer settles it.
        self._discover_tools()
        self._set_link(LINK_UP)

    def _on_reinit(self, message: dict[str, Any]) -> None:
        """The restarted server answered the replayed initialize."""
        self._reinit_id = None
        result = message.get("result")
        version = result.get("protocolVersion") if isinstance(result, dict) else None
        if version is None or (
            self._negotiated_version is not None and version != self._negotiated_version
        ):
            self._log(
                "reinit_failed",
                category="D",
                expected=self._negotiated_version,
                got=version,
                error=message.get("error"),
            )
            # Not a server this session can use: close it. Its exit is seen as
            # one more lost server, so the attempt counts against the budget.
            with self._to_server, contextlib.suppress(OSError):
                self._server_in.close()
            return
        self._era = "legacy"
        if self._client_initialized:
            try:
                self._send_server(
                    self._encode(
                        {"jsonrpc": JSONRPC_VERSION, "method": "notifications/initialized"}
                    )
                )
            except OSError as exc:
                self._log("reinit_unsent", category="E", error=str(exc))
                return
            self._handshake_done = True
        self._log("reinitialized", category="D", generation=self.generation, version=version)
        # Lists now if the client had completed its handshake; otherwise when
        # its notifications/initialized arrives (_pump_client).
        self._discover_tools()
        self._set_link(LINK_UP)

    def _abandon_own_listings(self) -> None:
        """Listings requested from a dead server will never complete."""
        with self._listing_done:
            epochs = set(self._epoch_of.values())
            self._epoch_of.clear()
            self._first_page_ids.clear()
            self._lost_epochs |= epochs
        for epoch in epochs:
            self._finish_epoch(epoch, ok=False)
        self._own_lists = ToolListAccumulator()
        self._listing_ready.set()  # calls are not held for a listing that cannot come

    def _pump_server(self) -> None:
        for line in self._server_out:
            self._tap(Direction.SERVER_TO_RELAY, line)
            if self._passthrough:
                self._send(self._client_out, self._to_client, line)
                continue
            message = _parse(line)
            identifier = request_id(message)
            if (
                is_response(message)
                and self._reinit_id is not None
                and identifier == self._reinit_id
            ):
                self._on_reinit(message)
                continue
            if is_response(message) and not (
                isinstance(identifier, str) and identifier.startswith(OWN_ID_PREFIX)
            ):
                try:
                    self._on_era_evidence(message, identifier)
                except Exception as exc:  # noqa: BLE001 -- fail open
                    self._internal_error("session lifecycle", exc)
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

    def _note_failure(self, client_id: Any, failure: FailureClassification | None) -> None:
        """Log an auth failure as category K: never retried, the operator's to resolve."""
        if failure is None or not failure.is_auth:
            return
        invocation = self.ledger.get(client_id)
        self._log(
            "auth_failure",
            category="K",
            id=client_id,
            tool=invocation.tool if invocation is not None else None,
            **failure.to_dict(),
        )

    def _deliverable(self, message: dict[str, Any]) -> bool:
        """Whether a server response goes to the client: not if it already has its answer."""
        identifier = message["id"]
        try:
            result = message.get("result")
            is_error = "error" in message or (
                isinstance(result, dict) and result.get("isError") is True
            )
            # What kind of failure (auth, protocol, tool ...): deterministic
            # rules only, see stateguard.adapters.mcp.failures. Decides the
            # retry verdict; never the answer, which is delivered as sent.
            failure = classify_response(message) if is_error else None
            observed = failure.observed if failure is not None else Observed.TOOL_ERROR
            disposition = self.ledger.response(identifier, is_error=is_error)
            if isinstance(identifier, str) and identifier.startswith(RETRY_ID_PREFIX):
                # Never delivered raw: the client knows its own id, not ours.
                client_id = self._client_of.get(identifier)
                if disposition is ResponseDisposition.DELIVER and client_id is not None:
                    self._note_failure(client_id, failure)
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
                self._note_failure(identifier, failure)
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
            self._log(
                "tools_list_failed",
                category="K" if classify_response(message).is_auth else None,
                error=message["error"],
                **classify_response(message).to_dict(),
            )
            self._finish_epoch(epoch, ok=False)
            self._listing_ready.set()  # do not hold calls forever


#: Client requests remembered while the session's era is still unknown.
_VERSIONED_LIMIT = 64


def _id_key(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True)


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


def not_sent_response(identifier: Any) -> dict[str, Any]:
    """The relay's answer for a request it never wrote: it certainly did not execute."""
    return {
        "jsonrpc": JSONRPC_VERSION,
        "id": identifier,
        "error": {
            "code": UNKNOWN_OUTCOME_CODE,
            "message": (
                "MCP server unavailable: the request was not sent and did not execute. "
                "It is safe to send again once the server is back."
            ),
            "data": {UNKNOWN_OUTCOME_KEY: "not_sent"},
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
    # JSON-exact: a repair from 2.0 to 2 (or 1 to True) is a change.
    return not same_json(outcome.proposed_arguments, received) or type(received) is not dict


def _proposal(request: ToolCallRequest, outcome: ToolCallOutcome) -> str:
    """
    What auto mode would do to this call, by kind -- for the audit log and
    the evidence it feeds, which must not count a default as a repair:

    ``none``          forwarded exactly as received;
    ``default_fill``  forwarded with omitted optional fields written out from
                      their declared defaults -- the call was *valid*, no
                      field was wrong, nothing was repaired;
    ``repair``        forwarded with at least one field (or the payload root)
                      repaired -- including a missing *required* field set
                      from its declared default, which is a real violation;
    ``refuse`` / ``escalate``  not forwarded.
    """
    auto = outcome.auto_action or outcome.action
    if auto is not MCPAction.FORWARD:
        return auto.value
    if not _intervenes(request, outcome):
        return "none"
    repaired = any(d.kind is FieldDecisionKind.REPAIRED for d in outcome.result.field_decisions())
    if repaired or root_normalisation(outcome.result) is not None:
        return "repair"
    return "default_fill"


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
        "--restart",
        type=int,
        default=0,
        metavar="N",
        help="Restart the server process if it exits, at most N times (default: 0, never).",
    )
    parser.add_argument(
        "--restart-policy",
        choices=["idle-only", "after-unknown"],
        default="idle-only",
        help="idle-only: never restart while a request's outcome is unknown (default).",
    )
    parser.add_argument(
        "--record",
        metavar="FILE",
        help="Write a redacted recording of all traffic (JSON lines) for replay.",
    )
    parser.add_argument(
        "--record-provenance",
        # ``replay`` is what a replay engine writes; the relay never replays.
        choices=[p.value for p in Provenance if p is not Provenance.REPLAY],
        default=Provenance.LOCAL_DEVELOPMENT.value,
        help="Provenance written into the recording (default: local_development). "
        "real_capture requires --record, --shadow and mask redaction.",
    )
    parser.add_argument(
        "--record-redaction",
        choices=[m.value for m in RecordRedaction],
        default=RecordRedaction.MASK.value,
        help="mask (default): persist no free-form values; secrets_only: drop credentials only.",
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
    real_capture = args.record_provenance == Provenance.REAL_CAPTURE.value
    if real_capture:
        # Checked before the server starts: a refused real capture runs nothing.
        if not args.record:
            parser.error("--record-provenance real_capture needs --record FILE")
        if not args.shadow or args.passthrough:
            parser.error(
                "--record-provenance real_capture is shadow-only: add --shadow "
                "(and drop --passthrough)"
            )
        if args.record_redaction != RecordRedaction.MASK.value:
            parser.error("--record-provenance real_capture requires --record-redaction mask")

    mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
    audit = open(args.audit, "a", encoding="utf-8") if args.audit else sys.stderr  # noqa: SIM115
    processes = [subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)]
    server = processes[0]
    assert server.stdin is not None and server.stdout is not None

    def restart() -> tuple[IO[bytes], IO[bytes]]:
        old = processes[-1]
        if old.poll() is None:  # stdout closed but still running: stop it first
            old.terminate()
            try:
                old.wait(timeout=5)
            except subprocess.TimeoutExpired:
                old.kill()
                old.wait()
        new = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        assert new.stdin is not None and new.stdout is not None
        processes.append(new)
        return new.stdin, new.stdout

    recorder = None
    if args.record:
        import uuid

        recorder = Recorder(
            open(args.record, "w", encoding="utf-8"),  # noqa: SIM115 -- closed at exit
            provenance=Provenance(args.record_provenance),
            session=uuid.uuid4().hex,
            policy=RedactionPolicy(mode=RecordRedaction(args.record_redaction)),
            deterministic=False,
            # Redact and write off the call path: a slow disk never stalls traffic.
            background=True,
            # The relay's settings only: the server command may carry secrets.
            config={
                "mode": "passthrough" if args.passthrough else mode.value,
                "retry": args.retry,
                "restart": args.restart,
                "restart_policy": args.restart_policy,
                "call_deadline": args.call_deadline,
                "listing_refresh": not args.no_listing_refresh,
                "unreadable": args.unreadable,
            },
        )
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
        restart=restart,
        max_restarts=args.restart,
        restart_policy=args.restart_policy,
        recorder=recorder,
    )
    relay._log(
        "relay_started",
        mode="passthrough" if args.passthrough else mode.value,
        # The audit log is not redacted. A real capture's server command may
        # carry credentials (``--token ...``): name the program only.
        **(
            {"server_program": os.path.basename(command[0]), "server_args": len(command) - 1}
            if real_capture
            else {"server": command}
        ),
    )
    to_server = threading.Thread(target=relay.pump_client, daemon=True)
    to_server.start()
    threading.Thread(target=relay.run_deadlines, daemon=True).start()
    relay.pump_server()  # until the server closes its stdout for good
    code = processes[-1].wait()
    if recorder is not None:
        recorder.close()
    return code


if __name__ == "__main__":
    code = main()
    # The client-side pump is a daemon thread that may still be blocked reading
    # stdin. Interpreter shutdown with a thread holding stdin's lock aborts the
    # process (SIGABRT), so flush what was written and exit directly.
    with contextlib.suppress(Exception):
        sys.stdout.flush()
        sys.stderr.flush()
    os._exit(code)

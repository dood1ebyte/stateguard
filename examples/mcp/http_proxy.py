"""
StateGuard relay for MCP over Streamable HTTP.

    python examples/mcp/http_proxy.py --listen 127.0.0.1:8800 --retry auto \\
        --effects effects.json -- http://127.0.0.1:9000/mcp

The client talks to ``http://127.0.0.1:8800/mcp``; the relay talks to the
server at the URL after ``--``. It applies the stdio relay's outcome rules
(``examples/mcp/stdio_proxy.py``) to the HTTP transport: an answer the server
gave reaches the client unchanged, a request that provably never reached the
server is sent again, and a request that may have executed is never sent
again unless that is safe (a declared read, or a call with an idempotency key
the server honours). Otherwise the client is told, in a JSON-RPC error, that
the outcome is unknown -- never a manufactured success.

What the relay does on each failure of a client request
--------------------------------------------------------
``not sent`` -- the connection to the server could not be opened (refused,
    connect timeout, no free connection). Nothing reached the server, so the
    request is sent again, for up to ``--connect-retry`` seconds (a server
    restarting), whatever the method. Then the client is told "not sent; it
    did not execute".
``session lost`` -- the server answers 404 to a request carrying its session
    id (handshake protocol revisions). The specification requires that for a
    session the server no longer has, so the request was not processed. The
    relay opens a new session with the server, replaying the client's
    ``initialize`` and ``notifications/initialized``, and sends the request
    on it. The client keeps the session id the relay gave it. Some servers
    answer 400 instead (the TypeScript SDK's own example server does, before
    any dispatch); a 400 whose body is a JSON-RPC error naming the session is
    treated the same way, and logged ``session_lost_400``.
``stream cut`` -- the server's event stream for the request ended before its
    answer. If the events carried ids, the relay resumes the stream
    (``GET`` with ``Last-Event-ID``): the server replays the events after that
    id, the answer included, and nothing is executed again. Otherwise, or if
    resuming fails, the outcome is unknown.
``outcome unknown`` -- the connection broke after the request was sent, or
    the server answered 5xx. For ``tools/call`` the decision is
    ``stateguard.adapters.mcp.effects.decide_retry``, as in the stdio relay:
    resent once only for a tool the operator declared ``read_only`` (or, with
    ``--trust-read-only-hints``, one the server annotates ``readOnlyHint``),
    or a tool whose listing advertises the idempotency-key contract (the
    resend carries the same key). Other client requests that change nothing
    (``tools/list``, ``resources/read``, ``ping``, ...) are requested again.
    Everything else is answered "outcome unknown".

Idempotency keys (``--retry auto``; ``--no-idempotency-keys`` turns them off):
a tool whose ``tools/list`` entry carries ``_meta``
``io.stateguard/idempotency: {"version": 1}`` gets a key
(``io.stateguard/idempotencyKey`` in ``params._meta``) on every call, unless
the client sent its own; ``stateguard.adapters.mcp.inject`` gives an
unmodified Python server that contract, the TypeScript preload a TypeScript
one. The relay learns listings from the ``tools/list`` answers passing
through it.

``--passthrough`` forwards everything and decides nothing (a baseline): no
keys, no resends, no re-initialized sessions. Failures reach the client as
the transport produced them.

Ids. Every attempt goes to the server under an id of the relay's own, so a
resend never reuses an id the server may still be working on; answers (and
the client's ``notifications/cancelled``) are mapped back. Requests the
server sends inside a response stream, and the client's answers to them,
pass through unchanged.

``deadline`` (``--call-deadline SECONDS``) -- a request the relay could send
    again without risk (a declared read, a request that changes nothing, a
    keyed call) that has no answer in time is sent once more, the first
    attempt left open; the first answer that settles it wins. A keyed resend
    answered "in progress" or "in doubt" waits one more deadline for the
    original's answer. No other request gets a deadline: cutting a slow
    mutation short could only turn its success into a failure. Such raced
    requests are answered as one JSON message (progress events are dropped).

Calls are checked against the server's schemas
--------------------------------------------
As in the stdio relay, each ``tools/call`` is validated against the tool's
definition from the server's last complete ``tools/list``, through
``stateguard.adapters.mcp.ToolRegistry``. In auto mode (the default) a
drifted call is repaired where that is safe, and refused or escalated where it
is not -- the client gets the relay's ``isError`` result, and the server never
sees the call; ``--shadow`` only logs what auto mode would do. The relay
changes or refuses a call only on a listing requested after the call arrived:
it lists the tools again on the call's session first (``--no-listing-refresh``
turns that off), and forwards the call unchanged if it cannot. A tool the
relay has no definition for is forwarded unvalidated.

Limits. One server per relay. A request without a deadline waits for the
server until ``--upstream-timeout``. No recording, no reconciliation probes. A client
resuming a response stream itself (``Last-Event-ID`` on its own ``GET``) sees
the relay's attempt ids in replayed answers; the relay resumes the streams of
its own attempts. The relay holds its
session table in memory: if the relay itself restarts, clients get 404 and
open new sessions, as the specification has them do. A server that answers
404 for any other reason than a lost session would be re-initialized once per
request, then its 404 delivered.
"""

from __future__ import annotations

import argparse
import contextlib
import itertools
import json
import math
import secrets
import sys
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import anyio
import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

from stateguard import ContractGuard
from stateguard.adapters.mcp import (
    MalformedMessageError,
    ToolCallRequest,
    ToolListAccumulator,
    ToolNotFoundError,
    ToolRegistry,
    ToolSchemaUnusableError,
    forward_message,
    refusal_response,
)
from stateguard.adapters.mcp.effects import (
    DEFAULT_MAX_ATTEMPTS,
    EffectPolicy,
    Observed,
    decide_retry,
)
from stateguard.adapters.mcp.idempotency import (
    IDEMPOTENCY_KEY,
    new_key,
    tool_supports_keys,
    with_key,
)
from stateguard.adapters.mcp.incidence import INVOCATION_EVENT, resolution, with_faults
from stateguard.adapters.mcp.jsonrpc import intervenes, proposal
from stateguard.core.models.config import GuardConfig, RepairMode

SESSION_HEADER = "mcp-session-id"
LAST_EVENT_ID = "last-event-id"
UNKNOWN_OUTCOME_CODE = -32000
UNKNOWN_OUTCOME_KEY = "io.stateguard/executionState"
#: The idempotency contract's "nothing executed now" errors (stateguard.adapters.mcp.idempotency).
IN_DOUBT_CODE, IN_PROGRESS_CODE = -32010, -32011
#: Client requests that change nothing on the server: requesting them again is safe.
SAFE_METHODS = frozenset(
    {
        "ping",
        "tools/list",
        "resources/list",
        "resources/templates/list",
        "resources/read",
        "prompts/list",
        "prompts/get",
        "completion/complete",
        "server/discover",
    }
)
#: Headers that describe one hop, never forwarded.
HOP = frozenset(
    {
        "host",
        "content-length",
        "connection",
        "keep-alive",
        "transfer-encoding",
        "te",
        "trailer",
        "upgrade",
        "proxy-connection",
        "accept-encoding",
        SESSION_HEADER,
        LAST_EVENT_ID,
    }
)


def unknown_outcome_response(identifier: Any, reason: str) -> dict[str, Any]:
    """The relay's answer for a request whose outcome it cannot know (as the stdio relay's)."""
    why = {
        "transport_closed": "the connection to the MCP server broke before it answered",
        "deadline": "the MCP server did not answer within the relay's deadline",
        "server_error": "the MCP server answered with an HTTP server error",
        "stream_cut": "the MCP server's event stream ended before its answer",
    }.get(reason, "the relay cannot determine the outcome")
    return {
        "jsonrpc": "2.0",
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
    """The relay's answer for a request that never reached the server: it did not execute."""
    return {
        "jsonrpc": "2.0",
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


def says_session_unknown(body: bytes) -> bool:
    """
    A 400 body that is a JSON-RPC error naming the session ("Bad Request: No
    valid session ID provided"): a server rejecting the session before any
    dispatch, where the specification would answer 404.
    """
    try:
        message = json.loads(body)
    except ValueError:
        return False
    error = message.get("error") if isinstance(message, dict) else None
    text = error.get("message") if isinstance(error, dict) else None
    return isinstance(text, str) and "session" in text.lower()


def sse(message: dict[str, Any]) -> bytes:
    return f"event: message\ndata: {json.dumps(message)}\n\n".encode()


async def sse_events(lines: AsyncIterator[str]) -> AsyncIterator[tuple[str | None, str]]:
    """
    ``(id, data)`` of each ``message`` event in an event stream, and of each
    event that only carries an id (a priming event: data ``""``), so that a
    reader always knows the last event id it received.
    """
    event_id: str | None = None
    kind = "message"
    data: list[str] = []
    async for line in lines:
        if line == "":
            if (data and kind == "message") or event_id is not None:
                yield event_id, "\n".join(data) if kind == "message" else ""
            event_id, kind, data = None, "message", []
            continue
        name, _, value = line.partition(":")
        value = value[1:] if value.startswith(" ") else value
        if name == "data":
            data.append(value)
        elif name == "id":
            event_id = value
        elif name == "event":
            kind = value
    if (data and kind == "message") or event_id is not None:
        yield event_id, "\n".join(data) if kind == "message" else ""


@dataclass
class Session:
    """One client session, and the server session it is relayed to."""

    client_id: str
    upstream_id: str | None
    initialize: dict[str, Any]
    headers: dict[str, str]
    lock: anyio.Lock = field(default_factory=anyio.Lock)
    initialized: bool = False


@dataclass
class Attempt:
    """How one attempt of a request ended."""

    kind: str  # answer | not_sent | session_lost | unknown | passthrough
    message: dict[str, Any] | None = None
    reason: str = ""
    response: httpx2.Response | None = None
    body: bytes = b""
    #: The id this attempt was sent under (an answer arrives with it).
    upstream_id: str = ""


class Relay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.upstream: str = args.upstream
        self.passthrough: bool = args.passthrough
        self.retry: str = "off" if args.passthrough else args.retry
        self.keys: bool = self.retry == "auto" and not args.no_idempotency_keys
        self.effects = EffectPolicy.from_file(args.effects) if args.effects else EffectPolicy()
        self.trust_read_only_hints: bool = args.trust_read_only_hints
        self.connect_retry: float = args.connect_retry
        self.deadline: float | None = args.call_deadline
        self.sessions: dict[str, Session] = {}
        mode = RepairMode.SHADOW if args.shadow else RepairMode.AUTO
        #: The server's tools, from complete tools/list answers: what calls are checked against.
        self.registry = ToolRegistry(ContractGuard.with_mcp(config=GuardConfig(mode=mode)))
        self.shadow: bool = args.shadow
        self.refresh: bool = not args.no_listing_refresh
        self._lists = ToolListAccumulator()
        #: (the client's session or None, a request's id) -> the live requests with that id,
        #: each by its token -> the ids of its attempts (where a cancellation goes).
        self._live: dict[tuple[str | None, str], dict[int, list[str]]] = {}
        self._tokens = itertools.count(1)
        self.tools: dict[str, dict[str, Any]] = {}
        self._ids = itertools.count(1)
        self._audit_lock = threading.Lock()
        self._audit = open(args.audit, "a", encoding="utf-8") if args.audit else None  # noqa: SIM115
        self.client = httpx2.AsyncClient(
            timeout=httpx2.Timeout(args.upstream_timeout, connect=5.0),
            follow_redirects=False,
        )

    # -- audit ---------------------------------------------------------------------------------

    def log(self, event: str, **fields: Any) -> None:
        if self._audit is None:
            return
        record = {"ts": datetime.now(UTC).isoformat(), "event": event, **fields}
        with self._audit_lock:
            self._audit.write(json.dumps(record, default=str) + "\n")
            self._audit.flush()

    # -- forwarding ------------------------------------------------------------------------------

    def _new_state(self) -> dict[str, Any]:
        """
        One client request's attempt counters, its token among live requests, the faults it
        met (for its ``invocation`` record), when it started, and the answer it was sent.
        """
        return {
            "attempts": 0,
            "reopened": 0,
            "token": next(self._tokens),
            "faults": [],
            "t0": time.monotonic(),
            "final": None,
        }

    def _record(
        self,
        session: Session | None,
        message: dict[str, Any],
        state: dict[str, Any],
        final: dict[str, Any] | Response | None,
        *,
        kind: str | None = None,
    ) -> None:
        """One ``invocation`` audit record per client request: no arguments, no results."""
        if state.get("recorded"):
            return
        state["recorded"] = True
        if kind is None:
            if isinstance(final, Response):
                kind, outcome = resolution(None, status=final.status_code)
            else:
                kind, outcome = resolution(final)
            kind = with_faults(kind, state["faults"])
        else:
            outcome = None
        params = message.get("params")
        tool = params.get("name") if isinstance(params, dict) else None
        self.log(
            INVOCATION_EVENT,
            id=message.get("id"),
            session=session.client_id if session is not None else None,
            method=message.get("method"),
            tool=tool if message.get("method") == "tools/call" else None,
            keyed=bool(
                isinstance(params, dict)
                and isinstance(params.get("_meta"), dict)
                and IDEMPOTENCY_KEY in params["_meta"]
            ),
            attempts=state["attempts"],
            faults=list(state["faults"]),
            resolution=kind,
            server_outcome=outcome,
            ms=round((time.monotonic() - state["t0"]) * 1000),
        )

    def _release(self, session: Session | None, client_id: Any, state: dict[str, Any]) -> None:
        """The request has its final answer: a cancellation no longer goes to its attempts."""
        scope = (session.client_id if session is not None else None, _key(client_id))
        live = self._live.get(scope)
        if live is not None:
            live.pop(state["token"], None)
            if not live:
                del self._live[scope]

    def _headers(self, request: Request, session: Session | None) -> dict[str, str]:
        out = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
        if session is not None and session.upstream_id is not None:
            out[SESSION_HEADER] = session.upstream_id
        return out

    def _session(self, request: Request) -> tuple[Session | None, Response | None]:
        client_session = request.headers.get(SESSION_HEADER)
        if client_session is None:
            return None, None
        session = self.sessions.get(client_session)
        if session is None:
            return None, Response(status_code=404)  # as a server would: start a new session
        return session, None

    async def handle(self, request: Request) -> Response:
        if request.method == "POST":
            return await self.post(request)
        if request.method == "GET":
            return await self.get(request)
        return await self.delete(request)

    async def post(self, request: Request) -> Response:
        body = await request.body()
        try:
            message = json.loads(body)
        except ValueError:
            message = None
        session, refused = self._session(request)
        if refused is not None:
            return refused
        is_request = isinstance(message, dict) and "method" in message and "id" in message
        if not is_request:
            return await self.forward_other(request, body, message, session)
        assert isinstance(message, dict)
        if message.get("method") == "initialize" and session is None:
            return await self.open_session(request, message)
        return await self.relay_request(request, message, session)

    async def forward_other(
        self, request: Request, body: bytes, message: Any, session: Session | None
    ) -> Response:
        """A notification, a client's answer to a server request, or a batch: forwarded."""
        bodies = [body]
        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/cancelled"
            and isinstance(message.get("params"), dict)
        ):
            targets = self._cancel_targets(message["params"].get("requestId"), session)
            if targets is None:
                return Response(status_code=202)
            if targets:
                bodies = [
                    json.dumps(
                        {**message, "params": {**message["params"], "requestId": t}}
                    ).encode()
                    for t in targets
                ]
        answer: httpx2.Response | None = None
        for one in bodies:
            forwarded = await self._forward_one(request, one, session)
            if isinstance(forwarded, Response):
                return forwarded
            answer = forwarded
        assert answer is not None
        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/initialized"
            and session is not None
        ):
            session.initialized = True
        return self._plain(answer, session)

    def _cancel_targets(self, request_id: Any, session: Session | None) -> list[str] | None:
        """
        The attempts a client's cancellation of *request_id* goes to: every attempt of that
        request (a raced request has two), and nothing of any other client's. ``[]``: no live
        request of this client has that id (forwarded as it is). ``None``: two live requests
        share it -- possible only without sessions, where two clients cannot be told apart --
        so cancelling either could stop the other client's call; the cancellation, advisory in
        MCP, is dropped instead.
        """
        live = self._live.get(
            (session.client_id if session is not None else None, _key(request_id))
        )
        if not live:
            return []
        if len(live) > 1:
            self.log("cancel_ambiguous", request=request_id)
            return None
        return list(next(iter(live.values())))

    async def _forward_one(
        self, request: Request, body: bytes, session: Session | None
    ) -> httpx2.Response | Response:
        """One notification (or another message that is not a request); a lost session reopened."""
        for _ in range(2):
            try:
                answer = await self.client.post(
                    self.upstream, content=body, headers=self._headers(request, session)
                )
            except httpx2.TransportError as exc:
                self.log("forward_failed", error=f"{type(exc).__name__}: {exc}")
                return Response(status_code=502)
            lost = (
                session is not None
                and not self.passthrough
                and (
                    answer.status_code == 404
                    or (answer.status_code == 400 and says_session_unknown(answer.content))
                )
            )
            if not lost:
                break
            assert session is not None
            if not await self.reopen(session, request):
                break
        return answer

    def _plain(self, answer: httpx2.Response, session: Session | None) -> Response:
        headers = {
            k: v
            for k, v in answer.headers.items()
            if k.lower() not in HOP and k != "content-encoding"
        }
        if session is not None and SESSION_HEADER in answer.headers:
            headers[SESSION_HEADER] = session.client_id
        return Response(content=answer.content, status_code=answer.status_code, headers=headers)

    # -- sessions ----------------------------------------------------------------------------------

    async def open_session(self, request: Request, message: dict[str, Any]) -> Response:
        headers = self._headers(request, None)
        try:
            answer = await self.client.post(self.upstream, json=message, headers=headers)
        except httpx2.TransportError as exc:
            self.log("initialize_failed", error=f"{type(exc).__name__}: {exc}")
            return Response(status_code=502)
        upstream_id = answer.headers.get(SESSION_HEADER)
        if upstream_id is None:
            return self._plain(answer, None)  # a server without sessions: nothing to map
        client_id = secrets.token_hex(16)
        session = Session(client_id, upstream_id, message, headers)
        self.sessions[client_id] = session
        self.log("session_opened", session=client_id, upstream=upstream_id)
        return self._plain(answer, session)

    async def reopen(self, session: Session, request: Request) -> bool:
        """The server lost the session: open another with the client's handshake replayed."""
        stale = session.upstream_id
        async with session.lock:
            if session.upstream_id != stale:
                return True  # another request already reopened it
            headers = {**session.headers, **self._headers(request, None)}
            try:
                answer = await self.client.post(
                    self.upstream, json=session.initialize, headers=headers
                )
                upstream_id = answer.headers.get(SESSION_HEADER)
                if answer.status_code != 200 or upstream_id is None:
                    self.log(
                        "session_reopen_failed",
                        session=session.client_id,
                        status=answer.status_code,
                    )
                    return False
                headers[SESSION_HEADER] = upstream_id
                note = {"jsonrpc": "2.0", "method": "notifications/initialized"}
                await self.client.post(self.upstream, json=note, headers=headers)
            except httpx2.TransportError as exc:
                self.log(
                    "session_reopen_failed",
                    session=session.client_id,
                    error=f"{type(exc).__name__}: {exc}",
                )
                return False
            session.upstream_id = upstream_id
            self.log(
                "session_reopened", session=session.client_id, stale=stale, upstream=upstream_id
            )
            return True

    async def delete(self, request: Request) -> Response:
        session, refused = self._session(request)
        if refused is not None:
            return refused
        try:
            answer = await self.client.delete(
                self.upstream, headers=self._headers(request, session)
            )
        except httpx2.TransportError:
            return Response(status_code=502)
        if session is not None:
            self.sessions.pop(session.client_id, None)
        return self._plain(answer, session)

    async def get(self, request: Request) -> Response:
        """The server-to-client event stream: relayed as it is."""
        session, refused = self._session(request)
        if refused is not None:
            return refused
        headers = self._headers(request, session)
        if LAST_EVENT_ID in request.headers:
            headers[LAST_EVENT_ID] = request.headers[LAST_EVENT_ID]
        upstream = self.client.build_request("GET", self.upstream, headers=headers)
        try:
            answer = await self.client.send(upstream, stream=True)
        except httpx2.TransportError:
            return Response(status_code=502)
        if answer.status_code != 200:
            await answer.aread()
            await answer.aclose()
            return self._plain(answer, session)

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in answer.aiter_raw():
                    yield chunk
            except httpx2.TransportError:
                return
            finally:
                await answer.aclose()

        out = {"content-type": answer.headers.get("content-type", "text/event-stream")}
        if session is not None:
            out[SESSION_HEADER] = session.client_id
        return StreamingResponse(stream(), status_code=200, headers=out)

    # -- requests --------------------------------------------------------------------------------

    def _keyed(self, message: dict[str, Any]) -> tuple[dict[str, Any], bool, str | None]:
        """The request as sent (a key added if its tool advertises keys), keyed?, tool name."""
        if message.get("method") != "tools/call":
            return message, False, None
        raw = message.get("params")
        params: dict[str, Any] = raw if isinstance(raw, dict) else {}
        name = params.get("name")
        tool = name if isinstance(name, str) else None
        definition = self.tools.get(tool) if tool else None
        if not self.keys or definition is None or not tool_supports_keys(definition):
            return message, False, tool
        return with_key(message, new_key()), True, tool

    def _declaration(self, tool: str | None) -> Any:
        definition = self.tools.get(tool or "")
        annotations = definition.get("annotations") if definition else None
        return self.effects.of(
            tool or "",
            annotations=annotations if isinstance(annotations, dict) else None,
            trust_read_only_hints=self.trust_read_only_hints,
        )

    def _decide(
        self,
        message: dict[str, Any],
        observed: Observed,
        attempts: int,
        keyed: bool,
        tool: str | None,
    ) -> tuple[bool, str]:
        method = str(message.get("method"))
        if self.retry != "auto":
            return False, "retry off"
        if method == "tools/call":
            verdict = decide_retry(
                observed,
                self._declaration(tool),
                attempts_made=attempts,
                reread_unknown=True,
                keyed=keyed,
            )
            return verdict.retry, verdict.reason
        if method in SAFE_METHODS and attempts < DEFAULT_MAX_ATTEMPTS:
            return True, f"{method} changes nothing on the server; requested again"
        return False, f"{method} is not known to be safe to send twice"

    def _recoverable(self, method: str, tool: str | None, keyed: bool) -> bool:
        """A request the relay may send again after a deadline: it cannot run twice."""
        if method in SAFE_METHODS or keyed:
            return True
        if method != "tools/call":
            return False
        declaration = self._declaration(tool)
        return bool(declaration.declared and declaration.effect.value == "read_only")

    async def relay_request(
        self, request: Request, message: dict[str, Any], session: Session | None
    ) -> Response:
        method = str(message.get("method"))
        if method == "tools/list":
            self._lists.observe_request(message)
        state = self._new_state()
        if method == "tools/call" and not self.passthrough:
            checked = await self._check_call(request, message, session)
            if isinstance(checked, Response):
                self._record(session, message, state, None, kind="refused")
                return checked
            message = checked
        sent, keyed, tool = self._keyed(message)
        if (
            self.deadline is not None
            and self.retry == "auto"
            and self._recoverable(method, tool, keyed)
        ):
            raced = await self._race(request, message, sent, session, keyed, tool, state)
            self._record(session, sent, state, raced)
            return self._json_answer(method, raced, session)
        first = await self._attempt(request, sent, session, state)
        # The response form follows the first answer: an event stream is relayed as one.
        if (
            first.kind == "answer"
            and first.response is not None
            and first.message is None
            and first.response.headers.get("content-type", "").startswith("text/event-stream")
        ):
            out = {"content-type": "text/event-stream", "cache-control": "no-cache"}
            if session is not None:
                out[SESSION_HEADER] = session.client_id
            events = self._stream(request, message, sent, session, state, first, keyed, tool)
            return StreamingResponse(_released(events, self, session, sent, state), headers=out)
        final = await self._settle(request, message, sent, session, state, first, keyed, tool)
        self._record(session, sent, state, final)
        return self._json_answer(method, final, session)

    def _json_answer(
        self, method: str, final: dict[str, Any] | Response, session: Session | None
    ) -> Response:
        if isinstance(final, Response):
            return final
        self._learn(method, final)
        out = {"content-type": "application/json"}
        if session is not None:
            out[SESSION_HEADER] = session.client_id
        return Response(content=json.dumps(final), status_code=200, headers=out)

    def _definitive(self, final: dict[str, Any] | Response, keyed: bool) -> bool:
        """An answer that settles it: not the relay's own "unknown", nor "still running"."""
        if isinstance(final, Response) or "result" in final:
            return True
        error = final.get("error")
        if not isinstance(error, dict):
            return True
        data = error.get("data")
        if isinstance(data, dict) and data.get(UNKNOWN_OUTCOME_KEY) == "unknown":
            return False
        return not (keyed and error.get("code") in (IN_DOUBT_CODE, IN_PROGRESS_CODE))

    async def _race(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        keyed: bool,
        tool: str | None,
        state: dict[str, Any],
    ) -> dict[str, Any] | Response:
        """
        A recoverable request under ``--call-deadline``. If it has no answer in
        time, it is sent once more -- the first attempt stays open -- and the
        first answer that settles it wins; the other is abandoned. A keyed
        resend answered "in progress" or "in doubt" waits one more deadline
        for the original's late answer before that answer is delivered.
        """
        assert self.deadline is not None
        client_id = message.get("id")
        send, receive = anyio.create_memory_object_stream[dict[str, Any] | Response](2)

        async def racer() -> None:
            first = await self._attempt(request, sent, session, state)
            await send.send(
                await self._settle(request, message, sent, session, state, first, keyed, tool)
            )

        winner: dict[str, Any] | Response | None = None
        fallback: dict[str, Any] | Response | None = None
        try:
            async with anyio.create_task_group() as tg:
                tg.start_soon(racer)
                with anyio.move_on_after(self.deadline):
                    winner = await receive.receive()
                if winner is None:
                    state["faults"].append("deadline")
                    retry, why = self._decide(
                        message, Observed.UNKNOWN_DEADLINE, state["attempts"], keyed, tool
                    )
                    self.log(
                        "retry_decision",
                        id=client_id,
                        method=message.get("method"),
                        tool=tool,
                        keyed=keyed,
                        observed=Observed.UNKNOWN_DEADLINE.value,
                        attempts=state["attempts"],
                        retry=retry,
                        reason=why,
                    )
                    pending = 1
                    if retry:
                        tg.start_soon(racer)
                        pending = 2
                    while pending:
                        # Unbounded while both run; one more deadline once one has answered.
                        wait = self.deadline if fallback is not None else math.inf
                        answer = None
                        with anyio.move_on_after(wait):
                            answer = await receive.receive()
                        if answer is None:
                            break
                        pending -= 1
                        if self._definitive(answer, keyed):
                            winner = answer
                            break
                        fallback = answer
                        self.log("raced_answer_not_definitive", id=client_id)
                tg.cancel_scope.cancel()
        finally:
            self._release(session, client_id, state)
        if winner is not None:
            return winner
        return fallback if fallback is not None else unknown_outcome_response(client_id, "deadline")

    async def _attempt(
        self,
        request: Request,
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, Any],
    ) -> Attempt:
        """One attempt, until its headers (and, for JSON, its body)."""
        state["attempts"] += 1
        upstream_id = f"sg-{next(self._ids)}"
        scope = (session.client_id if session is not None else None, _key(sent.get("id")))
        self._live.setdefault(scope, {}).setdefault(state["token"], []).append(upstream_id)
        payload = {**sent, "id": upstream_id}
        deadline_at = time.monotonic() + self.connect_retry
        refused = False
        while True:
            try:
                upstream = self.client.build_request(
                    "POST", self.upstream, json=payload, headers=self._headers(request, session)
                )
                answer = await self.client.send(upstream, stream=True)
            except (httpx2.ConnectError, httpx2.ConnectTimeout, httpx2.PoolTimeout) as exc:
                # The connection was never opened: the request did not reach the server.
                if self.passthrough or time.monotonic() >= deadline_at:
                    self.log("not_sent", id=sent.get("id"), error=f"{type(exc).__name__}: {exc}")
                    state["faults"].append("not_sent")
                    return Attempt("not_sent", reason=str(exc))
                if not refused:
                    refused = True
                    state["faults"].append("connect_retry")
                await anyio.sleep(0.2)
                continue
            except httpx2.TransportError as exc:
                self.log("transport_lost", id=sent.get("id"), error=f"{type(exc).__name__}: {exc}")
                state["faults"].append("transport_lost")
                return Attempt("unknown", reason="transport_closed")
            break
        status = answer.status_code
        if status == 404 and session is not None and not self.passthrough:
            await answer.aread()
            await answer.aclose()
            state["faults"].append("session_lost")
            return Attempt("session_lost", response=answer)
        if status == 400 and session is not None and not self.passthrough:
            body = await answer.aread()
            await answer.aclose()
            if says_session_unknown(body):
                self.log("session_lost_400", id=sent.get("id"))
                state["faults"].append("session_lost")
                return Attempt("session_lost", response=answer)
            return Attempt("passthrough", response=answer, body=body)
        if status >= 500 and not self.passthrough:
            await answer.aread()
            await answer.aclose()
            self.log("server_error", id=sent.get("id"), status=status)
            state["faults"].append("server_error")
            return Attempt("unknown", reason="server_error", response=answer)
        if status != 200:
            body = await answer.aread()
            await answer.aclose()
            return Attempt("passthrough", response=answer, body=body)
        attempt = Attempt("answer", response=answer, upstream_id=upstream_id)
        if answer.headers.get("content-type", "").startswith("text/event-stream"):
            return attempt
        try:
            body = await answer.aread()
        except httpx2.TransportError:
            self.log("transport_lost", id=sent.get("id"), phase="body")
            return Attempt("unknown", reason="transport_closed")
        finally:
            await answer.aclose()
        try:
            parsed = json.loads(body)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict):
            return Attempt("passthrough", response=answer, body=body)
        attempt.message = parsed
        return attempt

    async def _events(
        self,
        request: Request,
        attempt: Attempt,
        session: Session | None,
        state: dict[str, Any] | None = None,
    ) -> AsyncIterator[tuple[str, dict[str, Any] | None]]:
        """
        The messages of an attempt's event stream: ``("event", message)`` for
        each one, then ``("end", answer)`` -- the answer, or None if the stream
        ended without it and could not be resumed.
        """
        assert attempt.response is not None
        upstream_id = attempt.upstream_id
        last_id: str | None = None
        response = attempt.response
        resumed = 0
        while True:
            try:
                async for event_id, data in sse_events(response.aiter_lines()):
                    last_id = event_id or last_id
                    try:
                        message = json.loads(data)
                    except ValueError:
                        continue
                    if (
                        isinstance(message, dict)
                        and message.get("id") == upstream_id
                        and "method" not in message
                    ):
                        yield "end", message
                        return
                    yield "event", message
            except httpx2.TransportError as exc:
                self.log("stream_cut", id=upstream_id, error=f"{type(exc).__name__}: {exc}")
            finally:
                await response.aclose()
            if state is not None:
                state["faults"].append("stream_cut")
            # Ended without the answer: resume it if the server numbered its events.
            if self.passthrough or last_id is None or session is None or resumed >= 3:
                yield "end", None
                return
            resumed += 1
            headers = {**self._headers(request, session), LAST_EVENT_ID: last_id}
            try:
                upstream = self.client.build_request("GET", self.upstream, headers=headers)
                response = await self.client.send(upstream, stream=True)
            except httpx2.TransportError:
                yield "end", None
                return
            if response.status_code != 200:
                await response.aclose()
                self.log("resume_refused", id=upstream_id, status=response.status_code)
                yield "end", None
                return
            self.log("stream_resumed", id=upstream_id, last_event_id=last_id)

    async def _stream(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, Any],
        first: Attempt,
        keyed: bool,
        tool: str | None,
    ) -> AsyncIterator[bytes]:
        client_id = message.get("id")
        attempt = first
        while True:
            answer: dict[str, Any] | None = None
            async for kind, item in self._events(request, attempt, session, state):
                if kind == "event" and item is not None:
                    yield sse(item)
                elif kind == "end":
                    answer = item
            if answer is not None:
                final = {**answer, "id": client_id}
                self._learn(str(message.get("method")), final)
                self._release(session, client_id, state)
                state["final"] = final
                yield sse(final)
                return
            # The stream ended without the answer, and could not be resumed.
            if self.passthrough:
                self._release(session, client_id, state)
                return  # forward only: the client sees the stream end, as without the relay
            attempt = await self._recover(
                request,
                message,
                sent,
                session,
                state,
                Attempt("unknown", reason="stream_cut"),
                keyed,
                tool,
            )
            if attempt.kind == "passthrough":
                # The server answered a resend with an error status: its answer, if it is one.
                self._release(session, client_id, state)
                parsed = _json_object(attempt.body)
                state["final"] = (
                    {**parsed, "id": client_id}
                    if parsed is not None
                    else unknown_outcome_response(client_id, "server_error")
                )
                yield sse(state["final"])
                return
            if attempt.kind != "answer" or attempt.response is None:
                final_message = self._final(client_id, attempt)
                self._release(session, client_id, state)
                state["final"] = final_message
                yield sse(final_message)
                return
            if attempt.message is not None:
                self._release(session, client_id, state)
                final = {**attempt.message, "id": client_id}
                self._learn(str(message.get("method")), final)
                state["final"] = final
                yield sse(final)
                return

    async def _settle(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, Any],
        attempt: Attempt,
        keyed: bool,
        tool: str | None,
    ) -> dict[str, Any] | Response:
        """Drive a request to its final answer, buffered (a JSON response)."""
        client_id = message.get("id")
        while True:
            if attempt.kind == "answer":
                if attempt.message is not None:
                    self._release(session, client_id, state)
                    return {**attempt.message, "id": client_id}
                answer: dict[str, Any] | None = None
                async for kind, item in self._events(request, attempt, session, state):
                    if kind == "end":
                        answer = item
                if answer is not None:
                    self._release(session, client_id, state)
                    return {**answer, "id": client_id}
                attempt = Attempt("unknown", reason="stream_cut")
            if self.passthrough and attempt.kind in ("unknown", "not_sent"):
                self._release(session, client_id, state)
                return Response(status_code=502)  # forward only: a broken exchange, as without it
            if attempt.kind == "passthrough":
                self._release(session, client_id, state)
                assert attempt.response is not None
                return Response(
                    content=attempt.body,
                    status_code=attempt.response.status_code,
                    headers={
                        "content-type": attempt.response.headers.get(
                            "content-type", "application/json"
                        )
                    },
                )
            attempt = await self._recover(
                request, message, sent, session, state, attempt, keyed, tool
            )
            if attempt.kind in ("not_sent", "unknown", "session_lost", "final"):
                self._release(session, client_id, state)
                return self._final(client_id, attempt)

    async def _recover(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, Any],
        attempt: Attempt,
        keyed: bool,
        tool: str | None,
    ) -> Attempt:
        """After a failed attempt: the next attempt, or a final ``Attempt`` to answer from."""
        client_id = message.get("id")
        while True:
            if attempt.kind == "session_lost":
                if session is None or state["reopened"] >= 2:
                    return Attempt("final", message=None, reason="session_lost")
                state["reopened"] += 1
                if not await self.reopen(session, request):
                    return Attempt("final", reason="session_lost")
                # A 404 for a lost session: the request was not processed. Sent again as it was.
                self.log("resent", id=client_id, why="session lost; the request was not processed")
                attempt = await self._attempt(request, sent, session, state)
                if attempt.kind in ("answer", "passthrough"):
                    return attempt
                continue
            if attempt.kind == "not_sent":
                return Attempt("not_sent")
            if attempt.kind == "unknown":
                observed = (
                    Observed.UNKNOWN_DEADLINE
                    if attempt.reason == "deadline"
                    else Observed.UNKNOWN_TRANSPORT
                )
                retry, why = self._decide(message, observed, state["attempts"], keyed, tool)
                self.log(
                    "retry_decision",
                    id=client_id,
                    method=message.get("method"),
                    tool=tool,
                    keyed=keyed,
                    observed=observed.value,
                    attempts=state["attempts"],
                    retry=retry,
                    reason=why,
                )
                if not retry:
                    return Attempt("final", reason=attempt.reason or "transport_closed")
                attempt = await self._attempt(request, sent, session, state)
                if attempt.kind in ("answer", "passthrough"):
                    return attempt
                continue
            return attempt

    def _final(self, client_id: Any, attempt: Attempt) -> dict[str, Any]:
        if attempt.kind == "not_sent":
            return not_sent_response(client_id)
        if attempt.kind == "final" and attempt.reason == "session_lost":
            return {
                "jsonrpc": "2.0",
                "id": client_id,
                "error": {
                    "code": UNKNOWN_OUTCOME_CODE,
                    "message": "MCP server lost the session and a new one could not be opened; "
                    "the request was not processed.",
                    "data": {UNKNOWN_OUTCOME_KEY: "not_sent"},
                },
            }
        return unknown_outcome_response(client_id, attempt.reason or "transport_closed")

    def _learn(self, method: str, answer: dict[str, Any]) -> None:
        """Keep the tool definitions a ``tools/list`` answer carries."""
        if method != "tools/list":
            return
        result = answer.get("result")
        tools = result.get("tools") if isinstance(result, dict) else None
        if not isinstance(tools, list):
            return
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                self.tools[tool["name"]] = tool
        complete = self._lists.observe_response(answer)
        if complete is not None:
            self._sync(complete, "client")

    def _sync(self, tools: list[Any], source: str) -> None:
        report = self.registry.sync(tools)
        self.log("tools_synced", source=source, summary=report.to_dict()["summary"])

    # -- checking calls against the server's schemas -----------------------------------------

    async def _check_call(
        self, request: Request, message: dict[str, Any], session: Session | None
    ) -> dict[str, Any] | Response:
        """
        The call to send (repaired, in auto mode, where that is safe), or the
        relay's own answer refusing it. A call is changed or refused only on a
        listing requested after it arrived (as the stdio relay does): a tool's
        schema may have changed since the last listing the relay saw.
        """
        try:
            call = ToolCallRequest.parse(message)
        except MalformedMessageError:
            return message  # the server's to reject
        outcome = self._evaluate(call)
        if outcome is None:
            return message
        listing = "current"
        if self.refresh and intervenes(call, outcome):
            if not await self._refresh_listing(request, session):
                self.log(
                    "call",
                    id=call.id,
                    tool=call.name,
                    action="unvalidated",
                    reason="could not confirm the tool listing is current; forwarded "
                    "unchanged rather than act on a possibly stale schema",
                )
                return message
            outcome = self._evaluate(call)
            if outcome is None:
                return message
            listing = "confirmed"
        self.log(
            "call",
            id=call.id,
            tool=call.name,
            action=outcome.action.value,
            auto_action=(outcome.auto_action or outcome.action).value,
            proposal=proposal(call, outcome),
            reason=outcome.reason,
            listing=listing,
            would_modify=intervenes(call, outcome),
        )
        forwarded = forward_message(call, outcome)
        if forwarded is None:
            refused = refusal_response(call, outcome)
            out = {"content-type": "application/json"}
            if session is not None:
                out[SESSION_HEADER] = session.client_id
            return Response(content=json.dumps(refused), status_code=200, headers=out)
        return forwarded

    def _evaluate(self, call: ToolCallRequest) -> Any:
        try:
            return self.registry.process_call(call.name, call.arguments)
        except ToolNotFoundError:
            self.log(
                "call",
                id=call.id,
                tool=call.name,
                action="unvalidated",
                reason="no definition for this tool; forwarded for the server to answer",
            )
        except ToolSchemaUnusableError as exc:
            self.log(
                "call",
                id=call.id,
                tool=call.name,
                action="unvalidated",
                reason=f"schema unreadable; forwarded: {exc.cause}",
            )
        return None

    async def _refresh_listing(self, request: Request, session: Session | None) -> bool:
        """List the server's tools now, on the call's own session; True once synced."""
        tools: list[Any] = []
        cursor: str | None = None
        for page in itertools.count():
            params: dict[str, Any] = {"cursor": cursor} if cursor is not None else {}
            listing = {
                "jsonrpc": "2.0",
                "id": f"sg-list-{next(self._ids)}",
                "method": "tools/list",
                "params": params,
            }
            state = self._new_state()
            first = await self._attempt(request, listing, session, state)
            answer = await self._settle(
                request, listing, listing, session, state, first, False, None
            )
            result = answer.get("result") if isinstance(answer, dict) else None
            if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
                self.log("tools_list_failed", page=page)
                return False
            tools.extend(result["tools"])
            for tool in result["tools"]:
                if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                    self.tools[tool["name"]] = tool
            following = result.get("nextCursor")
            if not isinstance(following, str) or page >= 50:
                break
            cursor = following
        self._sync(tools, "relay")
        return True


async def _released(
    events: AsyncIterator[bytes],
    relay: Relay,
    session: Session | None,
    message: dict[str, Any],
    state: dict[str, Any],
) -> AsyncIterator[bytes]:
    """
    *events*, then the request released and recorded however the stream ends (its client gone,
    too: then it was sent no answer).
    """
    try:
        async for chunk in events:
            yield chunk
    finally:
        relay._release(session, message.get("id"), state)
        relay._record(session, message, state, state.get("final"))


def _key(identifier: Any) -> str:
    return json.dumps(identifier)


def _json_object(body: bytes) -> dict[str, Any] | None:
    try:
        parsed = json.loads(body)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def build_app(relay: Relay, path: str) -> Starlette:
    async def endpoint(request: Request) -> Response:
        return await relay.handle(request)

    @contextlib.asynccontextmanager
    async def lifespan(_: Starlette) -> AsyncIterator[None]:
        yield
        await relay.client.aclose()

    return Starlette(
        routes=[Route(path, endpoint, methods=["GET", "POST", "DELETE"])], lifespan=lifespan
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="StateGuard relay for MCP over Streamable HTTP.")
    parser.add_argument("--listen", default="127.0.0.1:8800", help="host:port to serve on")
    parser.add_argument("--path", default="/mcp", help="the MCP endpoint path to serve")
    parser.add_argument("--passthrough", action="store_true", help="forward only (a baseline)")
    parser.add_argument(
        "--shadow", action="store_true", help="check calls and log; never change or refuse one"
    )
    parser.add_argument(
        "--no-listing-refresh",
        action="store_true",
        help="act on the last listing seen, without listing again first",
    )
    parser.add_argument("--retry", choices=["off", "auto"], default="off")
    parser.add_argument("--no-idempotency-keys", action="store_true")
    parser.add_argument("--effects", help="operator effect declarations (JSON), as the stdio relay")
    parser.add_argument("--trust-read-only-hints", action="store_true")
    parser.add_argument(
        "--connect-retry",
        type=float,
        default=10.0,
        help="seconds to keep re-sending a request that could not be sent",
    )
    parser.add_argument(
        "--call-deadline",
        type=float,
        default=None,
        help="seconds before a recoverable request (a declared read, a request that changes "
        "nothing, a keyed call) is sent once more; the first answer wins (needs --retry auto)",
    )
    parser.add_argument(
        "--upstream-timeout",
        type=float,
        default=300.0,
        help="seconds to wait for the server's answer",
    )
    parser.add_argument("--audit", help="append one JSON line per relay decision here")
    parser.add_argument("upstream", help="the MCP server's Streamable HTTP endpoint URL")
    args = parser.parse_args(argv)
    host, _, port = args.listen.rpartition(":")
    relay = Relay(args)
    uvicorn.run(
        build_app(relay, args.path),
        host=host or "127.0.0.1",
        port=int(port),
        log_level="warning",
        lifespan="on",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

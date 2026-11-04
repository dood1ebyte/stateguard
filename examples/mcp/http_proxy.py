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

Limits. One server per relay. No relay deadline yet: a server that never
answers is waited for until ``--upstream-timeout`` (the stdio relay's
``--call-deadline`` has no HTTP counterpart here). No schema validation or
repair (the stdio relay's), no recording, no reconciliation probes. A client
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

from stateguard.adapters.mcp.effects import (
    DEFAULT_MAX_ATTEMPTS,
    EffectPolicy,
    Observed,
    decide_retry,
)
from stateguard.adapters.mcp.idempotency import new_key, tool_supports_keys, with_key

SESSION_HEADER = "mcp-session-id"
LAST_EVENT_ID = "last-event-id"
UNKNOWN_OUTCOME_CODE = -32000
UNKNOWN_OUTCOME_KEY = "io.stateguard/executionState"
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
        self.sessions: dict[str, Session] = {}
        #: A client request's id -> the id of its current attempt (for cancellations).
        self._live: dict[str, str] = {}
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
        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/cancelled"
            and isinstance(message.get("params"), dict)
        ):
            upstream_id = self._live.get(_key(message["params"].get("requestId")))
            if upstream_id is not None:
                message = {**message, "params": {**message["params"], "requestId": upstream_id}}
                body = json.dumps(message).encode()
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
        if (
            isinstance(message, dict)
            and message.get("method") == "notifications/initialized"
            and session is not None
        ):
            session.initialized = True
        return self._plain(answer, session)

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

    async def relay_request(
        self, request: Request, message: dict[str, Any], session: Session | None
    ) -> Response:
        sent, keyed, tool = self._keyed(message)
        method = str(message.get("method"))
        state = {"attempts": 0, "reopened": 0}
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
            return StreamingResponse(events, status_code=200, headers=out)
        final = await self._settle(request, message, sent, session, state, first, keyed, tool)
        if isinstance(final, Response):
            return final
        self._learn(method, final)
        out = {"content-type": "application/json"}
        if session is not None:
            out[SESSION_HEADER] = session.client_id
        return Response(content=json.dumps(final), status_code=200, headers=out)

    async def _attempt(
        self,
        request: Request,
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, int],
    ) -> Attempt:
        """One attempt, until its headers (and, for JSON, its body)."""
        state["attempts"] += 1
        upstream_id = f"sg-{next(self._ids)}"
        self._live[_key(sent.get("id"))] = upstream_id
        payload = {**sent, "id": upstream_id}
        deadline_at = time.monotonic() + self.connect_retry
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
                    return Attempt("not_sent", reason=str(exc))
                await anyio.sleep(0.2)
                continue
            except httpx2.TransportError as exc:
                self.log("transport_lost", id=sent.get("id"), error=f"{type(exc).__name__}: {exc}")
                return Attempt("unknown", reason="transport_closed")
            break
        status = answer.status_code
        if status == 404 and session is not None and not self.passthrough:
            await answer.aread()
            await answer.aclose()
            return Attempt("session_lost", response=answer)
        if status == 400 and session is not None and not self.passthrough:
            body = await answer.aread()
            await answer.aclose()
            if says_session_unknown(body):
                self.log("session_lost_400", id=sent.get("id"))
                return Attempt("session_lost", response=answer)
            return Attempt("passthrough", response=answer, body=body)
        if status >= 500 and not self.passthrough:
            await answer.aread()
            await answer.aclose()
            self.log("server_error", id=sent.get("id"), status=status)
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
        self, request: Request, attempt: Attempt, session: Session | None
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
        state: dict[str, int],
        first: Attempt,
        keyed: bool,
        tool: str | None,
    ) -> AsyncIterator[bytes]:
        client_id = message.get("id")
        attempt = first
        while True:
            answer: dict[str, Any] | None = None
            async for kind, item in self._events(request, attempt, session):
                if kind == "event" and item is not None:
                    yield sse(item)
                elif kind == "end":
                    answer = item
            if answer is not None:
                final = {**answer, "id": client_id}
                self._learn(str(message.get("method")), final)
                self._live.pop(_key(client_id), None)
                yield sse(final)
                return
            # The stream ended without the answer, and could not be resumed.
            if self.passthrough:
                self._live.pop(_key(client_id), None)
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
                self._live.pop(_key(client_id), None)
                parsed = _json_object(attempt.body)
                yield sse(
                    {**parsed, "id": client_id}
                    if parsed is not None
                    else unknown_outcome_response(client_id, "server_error")
                )
                return
            if attempt.kind != "answer" or attempt.response is None:
                final_message = self._final(client_id, attempt)
                self._live.pop(_key(client_id), None)
                yield sse(final_message)
                return
            if attempt.message is not None:
                self._live.pop(_key(client_id), None)
                final = {**attempt.message, "id": client_id}
                self._learn(str(message.get("method")), final)
                yield sse(final)
                return

    async def _settle(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, int],
        attempt: Attempt,
        keyed: bool,
        tool: str | None,
    ) -> dict[str, Any] | Response:
        """Drive a request to its final answer, buffered (a JSON response)."""
        client_id = message.get("id")
        while True:
            if attempt.kind == "answer":
                if attempt.message is not None:
                    self._live.pop(_key(client_id), None)
                    return {**attempt.message, "id": client_id}
                answer: dict[str, Any] | None = None
                async for kind, item in self._events(request, attempt, session):
                    if kind == "end":
                        answer = item
                if answer is not None:
                    self._live.pop(_key(client_id), None)
                    return {**answer, "id": client_id}
                attempt = Attempt("unknown", reason="stream_cut")
            if self.passthrough and attempt.kind in ("unknown", "not_sent"):
                self._live.pop(_key(client_id), None)
                return Response(status_code=502)  # forward only: a broken exchange, as without it
            if attempt.kind == "passthrough":
                self._live.pop(_key(client_id), None)
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
                self._live.pop(_key(client_id), None)
                return self._final(client_id, attempt)

    async def _recover(
        self,
        request: Request,
        message: dict[str, Any],
        sent: dict[str, Any],
        session: Session | None,
        state: dict[str, int],
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

"""
The JSON-RPC boundary: just enough of MCP's envelope to sit in the middle of it.

A proxy that works on raw MCP messages -- a stdio relay, a gateway -- needs
to change exactly one thing in a ``tools/call`` request, ``params.arguments``,
and preserve everything else: the request ``id`` the client will match the
response against, ``params._meta`` (progress tokens, tracing), and any
parameter a later protocol revision adds. Doing that with ad-hoc dict
surgery at every call site is how an ``id`` gets lost or a ``_meta``
silently dropped. These helpers do it once.

What is here
------------
* ``ToolCallRequest.parse`` -- a ``tools/call`` request, validated, with the
  original message kept verbatim.
* ``forward_message`` -- the request to send on, given a ``ToolCallOutcome``:
  the original message byte-for-byte when the arguments are unchanged, the
  original with only ``params.arguments`` replaced otherwise, ``None`` when
  the call is not forwarded.
* ``refusal_response`` -- the response to return instead, for ``REFUSE`` /
  ``ESCALATE``: a ``CallToolResult`` with ``isError: true``, so the model
  sees why and can correct itself. MCP distinguishes tool errors, reported
  in the result where the model reads them, from protocol errors, which a
  client handles and the model usually never sees; a correctable argument
  problem belongs in the first.
* ``error_response`` -- a JSON-RPC error, for the cases that are protocol
  errors rather than tool errors.
* ``ToolListAccumulator`` -- collects a paginated ``tools/list`` into one
  complete listing for ``ToolRegistry.sync``. Syncing a single page would
  remove every tool listed on the others.

What is not
-----------
No transport, no sessions, no initialization handshake, no capability
negotiation -- StateGuard has no reason to touch any of them, and an MCP SDK
does them better. Results pass through untouched: there is no helper for
rewriting a ``tools/call`` *result*, on purpose. Repairing tool output is out
of scope, and a relay that never builds a result for a forwarded call cannot
corrupt one.

JSON-RPC batches (a top-level array) are not parsed here; MCP removed batch
support in its 2025-06-18 revision. A relay that still sees one can apply
these helpers per element.

Zero external dependencies -- dicts in, dicts out.
"""

from __future__ import annotations

import copy
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from stateguard.adapters.mcp.errors import MalformedMessageError
from stateguard.adapters.mcp.outcomes import MCPAction, ToolCallOutcome
from stateguard.core.paths import MAX_PAYLOAD_DEPTH, nests_deeper_than

__all__ = [
    "INVALID_PARAMS",
    "JSONRPC_VERSION",
    "PROTOCOL_VERSION_META_KEY",
    "RESULT_TYPE_SINCE",
    "TOOLS_CALL",
    "TOOLS_LIST",
    "TOOLS_LIST_CHANGED",
    "ToolCallRequest",
    "ToolListAccumulator",
    "error_response",
    "forward_message",
    "is_notification",
    "is_request",
    "is_response",
    "protocol_version",
    "refusal_response",
    "request_id",
    "tool_error_response",
]

JSONRPC_VERSION = "2.0"
TOOLS_CALL = "tools/call"
TOOLS_LIST = "tools/list"
TOOLS_LIST_CHANGED = "notifications/tools/list_changed"

#: JSON-RPC 2.0 "Invalid params".
INVALID_PARAMS = -32602

#: Where MCP revisions without an ``initialize`` handshake carry the protocol
#: version: on every request, in ``params._meta``.
PROTOCOL_VERSION_META_KEY = "io.modelcontextprotocol/protocolVersion"

#: The first revision whose results carry ``resultType`` ("complete" or
#: "input_required"). A result a relay *synthesizes* for such a request must
#: carry it too: a client of that revision discriminates results on it and
#: rejects one without it. Older sessions never see the field.
RESULT_TYPE_SINCE = "2026-07-28"

RequestId = str | int


# ---------------------------------------------------------------------------
# Message classification
# ---------------------------------------------------------------------------


def _valid_id(value: Any) -> bool:
    # bool is an int subclass; JSON-RPC ids are strings or numbers, never true.
    return isinstance(value, str) or (isinstance(value, int) and not isinstance(value, bool))


def is_request(message: Any, method: str | None = None) -> bool:
    """A JSON-RPC request (has ``method`` and an ``id``), optionally of *method*."""
    return (
        isinstance(message, Mapping)
        and isinstance(message.get("method"), str)
        and "id" in message
        and (method is None or message["method"] == method)
    )


def is_notification(message: Any, method: str | None = None) -> bool:
    """A JSON-RPC notification (has ``method``, no ``id``), optionally of *method*."""
    return (
        isinstance(message, Mapping)
        and isinstance(message.get("method"), str)
        and "id" not in message
        and (method is None or message["method"] == method)
    )


def is_response(message: Any) -> bool:
    """A JSON-RPC response (an ``id`` and a ``result`` or an ``error``, no ``method``)."""
    return (
        isinstance(message, Mapping)
        and "method" not in message
        and "id" in message
        and ("result" in message or "error" in message)
    )


def request_id(message: Any) -> RequestId | None:
    """The message's ``id`` when it is a valid one, else ``None``."""
    if isinstance(message, Mapping) and _valid_id(message.get("id")):
        value: RequestId = message["id"]
        return value
    return None


# ---------------------------------------------------------------------------
# tools/call
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ToolCallRequest:
    """
    One ``tools/call`` request, parsed. The original message is kept intact.

    Attributes
    ----------
    id:
        The JSON-RPC request id -- what the client matches the response to.
    name:
        ``params.name``.
    arguments:
        ``params.arguments`` exactly as received; ``None`` when absent.
        ``ToolRegistry.process_call`` / ``repair_tool_call`` read ``None`` as
        ``{}``, as MCP specifies.
    meta:
        ``params._meta`` when present.
    message:
        A deep copy of the whole request, for forwarding.
    """

    id: RequestId
    name: str
    arguments: Any
    meta: Mapping[str, Any] | None
    message: Mapping[str, Any] = field(repr=False)

    @classmethod
    def parse(cls, message: Any) -> ToolCallRequest:
        """
        Parse a ``tools/call`` request; ``MalformedMessageError`` if it is not one.

        ``arguments`` is not validated here -- a non-object ``arguments`` is a
        payload problem, and StateGuard reports those as outcomes, not as
        malformed messages.
        """
        if not is_request(message, TOOLS_CALL):
            raise MalformedMessageError(
                f"Not a {TOOLS_CALL!r} request: expected an object with 'method': "
                f"{TOOLS_CALL!r} and an 'id'."
            )
        if message.get("jsonrpc") != JSONRPC_VERSION:
            raise MalformedMessageError(f"'jsonrpc' must be {JSONRPC_VERSION!r}.")
        identifier = message["id"]
        if not _valid_id(identifier):
            raise MalformedMessageError(
                f"Request 'id' must be a string or an integer, got {identifier!r}."
            )
        params = message.get("params")
        if not isinstance(params, Mapping):
            raise MalformedMessageError(f"{TOOLS_CALL!r} needs 'params' as an object.")
        name = params.get("name")
        if not isinstance(name, str) or not name:
            raise MalformedMessageError(f"{TOOLS_CALL!r} needs a non-empty string 'params.name'.")
        if nests_deeper_than(params, MAX_PAYLOAD_DEPTH):
            # Refused before the deep copy below, which would otherwise raise
            # RecursionError on a message json.loads accepted without fuss.
            raise MalformedMessageError(
                f"{TOOLS_CALL!r} params nest deeper than {MAX_PAYLOAD_DEPTH} levels."
            )
        # Read everything from the copy: handing back the caller's own
        # ``arguments`` dict would let a later mutation of their message
        # change what this request says was received.
        kept = copy.deepcopy(dict(message))
        meta = kept["params"].get("_meta")
        return cls(
            id=identifier,
            name=name,
            arguments=kept["params"].get("arguments"),
            meta=meta if isinstance(meta, Mapping) else None,
            message=kept,
        )

    def with_arguments(self, arguments: Any) -> dict[str, Any]:
        """The original request with only ``params.arguments`` replaced."""
        message = copy.deepcopy(dict(self.message))
        params = dict(message["params"])
        params["arguments"] = copy.deepcopy(arguments)
        message["params"] = params
        return message


def forward_message(request: ToolCallRequest, outcome: ToolCallOutcome) -> dict[str, Any] | None:
    """
    The request to send to the server, or ``None`` if the call is not forwarded.

    When the outcome forwards the arguments as received -- ``HOLD``, or a
    ``FORWARD`` that changed nothing -- this is the original message,
    unmodified: a proxy in shadow mode puts on the wire exactly what the
    client sent, absent ``arguments`` included. Otherwise it is the original
    with ``params.arguments`` replaced, and nothing else touched.
    """
    if not outcome.should_forward:
        return None
    received = {} if request.arguments is None else request.arguments
    if outcome.arguments == received and type(outcome.arguments) is type(received):
        return copy.deepcopy(dict(request.message))
    return request.with_arguments(outcome.arguments)


def protocol_version(request: ToolCallRequest) -> str | None:
    """
    The MCP protocol version the request declares in ``params._meta``, if any.

    Revisions from ``2026-07-28`` have no session handshake; each request
    names its version there. Under the older ``initialize`` handshake the
    version was negotiated once per session and requests carry none, so this
    is ``None`` -- the caller knows the session's version, this helper does not.
    """
    if request.meta is None:
        return None
    version = request.meta.get(PROTOCOL_VERSION_META_KEY)
    return version if isinstance(version, str) else None


def tool_error_response(request: ToolCallRequest, text: str) -> dict[str, Any]:
    """
    A ``CallToolResult`` with ``isError: true`` and one text block, answering *request*.

    Shaped for the protocol revision the request declares: ``resultType:
    "complete"`` is included when that revision has it (``RESULT_TYPE_SINCE``
    and later). Found by driving a real MCP 2.x client through a relay -- it
    rejected a synthesized result without the field, though every result the
    server itself produced had it.
    """
    result: dict[str, Any] = {"content": [{"type": "text", "text": text}], "isError": True}
    version = protocol_version(request)
    if version is not None and version >= RESULT_TYPE_SINCE:
        result["resultType"] = "complete"
    return {"jsonrpc": JSONRPC_VERSION, "id": request.id, "result": result}


def refusal_response(request: ToolCallRequest, outcome: ToolCallOutcome) -> dict[str, Any]:
    """
    The response to return to the client for a call StateGuard did not forward.

    A ``CallToolResult`` with ``isError: true`` and one text block: why the
    call was not sent (``outcome.reason``) and the per-field explanation.
    Returned as a *tool* error, not a JSON-RPC error, so the model reads it
    and can correct the call; use ``error_response`` with ``INVALID_PARAMS``
    instead if a deployment wants refusals handled by the client. Value-free:
    it names fields and reasons, never argument values.

    Raises ``ValueError`` for an outcome that *is* forwarded: there is
    nothing to refuse.
    """
    if outcome.should_forward:
        raise ValueError(f"{outcome.action.value} is forwarded; there is nothing to refuse.")
    lead = (
        "The arguments were not sent: StateGuard found a possible correction but "
        "could not apply it with confidence. Re-issue the call with the intended "
        "parameter names."
        if outcome.action is MCPAction.ESCALATE
        else "The arguments were not sent: they do not match the tool's input schema, "
        "and no safe correction exists."
    )
    return tool_error_response(request, f"{lead}\n\n{outcome.explain()}")


def error_response(
    identifier: RequestId | None, code: int, message: str, data: Any = None
) -> dict[str, Any]:
    """A JSON-RPC error response. *identifier* ``None`` when the request id was unreadable."""
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": JSONRPC_VERSION, "id": identifier, "error": error}


# ---------------------------------------------------------------------------
# tools/list
# ---------------------------------------------------------------------------


class ToolListAccumulator:
    """
    Turns the ``tools/list`` traffic a relay observes into complete listings.

    ``tools/list`` is paginated: a response may carry ``nextCursor``, and the
    client fetches the next page with it. Syncing a registry from one page
    would remove every tool on the others, so this collects pages until a
    response arrives with no ``nextCursor``, and only then hands over the
    complete list.

    Feed it every client->server request (``observe_request``) and every
    server->client response (``observe_response``). Responses it did not
    see the request for, error responses, and pages from a listing that a
    newer one superseded are ignored. A request with no cursor starts a new
    listing.

    Not thread-safe; one per connection.
    """

    def __init__(self) -> None:
        self._pending: dict[RequestId, str | None] = {}
        self._pages: list[Any] = []
        self._expected_cursor: str | None = None
        self._open = False

    def observe_request(self, message: Any) -> None:
        if not is_request(message, TOOLS_LIST):
            return
        identifier = request_id(message)
        if identifier is None:
            return
        params = message.get("params")
        cursor = params.get("cursor") if isinstance(params, Mapping) else None
        self._pending[identifier] = cursor if isinstance(cursor, str) else None

    def observe_response(self, message: Any) -> list[Any] | None:
        """The complete tool list when *message* finishes one; else ``None``."""
        if not is_response(message):
            return None
        identifier = request_id(message)
        if identifier is None or identifier not in self._pending:
            return None
        cursor = self._pending.pop(identifier)
        result = message.get("result")
        if not isinstance(result, Mapping) or not isinstance(result.get("tools"), list):
            return None  # an error, or not a listing: nothing to learn

        if cursor is None:
            self._pages, self._open = [], True  # a fresh listing
        elif not self._open or cursor != self._expected_cursor:
            return None  # a page of a listing that has since been superseded

        self._pages.extend(result["tools"])
        next_cursor = result.get("nextCursor")
        if isinstance(next_cursor, str) and next_cursor:
            self._expected_cursor = next_cursor
            return None

        complete, self._pages, self._open = self._pages, [], False
        self._expected_cursor = None
        return complete

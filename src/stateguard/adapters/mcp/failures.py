"""
Failure classification: is this an authentication or authorization failure?

StateGuard does not authenticate, authorize, hold credentials or decide
security policy. It only needs to *recognise* an auth failure, so that the
rest of the runtime does the right thing with it: never retry it, never
repair around it, never count it as a transient fault or an ordinary tool
bug, never claim to have recovered it. Recognising one is a fact-finding
exercise, so the classifier is deterministic, small, auditable and
fail-safe: every result names the single rule that produced it, and when
no rule fires the existing class stands.

Classes
-------
=====================  ======================================================
``AUTHENTICATION``     Missing, invalid or expired credentials (401-like).
``AUTHORIZATION``      Authenticated, but not permitted (403-like).
``PROTOCOL``           A JSON-RPC error whose meaning is not auth.
``TRANSPORT``          The connection failed and the request certainly did
                       not execute (e.g. another relay's "not sent").
``TOOL``               A ``CallToolResult`` with ``isError: true``.
``UNKNOWN``            Execution state unknown (an upstream relay's
                       "outcome unknown"). Never reclassified.
``NONE``               Not a failure.
=====================  ======================================================

Signals, strongest first
------------------------
MCP defines no JSON-RPC error code for auth: authorization lives in the HTTP
transport (401 + ``WWW-Authenticate: Bearer error="invalid_token"``, 403 +
``error="insufficient_scope"``, RFC 6750). So:

1. **StateGuard's own execution-state marker** (an upstream StateGuard relay's
   ``data["io.stateguard/executionState"]``) wins over everything: ``unknown``
   is ``UNKNOWN``, ``not_sent`` is ``TRANSPORT``. Auth-like text never
   reclassifies it.
2. **Structured fields** in a JSON-RPC ``error.data`` object:

   * an HTTP status under ``status`` / ``statusCode`` / ``status_code`` /
     ``httpStatus`` / ``http_status`` -- integer 401 or 403 only;
   * an OAuth / Bearer error code under ``error`` / ``error_code`` /
     ``errorCode``: ``invalid_token``, ``invalid_client``, ``invalid_grant``
     (authentication); ``insufficient_scope``, ``access_denied``,
     ``unauthorized_client`` (authorization). Codes defined by RFC 6749 /
     6750, compared exactly.

   A tool result's ``structuredContent`` is the tool's own data -- a status
   in it may describe some other resource -- and is never read.
3. **HTTP transport metadata**, for hosts that have it
   (``classify_http_status``): 401 / 403, refined by the ``WWW-Authenticate``
   ``error`` parameter.
4. **Narrow text fallback** -- only on a JSON-RPC ``error.message`` or the
   text items of an ``isError`` tool result, and only these shapes:

   * ``401 Unauthorized`` / ``403 Forbidden`` -- the RFC 9110 status line,
     number and reason phrase together (not followed by a capitalised word,
     so ``403 Forbidden City`` does not count);
   * ``HTTP 401``, ``HTTP/1.1 403``, ``HTTP Error 401`` -- an HTTP-prefixed
     status;
   * ``401 Client Error: Unauthorized`` (the ``requests`` library's form);
   * ``error="invalid_token"`` / ``error="insufficient_scope"`` -- a
     ``WWW-Authenticate`` parameter echoed in the text;
   * a message that *is* the reason phrase: ``Unauthorized``,
     ``Unauthenticated`` or ``Forbidden``, alone or followed by punctuation
     (``Unauthorized: token expired``), never by more words.

   Words such as *token*, *permission*, *access*, *auth*, *credential* or
   *authorization* on their own never classify anything.

5. **JSON-RPC error codes are not auth signals.** No MCP code means auth,
   and ``-32001`` is the MCP SDK's *request timeout*.

When signals of both kinds are present the result is ``AUTHENTICATION`` (a
credential problem comes before a permission problem); either way it is an
auth failure and is never retried.

Zero dependencies.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from stateguard.adapters.mcp.effects import Observed

__all__ = [
    "AUTH_CODES",
    "FailureClass",
    "FailureClassification",
    "classify_http_status",
    "classify_response",
    "signal_spans",
]

#: Text beyond this many characters is not scanned (bounded work per message).
MAX_TEXT_SCANNED = 4096


class FailureClass(StrEnum):
    AUTHENTICATION = "authentication"
    AUTHORIZATION = "authorization"
    PROTOCOL = "protocol"
    TRANSPORT = "transport"
    TOOL = "tool"
    UNKNOWN = "unknown"
    NONE = "none"

    @property
    def is_auth(self) -> bool:
        return self in (FailureClass.AUTHENTICATION, FailureClass.AUTHORIZATION)


@dataclass(frozen=True)
class FailureClassification:
    failure: FailureClass
    #: The one rule that decided it (``"default"`` when none fired). Value-free.
    signal: str

    @property
    def is_auth(self) -> bool:
        return self.failure.is_auth

    @property
    def observed(self) -> Observed:
        """The attempt as ``decide_retry`` sees it."""
        return {
            FailureClass.AUTHENTICATION: Observed.AUTH_ERROR,
            FailureClass.AUTHORIZATION: Observed.AUTH_ERROR,
            FailureClass.PROTOCOL: Observed.PROTOCOL_ERROR,
            FailureClass.TRANSPORT: Observed.PROTOCOL_ERROR,  # a definite answer: not retried
            FailureClass.TOOL: Observed.TOOL_ERROR,
            # An upstream relay's definite answer that the outcome is unknown:
            # delivered as-is, never retried here (as any JSON-RPC error).
            FailureClass.UNKNOWN: Observed.PROTOCOL_ERROR,
            FailureClass.NONE: Observed.SUCCESS,
        }[self.failure]

    def to_dict(self) -> dict[str, Any]:
        return {"failure": self.failure.value, "signal": self.signal}


_AUTHN, _AUTHZ = FailureClass.AUTHENTICATION, FailureClass.AUTHORIZATION

_STATUS_KEYS = ("status", "statusCode", "status_code", "httpStatus", "http_status")
_CODE_KEYS = ("error", "error_code", "errorCode")
_OAUTH_CODES = {
    "invalid_token": _AUTHN,
    "invalid_client": _AUTHN,
    "invalid_grant": _AUTHN,
    "insufficient_scope": _AUTHZ,
    "access_denied": _AUTHZ,
    "unauthorized_client": _AUTHZ,
}
_HTTP_STATUS = {401: _AUTHN, 403: _AUTHZ}
#: The OAuth / Bearer error codes the classifier reads: fixed vocabulary, never data.
AUTH_CODES = frozenset(_OAUTH_CODES)

_EXECUTION_STATE_KEY = "io.stateguard/executionState"

# Each pattern's first group is what decides the class (see _text_class).
_TEXT_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "text:status_line",
        # Case-insensitive phrase; a following capitalised word disqualifies it.
        re.compile(r"\b((?i:401\s+unauthori[sz]ed|403\s+forbidden))\b(?![ \t]+[A-Z][a-z])"),
    ),
    ("text:http_status", re.compile(r"(?i)\bHTTP(?:/\d(?:\.\d)?)?(?:\s+error)?:?\s+(401|403)\b")),
    (
        "text:client_error",
        re.compile(r"(?i)\b(401|403) Client Error: (?:Unauthorized|Forbidden)\b"),
    ),
    ("text:bearer_error", re.compile(r'\berror="(invalid_token|insufficient_scope)"')),
    (
        "text:reason_phrase",
        re.compile(r"(?i)^\s*(unauthori[sz]ed|unauthenticated|forbidden)\s*(?:[:.;,!\-–—]|$)"),
    ),
)


def _text_class(token: str) -> FailureClass:
    word = token.lower()
    if word.startswith(("401", "unauthori", "unauthenticated", "invalid_token")):
        return _AUTHN
    return _AUTHZ  # 403, forbidden, insufficient_scope


def _resolve(found: list[tuple[FailureClass, str]]) -> FailureClassification | None:
    if not found:
        return None
    classes = {failure for failure, _ in found}
    if len(classes) == 1:
        failure, signal = found[0]
        return FailureClassification(failure, signal)
    # Both kinds: a credential problem precedes a permission problem.
    signal = next(s for f, s in found if f is _AUTHN)
    return FailureClassification(_AUTHN, f"{signal}+conflicting")


def _structured(data: Any) -> FailureClassification | None:
    if not isinstance(data, Mapping):
        return None
    found: list[tuple[FailureClass, str]] = []
    for key in _STATUS_KEYS:
        status = data.get(key)
        if isinstance(status, int) and not isinstance(status, bool) and status in _HTTP_STATUS:
            found.append((_HTTP_STATUS[status], f"data:{key}"))
    for key in _CODE_KEYS:
        code = data.get(key)
        if isinstance(code, str) and code in _OAUTH_CODES:
            found.append((_OAUTH_CODES[code], f"data:{key}"))
    return _resolve(found)


def _text(texts: list[str]) -> FailureClassification | None:
    found: list[tuple[FailureClass, str]] = []
    for text in texts:
        sample = text[:MAX_TEXT_SCANNED]
        for name, pattern in _TEXT_RULES:
            match = pattern.search(sample)
            if match:
                found.append((_text_class(match.group(1)), name))
    return _resolve(found)


def signal_spans(text: str) -> list[tuple[int, int]]:
    """
    Where in *text* the text rules match (within the scanned prefix). The
    matched spans are fixed vocabulary -- a status number and a reason
    phrase, or an RFC 6750 code -- so a redactor can keep them verbatim and
    a recording still classifies as the live traffic did.
    """
    sample = text[:MAX_TEXT_SCANNED]
    return [m.span() for _, pattern in _TEXT_RULES for m in pattern.finditer(sample)]


def classify_response(message: Any) -> FailureClassification:
    """
    Classify a JSON-RPC response to a request (a ``tools/call`` answer, or
    any other). Never raises; never reads values beyond the documented fields.
    """
    if not isinstance(message, Mapping):
        return FailureClassification(FailureClass.PROTOCOL, "malformed")
    if "error" in message:
        error = message.get("error")
        if not isinstance(error, Mapping):
            return FailureClassification(FailureClass.PROTOCOL, "malformed")
        data = error.get("data")
        state = data.get(_EXECUTION_STATE_KEY) if isinstance(data, Mapping) else None
        if state == "unknown":
            return FailureClassification(FailureClass.UNKNOWN, "data:executionState")
        if state == "not_sent":
            return FailureClassification(FailureClass.TRANSPORT, "data:executionState")
        decided = _structured(data)
        if decided is not None:
            return decided
        text = error.get("message")
        decided = _text([text]) if isinstance(text, str) else None
        return decided or FailureClassification(FailureClass.PROTOCOL, "default")
    result = message.get("result")
    if not isinstance(result, Mapping):
        return FailureClassification(FailureClass.PROTOCOL, "malformed")
    if result.get("isError") is not True:
        return FailureClassification(FailureClass.NONE, "default")
    content = result.get("content")
    texts = [
        item["text"]
        for item in (content if isinstance(content, list) else [])
        if isinstance(item, Mapping)
        and item.get("type") == "text"
        and isinstance(item.get("text"), str)
    ]
    return _text(texts) or FailureClassification(FailureClass.TOOL, "default")


def classify_http_status(
    status: int | None, www_authenticate: str | None = None
) -> FailureClassification:
    """
    For hosts on an HTTP transport: the response status (and its
    ``WWW-Authenticate`` header). 401 is authentication and 403
    authorization, unless the header's ``error`` parameter says otherwise
    (``insufficient_scope`` on a 401 is authorization). Any other status is
    a transport failure as far as auth is concerned -- not this module's to
    refine.
    """
    if status not in _HTTP_STATUS:
        return FailureClassification(FailureClass.TRANSPORT, "default")
    if isinstance(www_authenticate, str):
        match = re.search(r'\berror="([a-z_]+)"', www_authenticate[:MAX_TEXT_SCANNED])
        if match and match.group(1) in _OAUTH_CODES:
            return FailureClassification(_OAUTH_CODES[match.group(1)], "http:www-authenticate")
    return FailureClassification(_HTTP_STATUS[status], "http:status")

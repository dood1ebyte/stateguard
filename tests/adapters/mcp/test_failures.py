"""
Auth-failure classification (T11): the rules, their precedence, and the
words that must not trigger them.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stateguard.adapters.mcp import (
    Effect,
    EffectDeclaration,
    FailureClass,
    Observed,
    classify_http_status,
    classify_response,
    decide_retry,
)
from stateguard.adapters.mcp.failures import MAX_TEXT_SCANNED

AUTHN, AUTHZ = FailureClass.AUTHENTICATION, FailureClass.AUTHORIZATION


def rpc(message: Any = "Request failed", *, code: int = -32603, data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": 1, "error": error}


def tool(*texts: str, is_error: bool = True) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {
            "content": [{"type": "text", "text": t} for t in texts],
            "isError": is_error,
        },
    }


# ---------------------------------------------------------------------------
# Structured signals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("data", "expected", "signal"),
    [
        ({"status": 401}, AUTHN, "data:status"),
        ({"statusCode": 403}, AUTHZ, "data:statusCode"),
        ({"status_code": 401}, AUTHN, "data:status_code"),
        ({"httpStatus": 403}, AUTHZ, "data:httpStatus"),
        ({"http_status": 401}, AUTHN, "data:http_status"),
        ({"error": "invalid_token"}, AUTHN, "data:error"),
        ({"error": "invalid_client"}, AUTHN, "data:error"),
        ({"error": "invalid_grant"}, AUTHN, "data:error"),
        ({"error_code": "insufficient_scope"}, AUTHZ, "data:error_code"),
        ({"errorCode": "access_denied"}, AUTHZ, "data:errorCode"),
        ({"error": "unauthorized_client"}, AUTHZ, "data:error"),
        ({"status": 401, "error": "invalid_token"}, AUTHN, "data:status"),
        ({"status": 403, "error": "insufficient_scope"}, AUTHZ, "data:status"),
    ],
)
def test_structured_error_data(data: dict[str, Any], expected: FailureClass, signal: str) -> None:
    result = classify_response(rpc(data=data))
    assert (result.failure, result.signal) == (expected, signal)
    assert result.observed is Observed.AUTH_ERROR


def test_conflicting_structured_signals_are_authentication() -> None:
    result = classify_response(rpc(data={"status": 403, "error": "invalid_token"}))
    assert result.failure is AUTHN and result.signal.endswith("+conflicting")


@pytest.mark.parametrize(
    "data",
    [
        {"status": "401"},  # a string, not a status
        {"status": True},
        {"status": 400},
        {"status": 404},
        {"status": 429},
        {"status": 500},
        {"error": "INVALID_TOKEN"},  # codes are exact
        {"error": "invalid_request"},
        {"error": "server_error"},
        {"token": "invalid"},
        {"reason": "unauthorized"},
        ["status", 401],
        "401",
    ],
)
def test_structured_non_signals(data: Any) -> None:
    assert classify_response(rpc(data=data)).failure is FailureClass.PROTOCOL


@pytest.mark.parametrize("code", [-32000, -32001, -32002, -32600, -32601, -32602, -32603, 401, 403])
def test_json_rpc_codes_alone_never_mean_auth(code: int) -> None:
    # MCP has no auth error code; -32001 is the SDK's request timeout.
    assert classify_response(rpc("Request failed", code=code)).failure is FailureClass.PROTOCOL


def test_tool_structured_content_is_never_read() -> None:
    message = tool("fetched", is_error=True)
    message["result"]["structuredContent"] = {"status": 401, "error": "invalid_token"}
    assert classify_response(message).failure is FailureClass.TOOL


# ---------------------------------------------------------------------------
# Execution state wins
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("state", "expected"),
    [("unknown", FailureClass.UNKNOWN), ("not_sent", FailureClass.TRANSPORT)],
)
def test_execution_state_is_never_reclassified(state: str, expected: FailureClass) -> None:
    for message in ("401 Unauthorized", "Forbidden", 'error="invalid_token"'):
        data = {"io.stateguard/executionState": state, "status": 401, "error": "invalid_token"}
        result = classify_response(rpc(message, code=-32000, data=data))
        assert result.failure is expected and not result.is_auth
        assert result.observed is Observed.PROTOCOL_ERROR  # delivered as-is, never retried here


# ---------------------------------------------------------------------------
# Text fallback
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected", "signal"),
    [
        ("401 Unauthorized", AUTHN, "text:status_line"),
        ("GET https://api.example.com/v1/items: 401 Unauthorized", AUTHN, "text:status_line"),
        ("Client error '401 Unauthorized' for url 'https://x'", AUTHN, "text:status_line"),
        ("upstream said 403 Forbidden.", AUTHZ, "text:status_line"),
        ("403 Forbidden for url https://x", AUTHZ, "text:status_line"),
        ("HTTP 401", AUTHN, "text:http_status"),
        ("HTTP/1.1 403", AUTHZ, "text:http_status"),
        ("HTTP Error 401: Unauthorized", AUTHN, "text:http_status"),
        ("HTTP Error 403: Forbidden", AUTHZ, "text:http_status"),
        ("401 Client Error: Unauthorized for url: https://x", AUTHN, "text:client_error"),
        ("403 Client Error: Forbidden for url: https://x", AUTHZ, "text:client_error"),
        ('Bearer error="invalid_token", error_description="expired"', AUTHN, "text:bearer_error"),
        ('Bearer realm="x", error="insufficient_scope"', AUTHZ, "text:bearer_error"),
        ("Unauthorized", AUTHN, "text:reason_phrase"),
        ("Unauthorized: token expired", AUTHN, "text:reason_phrase"),
        ("unauthenticated.", AUTHN, "text:reason_phrase"),
        ("Forbidden", AUTHZ, "text:reason_phrase"),
        ("  FORBIDDEN - scope missing", AUTHZ, "text:reason_phrase"),
    ],
)
def test_text_fallback_positive(text: str, expected: FailureClass, signal: str) -> None:
    for message in (rpc(text), tool(text)):
        result = classify_response(message)
        assert result.failure is expected, text
        assert result.signal.split("+")[0] == signal or result.signal.endswith("+conflicting")


#: Auth-like words in ordinary failures. None of these is an auth failure.
FALSE_POSITIVES = [
    "Permission denied: field 'token' needs access_level=admin",
    "Order rejected: the card's authorization hold expired",
    "Validation failed: access must be one of [read, write]",
    "403 Forbidden City tours are sold out",
    "401 Unauthorized Biography is out of print",
    "auth gateway connection reset by peer",
    "Invalid authorization header format in payload field 'note'",
    "The token budget (401) was exceeded",
    "Room 403 is forbidden to guests",
    "user lacks permission to edit this draft",
    "Access denied to the archive: file locked by another process",
    "credential rotation scheduled for 04:01",
    "Expected field 'auth_mode' to be one of [basic, oauth]",
    "Authentication service documentation: see /docs/auth",
    "Unauthorized access attempts are logged",
    "Forbidden words list updated",
    "status 401 returned by health check of replica 3 (ignored)",
    "error: invalid_token_format in CSV row 4",
    "HTTP 4010 errors are not supported",
    "retry after 401 seconds",
    "tokens: 403",
]


@pytest.mark.parametrize("text", FALSE_POSITIVES)
def test_text_false_positives_are_not_auth(text: str) -> None:
    assert classify_response(rpc(text)).failure is FailureClass.PROTOCOL, text
    assert classify_response(tool(text)).failure is FailureClass.TOOL, text


def test_only_error_text_is_read() -> None:
    # A successful result that mentions 401 Unauthorized is not a failure.
    assert classify_response(tool("401 Unauthorized", is_error=False)).failure is (
        FailureClass.NONE
    )
    non_text = tool()
    non_text["result"]["content"] = [{"type": "image", "data": "401 Unauthorized"}]
    assert classify_response(non_text).failure is FailureClass.TOOL


def test_text_beyond_the_scan_limit_is_not_read() -> None:
    assert classify_response(tool("x" * MAX_TEXT_SCANNED + " 401 Unauthorized")).failure is (
        FailureClass.TOOL
    )


def test_both_kinds_in_text_is_authentication() -> None:
    result = classify_response(tool("401 Unauthorized", "403 Forbidden"))
    assert result.failure is AUTHN and result.signal.endswith("+conflicting")


# ---------------------------------------------------------------------------
# HTTP transport metadata
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "header", "expected", "signal"),
    [
        (401, None, AUTHN, "http:status"),
        (403, None, AUTHZ, "http:status"),
        (401, 'Bearer error="invalid_token"', AUTHN, "http:www-authenticate"),
        (
            403,
            'Bearer error="insufficient_scope", scope="tools:write"',
            AUTHZ,
            "http:www-authenticate",
        ),
        (401, 'Bearer error="insufficient_scope"', AUTHZ, "http:www-authenticate"),
        (401, 'Bearer realm="mcp"', AUTHN, "http:status"),
        (403, 'Bearer error="made_up"', AUTHZ, "http:status"),
        (400, 'Bearer error="invalid_token"', FailureClass.TRANSPORT, "default"),
        (500, None, FailureClass.TRANSPORT, "default"),
        (None, None, FailureClass.TRANSPORT, "default"),
    ],
)
def test_http_status(
    status: int | None, header: str | None, expected: FailureClass, signal: str
) -> None:
    result = classify_http_status(status, header)
    assert (result.failure, result.signal) == (expected, signal)


# ---------------------------------------------------------------------------
# Shapes and robustness
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        (None, FailureClass.PROTOCOL),
        ("401 Unauthorized", FailureClass.PROTOCOL),
        ({"jsonrpc": "2.0", "id": 1}, FailureClass.PROTOCOL),
        ({"jsonrpc": "2.0", "id": 1, "error": "401 Unauthorized"}, FailureClass.PROTOCOL),
        ({"jsonrpc": "2.0", "id": 1, "result": None}, FailureClass.PROTOCOL),
        ({"jsonrpc": "2.0", "id": 1, "result": {"content": []}}, FailureClass.NONE),
        ({"jsonrpc": "2.0", "id": 1, "result": {"isError": True}}, FailureClass.TOOL),
        (rpc(message=None), FailureClass.PROTOCOL),
    ],
)
def test_shapes(message: Any, expected: FailureClass) -> None:
    assert classify_response(message).failure is expected


_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=20)),
    lambda inner: st.one_of(
        st.lists(inner, max_size=4), st.dictionaries(st.text(max_size=10), inner, max_size=4)
    ),
    max_leaves=20,
)


@settings(max_examples=400, deadline=None)
@given(message=_json)
def test_property_never_raises_and_is_deterministic(message: Any) -> None:
    first = classify_response(message)
    assert classify_response(message) == first
    assert first.signal  # always says which rule decided


_DISTRACTORS = [
    "auth",
    "authorization",
    "authentication",
    "token",
    "access",
    "permission",
    "denied",
    "credential",
    "login",
    "password",
    "scope",
    "401",
    "403",
    "unauthorized",
    "forbidden",
    "expired",
    "invalid",
    "user",
    "field",
    "the",
    "of",
    "error:",
    "failed",
]


@settings(max_examples=600, deadline=None)
@given(words=st.lists(st.sampled_from(_DISTRACTORS), min_size=2, max_size=8))
def test_property_auth_words_without_a_signal_shape_never_classify(words: list[str]) -> None:
    """Sentences made of auth-ish words are auth only when they form one of the
    documented shapes -- checked independently of the classifier's regexes."""
    text = "Request " + " ".join(words)  # never *is* the bare reason phrase
    joined = " ".join(words).lower()
    shaped = "401 unauthorized" in joined or "403 forbidden" in joined
    result = classify_response(tool(text))
    if not shaped:
        assert result.failure is FailureClass.TOOL, text


# ---------------------------------------------------------------------------
# Retry: an auth failure is never retried, whatever the operator declared
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("message", "effect", "attempts", "connected"),
    list(
        itertools.product(
            [rpc(data={"status": 401}), rpc("Forbidden"), tool("HTTP Error 403: Forbidden")],
            list(Effect),
            [0, 1, 5],
            [True, False],
        )
    ),
)
def test_auth_is_never_retried(
    message: dict[str, Any], effect: Effect, attempts: int, connected: bool
) -> None:
    observed = classify_response(message).observed
    assert observed is Observed.AUTH_ERROR
    verdict = decide_retry(
        observed,
        EffectDeclaration(effect, declared=True),
        attempts_made=attempts,
        connection_available=connected,
    )
    assert not verdict.retry


def test_non_auth_tool_errors_keep_the_existing_retry_policy() -> None:
    observed = classify_response(tool(FALSE_POSITIVES[0])).observed
    assert observed is Observed.TOOL_ERROR
    declared = EffectDeclaration(Effect.READ_ONLY, declared=True)
    assert decide_retry(observed, declared, attempts_made=1).retry  # as before T11
    assert not decide_retry(
        observed, EffectDeclaration(Effect.UNKNOWN, declared=False), attempts_made=1
    ).retry

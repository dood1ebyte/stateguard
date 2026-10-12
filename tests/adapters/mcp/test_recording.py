"""
Recording format, redaction and parser (T13).

What must hold, whatever the input:

* no credential reaches the sink -- under any key spelling, at any depth,
  in ``_meta``, in error messages, in audit text, in labels, in tool
  listings -- and in ``mask`` mode no free-form text does either;
* redaction is deterministic and keeps JSON structure;
* a recorder never raises into its caller, and a recorder that failed
  leaves a recording that says it is incomplete;
* the parser accepts its own output and refuses everything else with
  ``RecordingError`` -- never another exception, never a value in the message.
"""

from __future__ import annotations

import contextlib
import io
import json
import string
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from stateguard.adapters.mcp.failures import AUTH_CODES, signal_spans
from stateguard.adapters.mcp.recording import (
    FORMAT,
    MAX_DEPTH,
    REDACTED,
    Direction,
    MalformedRecordingError,
    Provenance,
    Recorder,
    RecordingError,
    RedactionMode,
    RedactionPolicy,
    Redactor,
    UnsupportedRecordingVersionError,
    mask_string,
    parse_recording,
    read_recording,
    secret_pattern,
    sensitive_key,
)

SECRETS = {
    "bearer": "Bearer abcDEF123456ghiJKL",
    "basic": "Basic dXNlcjpwYXNzd29yZA==",
    "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N",
    "aws": "AKIAIOSFODNN7EXAMPLE",
    "github": "ghp_0123456789abcdefghijklmnopqrstuvwxyz",
    "github_pat": "github_pat_11ABCDEFG0123456789_abcdefghijklmnop",
    "slack": "xoxb-1234567890-abcdefghij",
    # Assembled at runtime: no credential-shaped literal in the source.
    "stripe": "_".join(("sk", "live", "0FAKE0FIXTURE0not0a0key")),
    "sk": "sk-ant-api03-abcdefghijklmnopqrstuv",
    "google": "AIzaSyA-1234567890abcdefghijklmnopqrstu",
    "pem": "-----BEGIN RSA PRIVATE KEY-----\nMIIEow...",
    "url": "postgres://admin:s3cr3tpw@db.internal:5432/app",
    "query": "https://api.example.com/v1?user=1&access_token=zzTOPzz",
    "assignment": "connect failed: password=hunter2xyz",
}


def _recorder(**kwargs: Any) -> tuple[Recorder, io.StringIO]:
    sink = io.StringIO()
    kwargs.setdefault("provenance", Provenance.SYNTHETIC)
    kwargs.setdefault("session", "test-session")
    return Recorder(sink, **kwargs), sink


def _line(message: Any) -> bytes:
    return json.dumps(message).encode()


LISTING = {
    "jsonrpc": "2.0",
    "id": "stateguard-relay:1",
    "result": {
        "resultType": "complete",
        "tools": [
            {
                "name": "list_issues",
                "description": "List issues for Acme Corp",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "state": {"type": "string", "enum": ["open", "closed", "all"]},
                        "limit": {"type": "integer"},
                        "include": {"type": "boolean"},
                        "q": {"type": "string", "description": "free text"},
                        "password": {"type": "string", "default": "letmein-default"},
                    },
                },
            }
        ],
    },
}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "authorization",
        "Proxy-Authorization",
        "x-api-key",
        "X-Goog-Api-Key",
        "api_key",
        "apiKey",
        "API-KEY",
        "access_token",
        "refreshToken",
        "id_token",
        "client_secret",
        "secretValue",
        "password",
        "PASSWD",
        "passphrase",
        "pwd",
        "cookie",
        "Set-Cookie",
        "session_id",
        "credentials",
        "private_key",
        "signing_key",
        "auth",
        "authHeader",
        "auth_code",
        "bearer",
        "signature",
        "key",
        "pin",
        "otp",
        "credit_card",
        "card_number",
        "ssn",
        "connection_string",
    ],
)
def test_sensitive_key_names(key: str) -> None:
    assert sensitive_key(key)


@pytest.mark.parametrize(
    "key",
    ["author", "authority", "state", "limit", "query", "name", "path", "monkey_count", "keyword"],
)
def test_ordinary_key_names(key: str) -> None:
    # "monkey"/"keyword" contain "key" but are not the exact name "key".
    assert not sensitive_key(key)


@pytest.mark.parametrize("name,value", sorted(SECRETS.items()))
def test_value_patterns(name: str, value: str) -> None:
    assert secret_pattern(value) is not None, name
    assert secret_pattern(f"prefix {value} suffix") is not None, name


@pytest.mark.parametrize(
    "text", ["open", "hello world", "2024-01-05", "user@example.com", "sk-short", "token"]
)
def test_ordinary_values_match_no_pattern(text: str) -> None:
    assert secret_pattern(text) is None


def test_mask_preserves_shape() -> None:
    assert mask_string("Hello, World 2024!") == "Aaaaa, Aaaaa 1111!"
    assert mask_string("2024-01-05") == "1111-11-11"
    assert mask_string("ünïcödé") == "aaaaaaa"
    assert len(mask_string("x" * 50)) == 50


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def _recorded(*messages: tuple[str, Any], **kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
    recorder, sink = _recorder(**kwargs)
    for direction, message in messages:
        recorder.message(direction, message if isinstance(message, bytes) else _line(message))
    recorder.close()
    text = sink.getvalue()
    return text, [json.loads(line) for line in text.splitlines()]


def test_no_credentials_persisted_anywhere_in_a_call() -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 7,
        "method": "tools/call",
        "params": {
            "name": "list_issues",
            "arguments": {
                "state": "open",
                "q": "bug reported by alice@example.com",
                "api_key": "plainvalue-123",
                "headers": {"Authorization": "opaque-value-9", "X-Trace": SECRETS["bearer"]},
                "deep": [[{"x": [{"client_secret": {"nested": ["s1-value", 42, True]}}]}]],
                "url": SECRETS["url"],
            },
            "_meta": {
                "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                "authToken": "meta-token-value",
                "io.example/session": SECRETS["jwt"],
                "trace": "trace-abc-123",
            },
        },
    }
    text, records = _recorded(
        (Direction.SERVER_TO_RELAY, LISTING), (Direction.CLIENT_TO_RELAY, call)
    )
    for needle in [
        "alice",
        "plainvalue",
        "opaque-value",
        "abcDEF",
        "s1-value",
        "s3cr3tpw",
        "meta-token-value",
        "eyJhbGci",
        "trace-abc",
        "Acme",
        "letmein-default",
    ]:
        assert needle not in text, needle
    message = records[2]["message"]
    arguments = message["params"]["arguments"]
    assert arguments["state"] == "open"  # the schema's enum vouches for it
    assert arguments["api_key"] == REDACTED
    assert arguments["deep"][0][0]["x"][0]["client_secret"] == {"nested": [REDACTED, 0, False]}
    meta = message["params"]["_meta"]
    assert meta["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert meta["authToken"] == REDACTED
    assert message["params"]["name"] == "list_issues"
    assert message["method"] == "tools/call" and message["id"] == 7


def test_schema_vouches_only_for_its_own_values() -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "list_issues",
            "arguments": {
                "state": "Opne",  # within two edits of an enum member: kept (drift)
                "limit": "25",  # numeric string in an integer field: kept
                "include": "yes",  # boolean word in a boolean field: kept
                "q": "open",  # free text that happens to be an enum word: masked
                "undeclared": "closed",
            },
        },
    }
    _, records = _recorded((Direction.SERVER_TO_RELAY, LISTING), (Direction.CLIENT_TO_RELAY, call))
    arguments = records[2]["message"]["params"]["arguments"]
    assert arguments == {
        "state": "Opne",
        "limit": "25",
        "include": "yes",
        "q": "aaaa",
        "undeclared": "aaaaaa",
    }


def test_numbers_large_ones_reduced() -> None:
    redactor = Redactor()
    assert redactor.value({"n": 42, "big": 4111111111111111, "neg": -12345678, "f": 21.5}) == {
        "n": 42,
        "big": 1000000000000000,
        "neg": -10000000,
        "f": 21.5,
    }
    assert redactor.value(float("nan")) == 0.0


def test_keys_that_are_data_are_masked_without_collisions() -> None:
    redactor = Redactor()
    out = redactor.value({"alice@example.com": "admin", "bob@example.com": "user", "ok_key": 1})
    assert "alice@example.com" not in json.dumps(out)
    assert len(out) == 3  # masked keys collide ("aaaaa@aaaaaaa.aaa"); none overwritten
    assert out["ok_key"] == 1


def test_secret_shaped_ids_and_keys_are_aliased_consistently() -> None:
    redactor = Redactor()
    first = redactor.request_id(SECRETS["github"])
    assert first != SECRETS["github"] and first == redactor.request_id(SECRETS["github"])
    assert redactor.request_id(5) == 5 and redactor.request_id("req-1") == "req-1"
    out = redactor.value({SECRETS["aws"]: 1})
    assert SECRETS["aws"] not in json.dumps(out)


def test_errors_and_results_redacted() -> None:
    error = {
        "jsonrpc": "2.0",
        "id": 3,
        "error": {
            "code": -32000,
            "message": f"upstream said {SECRETS['assignment']}",
            "data": {"io.stateguard/executionState": "unknown", "detail": "user bob"},
        },
    }
    result = {
        "jsonrpc": "2.0",
        "id": 4,
        "result": {
            "resultType": "complete",
            "isError": False,
            "content": [{"type": "text", "text": "Customer Jane Doe owes 1200"}],
            "structuredContent": {"name": "Jane", "balance": 1200, "token": "tok-123"},
        },
    }
    text, records = _recorded(
        (Direction.SERVER_TO_RELAY, error), (Direction.SERVER_TO_RELAY, result)
    )
    assert "hunter2" not in text and "Jane" not in text and "bob" not in text
    assert "tok-123" not in text
    err = records[1]["message"]["error"]
    assert err["code"] == -32000
    assert err["data"]["io.stateguard/executionState"] == "unknown"
    res = records[2]["message"]["result"]
    assert res["content"][0]["type"] == "text"
    assert res["isError"] is False and res["resultType"] == "complete"
    assert res["structuredContent"]["balance"] == 1200


def test_listing_keeps_schema_masks_prose() -> None:
    _, records = _recorded((Direction.SERVER_TO_RELAY, LISTING))
    tool = records[1]["message"]["result"]["tools"][0]
    assert tool["name"] == "list_issues"
    schema = tool["inputSchema"]
    assert schema["properties"]["state"]["enum"] == ["open", "closed", "all"]
    assert schema["properties"]["q"]["description"] == "aaaa aaaa"
    assert schema["properties"]["password"]["default"] == REDACTED
    assert tool["description"] == "Aaaa aaaaaa aaa Aaaa Aaaa"


def test_secrets_only_mode_keeps_free_text_drops_credentials() -> None:
    call = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {
            "name": "x",
            "arguments": {"q": "hello world", "token": "abc", "note": SECRETS["stripe"]},
        },
    }
    text, records = _recorded(
        (Direction.CLIENT_TO_RELAY, call),
        policy=RedactionPolicy(mode=RedactionMode.SECRETS_ONLY),
    )
    arguments = records[1]["message"]["params"]["arguments"]
    assert arguments == {"q": "hello world", "token": REDACTED, "note": REDACTED}
    assert records[0]["redaction"]["mode"] == "secrets_only"
    assert "sk_live" not in text


def test_extra_sensitive_keys_are_configurable() -> None:
    redactor = Redactor(RedactionPolicy(extra_sensitive_keys=frozenset({"Account-Number"})))
    assert redactor.value({"account_number": "12345"}) == {"account_number": REDACTED}


def test_unparsed_lines_are_not_persisted() -> None:
    text, records = _recorded((Direction.SERVER_TO_RELAY, b'{"secret": "' + b"z" * 10))
    assert records[1]["kind"] == "unparsed" and "message" not in records[1]
    assert "zzzz" not in text


def test_depth_beyond_limit_is_replaced_not_walked() -> None:
    deep: Any = "leaf-value"
    for _ in range(MAX_DEPTH + 5):
        deep = {"k": deep}
    out = Redactor(RedactionPolicy(mode=RedactionMode.SECRETS_ONLY)).value(deep)
    assert "leaf-value" not in json.dumps(out)


def test_audit_events_keep_decisions_mask_text() -> None:
    recorder, sink = _recorder()
    recorder.audit(
        {
            "event": "call",
            "id": 9,
            "tool": "list_issues",
            "action": "forward",
            "auto_action": "forward",
            "reason": "repaired 'opne' for customer Jane",
            "decisions": [{"path": "state", "decision": "apply", "op": "fuzzy", "trust": 0.91}],
        }
    )
    recorder.audit({"event": "internal_error", "category": "Z", "error": SECRETS["assignment"]})
    recorder.close()
    text = sink.getvalue()
    assert "Jane" not in text and "hunter2" not in text
    event = json.loads(text.splitlines()[1])["event"]
    assert event["action"] == "forward" and event["decisions"][0]["trust"] == 0.91
    assert event["decisions"][0]["path"] == "state"


def test_label_redacted_like_the_request() -> None:
    recorder, sink = _recorder()
    recorder.message(Direction.SERVER_TO_RELAY, _line(LISTING))
    args = {"state": "open", "q": "Jane's bug"}
    recorder.message(
        Direction.CLIENT_TO_RELAY,
        _line(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "list_issues", "arguments": args},
            }
        ),
    )
    recorder.label(1, tool="list_issues", intended=args, category="A")
    recorder.close()
    records = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert records[3]["intended"] == records[2]["message"]["params"]["arguments"]
    assert "Jane" not in sink.getvalue()


def test_redactors_do_not_share_state_across_recordings() -> None:
    first, _ = _recorder()
    first.message(Direction.SERVER_TO_RELAY, _line(LISTING))
    second = Redactor()
    # The second recording never saw the listing: nothing vouches for "open".
    assert second.arguments_for("list_issues", {"state": "open"}) == {"state": "aaaa"}
    assert first.redactor.arguments_for("list_issues", {"state": "open"}) == {"state": "open"}


def test_message_records_index_fields() -> None:
    call = {
        "jsonrpc": "2.0",
        "id": "c-1",
        "method": "tools/call",
        "params": {"name": "list_issues", "arguments": {}},
    }
    response = {"jsonrpc": "2.0", "id": "c-1", "result": {"content": []}}
    notification = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    _, records = _recorded(
        (Direction.CLIENT_TO_RELAY, call),
        (Direction.SERVER_TO_RELAY, response),
        (Direction.CLIENT_TO_RELAY, notification),
        (Direction.CLIENT_TO_RELAY, [1, 2]),
    )
    assert [r.get("kind") for r in records[1:5]] == ["request", "response", "notification", "other"]
    assert records[2]["method"] == "tools/call" and records[2]["tool"] == "list_issues"
    assert [r["seq"] for r in records] == list(range(len(records)))


def test_timing_only_in_diag_and_only_when_asked() -> None:
    _, deterministic = _recorded((Direction.CLIENT_TO_RELAY, {"jsonrpc": "2.0", "method": "x"}))
    assert "t_ms" not in deterministic[1]["diag"]
    clock = iter([0.0, 0.25])
    recorder, sink = _recorder(deterministic=False, clock=lambda: next(clock))
    recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"x"}')
    record = json.loads(sink.getvalue().splitlines()[1])
    assert record["diag"]["t_ms"] == 250.0


# ---------------------------------------------------------------------------
# Recorder failures
# ---------------------------------------------------------------------------


class _BrokenSink(io.StringIO):
    def __init__(self, fail_after: int) -> None:
        super().__init__()
        self.writes = 0
        self.fail_after = fail_after

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > self.fail_after:
            raise OSError("disk full: /secret/path/hunter2")
        return super().write(text)


def test_a_failing_sink_never_raises_and_marks_the_recording_incomplete() -> None:
    sink = _BrokenSink(fail_after=2)
    recorder = Recorder(sink, provenance=Provenance.SYNTHETIC, session="s")
    for _ in range(5):
        recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"x"}')
    recorder.audit({"event": "x"})
    recorder.close()
    assert recorder.stopped and recorder.failures == 1
    assert recorder.failure_types == ["OSError"]  # the type, never the message
    recording = parse_recording(sink.getvalue().splitlines())
    assert not recording.complete and recording.end is None


def test_a_failing_sink_at_construction_never_raises() -> None:
    recorder = Recorder(_BrokenSink(fail_after=0), provenance=Provenance.SYNTHETIC, session="s")
    recorder.message(Direction.CLIENT_TO_RELAY, b"{}")
    recorder.close()
    assert recorder.stopped


def test_a_redaction_failure_stops_recording_and_says_so(monkeypatch: pytest.MonkeyPatch) -> None:
    recorder, sink = _recorder()
    recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"a"}')

    def explode(_message: Any) -> Any:
        raise RuntimeError("value hunter2 leaked?")

    monkeypatch.setattr(recorder.redactor, "message", explode)
    recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"b"}')
    recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"c"}')
    recorder.close()
    assert "hunter2" not in sink.getvalue()
    recording = parse_recording(sink.getvalue().splitlines())
    assert not recording.complete
    assert recording.end is not None and recording.end["failure_types"] == ["RuntimeError"]
    assert [r["message"]["method"] for r in recording.messages()] == ["a"]


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _valid_lines() -> list[str]:
    recorder, sink = _recorder(provenance=Provenance.FAULT_INJECTION)
    recorder.message(Direction.SERVER_TO_RELAY, _line(LISTING))
    recorder.message(
        Direction.CLIENT_TO_RELAY,
        _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}}),
    )
    recorder.audit({"event": "call", "id": 1, "action": "forward"})
    recorder.fault("drop_response", id=1)
    recorder.label(1, tool="x", intended={})
    recorder.close()
    return sink.getvalue().splitlines()


def test_round_trip() -> None:
    recording = parse_recording(_valid_lines())
    assert recording.complete and recording.provenance is Provenance.FAULT_INJECTION
    assert [r["type"] for r in recording.records] == [
        "message",
        "message",
        "audit",
        "fault",
        "label",
    ]
    assert len(recording.messages(Direction.CLIENT_TO_RELAY)) == 1


def test_read_recording_from_file(tmp_path: Any) -> None:
    path = tmp_path / "tape.jsonl"
    path.write_text("\n".join(_valid_lines()) + "\n")
    assert read_recording(path).complete
    with pytest.raises(RecordingError):
        read_recording(tmp_path / "missing.jsonl")


def _mutate(index: int, change: Any) -> list[str]:
    lines = _valid_lines()
    record = json.loads(lines[index])
    change(record)
    lines[index] = json.dumps(record)
    return lines


@pytest.mark.parametrize(
    "lines",
    [
        [],
        ["not json"],
        ["[1, 2]"],
        [json.dumps({"type": "message"})],
        _mutate(0, lambda r: r.update(format="something.else")),
        _mutate(0, lambda r: r.update(version="1")),
        _mutate(0, lambda r: r.update(provenance="production")),
        _mutate(0, lambda r: r.pop("session")),
        _mutate(0, lambda r: r.update(redaction={"mode": "none"})),
        _mutate(1, lambda r: r.update(dir="sideways")),
        _mutate(1, lambda r: r.update(gen=-1)),
        _mutate(1, lambda r: r.update(kind="mystery")),
        _mutate(1, lambda r: r.update(id=1.5)),
        _mutate(1, lambda r: r.pop("message")),
        _mutate(1, lambda r: r.update(message=[1])),
        _mutate(2, lambda r: r.update(seq=1)),
        _mutate(3, lambda r: r.update(event={"no": "name"})),
        _mutate(4, lambda r: r.pop("fault")),
        _mutate(5, lambda r: r.pop("id")),
        _mutate(6, lambda r: r.update(complete="yes")),
        _mutate(2, lambda r: r.update(type="header")),
        _mutate(2, lambda r: r.update(type="surprise")),
        _mutate(2, lambda r: r.update(diag=[1])),
        [*_valid_lines(), json.dumps({"type": "audit", "seq": 99, "event": {"event": "x"}})],
        [_valid_lines()[0], '{"type": "audit", "seq": 1, "event": {"event": NaN}}'],
    ],
)
def test_malformed_recordings_refused(lines: list[str]) -> None:
    with pytest.raises(MalformedRecordingError):
        parse_recording(lines)


def test_unsupported_version_refused() -> None:
    with pytest.raises(UnsupportedRecordingVersionError) as raised:
        parse_recording(_mutate(0, lambda r: r.update(version=2)))
    assert "version 2" in str(raised.value)


def test_errors_never_echo_content() -> None:
    lines = _valid_lines()
    lines[2] = '{"type": "message", "seq": 2, "secret": "hunter2-leak"'
    with pytest.raises(RecordingError) as raised:
        parse_recording(lines)
    assert "hunter2" not in str(raised.value)
    assert str(raised.value).startswith("line 3:")


def test_bytes_lines_and_limits() -> None:
    lines = [line.encode() for line in _valid_lines()]
    assert parse_recording(lines).complete
    with pytest.raises(MalformedRecordingError):
        parse_recording([lines[0], b"\xff\xfe"])
    with pytest.raises(MalformedRecordingError):
        parse_recording([lines[0], b"x" * (8 * 1024 * 1024 + 1)])


def test_a_recording_without_end_is_incomplete() -> None:
    recording = parse_recording(_valid_lines()[:-1])
    assert not recording.complete and recording.end is None


def test_header_names_the_format() -> None:
    header = json.loads(_valid_lines()[0])
    assert header["format"] == FORMAT and header["version"] == 1 and header["seq"] == 0


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

_scalars = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**12), max_value=10**12),
    st.floats(allow_nan=False, allow_infinity=False),
    st.text(max_size=12),
)
_json = st.recursive(
    _scalars,
    lambda inner: st.one_of(
        st.lists(inner, max_size=4),
        st.dictionaries(st.text(max_size=8), inner, max_size=4),
    ),
    max_leaves=25,
)
_sensitive_names = st.sampled_from(
    ["password", "Authorization", "x-api-key", "client_secret", "refresh_token", "Cookie", "auth"]
)
_MARK = "QZXMARK"
_secret_values = st.text(alphabet=string.ascii_letters + string.digits, min_size=4, max_size=16)


def _plant(tree: Any, key: str, secret: str, path: list[int]) -> Any:
    """Put ``{key: secret}`` somewhere inside *tree*, steered by *path*."""
    if isinstance(tree, dict) and tree and path:
        name = sorted(tree)[path[0] % len(tree)]
        return {**tree, name: _plant(tree[name], key, secret, path[1:])}
    if isinstance(tree, list) and tree and path:
        i = path[0] % len(tree)
        return [*tree[:i], _plant(tree[i], key, secret, path[1:]), *tree[i + 1 :]]
    return {"wrapper": tree, key: secret}


def _shape(value: Any) -> Any:
    if isinstance(value, dict):
        return ("dict", len(value))
    if isinstance(value, list):
        return ("list", [_shape(v) for v in value])
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, (int, float)):
        return "number"
    return type(value).__name__


@settings(max_examples=400, deadline=None)
@given(
    tree=_json,
    key=_sensitive_names,
    secret=_secret_values,
    path=st.lists(st.integers(min_value=0, max_value=10), max_size=6),
    mode=st.sampled_from(list(RedactionMode)),
)
def test_property_a_value_under_a_credential_key_never_persists(
    tree: Any, key: str, secret: str, path: list[int], mode: RedactionMode
) -> None:
    secret = _MARK + secret
    planted = _plant(tree, key, secret, path)
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "t", "arguments": planted, "_meta": {key: secret}},
    }
    recorder, sink = _recorder(policy=RedactionPolicy(mode=mode))
    recorder.message(Direction.CLIENT_TO_RELAY, _line(message))
    recorder.audit({"event": "call", "detail": planted})
    recorder.label(1, tool="t", intended=planted)
    recorder.close()
    assert secret not in sink.getvalue()
    assert parse_recording(sink.getvalue().splitlines()).complete


@settings(max_examples=300, deadline=None)
@given(
    tree=_json,
    secret=st.sampled_from(sorted(SECRETS.values())),
    path=st.lists(st.integers(min_value=0, max_value=10), max_size=6),
)
def test_property_a_credential_shaped_value_never_persists_in_any_mode(
    tree: Any, secret: str, path: list[int]
) -> None:
    planted = _plant(tree, "innocent_field", secret, path)
    for mode in RedactionMode:
        out = json.dumps(Redactor(RedactionPolicy(mode=mode)).value(planted))
        assert secret not in out and json.dumps(secret)[1:-1] not in out


@settings(max_examples=300, deadline=None)
@given(tree=_json)
def test_property_redaction_is_deterministic_and_keeps_structure(tree: Any) -> None:
    first = Redactor().value(tree)
    assert first == Redactor().value(tree)
    json.dumps(first, allow_nan=False)  # always strict JSON
    assert _shape(first) == _shape(tree)


@settings(max_examples=300, deadline=None)
@given(tree=_json)
def test_property_mask_mode_persists_no_free_text(tree: Any) -> None:
    out = Redactor().value(tree)

    def strings(value: Any) -> list[str]:
        if isinstance(value, dict):
            return [s for v in value.values() for s in strings(v)]
        if isinstance(value, list):
            return [s for v in value for s in strings(v)]
        return [value] if isinstance(value, str) else []

    for text in strings(out):
        if text == REDACTED or text in AUTH_CODES:
            continue
        # Kept verbatim: only the auth classifier's fixed-vocabulary spans (T11).
        kept = {i for start, end in signal_spans(text) for i in range(start, end)}
        assert all(not ch.isalnum() or ch in "aA1" for i, ch in enumerate(text) if i not in kept)


@settings(max_examples=500, deadline=None)
@given(st.lists(st.one_of(st.text(max_size=60), st.binary(max_size=60)), max_size=8))
def test_property_parser_raises_only_recording_errors(lines: list[Any]) -> None:
    with contextlib.suppress(RecordingError):
        parse_recording(lines)


@settings(max_examples=400, deadline=None)
@given(
    drop=st.lists(st.integers(min_value=0, max_value=6), max_size=3),
    flips=st.lists(
        st.tuples(st.integers(min_value=0, max_value=6), st.integers(0, 400), st.characters()),
        max_size=4,
    ),
)
def test_property_parser_on_damaged_recordings(
    drop: list[int], flips: list[tuple[int, int, str]]
) -> None:
    lines = _valid_lines()
    for line_no, pos, ch in flips:
        line = lines[line_no % len(lines)]
        at = pos % (len(line) + 1)
        lines[line_no % len(lines)] = line[:at] + ch + line[at + 1 :]
    for index in sorted({d % len(lines) for d in drop}, reverse=True):
        del lines[index]
    try:
        recording = parse_recording(lines)
    except RecordingError:
        return
    assert recording.header["format"] == FORMAT


def test_json_sent_as_a_string_is_redacted_as_structure() -> None:
    redactor = Redactor()
    redactor.learn_tools(LISTING["result"]["tools"])
    out = redactor.arguments_for(
        "list_issues", '{"state": "Closed", "q": "Jane Doe", "token": "abc123"}'
    )
    assert isinstance(out, str)
    assert json.loads(out) == {"state": "Closed", "q": "Aaaa Aaa", "token": REDACTED}
    # A string that is itself a credential is never parsed, only replaced.
    assert redactor.value('{"a": "' + SECRETS["github"] + '"}') == REDACTED


@pytest.mark.parametrize(
    "key,kept",
    [
        ("channelId", True),
        ("per_page", True),
        ("v2_api", True),
        ("acct_12345", False),  # four digits: could be an opaque id
        ("a8f93c0d7e1b", False),
        ("x" * 41, False),
        ("user name", False),
    ],
)
def test_undeclared_keys_kept_only_when_word_like(key: str, kept: bool) -> None:
    out = Redactor().value({key: 1})
    assert (key in out) is kept


# ---------------------------------------------------------------------------
# Background recording
# ---------------------------------------------------------------------------


def _feed(recorder: Recorder) -> None:
    recorder.message(Direction.SERVER_TO_RELAY, _line(LISTING))
    for n in range(50):
        recorder.message(
            Direction.CLIENT_TO_RELAY,
            _line(
                {
                    "jsonrpc": "2.0",
                    "id": n,
                    "method": "tools/call",
                    "params": {"name": "list_issues", "arguments": {"state": "open", "q": f"n{n}"}},
                }
            ),
        )
        recorder.audit({"event": "call", "id": n, "action": "forward"})


def test_background_recording_equals_synchronous_recording() -> None:
    sync, sync_sink = _recorder()
    _feed(sync)
    sync.close()
    background, background_sink = _recorder(background=True)
    _feed(background)
    background.close()
    assert background.healthy
    assert background_sink.getvalue() == sync_sink.getvalue()


class _BlockingSink(io.StringIO):
    """A sink that blocks (a full pipe, a stalled disk) until released."""

    def __init__(self) -> None:
        super().__init__()
        self.release = __import__("threading").Event()
        self.writes = 0

    def write(self, text: str) -> int:
        self.writes += 1
        if self.writes > 1:  # the header goes through; then the sink stalls
            self.release.wait(10)
        return super().write(text)


def test_a_blocked_sink_never_blocks_the_host() -> None:
    import time

    sink = _BlockingSink()
    recorder = Recorder(
        sink,
        provenance=Provenance.SYNTHETIC,
        session="s",
        background=True,
        queue_size=8,
        close_timeout=0.2,
    )
    started = time.monotonic()
    for _ in range(100):
        recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"x"}')
    recorder.close()
    elapsed = time.monotonic() - started
    sink.release.set()
    assert elapsed < 2.0  # never waited for the sink beyond the close timeout
    assert recorder.stopped and "RecorderOverflowError" in recorder.failure_types
    assert "RecorderCloseTimeoutError" in recorder.failure_types


def test_background_failures_are_reported_and_never_raised() -> None:
    sink = _BrokenSink(fail_after=3)
    recorder = Recorder(sink, provenance=Provenance.SYNTHETIC, session="s", background=True)
    for _ in range(10):
        recorder.message(Direction.CLIENT_TO_RELAY, b'{"jsonrpc":"2.0","method":"x"}')
    recorder.close()
    assert recorder.stopped and recorder.failure_types == ["OSError"]
    assert not parse_recording(sink.getvalue().splitlines()).complete


# ---------------------------------------------------------------------------
# T11: a recording classifies as the live traffic did
# ---------------------------------------------------------------------------

_AUTH_TEXTS = [
    "Unauthorized: token expired",
    "GET https://api.example.com/v1/items: 401 Unauthorized",
    "HTTP Error 403: Forbidden",
    "401 Client Error: Unauthorized for url: https://x",
    'upstream refused: WWW-Authenticate: Bearer error="invalid_token"',
    "Forbidden",
]
_NOT_AUTH_TEXTS = [
    "Permission denied: field 'token' needs access_level=admin",
    "403 Forbidden City tours are sold out",
    "Order rejected: the card's authorization hold expired",
    "Request timed out",
    "retry after 401 seconds",
]


def _rpc(message: str, data: Any = None, code: int = -32603) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": 1, "error": error}


def _tool_error(text: str) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": text}], "isError": True},
    }


@pytest.mark.parametrize("mode", list(RedactionMode))
@pytest.mark.parametrize(
    "message",
    [
        *[_rpc(t) for t in _AUTH_TEXTS + _NOT_AUTH_TEXTS],
        *[_tool_error(t) for t in _AUTH_TEXTS + _NOT_AUTH_TEXTS],
        _rpc("Request failed", {"status": 401, "error": "invalid_token"}),
        _rpc("Request failed", {"status": 403, "error": "insufficient_scope"}),
        _rpc("Request failed", {"error": "access_denied"}),
        _rpc("401 Unauthorized", {"io.stateguard/executionState": "unknown"}, code=-32000),
    ],
)
def test_redaction_preserves_the_failure_class(
    message: dict[str, Any], mode: RedactionMode
) -> None:
    from stateguard.adapters.mcp import classify_response

    redacted = Redactor(RedactionPolicy(mode=mode)).message(message)
    assert classify_response(redacted) == classify_response(message)


def test_auth_signals_survive_masking_and_nothing_else_does() -> None:
    redacted = Redactor().message(
        _tool_error("GET https://api.acme.example/v1/u/jane: 401 Unauthorized")
    )
    text = redacted["result"]["content"][0]["text"]
    assert text.endswith("401 Unauthorized")
    assert "acme" not in text and "jane" not in text


@settings(max_examples=300, deadline=None)
@given(text=st.text(max_size=60))
def test_property_redaction_never_creates_or_destroys_an_auth_class(text: str) -> None:
    from stateguard.adapters.mcp import classify_response

    for message in (_rpc(text), _tool_error(text)):
        redacted = Redactor().message(message)
        if secret_pattern(text) is None:  # a secret-shaped string is replaced whole
            assert classify_response(redacted) == classify_response(message)

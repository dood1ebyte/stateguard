"""
Real-capture provenance and evidence (library level).

What must hold:

* the provenance classes stay apart: synthetic / fault_injection (generated,
  may carry ground truth), replay (a derivative), local_development /
  real_capture (observed, never carry ground truth);
* a real_capture recording is masked -- the recorder refuses any other
  redaction, and the parser refuses a file that claims otherwise;
* no fault or label record is ever written into, or accepted from, an
  observed recording;
* redaction of a real capture is exactly the redaction of any other masked
  recording: one path, same output;
* the evidence summary keeps observed failures, StateGuard's observations,
  its (hypothetical) proposals and unknown outcomes apart.
"""

from __future__ import annotations

import io
import json
import re
from typing import Any

import pytest

from stateguard.adapters.mcp import summarize_evidence
from stateguard.adapters.mcp.evidence import NOT_CLAIMED
from stateguard.adapters.mcp.recording import (
    REDACTED,
    Direction,
    MalformedRecordingError,
    Provenance,
    Recorder,
    RedactionMode,
    RedactionPolicy,
    Redactor,
    parse_recording,
    sensitive_key,
)
from tests.adapters.mcp.test_recording import LISTING, SECRETS

OBSERVED = (Provenance.LOCAL_DEVELOPMENT, Provenance.REAL_CAPTURE)
GENERATED = (Provenance.SYNTHETIC, Provenance.FAULT_INJECTION)


def _line(message: Any) -> bytes:
    return json.dumps(message).encode()


def _capture(**kwargs: Any) -> tuple[Recorder, io.StringIO]:
    sink = io.StringIO()
    kwargs.setdefault("provenance", Provenance.REAL_CAPTURE)
    kwargs.setdefault("session", "capture-test")
    return Recorder(sink, **kwargs), sink


# ---------------------------------------------------------------------------
# Provenance classes
# ---------------------------------------------------------------------------


def test_the_provenance_classes_are_distinct() -> None:
    assert {p.value for p in Provenance} == {
        "synthetic",
        "fault_injection",
        "replay",
        "local_development",
        "real_capture",
    }
    assert [p for p in Provenance if p.is_real_traffic] == [Provenance.REAL_CAPTURE]
    assert [p for p in Provenance if p.observed] == list(OBSERVED)
    assert [p for p in Provenance if p.admits_ground_truth] == list(GENERATED)
    assert not Provenance.REPLAY.observed and not Provenance.REPLAY.admits_ground_truth


def test_no_evidence_text_overclaims() -> None:
    real = Provenance.REAL_CAPTURE.evidence
    assert "not proof of reliability improvement" in real
    for provenance in Provenance:
        if provenance is not Provenance.REAL_CAPTURE:
            assert re.search(r"\bnot (real-world|new|representative) evidence", provenance.evidence)


# ---------------------------------------------------------------------------
# Recorder: real capture is masked, and carries no ground truth
# ---------------------------------------------------------------------------


def test_real_capture_refuses_secrets_only_redaction() -> None:
    with pytest.raises(ValueError, match="mask redaction"):
        _capture(policy=RedactionPolicy(mode=RedactionMode.SECRETS_ONLY))
    sink = io.StringIO()
    with pytest.raises(ValueError):
        Recorder(
            sink,
            provenance="real_capture",  # type: ignore[arg-type] -- a CLI string
            session="s",
            policy=RedactionPolicy(mode=RedactionMode.SECRETS_ONLY),
        )
    assert sink.getvalue() == ""  # refused before anything was written


@pytest.mark.parametrize("provenance", [p for p in Provenance if p is not Provenance.REAL_CAPTURE])
def test_other_provenances_may_still_choose_secrets_only(provenance: Provenance) -> None:
    recorder, _ = _capture(
        provenance=provenance, policy=RedactionPolicy(mode=RedactionMode.SECRETS_ONLY)
    )
    recorder.close()


@pytest.mark.parametrize("provenance", [Provenance.REPLAY, *OBSERVED])
def test_ground_truth_is_refused_where_nobody_knows_it(provenance: Provenance) -> None:
    recorder, sink = _capture(provenance=provenance)
    recorder.fault("drop_response", id=1)
    recorder.label(1, tool="search", intended={"query": "x"}, category="A")
    recorder.close()
    recording = parse_recording(sink.getvalue().splitlines())
    assert recording.of_type("fault") == () and recording.of_type("label") == ()
    assert recorder.ground_truth_refused == 2
    assert recording.end is not None and recording.end["ground_truth_refused"] == 2
    assert recording.complete  # refusing is not a recorder failure


@pytest.mark.parametrize("provenance", GENERATED)
def test_harness_recordings_keep_their_ground_truth(provenance: Provenance) -> None:
    recorder, sink = _capture(provenance=provenance)
    recorder.fault("drop_response", id=1)
    recorder.label(1, tool="search", intended={"query": "x"})
    recorder.close()
    recording = parse_recording(sink.getvalue().splitlines())
    assert len(recording.of_type("fault")) == 1 and len(recording.of_type("label")) == 1
    assert "ground_truth_refused" not in (recording.end or {})


def test_the_header_says_when_a_live_capture_began() -> None:
    recorder, sink = _capture(deterministic=False, wall_clock=lambda: 1_760_000_000.0)
    recorder.close()
    header = parse_recording(sink.getvalue().splitlines()).header
    assert header["started_at"] == "2025-10-09T08:53:20Z"
    assert header["provenance"] == "real_capture" and header["redaction"]["mode"] == "mask"
    recorder, sink = _capture()  # deterministic: no wall clock anywhere
    recorder.close()
    assert "started_at" not in parse_recording(sink.getvalue().splitlines()).header


# ---------------------------------------------------------------------------
# Parser: a file that mixes observed traffic with ground truth is refused
# ---------------------------------------------------------------------------


def _lines(provenance: Provenance, mode: str = "mask") -> list[str]:
    recorder, sink = _capture(provenance=Provenance.FAULT_INJECTION)
    recorder.message(Direction.CLIENT_TO_RELAY, _line({"jsonrpc": "2.0", "id": 1, "method": "x"}))
    recorder.fault("drop_response", id=1)
    recorder.label(1, tool="x", intended={})
    recorder.close()
    lines = sink.getvalue().splitlines()
    header = json.loads(lines[0])
    header["provenance"] = provenance.value
    header["redaction"]["mode"] = mode
    if provenance is Provenance.REAL_CAPTURE:
        header["redaction"]["rules"].append("mask_numbers")
    return [json.dumps(header), *lines[1:]]


@pytest.mark.parametrize("provenance", [Provenance.REPLAY, *OBSERVED])
@pytest.mark.parametrize("kind", ["fault", "label"])
def test_parser_refuses_ground_truth_in_observed_traffic(provenance: Provenance, kind: str) -> None:
    lines = [
        line
        for line in _lines(provenance)
        if json.loads(line)["type"] in ("header", "message", "end", kind)
    ]
    with pytest.raises(MalformedRecordingError, match=f"a {kind} record in a {provenance.value}"):
        parse_recording(lines)


def test_parser_refuses_an_unmasked_real_capture() -> None:
    with pytest.raises(MalformedRecordingError, match="mask redaction"):
        parse_recording(_lines(Provenance.REAL_CAPTURE, mode="secrets_only")[:2])


def test_parser_refuses_a_real_capture_that_kept_numbers() -> None:
    header = json.loads(_lines(Provenance.REAL_CAPTURE)[0])
    header["redaction"]["rules"].remove("mask_numbers")
    with pytest.raises(MalformedRecordingError, match="must mask numbers"):
        parse_recording([json.dumps(header)])


def test_parser_accepts_generated_ground_truth() -> None:
    assert len(parse_recording(_lines(Provenance.SYNTHETIC)).of_type("fault")) == 1


def test_a_truncated_real_capture_parses_as_incomplete() -> None:
    recorder, sink = _capture()
    recorder.message(Direction.CLIENT_TO_RELAY, _line({"jsonrpc": "2.0", "id": 1, "method": "x"}))
    lines = sink.getvalue().splitlines()  # never closed: no end record
    recording = parse_recording(lines)
    assert not recording.complete
    summary = summarize_evidence(recording)
    assert summary["integrity"]["complete"] is False
    with pytest.raises(MalformedRecordingError):
        parse_recording([*lines, lines[-1][: len(lines[-1]) // 2]])


# ---------------------------------------------------------------------------
# Redaction: one path, the same for real capture as for any masked recording
# ---------------------------------------------------------------------------

TRAFFIC: list[tuple[Direction, Any]] = [
    (Direction.SERVER_TO_RELAY, LISTING),
    (
        Direction.CLIENT_TO_RELAY,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "method": "tools/call",
            "params": {
                "name": "list_issues",
                "arguments": {
                    "state": "open",
                    "q": f"find {SECRETS['bearer']} near alice@example.com",
                    "nested": {"deeper": [{"api_key": SECRETS["sk"]}, SECRETS["aws"]]},
                    "verification_code": 482913,
                    "passcode": "1234",
                },
                "_meta": {"authorization": SECRETS["basic"], "trace": SECRETS["jwt"]},
            },
        },
    ),
    (
        Direction.SERVER_TO_RELAY,
        {
            "jsonrpc": "2.0",
            "id": 7,
            "result": {
                "resultType": "complete",
                "isError": True,
                "content": [{"type": "text", "text": "401 Unauthorized"}],
                "structuredContent": {"retry_with": SECRETS["github"]},
            },
        },
    ),
    (
        Direction.SERVER_TO_RELAY,
        {
            "jsonrpc": "2.0",
            "id": 8,
            "error": {"code": -32603, "message": SECRETS["assignment"], "data": SECRETS["url"]},
        },
    ),
]


def _record(provenance: Provenance) -> str:
    recorder, sink = _capture(provenance=provenance)
    for direction, message in TRAFFIC:
        recorder.message(direction, _line(message))
    recorder.audit({"event": "internal_error", "category": "Z", "error": SECRETS["pem"]})
    recorder.close()
    return sink.getvalue()


def _body(text: str) -> list[dict[str, Any]]:
    return [r for r in map(json.loads, text.splitlines()) if r["type"] in ("message", "audit")]


@pytest.mark.parametrize("provenance", [p for p in Provenance if p is not Provenance.REAL_CAPTURE])
def test_real_capture_redacts_through_the_same_path(provenance: Provenance) -> None:
    """One redaction path: on traffic with no free-standing numbers, a real
    capture's records equal any masked recording's. (Numbers: see below.)"""
    assert _body(_record(Provenance.REAL_CAPTURE)) == _body(_record(provenance))


def test_no_secret_shaped_value_reaches_a_real_capture() -> None:
    text = _record(Provenance.REAL_CAPTURE)
    for value in SECRETS.values():
        assert value not in text
        # Nor any distinctive fragment of it (a run with a digit: not a word
        # like "internal" that the relay's own vocabulary also uses).
        for fragment in re.findall(r"[A-Za-z0-9]{8,}", value):
            if not re.search(r"\d", fragment):
                continue
            assert fragment not in text, fragment
    assert "alice@example.com" not in text and "482913" not in text and "1234" not in text
    # The auth signal survives (fixed vocabulary), so the class survives.
    assert "401 Unauthorized" in text


def test_one_time_code_keys_are_credentials() -> None:
    for key in (
        "passcode",
        "verification_code",
        "securityCode",
        "recovery-code",
        "backup_code",
        "mfaCode",
        "otp_code",
    ):
        assert sensitive_key(key), key
    for key in ("code", "zip_code", "country_code", "status_code", "language"):
        assert not sensitive_key(key), key


def test_server_identity_is_kept_and_its_prose_is_not() -> None:
    redactor = Redactor()
    out = redactor.message(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "serverInfo": {
                    "name": "github-mcp-server",
                    "version": "1.4.2",
                    "title": "Acme internal tools",
                    "websiteUrl": SECRETS["url"],
                },
                "instructions": "Use the token for the Acme workspace",
            },
        }
    )
    info = out["result"]["serverInfo"]
    assert (info["name"], info["version"]) == ("github-mcp-server", "1.4.2")
    assert "Acme" not in json.dumps(out) and SECRETS["url"] not in json.dumps(out)
    odd = redactor.message(
        {"jsonrpc": "2.0", "id": 2, "result": {"serverInfo": {"name": SECRETS["slack"]}}}
    )
    assert odd["result"]["serverInfo"]["name"] == REDACTED
    spaced = redactor.message(
        {"jsonrpc": "2.0", "id": 3, "result": {"serverInfo": {"name": "Alice's laptop"}}}
    )
    assert "Alice" not in json.dumps(spaced)


# ---------------------------------------------------------------------------
# Evidence: observed, StateGuard-observed, proposed and unknown stay apart
# ---------------------------------------------------------------------------


def _call(identifier: int, arguments: Any) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {"name": "list_issues", "arguments": arguments},
    }


def _shadow_session() -> list[str]:
    """A shadow session as the relay records it (hand-fed, deterministic)."""
    recorder, sink = _capture(config={"mode": "shadow"})
    tap = recorder.message
    tap(Direction.SERVER_TO_RELAY, _line(LISTING))
    # 1: fine. 2: repairable -- forwarded as received; auto would repair.
    # 3: the server says 401. 4: never answered -- unknown.
    for identifier, arguments in ((1, {"state": "open"}), (2, {"state": "Open"}), (3, {}), (4, {})):
        tap(Direction.CLIENT_TO_RELAY, _line(_call(identifier, arguments)))
        recorder.audit(
            {
                "event": "call",
                "id": identifier,
                "tool": "list_issues",
                "action": "forward" if identifier != 2 else "hold",
                "auto_action": "forward",
                "would_modify": identifier == 2,
                "proposal": "repair" if identifier == 2 else "none",
                "decisions": (
                    [{"path": "state", "decision": "repaired", "op": "coerce", "trust": 0.9}]
                    if identifier == 2
                    else []
                ),
                "listing": "current",
            }
        )
        tap(Direction.RELAY_TO_SERVER, _line(_call(identifier, arguments)))
    ok = {"resultType": "complete", "content": [{"type": "text", "text": "fine"}]}
    for identifier, result in (
        (1, ok),
        (2, ok),
        (
            3,
            {
                "resultType": "complete",
                "isError": True,
                "content": [{"type": "text", "text": "401 Unauthorized"}],
            },
        ),
    ):
        answer = {"jsonrpc": "2.0", "id": identifier, "result": result}
        tap(Direction.SERVER_TO_RELAY, _line(answer))
        tap(Direction.RELAY_TO_CLIENT, _line(answer))
    recorder.audit({"event": "auth_failure", "category": "K", "id": 3, "failure": "authentication"})
    recorder.audit({"event": "retry_decision", "id": 3, "decision": "reject", "acted": False})
    recorder.audit(
        {"event": "outcome_unknown", "id": 4, "reason": "transport_closed", "surfaced": False}
    )
    recorder.audit({"event": "server_lost", "category": "E", "in_flight_unknown": 1})
    recorder.audit({"event": "restart_would_attempt", "category": "E"})
    recorder.close()
    return sink.getvalue().splitlines()


def test_evidence_keeps_observed_proposed_and_unknown_apart() -> None:
    summary = summarize_evidence(parse_recording(_shadow_session()))
    assert summary["provenance"] == "real_capture" and summary["mode"] == "shadow"
    assert summary["evidence"] == Provenance.REAL_CAPTURE.evidence

    wire = summary["wire"]
    assert wire["tool_calls"] == 4 and wire["tool_calls_forwarded_unchanged"] == 4
    assert wire["tool_calls_forwarded_changed"] == wire["client_answers_not_from_server"] == 0

    observed = summary["observed"]
    # What the server said -- the repairable call 2 succeeded as sent.
    assert observed["by_failure_class"] == {"authentication": 1, "none": 2}
    assert observed["tool_calls_never_answered"] == 1 and observed["server_lost"] == 1

    assert summary["stateguard_observed"]["auth_failures"] == {"authentication": 1}

    proposed = summary["proposed"]
    assert proposed["hypothetical"] is True
    assert proposed["would_modify_call"] == 1 and proposed["would_modify_by_kind"] == {"repair": 1}
    assert proposed["would_repair_call"] == 1
    assert summary["stateguard_observed"]["calls_with_schema_violations"] == 1
    assert proposed["would_restart"] == 1
    assert proposed["acted"] == {"retries_sent": 0, "restarts": 0, "responses_dropped": 0}

    unknown = summary["unknown"]
    assert unknown == {
        "outcome_unknown": 1,
        "by_reason": {"transport_closed": 1},
        "answer_seen_later": 0,
        "never_answered": 1,
    }
    # A proposal never shows up as a failure, a prevention, or a success.
    assert "prevented" not in json.dumps(summary).replace("prevented failure", "")
    assert summary["not_claimed"] == list(NOT_CLAIMED)
    assert summary["integrity"]["ground_truth_records"] == 0


def test_evidence_is_deterministic() -> None:
    one = summarize_evidence(parse_recording(_shadow_session()))
    assert one == summarize_evidence(parse_recording(_shadow_session()))


def test_auto_mode_decisions_are_not_called_hypothetical() -> None:
    recorder, sink = _capture(provenance=Provenance.LOCAL_DEVELOPMENT, config={"mode": "auto"})
    recorder.close()
    summary = summarize_evidence(parse_recording(sink.getvalue().splitlines()))
    assert summary["proposed"]["hypothetical"] is False


def test_a_live_recording_digests_lines_under_a_key_it_never_writes() -> None:
    import hashlib

    line = _line({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "x"}})
    recorder, sink = _capture(deterministic=False)
    recorder.message(Direction.CLIENT_TO_RELAY, line + b"\n")
    recorder.message(Direction.RELAY_TO_SERVER, line)
    recorder.close()
    first, second = parse_recording(sink.getvalue().splitlines()).messages()
    digest = first["diag"]["digest"]
    assert digest == second["diag"]["digest"]  # same bytes, newline aside
    assert digest not in hashlib.sha256(line).hexdigest()  # keyed: not a plain hash
    other, other_sink = _capture(deterministic=False)
    other.message(Direction.CLIENT_TO_RELAY, line)
    other.close()
    (again,) = parse_recording(other_sink.getvalue().splitlines()).messages()
    assert again["diag"]["digest"] != digest  # a fresh key per recording
    recorder, sink = _capture()  # deterministic: no digest
    recorder.message(Direction.CLIENT_TO_RELAY, line)
    recorder.close()
    (plain,) = parse_recording(sink.getvalue().splitlines()).messages()
    assert "digest" not in plain["diag"]


# ---------------------------------------------------------------------------
# Numbers: a real capture keeps no number a user could have typed
# ---------------------------------------------------------------------------

NUMERIC_TOOL = {
    "name": "verify",
    "inputSchema": {
        "type": "object",
        "properties": {
            "code": {"type": "integer"},
            "code_text": {"type": "integer"},
            "limit": {"type": "integer", "maximum": 1000},
            "page_size": {"type": "integer", "enum": [10, 25, 50]},
            "version": {"const": 3},
            "ratio": {"type": "number"},
        },
    },
}
NUMERIC_LISTING = {
    "jsonrpc": "2.0",
    "id": "stateguard-relay:1",
    "result": {"resultType": "complete", "tools": [NUMERIC_TOOL]},
}
#: Values a user might type that no key name marks as a credential.
TYPED = (482913, 7351, 664422, 118842, 90210, 31415, 271828)


def _numeric_call(identifier: int = 123456) -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": identifier,
        "method": "tools/call",
        "params": {
            "name": "verify",
            "arguments": {
                "code": 482913,  # an OTP under an ordinary key
                "code_text": "7351",  # a PIN as a numeric string in an integer field
                "limit": 25,  # ordinary, not sensitive
                "page_size": 25,  # a published enum member
                "version": 3,  # a published const
                "ratio": 3.14159,  # a float
                "flow": {"step": 2, "answer": 664422, "geo": [37.774929, -122.419416]},
                "history": [118842, {"value": 90210}],
                "embedded": json.dumps({"value": 31415}),
                "value": "hunter2xyz",  # a password under a non-obvious key
                "note": SECRETS["github"],
            },
            "_meta": {"io.example/counter": 271828},
        },
    }


def _numeric_recording(provenance: Provenance, **kwargs: Any) -> tuple[str, list[dict[str, Any]]]:
    recorder, sink = _capture(provenance=provenance, **kwargs)
    recorder.message(Direction.SERVER_TO_RELAY, _line(NUMERIC_LISTING))
    recorder.message(Direction.CLIENT_TO_RELAY, _line(_numeric_call()))
    recorder.message(
        Direction.SERVER_TO_RELAY,
        _line(
            {
                "jsonrpc": "2.0",
                "id": 123456,
                "result": {
                    "resultType": "complete",
                    "isError": False,
                    "content": [{"type": "text", "text": "balance 482913"}],
                    "structuredContent": {"balance": 7351.25, "pin": 1234, "items": [664422]},
                },
            }
        ),
    )
    recorder.message(
        Direction.SERVER_TO_RELAY,
        _line(
            {
                "jsonrpc": "2.0",
                "id": 123457,
                "error": {
                    "code": -32603,
                    "message": "Request failed",
                    "data": {"status": 401, "retry_after": 118842, "statusCode": 4821},
                },
            }
        ),
    )
    recorder.close()
    text = sink.getvalue()
    return text, list(map(json.loads, text.splitlines()))


def _numbers(value: Any, path: str = "") -> list[tuple[str, int | float]]:
    if isinstance(value, dict):
        return [n for k, v in value.items() for n in _numbers(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [n for i, v in enumerate(value) for n in _numbers(v, f"{path}[{i}]")]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return [(path, value)]
    return []


def _power_of_ten(value: int | float) -> bool:
    size = abs(value)
    if size == 0:
        return True
    while size >= 10:
        size /= 10
    while size < 1:
        size *= 10
    return abs(size - 1) < 1e-9


def test_a_real_capture_keeps_no_typed_number() -> None:
    text, records = _numeric_recording(Provenance.REAL_CAPTURE)
    for typed in TYPED:
        assert str(typed) not in text, typed
    for fragment in ("37.774929", "122.419416", "3.14159", "7351.25", "hunter2", "4821"):
        assert fragment not in text, fragment
    assert SECRETS["github"] not in text
    # Whatever number is left in a message is a protocol value or a scale.
    kept = {
        ".id": {123456, 123457},
        ".error.code": {-32603},
        ".error.data.status": {401},
        ".params.arguments.page_size": {25},
        ".params.arguments.version": {3},
    }
    for record in records:
        if record["type"] != "message":
            continue
        for path, number in _numbers(record["message"]):
            if ".inputSchema." in path:
                continue  # a published tool schema: the server's contract
            assert number in kept.get(path, set()) or _power_of_ten(number), (path, number)


def test_real_capture_numbers_keep_type_sign_and_scale() -> None:
    _, records = _numeric_recording(Provenance.REAL_CAPTURE)
    (call,) = [r for r in records if r["type"] == "message" and r["kind"] == "request"]
    arguments = call["message"]["params"]["arguments"]
    assert arguments["code"] == 100000 and isinstance(arguments["code"], int)
    assert arguments["code_text"] == "1111"  # masked text, not vouched by the integer type
    assert arguments["limit"] == 10  # ordinary: scale kept, value not
    assert (arguments["page_size"], arguments["version"]) == (25, 3)  # published members
    assert arguments["ratio"] == 1.0 and isinstance(arguments["ratio"], float)
    assert arguments["flow"] == {"step": 1, "answer": 100000, "geo": [10.0, -100.0]}
    assert arguments["history"] == [100000, {"value": 10000}]
    assert json.loads(arguments["embedded"]) == {"value": 10000}
    assert arguments["value"] == "aaaaaa1aaa"
    assert call["message"]["params"]["_meta"]["io.example/counter"] == 100000


def test_real_capture_keeps_what_analysis_needs() -> None:
    from stateguard.adapters.mcp.failures import FailureClass, classify_response

    _, records = _numeric_recording(Provenance.REAL_CAPTURE)
    listing = records[1]["message"]["result"]["tools"][0]["inputSchema"]
    assert listing["properties"]["limit"]["maximum"] == 1000  # the contract
    assert listing["properties"]["page_size"]["enum"] == [10, 25, 50]
    (error,) = [r for r in records if r["type"] == "message" and r["kind"] == "error"]
    data = error["message"]["error"]["data"]
    # The status the auth classifier reads survives; other numbers do not,
    # and an out-of-range "status" is not an HTTP status.
    assert data == {"status": 401, "retry_after": 100000, "statusCode": 1000}
    assert classify_response(error["message"]).failure is FailureClass.AUTHENTICATION
    (result,) = [
        r
        for r in records
        if r["type"] == "message" and r["kind"] == "response" and r.get("id") == 123456
    ]
    assert result["message"]["result"]["structuredContent"] == {
        "balance": 1000.0,
        "pin": 0,  # a credential key: scrubbed, as before
        "items": [100000],
    }
    assert records[0]["redaction"]["rules"][-1] == "mask_numbers"


def test_real_capture_keeps_relay_counters_and_settings() -> None:
    recorder, sink = _capture(config={"mode": "shadow", "restart": 2, "call_deadline": 2.5})
    recorder.audit(
        {
            "event": "server_restarted",
            "category": "E",
            "generation": 3,
            "attempt": 2,
            "in_flight_unknown": 1,
            "decisions": [{"path": "limit", "trust": 0.873}],
            "summary": {"added": 2, "removed": 0, "breaking": 1},
            "error_count": 482913,  # not a relay counter: masked
        }
    )
    recorder.close()
    header, audit, _ = map(json.loads, sink.getvalue().splitlines())
    assert header["config"]["restart"] == 2 and header["config"]["call_deadline"] == 2.5
    event = audit["event"]
    assert (event["generation"], event["attempt"], event["in_flight_unknown"]) == (3, 2, 1)
    assert event["decisions"] == [{"path": "limit", "trust": 0.873}]
    assert event["summary"] == {"added": 2, "removed": 0, "breaking": 1}
    assert event["error_count"] == 100000


@pytest.mark.parametrize("provenance", [p for p in Provenance if p is not Provenance.REAL_CAPTURE])
def test_generated_recordings_keep_their_numbers_as_before(provenance: Provenance) -> None:
    """Unchanged outside real capture: synthetic, fault-injection, replay and
    local-development recordings keep numbers below 1,000,000 (the documented
    residual of the default policy)."""
    text, records = _numeric_recording(provenance)
    (call,) = [r for r in records if r["type"] == "message" and r["kind"] == "request"]
    arguments = call["message"]["params"]["arguments"]
    assert (arguments["code"], arguments["limit"], arguments["ratio"]) == (482913, 25, 3.14159)
    assert arguments["code_text"] == "7351"
    assert "mask_numbers" not in records[0]["redaction"]["rules"]
    assert SECRETS["github"] not in text and "hunter2" not in text  # strings: masked as ever


def test_real_capture_masks_numbers_whatever_policy_is_passed() -> None:
    text, records = _numeric_recording(
        Provenance.REAL_CAPTURE, policy=RedactionPolicy(mask_numbers=False)
    )
    assert "482913" not in text and "mask_numbers" in records[0]["redaction"]["rules"]
    parse_recording(text.splitlines())  # and it is a valid real capture


def test_any_recording_can_opt_into_masking_numbers() -> None:
    text, _ = _numeric_recording(
        Provenance.LOCAL_DEVELOPMENT, policy=RedactionPolicy(mask_numbers=True)
    )
    assert all(str(typed) not in text for typed in TYPED)


def record_jsonrpc(provenance: Provenance) -> Any:
    recorder, sink = _capture(provenance=provenance)
    recorder.message(Direction.CLIENT_TO_RELAY, _line({"jsonrpc": "2.0", "id": 1, "method": "m"}))
    recorder.message(
        Direction.SERVER_TO_RELAY,
        _line(
            {"jsonrpc": "2.0", "id": 1, "result": {"serverInfo": {"name": "s", "version": "2.0"}}}
        ),
    )
    recorder.close()
    first, second = (json.loads(x) for x in sink.getvalue().splitlines()[1:3])
    assert second["message"]["result"]["serverInfo"]["version"] == "2.0"
    return first["message"]["jsonrpc"]


def test_numbers_in_identifier_positions_and_keys_are_masked_too() -> None:
    def record(provenance: Provenance) -> dict[str, Any]:
        recorder, sink = _capture(provenance=provenance)
        recorder.message(
            Direction.CLIENT_TO_RELAY,
            _line(
                {
                    "jsonrpc": "2.0",
                    "id": 9,
                    "method": "tools/call",
                    "params": {
                        "name": "lookup",
                        "arguments": {"by_code": {"482": "x", "7351": "y", "open": "z"}},
                        "_meta": {
                            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                            "io.modelcontextprotocol/ref": "664422",
                        },
                    },
                }
            ),
        )
        recorder.close()
        lines = sink.getvalue().splitlines()
        return dict(json.loads(lines[1])["message"]["params"])

    real = record(Provenance.REAL_CAPTURE)
    assert set(real["arguments"]["by_code"]) == {"111", "1111", "open"}
    assert real["_meta"]["io.modelcontextprotocol/ref"] == "111111"
    assert real["_meta"]["io.modelcontextprotocol/protocolVersion"] == "2026-07-28"
    assert record_jsonrpc(Provenance.REAL_CAPTURE) == "2.0"
    synthetic = record(Provenance.SYNTHETIC)  # unchanged there
    assert "482" in synthetic["arguments"]["by_code"]
    assert synthetic["_meta"]["io.modelcontextprotocol/ref"] == "664422"


def _call_event(identifier: int, **fields: Any) -> dict[str, Any]:
    return {
        "event": "call",
        "id": identifier,
        "tool": "list_issues",
        "action": "hold",
        "auto_action": "forward",
        "listing": "current",
        "decisions": [],
        **fields,
    }


def test_a_default_fill_is_not_counted_as_a_repair_or_a_mismatch() -> None:
    """Capture 1a: 5 of 18 valid calls only omitted defaulted optionals."""
    recorder, sink = _capture(config={"mode": "shadow"})
    recorder.audit(_call_event(1, would_modify=True, proposal="default_fill"))
    recorder.audit(_call_event(2, would_modify=True, proposal="default_fill"))
    recorder.audit(
        _call_event(
            3,
            would_modify=True,
            proposal="repair",
            decisions=[{"path": "limit", "decision": "repaired", "op": "coerce", "trust": 1.0}],
        )
    )
    recorder.audit(
        _call_event(
            4,
            auto_action="refuse",
            would_modify=True,
            proposal="refuse",
            decisions=[{"path": "x", "decision": "unresolved", "op": None, "trust": None}],
        )
    )
    recorder.audit(_call_event(5, action="forward", would_modify=False, proposal="none"))
    # A recording from before the relay classified proposals:
    recorder.audit({k: v for k, v in _call_event(6, would_modify=True).items()})
    recorder.close()
    summary = summarize_evidence(parse_recording(sink.getvalue().splitlines()))
    proposed = summary["proposed"]
    assert proposed["would_modify_call"] == 5
    assert proposed["would_modify_by_kind"] == {
        "default_fill": 2,
        "refuse": 1,
        "repair": 1,
        "unclassified": 1,
    }
    assert proposed["would_repair_call"] == 1  # default fills are not repairs
    assert summary["stateguard_observed"]["calls_with_schema_violations"] == 2

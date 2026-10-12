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
def test_real_capture_redacts_exactly_as_any_masked_recording(provenance: Provenance) -> None:
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
    assert proposed["would_modify_call"] == 1 and proposed["would_modify_by_action"] == {
        "repair": 1
    }
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

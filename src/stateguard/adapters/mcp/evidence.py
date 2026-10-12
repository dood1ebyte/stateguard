"""
Summarize a recording as evidence -- keeping apart what happened and what
StateGuard only thought.

A shadow-mode recording holds two different kinds of statement, and a
report that blurs them overstates what StateGuard did:

* **Observed** -- facts on the wire. What the client sent, what reached the
  server, what the server answered (a tool error, an auth failure, a
  JSON-RPC error), whether the server process went away. These happened.
* **Proposed** -- what auto mode *would* have done: repair, refuse or
  escalate a call; write out the declared defaults of optional fields a
  valid call omitted (``default_fill``, counted apart: it is not a repair);
  retry it; restart the server; drop a duplicate answer. In shadow mode none
  of it happened. A proposal is not an observed failure, and a call auto
  mode would have repaired is **not** a prevented production failure: nobody
  knows whether the original call failed because of what StateGuard would
  have changed, or would have succeeded after the change.

And one kind is neither: **unknown**. A request whose outcome the relay could
not determine (its server went away with it in flight, or a deadline passed)
stays unknown here. It is never counted as a success or a failure.

``summarize_evidence`` reads only what the recording holds -- message
records and the relay's audit events, already redacted -- and adds nothing.
Failure classes are re-derived with the T11 classifier on the redacted
answers; redaction keeps the fixed-vocabulary auth signals, so they match
what the relay classified live. It is evidence collection, not proof of a
reliability improvement.

Zero external dependencies.
"""

from __future__ import annotations

import json
from collections import Counter
from typing import Any

from stateguard.adapters.mcp.failures import FailureClass, classify_response
from stateguard.adapters.mcp.recording import Direction, Recording

__all__ = ["EVIDENCE_SCHEMA", "NOT_CLAIMED", "summarize_evidence"]

#: Identifies the summary's layout (not the recording format).
EVIDENCE_SCHEMA = "stateguard.mcp.evidence/1"

#: What a summary never supports, whatever its numbers. Printed with it.
NOT_CLAIMED = (
    "Proposals are hypothetical: in shadow mode no call was changed, refused, retried or "
    "restarted, and none is counted as a prevented failure.",
    "A call auto mode would have modified is not an observed failure, and its modification is "
    "not shown to have helped.",
    "An unknown outcome is neither a success nor a failure.",
    "Counts describe this recording only; they are not rates for any other server, workload "
    "or period, and a synthetic benchmark's rates say nothing about them.",
    "real_capture provides evidence collection, not proof of reliability improvement.",
)


def _key(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True)


def _body(record: dict[str, Any]) -> str:
    return json.dumps(record.get("message"), sort_keys=True)


def _digest(record: dict[str, Any]) -> str | None:
    digest = (record.get("diag") or {}).get("digest")
    return digest if isinstance(digest, str) else None


def _same_line(record: dict[str, Any], forwarded: list[dict[str, Any]]) -> tuple[str, bool]:
    """Whether a client line reached the server unchanged, and how that was decided."""
    digest = _digest(record)
    if digest is not None and all(_digest(f) is not None for f in forwarded):
        return "digest", any(_digest(f) == digest for f in forwarded)
    return "redacted_body", any(_body(f) == _body(record) for f in forwarded)


def _proposal_kind(event: dict[str, Any]) -> str:
    kind = event.get("proposal")
    if isinstance(kind, str):
        return kind
    action = event.get("auto_action")
    return "unclassified" if action == "forward" else str(action)


def _events(recording: Recording, name: str) -> list[dict[str, Any]]:
    return [r["event"] for r in recording.of_type("audit") if r["event"].get("event") == name]


def _field_findings(decided: list[dict[str, Any]]) -> tuple[int | None, int | None]:
    """
    ``(calls_with_field_violations, calls_with_undeclared_fields)``.

    A *violation* is a field StateGuard's own validator found invalid: any
    repaired, held or discarded decision (those exist only for violations),
    or an unresolved one of severity ``error``. An *undeclared field* is an
    unresolved decision of severity ``warning``: a key the schema does not
    declare but, being open, allows -- passed through, observed, not invalid.

    Both are StateGuard's contract model, not the server's validator: a call
    can violate one and satisfy the other (an integral float for an integer).

    ``(None, None)`` when an unresolved decision carries no ``severity``
    (recordings made before the relay recorded it): the two cannot be told
    apart, and a guess would be a number with no meaning.
    """
    violations = undeclared = 0
    for event in decided:
        decisions = [d for d in event.get("decisions") or [] if isinstance(d, dict)]
        unresolved = [d for d in decisions if d.get("decision") == "unresolved"]
        if any(d.get("severity") is None for d in unresolved):
            return None, None
        if any(d.get("decision") != "unresolved" for d in decisions) or any(
            d.get("severity") == "error" for d in unresolved
        ):
            violations += 1
        if any(d.get("severity") == "warning" for d in unresolved):
            undeclared += 1
    return violations, undeclared


def summarize_evidence(recording: Recording) -> dict[str, Any]:
    """The recording's evidence, by kind. Deterministic; reads nothing but *recording*."""
    header = recording.header
    end = recording.end or {}
    client_out = recording.messages(Direction.CLIENT_TO_RELAY)
    to_server = recording.messages(Direction.RELAY_TO_SERVER)
    from_server = recording.messages(Direction.SERVER_TO_RELAY)
    to_client = recording.messages(Direction.RELAY_TO_CLIENT)

    sent: dict[str, list[dict[str, Any]]] = {}
    for record in to_server:
        if record["kind"] == "request":
            sent.setdefault(_key(record.get("id")), []).append(record)
    server_answers: dict[str, list[str]] = {}
    for record in from_server:
        if record["kind"] in ("response", "error"):
            server_answers.setdefault(_key(record.get("id")), []).append(_body(record))

    # -- wire: what crossed, and whether the relay changed it -----------------
    calls = [r for r in client_out if r["kind"] == "request" and r.get("method") == "tools/call"]
    unchanged = changed = not_forwarded = 0
    compared: Counter[str] = Counter()
    for record in calls:
        forwarded = sent.get(_key(record.get("id")), [])
        if not forwarded:
            not_forwarded += 1
            continue
        by, same = _same_line(record, forwarded)
        compared[by] += 1
        if same:
            unchanged += 1
        else:
            changed += 1
    client_ids = {_key(r.get("id")) for r in client_out if r["kind"] == "request"}
    answers = [
        r
        for r in to_client
        if r["kind"] in ("response", "error") and _key(r.get("id")) in client_ids
    ]
    relay_generated = sum(
        1 for r in answers if _body(r) not in server_answers.get(_key(r.get("id")), [])
    )

    # -- observed: what the server answered to the client's tool calls --------
    call_ids = {_key(r.get("id")) for r in calls}
    classes: Counter[str] = Counter()
    signals: Counter[str] = Counter()
    answered: set[str] = set()
    for record in from_server:
        identifier = _key(record.get("id"))
        if record["kind"] not in ("response", "error") or identifier not in call_ids:
            continue
        answered.add(identifier)
        found = classify_response(record.get("message"))
        classes[found.failure.value] += 1
        if found.failure is not FailureClass.NONE:
            signals[f"{found.failure.value}:{found.signal}"] += 1

    # -- unknown: outcomes the relay could not determine ----------------------
    unknown = _events(recording, "outcome_unknown")
    unknown_ids = {_key(e.get("id")) for e in unknown}
    answered_any = {_key(r.get("id")) for r in from_server if r["kind"] in ("response", "error")}

    # -- proposals: what auto mode would have done -----------------------------
    calls = _events(recording, "call")
    # A call for a tool the listing does not name is forwarded unvalidated:
    # StateGuard decided nothing about it, so it is counted apart.
    unvalidated = [e for e in calls if e.get("action") == "unvalidated"]
    decided = [e for e in calls if e.get("action") != "unvalidated"]
    would_modify = [e for e in decided if e.get("would_modify") is True]
    field_violations, undeclared_fields = _field_findings(decided)
    retry_decisions = _events(recording, "retry_decision")

    servers = sorted(
        {
            (
                int(r.get("gen", 0)),
                json.dumps(
                    (r.get("message") or {}).get("result", {}).get("serverInfo"), sort_keys=True
                ),
            )
            for r in from_server
            if isinstance((r.get("message") or {}).get("result"), dict)
            and isinstance(r["message"]["result"].get("serverInfo"), dict)
        }
    )

    mode = (header.get("config") or {}).get("mode")
    shadow = mode == "shadow"
    return {
        "schema": EVIDENCE_SCHEMA,
        "provenance": header["provenance"],
        "evidence": recording.provenance.evidence,
        "session": header["session"],
        "started_at": header.get("started_at"),
        "mode": mode,
        "redaction": header["redaction"]["mode"],
        "integrity": {
            "complete": recording.complete,
            "records": len(recording.records),
            "recorder_failures": end.get("failures"),
            "values_redacted": end.get("values_redacted"),
            "ground_truth_records": len(recording.of_type("fault"))
            + len(recording.of_type("label")),
        },
        "servers": [{"generation": g, "server_info": json.loads(s)} for g, s in servers],
        "wire": {
            "client_requests": sum(1 for r in client_out if r["kind"] == "request"),
            "tool_calls": len(calls),
            "tool_calls_forwarded_unchanged": unchanged,
            "tool_calls_forwarded_changed": changed,
            "tool_calls_not_forwarded": not_forwarded,
            # "digest": the raw bytes compared (a live recording). "redacted_body":
            # a deterministic recording has no digest; the redacted messages are
            # compared, and redaction that learned a schema between the two
            # lines can make an identical call look changed.
            "tool_calls_compared_by": dict(sorted(compared.items())),
            "client_answers": len(answers),
            "client_answers_not_from_server": relay_generated,
            "relay_own_requests": sum(
                1
                for r in to_server
                if r["kind"] == "request" and _key(r.get("id")) not in client_ids
            ),
            "unparsed_lines": sum(
                1 for r in recording.records if r["type"] == "message" and r["kind"] == "unparsed"
            ),
            "server_generations": len({int(r.get("gen", 0)) for r in recording.messages()}),
        },
        "observed": {
            "tool_call_answers": sum(classes.values()),
            "by_failure_class": dict(sorted(classes.items())),
            "by_signal": dict(sorted(signals.items())),
            "tool_calls_never_answered": len(call_ids - answered),
            "server_lost": len(_events(recording, "server_lost")),
            "transport_closed": len(_events(recording, "transport_closed")),
            "send_failed": len(_events(recording, "send_failed")),
            # Arrived with no live server: answered "not sent" by the relay
            # (certainly not executed), in every mode.
            "not_sent": len(_events(recording, "not_sent")),
        },
        "stateguard_observed": {
            # Calls StateGuard checked against a listed schema.
            "calls_decided": len(decided),
            # Calls for a tool the listing did not name: forwarded unchecked.
            "calls_unvalidated": len(unvalidated),
            "decided_on_listing": dict(
                sorted(Counter(str(e.get("listing")) for e in decided).items())
            ),
            "auth_failures": dict(
                sorted(
                    Counter(
                        str(e.get("failure")) for e in _events(recording, "auth_failure")
                    ).items()
                )
            ),
            "result_findings": sum(
                1 for e in _events(recording, "result_check") if e.get("category") == "H"
            ),
            "tool_listings_synced": len(_events(recording, "tools_synced")),
            "breaking_schema_changes": sum(
                1
                for e in _events(recording, "tools_synced")
                for c in e.get("changes") or []
                if isinstance(c, dict) and c.get("breaking") is True
            ),
            "internal_errors": len(_events(recording, "internal_error")),
            # Calls with at least one field StateGuard's validator found
            # invalid -- its contract model, which is stricter than JSON Schema
            # in places (an integral float for an integer), so this is not
            # "calls the server would reject". None for recordings without
            # decision severities.
            "calls_with_field_violations": field_violations,
            # Calls carrying a key the schema does not declare but, being
            # open, allows: passed through as sent. An observation, not a
            # violation -- StateGuard cannot tell a harmless extra from a
            # misnamed optional parameter the server will silently ignore.
            "calls_with_undeclared_fields": undeclared_fields,
            # Legacy, kept so summaries stay comparable with earlier reports:
            # calls with *any* field decision, which also counts calls whose
            # only decision is an undeclared field. Prefer the two above.
            "calls_with_schema_violations": sum(1 for e in decided if e.get("decisions")),
        },
        "proposed": {
            # In shadow mode nothing below "acted" happened. In auto mode
            # these were StateGuard's actual decisions.
            "hypothetical": shadow,
            # Calls auto mode would not forward exactly as received.
            "would_modify_call": len(would_modify),
            # By kind (the relay's ``proposal``): ``repair``, ``default_fill``
            # (a valid call with omitted optionals written out from their
            # declared defaults -- not a repair, not drift, not a failure),
            # ``refuse``, ``escalate``. A recording made before the relay
            # classified proposals says ``unclassified`` for a changed forward.
            "would_modify_by_kind": dict(
                sorted(Counter(_proposal_kind(e) for e in would_modify).items())
            ),
            "would_repair_call": sum(1 for e in would_modify if _proposal_kind(e) == "repair"),
            "would_retry": sum(
                1
                for e in retry_decisions
                if e.get("decision") == "apply" and e.get("acted") is not True
            ),
            "retry_decisions": dict(
                sorted(Counter(str(e.get("decision")) for e in retry_decisions).items())
            ),
            "would_restart": len(_events(recording, "restart_would_attempt")),
            "would_drop_response": len(_events(recording, "response_would_drop")),
            "acted": {
                # Must be zero in a shadow recording: anything here happened.
                "retries_sent": sum(1 for e in retry_decisions if e.get("acted") is True),
                "restarts": len(_events(recording, "server_restarted")),
                "responses_dropped": len(_events(recording, "response_dropped")),
            },
        },
        "unknown": {
            "outcome_unknown": len(unknown),
            "by_reason": dict(sorted(Counter(str(e.get("reason")) for e in unknown).items())),
            # A late answer shows the call did finish; the relay still could
            # not know it in time, and nothing here re-labels that.
            "answer_seen_later": len(unknown_ids & answered_any),
            "never_answered": len(unknown_ids - answered_any),
        },
        "not_claimed": list(NOT_CLAIMED),
    }

"""
Fault incidence from a relay's own audit log: how often each fault happens, and how each
invocation ended.

Every number StateGuard has published about failure rates comes from fault injection, where each
fault is made to happen and every fault weighs the same. A traffic-weighted rate needs the
opposite: the faults real traffic meets, counted per invocation. A relay already sees every
fault on its hop to the server, so each relay writes one ``invocation`` record per client
request to its audit log, and this module turns those records into rates:

* the HTTP relay (``examples/mcp/http_proxy.py``): faults ``connect_retry``, ``not_sent``,
  ``transport_lost``, ``server_error``, ``internal_error``, ``session_lost``, ``stream_cut``,
  ``deadline``;
* the stdio relay (``examples/mcp/stdio_proxy.py``), from its ``InvocationLedger``: why the
  request went UNKNOWN, if it did (``transport_closed``, ``send_failed``, ``deadline``,
  ``not_sent_unconfirmed``, ...), or ``resent`` if it was sent again for another reason.

A record holds no arguments, no results and no message text: the method, the tool's name,
whether the call was keyed, how many attempts it took, the faults met (in order), how it ended,
and how long it took.

What a record can and cannot see
--------------------------------
* Only the relay-to-server hop. A fault between the client and the relay (the client's own
  connection, its timeout) is invisible here; so is anything the server never reports.
* The ``resolution`` is what the *client* was answered, not what the server did:

  ``answered``         the server's answer, with no fault on the way;
  ``answered_error``   the server's own JSON-RPC error, with no fault on the way;
  ``recovered``        the server's answer after one or more faults (the relay resent only what
                       its rules allow: a request that was never sent or processed, a read, a
                       keyed call, or a resumed stream);
  ``recovered_error``  the server's own JSON-RPC error after faults;
  ``unknown``          answered "outcome unknown" -- it may or may not have executed;
  ``not_sent``         answered "not sent" -- it was not executed;
  ``in_doubt``         the server's keyed answer "in doubt" (-32010): claimed, never recorded;
  ``in_progress``      the server's keyed answer "still running" (-32011);
  ``refused``          refused by the relay's schema check, never sent;
  ``client_gone``      the client went away before its answer;
  ``unfinished``       open when the log was read: the relay stopped (or was killed) before it
                       answered, or the request is still running. A relay writes an
                       ``invocation_open`` line when a request arrives, and its ``invocation``
                       record carries the same ``inv`` token; an open line with no record is
                       this. Read a log after its relay has stopped;
  ``http_<status>``    an HTTP status passed through from the server, or the relay's own.

  ``unknown``, ``in_doubt``, ``in_progress``, ``client_gone``, ``unfinished`` and ``http_5xx``
  are *unresolved*: the client cannot tell whether the call ran. *Failed* is every invocation
  the server did not answer: the unresolved ones and also ``not_sent``, ``refused`` and any
  other ``http_<status>``. A tool's own error (``answered_error``) is an answer, not a failure
  of the invocation. Rates are per invocation, with Wilson 95% intervals, and nothing is dropped
  from the denominator.

Usage: ``stateguard incidence AUDIT.jsonl [...] [--json]``.
"""

from __future__ import annotations

import json
import math
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

#: The audit event name of one finished client request.
INVOCATION_EVENT = "invocation"
#: The audit event name of a client request as it arrives. Its ``invocation`` record carries the
#: same ``inv`` token, so a request a relay never finished (it was stopped or killed) is still
#: counted.
INVOCATION_OPEN_EVENT = "invocation_open"
#: The fields an ``invocation_open`` line carries into an ``unfinished`` record.
_OPEN_FIELDS = ("ts", "inv", "id", "session", "method", "tool")
#: Where a keyed server says what it did (replayed, executed, ...).
IDEMPOTENCY_OUTCOME = "io.stateguard/idempotencyOutcome"
#: Where a relay's own answer says what it knows about execution.
EXECUTION_STATE = "io.stateguard/executionState"
IN_DOUBT_CODE = -32010
IN_PROGRESS_CODE = -32011

#: Fault names an HTTP relay record may carry (``faults``), in the order they happened. The stdio
#: relay records its ledger's UNKNOWN reasons instead (see the module docstring).
FAULTS = (
    "connect_retry",  # the connection could not be opened at first; sent once it could
    "not_sent",  # the connection never opened within the retry budget
    "transport_lost",  # the connection broke after the request was sent
    "server_error",  # an HTTP 5xx
    "internal_error",  # a JSON-RPC internal error (-32603), sent again because that was safe
    "nonconformant",  # an answer that is not conformant JSON-RPC 2.0, sent again because safe
    "session_lost",  # the server no longer knew the session (404, or a 400 naming it)
    "stream_cut",  # an event stream ended before its answer
    "deadline",  # no answer within --call-deadline
)
UNRESOLVED = frozenset({"unknown", "in_doubt", "in_progress", "client_gone", "unfinished"})
ANSWERED = frozenset({"answered", "answered_error", "recovered", "recovered_error"})


def resolution(final: Mapping[str, Any] | None, *, status: int | None = None) -> tuple[str, Any]:
    """
    How a client request ended, from the final JSON-RPC message the client was sent (``None``
    if it was sent none), or an HTTP *status* that stood in for one: ``(kind, server outcome)``.
    The server outcome is a keyed server's ``io.stateguard/idempotencyOutcome``, if any. The
    kind does not yet say whether faults came first; :func:`with_faults` adds that.
    """
    if status is not None:
        return f"http_{status}", None
    if final is None:
        return "client_gone", None
    result = final.get("result")
    if isinstance(result, Mapping):
        meta = result.get("_meta")
        return "answered", meta.get(IDEMPOTENCY_OUTCOME) if isinstance(meta, Mapping) else None
    error = final.get("error")
    if not isinstance(error, Mapping):
        return "answered", None
    data = error.get("data")
    known = data.get(EXECUTION_STATE) if isinstance(data, Mapping) else None
    if known in ("unknown", "not_sent"):
        return str(known), None
    if error.get("code") == IN_DOUBT_CODE:
        return "in_doubt", None
    if error.get("code") == IN_PROGRESS_CODE:
        return "in_progress", None
    return "answered_error", None


def with_faults(kind: str, faults: Sequence[str]) -> str:
    """A server's answer that came after a fault is a recovery."""
    if faults and kind in ("answered", "answered_error"):
        return "recovered" if kind == "answered" else "recovered_error"
    return kind


def unresolved(kind: str) -> bool:
    """The client cannot tell whether it ran."""
    return kind in UNRESOLVED or kind.startswith("http_5")


def failed(kind: str) -> bool:
    """The server did not answer it (whether or not it ran)."""
    return kind not in ANSWERED


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, c - h), min(1.0, c + h))


def _rate(k: int, n: int) -> dict[str, Any]:
    lo, hi = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [lo, hi]}


def read_records(paths: Iterable[str | Path]) -> list[dict[str, Any]]:
    """
    Every ``invocation`` record in the audit logs at *paths*, and one ``unfinished`` record for
    each ``invocation_open`` line that no record finished (other events are skipped).
    """
    records: list[dict[str, Any]] = []
    opened: list[dict[str, Any]] = []
    for path in paths:
        for line in Path(path).read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            if not isinstance(entry, dict):
                continue
            if entry.get("event") == INVOCATION_EVENT:
                records.append(entry)
            elif entry.get("event") == INVOCATION_OPEN_EVENT and entry.get("inv"):
                opened.append(entry)
    finished = {r.get("inv") for r in records if r.get("inv")}
    for entry in opened:
        if entry["inv"] not in finished:
            records.append(
                {
                    **{name: entry.get(name) for name in _OPEN_FIELDS},
                    "event": INVOCATION_EVENT,
                    "keyed": None,
                    "attempts": None,
                    "faults": [],
                    "resolution": "unfinished",
                    "server_outcome": None,
                    "ms": None,
                }
            )
    return records


def summarize(records: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Fault incidence and outcomes per invocation, overall and for ``tools/call`` alone."""
    rows = list(records)

    def part(selected: list[Mapping[str, Any]]) -> dict[str, Any]:
        n = len(selected)
        faulted = [r for r in selected if r.get("faults")]
        kinds = Counter(str(r.get("resolution")) for r in selected)
        by_fault = Counter(fault for r in selected for fault in set(r.get("faults") or ()))
        return {
            "invocations": n,
            "faulted": _rate(len(faulted), n),
            "invocations_with_fault": {f: _rate(by_fault[f], n) for f in sorted(by_fault)},
            "resolutions": dict(sorted(kinds.items())),
            "unresolved": _rate(
                sum(1 for r in selected if unresolved(str(r.get("resolution")))), n
            ),
            "failed": _rate(sum(1 for r in selected if failed(str(r.get("resolution")))), n),
            "recovered_of_faulted": _rate(
                sum(1 for r in faulted if str(r.get("resolution")).startswith("recovered")),
                len(faulted),
            ),
            "server_outcomes": dict(
                sorted(
                    Counter(
                        str(r["server_outcome"]) for r in selected if r.get("server_outcome")
                    ).items()
                )
            ),
        }

    calls = [r for r in rows if r.get("method") == "tools/call"]
    return {
        "all": part(rows),
        "tools/call": part(calls),
        "by_tool": {
            tool: part([r for r in calls if r.get("tool") == tool])
            for tool in sorted({str(r.get("tool")) for r in calls})
        },
    }


def _pct(entry: Mapping[str, Any]) -> str:
    if not entry["n"]:
        return "-"
    lo, hi = entry["ci95"]
    return f"{entry['k']}/{entry['n']} ({entry['k'] / entry['n']:.2%}, 95% CI {lo:.2%}-{hi:.2%})"


def render(summary: Mapping[str, Any]) -> str:
    lines: list[str] = []
    for name in ("all", "tools/call"):
        part = summary[name]
        lines.append(f"{name}: {part['invocations']} invocations")
        lines.append(f"  with a fault  {_pct(part['faulted'])}")
        for fault, entry in part["invocations_with_fault"].items():
            lines.append(f"    {fault:<15} {_pct(entry)}")
        lines.append(f"  unresolved    {_pct(part['unresolved'])}")
        lines.append(f"  failed        {_pct(part['failed'])}  (not answered by the server)")
        lines.append(f"  recovered     {_pct(part['recovered_of_faulted'])} of those with a fault")
        lines.append(f"  resolutions   {part['resolutions']}")
        if part["server_outcomes"]:
            lines.append(f"  keyed server  {part['server_outcomes']}")
    lines.append(
        "Only the relay-to-server hop is seen: faults between the client and the relay are not."
    )
    return "\n".join(lines)

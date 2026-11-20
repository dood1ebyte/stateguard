"""
Capture bundles and the report built from them (``stateguard capture`` / ``stateguard report``).

A capture is one relay session, recorded. ``stateguard capture`` runs a relay with a recording
and an audit log written to ``DIR/raw/`` (private: mode 0700), and when the relay exits it
writes a **bundle** to ``DIR/bundle/``: the only thing meant to leave the machine, and only by
the operator's hand (nothing here sends anything anywhere).

A bundle holds:

``recording.jsonl``
    the relay's recording (``stateguard.mcp.recording``), redacted inside the recorder, in mask
    mode with every user-typeable number masked (``mask_numbers``). A bundle is refused for a
    recording that is not.
``invocations.jsonl``
    one line per client request, projected from the relay's ``invocation`` audit records
    (``stateguard.adapters.mcp.incidence``) through an allowlist: method, tool name, attempts,
    faults, resolution, the server's idempotency outcome and the relay-observed milliseconds.
    No JSON-RPC ids, no session ids, no text; a value that is not identifier-shaped is
    replaced. The raw audit log is never copied: it is not redacted.
``manifest.json``
    what the bundle is (provenance, mode, the operator's label, the server's name and version),
    the files' SHA-256, and the stateguard version.
``report.json`` / ``report.txt``
    the report, computed from ``recording.jsonl`` and ``invocations.jsonl`` alone, so
    ``stateguard report --check BUNDLE`` rebuilds it from the bundle and compares.

The report counts per client ``tools/call`` (the denominator is every call the client made,
nothing dropped):

* outcomes: ``succeeded`` (answered, no error), ``tool_error`` (answered with ``isError`` or
  the server's own JSON-RPC error), ``failed`` (not answered by the server: UNKNOWN and the
  other unresolved outcomes, ``not_sent``, ``refused`` ...), and ``unknown`` (the unresolved
  part of ``failed``: UNKNOWN is never success or failure of the tool, and it is a residual
  failure of the call). ``not_successful`` = ``failed`` + ``tool_error``. Rates carry Wilson
  95% intervals;
* retries: calls the relay sent more than once, the extra attempts, and (shadow) the resends
  auto mode would have made;
* latency: the relay-observed time from the client's request to its answer, percentiles
  (nearest rank);
* relay overhead: per call forwarded exactly once, the client-side time minus the server's own
  answer time, from the recording's timestamps (live recordings only);
* denominators: client tool calls in the recording, invocation records, answers, unfinished
  requests, and whether they agree.

What it is not: a capture of one session of one server is evidence about that session. Nothing
here is a rate for any other server, workload or period. A scripted workload is labelled as such
by the operator (``--label``) and is not production traffic.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import time
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from stateguard import __version__
from stateguard.adapters.mcp.evidence import summarize_evidence
from stateguard.adapters.mcp.incidence import (
    INVOCATION_EVENT,
    failed,
    read_records,
    unresolved,
    wilson,
)
from stateguard.adapters.mcp.recording import Direction, Recording, read_recording

BUNDLE_SCHEMA = "stateguard.capture.bundle/1"
REPORT_SCHEMA = "stateguard.capture.report/1"
RECORDING = "recording.jsonl"
INVOCATIONS = "invocations.jsonl"
MANIFEST = "manifest.json"
REPORT = "report.json"
REPORT_TEXT = "report.txt"
#: Answered by the server (the tool may still have reported an error).
_ANSWERED = frozenset({"answered", "recovered"})
_ANSWERED_ERROR = frozenset({"answered_error", "recovered_error"})
#: What a projected invocation keeps, and nothing else.
_IDENTIFIER = re.compile(r"^[A-Za-z0-9_.:/+-]{1,128}$")
_REDACTED = "[redacted]"
_LABEL_MAX = 160
NOT_CLAIMED = (
    "No rate here is a rate for any other server, workload, client or period.",
    "A scripted workload is not production traffic; the label says which this is.",
    "In shadow mode, would_retry counts resends auto mode would have made, not resends made.",
    "UNKNOWN is never counted as success; it is counted as a residual failure of the call.",
    "Latency and overhead are this machine's, under this run's load.",
)


class BundleError(ValueError):
    """A bundle cannot be built, or a bundle on disk is not what its manifest says."""


# -- small helpers -------------------------------------------------------------------------


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rate(k: int, n: int) -> dict[str, Any]:
    low, high = wilson(k, n)
    return {"k": k, "n": n, "rate": (k / n) if n else None, "ci95": [low, high]}


def percentiles(values: Sequence[float]) -> dict[str, Any]:
    """Nearest-rank p50/p90/p95/p99 and max of *values* (milliseconds), rounded to 0.1 ms."""
    ordered = sorted(values)
    out: dict[str, Any] = {"n": len(ordered)}
    for name, q in (("p50", 0.50), ("p90", 0.90), ("p95", 0.95), ("p99", 0.99)):
        if ordered:
            rank = max(1, math.ceil(q * len(ordered)))
            out[name] = round(ordered[rank - 1], 1)
        else:
            out[name] = None
    out["max"] = round(ordered[-1], 1) if ordered else None
    return out


def _identifier(value: Any) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, str) and _IDENTIFIER.match(value):
        return value
    return _REDACTED


def project_invocation(record: Mapping[str, Any]) -> dict[str, Any]:
    """The bundle's view of one invocation record: the allowlisted fields, checked."""
    attempts = record.get("attempts")
    ms = record.get("ms")
    faults = record.get("faults")
    return {
        "method": _identifier(record.get("method")),
        "tool": _identifier(record.get("tool")),
        "attempts": attempts if isinstance(attempts, int) and attempts >= 0 else None,
        "faults": [_identifier(f) for f in faults] if isinstance(faults, list) else [],
        "resolution": _identifier(record.get("resolution")),
        "server_outcome": _identifier(record.get("server_outcome")),
        "ms": (
            ms if isinstance(ms, int | float) and not isinstance(ms, bool) and ms >= 0 else None
        ),
    }


def _check_recording_privacy(recording: Recording) -> None:
    """A bundle carries only a mask-redacted recording with numbers masked, no ground truth."""
    redaction = recording.header.get("redaction") or {}
    if redaction.get("mode") != "mask":
        raise BundleError("the recording is not mask-redacted; a bundle needs mask redaction")
    if "mask_numbers" not in (redaction.get("rules") or []):
        raise BundleError(
            "the recording does not mask numbers; record with --record-mask-numbers "
            "(stateguard capture does)"
        )
    if recording.of_type("fault") or recording.of_type("label"):
        raise BundleError("the recording carries ground-truth records; a capture must not")


# -- the report -----------------------------------------------------------------------------


def _messages(recording: Recording, direction: Direction) -> list[dict[str, Any]]:
    return recording.messages(direction)


def _key(identifier: Any) -> str:
    return json.dumps(identifier, sort_keys=True)


def _overhead(recording: Recording) -> tuple[list[float], dict[str, int]]:
    """
    Per tool call forwarded exactly once under its own id: (client answer - client request) -
    (server answer - forward), from ``diag.t_ms``. Calls sent more than once, answered by the
    relay, or with ids seen more than once are skipped, and counted by reason.
    """

    def times(direction: Direction, kinds: tuple[str, ...]) -> dict[str, list[float]]:
        out: dict[str, list[float]] = {}
        for record in _messages(recording, direction):
            if record.get("kind") not in kinds:
                continue
            t = (record.get("diag") or {}).get("t_ms")
            if isinstance(t, int | float):
                out.setdefault(_key(record.get("id")), []).append(float(t))
        return out

    calls = [
        r
        for r in _messages(recording, Direction.CLIENT_TO_RELAY)
        if r.get("kind") == "request" and r.get("method") == "tools/call"
    ]
    asked = times(Direction.CLIENT_TO_RELAY, ("request",))
    forwarded = times(Direction.RELAY_TO_SERVER, ("request",))
    served = times(Direction.SERVER_TO_RELAY, ("response", "error"))
    answered = times(Direction.RELAY_TO_CLIENT, ("response", "error"))
    values: list[float] = []
    skipped: Counter[str] = Counter()
    for record in calls:
        key = _key(record.get("id"))
        a, f, s, c = asked.get(key), forwarded.get(key), served.get(key), answered.get(key)
        if not a or not c:
            skipped["no_timing" if a is None or c is None else "unanswered"] += 1
        elif len(a) != 1 or len(c) != 1:
            skipped["id_reused"] += 1
        elif not f or not s:
            skipped["not_forwarded_or_not_served"] += 1
        elif len(f) != 1 or len(s) != 1:
            skipped["sent_more_than_once"] += 1
        else:
            values.append((c[0] - a[0]) - (s[0] - f[0]))
    return values, dict(sorted(skipped.items()))


def compute_report(
    recording: Recording,
    invocations: Sequence[Mapping[str, Any]],
    *,
    manifest: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The report, from the bundle's recording and projected invocations only. Deterministic."""
    evidence = summarize_evidence(recording)
    header = recording.header
    client = _messages(recording, Direction.CLIENT_TO_RELAY)
    to_client = _messages(recording, Direction.RELAY_TO_CLIENT)
    requests = [r for r in client if r.get("kind") == "request"]
    tool_calls = [r for r in requests if r.get("method") == "tools/call"]
    call_ids = Counter(_key(r.get("id")) for r in tool_calls)
    answers = [
        r
        for r in to_client
        if r.get("kind") in ("response", "error") and _key(r.get("id")) in call_ids
    ]
    answered_ids = Counter(_key(r.get("id")) for r in answers)
    tool_error_answers = sum(
        1
        for r in answers
        if isinstance((r.get("message") or {}).get("result"), dict)
        and (r["message"]["result"].get("isError") is True)
    )

    records = [i for i in invocations if i.get("method") == "tools/call"]
    by_resolution = Counter(str(i.get("resolution")) for i in records)
    unfinished = by_resolution.get("unfinished", 0)
    n = len(records)
    kinds = [str(i.get("resolution")) for i in records]
    failed_k = sum(1 for k in kinds if failed(k) and k not in _ANSWERED_ERROR)
    unknown_k = sum(1 for k in kinds if unresolved(k))
    server_error_k = sum(1 for k in kinds if k in _ANSWERED_ERROR)
    answered_k = sum(1 for k in kinds if k in _ANSWERED)
    # A tool's error result is an answer (the server ran it and said it failed): count it from
    # the recording, where the result is; it can only be one of the answered calls.
    tool_error_k = min(tool_error_answers, answered_k) + server_error_k
    succeeded_k = answered_k - min(tool_error_answers, answered_k)
    resent = [i for i in records if isinstance(i.get("attempts"), int) and i["attempts"] > 1]
    latencies = [float(i["ms"]) for i in records if isinstance(i.get("ms"), int | float)]
    overhead, overhead_skipped = _overhead(recording)

    denominators: dict[str, Any] = {
        "client_requests": len(requests),
        "client_tool_calls": len(tool_calls),
        "client_tool_call_ids_reused": sum(c - 1 for c in call_ids.values() if c > 1),
        "invocation_records": len(invocations),
        "tool_call_records": n,
        "unfinished": unfinished,
        "tool_calls_answered_to_client": sum(answered_ids.values()),
        "tool_calls_answered_more_than_once": sum(1 for c in answered_ids.values() if c > 1),
    }
    denominators["checks"] = {
        # Every call the client made has exactly one record (an unfinished one included).
        "tool_calls_equal_records": len(tool_calls) == n,
        # Every record that is not unfinished was answered to the client, once.
        "answered_equal_finished_records": sum(answered_ids.values()) == n - unfinished,
        "no_call_answered_twice": denominators["tool_calls_answered_more_than_once"] == 0,
        "recording_complete": recording.complete,
    }
    denominators["checks_hold"] = all(denominators["checks"].values())

    server = (evidence.get("servers") or [{}])[0].get("server_info") or {}
    return {
        "schema": REPORT_SCHEMA,
        "provenance": header.get("provenance"),
        "mode": (header.get("config") or {}).get("mode"),
        "label": (manifest or {}).get("label"),
        "server": {"name": server.get("name"), "version": server.get("version")},
        "started_at": header.get("started_at"),
        "denominators": denominators,
        "calls": {
            "n": n,
            "by_resolution": dict(sorted(by_resolution.items())),
            "by_tool": dict(sorted(Counter(str(i.get("tool")) for i in records).items())),
            "succeeded": _rate(succeeded_k, n),
            "tool_error": _rate(tool_error_k, n),
            "failed": _rate(failed_k, n),
            "unknown": _rate(unknown_k, n),
            "not_successful": _rate(n - succeeded_k, n),
        },
        "retries": {
            "calls_resent": len(resent),
            "extra_attempts": sum(int(i["attempts"]) - 1 for i in resent),
            "would_retry": (evidence.get("proposed") or {}).get("would_retry"),
            "proposals_hypothetical": (evidence.get("proposed") or {}).get("hypothetical"),
        },
        "latency_ms": percentiles(latencies),
        "overhead_ms": {**percentiles(overhead), "skipped": overhead_skipped},
        "integrity": evidence.get("integrity"),
        "wire": evidence.get("wire"),
        "not_claimed": list(NOT_CLAIMED),
    }


def render(report: Mapping[str, Any]) -> str:
    """The report as text."""

    def pct(entry: Mapping[str, Any]) -> str:
        if not entry["n"]:
            return f"{entry['k']}/0"
        low, high = entry["ci95"]
        return f"{entry['k']}/{entry['n']} ({entry['rate']:.1%}, ci {low:.1%}-{high:.1%})"

    calls = report["calls"]
    d = report["denominators"]
    lat = report["latency_ms"]
    over = report["overhead_ms"]
    lines = [
        f"capture     provenance {report['provenance']}, mode {report['mode']}",
        f"label       {report['label']}",
        f"server      {report['server']['name']} {report['server']['version']}",
        "",
        "denominators",
        f"  client tool calls {d['client_tool_calls']}  records {d['tool_call_records']}  "
        f"answered {d['tool_calls_answered_to_client']}  unfinished {d['unfinished']}  "
        f"checks {'hold' if d['checks_hold'] else 'FAIL'} {json.dumps(d['checks'])}",
        "",
        "per call",
        f"  succeeded         {pct(calls['succeeded'])}",
        f"  tool error        {pct(calls['tool_error'])}",
        f"  failed            {pct(calls['failed'])}",
        f"    of which unknown {pct(calls['unknown'])}",
        f"  not successful    {pct(calls['not_successful'])}",
        f"  by resolution     {json.dumps(calls['by_resolution'])}",
        "",
        f"retries     resent {report['retries']['calls_resent']} calls "
        f"(+{report['retries']['extra_attempts']} attempts); would_retry "
        f"{report['retries']['would_retry']}",
        f"latency ms  n {lat['n']}  p50 {lat['p50']}  p90 {lat['p90']}  p95 {lat['p95']}  "
        f"p99 {lat['p99']}  max {lat['max']}",
        f"overhead ms n {over['n']}  p50 {over['p50']}  p90 {over['p90']}  p95 {over['p95']}  "
        f"p99 {over['p99']}  max {over['max']}  skipped {json.dumps(over['skipped'])}",
        "",
        "not claimed",
        *(f"  - {line}" for line in report["not_claimed"]),
    ]
    return "\n".join(lines)


# -- bundles --------------------------------------------------------------------------------


def _clean_label(label: str | None) -> str | None:
    if label is None:
        return None
    text = " ".join(str(label).split())[:_LABEL_MAX]
    return text or None


def build_bundle(
    raw: str | os.PathLike[str],
    out: str | os.PathLike[str],
    *,
    label: str | None = None,
    recording_name: str = RECORDING,
    audit_name: str = "audit.jsonl",
) -> dict[str, Any]:
    """
    Write a bundle to *out* from a capture's *raw* directory (its recording and audit log).
    Returns the report. Refuses (``BundleError``) a recording that is not mask-redacted with
    numbers masked, or that carries ground truth. *out* must not exist yet.
    """
    raw_dir, out_dir = Path(raw), Path(out)
    recording = read_recording(raw_dir / recording_name)
    _check_recording_privacy(recording)
    audit = raw_dir / audit_name
    records = read_records([audit]) if audit.exists() else []
    invocations = [project_invocation(r) for r in records if r.get("event") == INVOCATION_EVENT]
    if out_dir.exists():
        raise BundleError(f"{out_dir} exists; a bundle is never overwritten")
    out_dir.mkdir(parents=True, mode=0o700)
    shutil.copyfile(raw_dir / recording_name, out_dir / RECORDING)
    with (out_dir / INVOCATIONS).open("w", encoding="utf-8") as handle:
        for item in invocations:
            handle.write(json.dumps(item, sort_keys=True) + "\n")
    evidence_servers = summarize_evidence(recording).get("servers") or [{}]
    server = evidence_servers[0].get("server_info") or {}
    files: dict[str, str] = {}
    manifest: dict[str, Any] = {
        "schema": BUNDLE_SCHEMA,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "stateguard_version": __version__,
        "provenance": recording.header.get("provenance"),
        "mode": (recording.header.get("config") or {}).get("mode"),
        "label": _clean_label(label),
        "server": {"name": server.get("name"), "version": server.get("version")},
        "files": files,
    }
    files[RECORDING] = _sha256(out_dir / RECORDING)
    files[INVOCATIONS] = _sha256(out_dir / INVOCATIONS)
    report = compute_report(recording, invocations, manifest=manifest)
    (out_dir / REPORT).write_text(json.dumps(report, indent=1, sort_keys=True) + "\n")
    (out_dir / REPORT_TEXT).write_text(render(report) + "\n")
    files[REPORT] = _sha256(out_dir / REPORT)
    (out_dir / MANIFEST).write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    return report


def read_invocations(path: str | os.PathLike[str]) -> list[dict[str, Any]]:
    out = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").splitlines(), 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            raise BundleError(f"{path}: line {number} is not JSON") from exc
        if not isinstance(item, dict) or project_invocation(item) != item:
            raise BundleError(f"{path}: line {number} is not a projected invocation")
        out.append(item)
    return out


def load_bundle(bundle: str | os.PathLike[str]) -> dict[str, Any]:
    """
    Read a bundle, check its files against the manifest, and rebuild its report from the
    recording and invocations alone. Returns ``{"report", "stored", "rebuilt_equal",
    "manifest"}``. Raises ``BundleError`` for a malformed or altered bundle.
    """
    root = Path(bundle)
    try:
        manifest = json.loads((root / MANIFEST).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BundleError(f"{root}: no readable {MANIFEST}") from exc
    if manifest.get("schema") != BUNDLE_SCHEMA:
        raise BundleError(f"{root}: not a {BUNDLE_SCHEMA} bundle")
    for name, digest in (manifest.get("files") or {}).items():
        path = root / name
        if not path.is_file() or _sha256(path) != digest:
            raise BundleError(f"{root}: {name} does not match the manifest")
    recording = read_recording(root / RECORDING)
    _check_recording_privacy(recording)
    invocations = read_invocations(root / INVOCATIONS)
    report = compute_report(recording, invocations, manifest=manifest)
    stored_path = root / REPORT
    stored = json.loads(stored_path.read_text()) if stored_path.is_file() else None
    return {
        "report": report,
        "stored": stored,
        "rebuilt_equal": stored == json.loads(json.dumps(report)),
        "manifest": manifest,
    }


def find_values(bundle: str | os.PathLike[str], needles: Iterable[str]) -> dict[str, int]:
    """How often each of *needles* appears in the bundle's files (a privacy spot check)."""
    root = Path(bundle)
    texts = [p.read_text(encoding="utf-8", errors="replace") for p in sorted(root.iterdir())]
    return {needle: sum(t.count(needle) for t in texts) for needle in needles}

"""
The bounded approval workflow and its state taxonomy.

Four dependent steps against the fixture sandbox:

``S1 inspect``  read the order (read_text_file)          -> confirms it is pending
``S2 approve``  edit status pending -> approved (edit_file) -> needs S1
``S3 file``     move the order from inbox/ to done/ (move_file) -> needs S2
``S4 log``      insert the approval line before END (edit_file) -> needs S3

Every step has a precondition and a postcondition checked **on disk** by this
module (``fixture.snapshot`` / ``fixture.read``), never from the server's answer
or StateGuard's audit log; and a set of paths it is allowed to change. A step is
classified from two independent things: what the client was told (the wire
outcome) and what actually happened (the snapshot diff). The workflow stops at
the first step that is not ``completed`` / ``recovered``. Nothing here retries;
a "naive retry" baseline exists only as an explicit option of ``run``.

Step states
-----------
not_attempted        never issued (an earlier step stopped the run, or its
                     precondition did not hold)
known_not_executed   the client holds proof the request never reached the
                     server: the relay answered "not sent" / refused it, or the
                     write to the server's pipe failed
failed_reported      the server (or relay) answered with an error result; the
                     client was told it failed (ground truth says what changed)
completed            success answer, valid result, postcondition holds on disk
recovered            completed although the client sent a faulty call (the
                     relay repaired it before execution)
execution_unknown    sent, and no usable answer: the client cannot know whether
                     it executed (ground truth is recorded beside it)
result_invalid       an answer arrived but the result is malformed, breaks the
                     tool's outputSchema, or contradicts the disk
postcondition_violated  a success-looking answer, but the disk does not hold
                     the step's intended outcome (or holds more than it)

Workflow states
---------------
completed / recovered      every step completed (recovered: at least one by repair)
partially_completed_stopped  stopped; the disk equals the checkpoint of the steps
                     the client believes completed -- a clean, resumable stop
not_started_stopped  stopped before any step completed; the disk is untouched
stopped_execution_unknown / stopped_result_invalid / stopped_postcondition_violated
                     stopped at a step in that state; ``disk_checkpoint`` records
                     what an independent inspection finds (the believed
                     checkpoint, or the next one)
failed_unrecovered   the disk matches no checkpoint (a wrong or duplicate
                     effect), or the client was told a step failed or never ran
                     and the disk moved on anyway
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import fixture
from rawclient import RawSession, outcome

STEPS = ("S1", "S2", "S3", "S4")

#: Paths each step may change (relative to the sandbox).
ALLOWED: dict[str, set[str]] = {
    "S1": set(),
    "S2": {fixture.ORDER},
    "S3": {fixture.ORDER, fixture.DONE},
    "S4": {fixture.LOG},
}


def arguments(step: str, sandbox: Path) -> tuple[str, dict[str, Any]]:
    """The correct call for *step*."""
    order, done, log = (str(sandbox / r) for r in (fixture.ORDER, fixture.DONE, fixture.LOG))
    calls: dict[str, tuple[str, dict[str, Any]]] = {
        "S1": ("read_text_file", {"path": order}),
        "S2": (
            "edit_file",
            {
                "path": order,
                "edits": [{"oldText": "status: pending", "newText": "status: approved"}],
            },
        ),
        "S3": ("move_file", {"source": order, "destination": done}),
        "S4": (
            "edit_file",
            {"path": log, "edits": [{"oldText": "END", "newText": f"{fixture.LOG_LINE}\nEND"}]},
        ),
    }
    return calls[step]


def precondition(step: str, sandbox: Path) -> tuple[bool, str]:
    order, done, log = (
        fixture.read(sandbox, r) for r in (fixture.ORDER, fixture.DONE, fixture.LOG)
    )
    if step == "S1":
        return order == fixture.FILES[fixture.ORDER], "order pending in inbox"
    if step == "S2":
        ok = (
            order is not None
            and order.count("status: pending") == 1
            and "status: approved" not in order
        )
        return ok, "order in inbox, pending exactly once"
    if step == "S3":
        return (
            order == fixture.APPROVED_ORDER and done is None,
            "approved order in inbox; done/ slot free",
        )
    return (
        log is not None and fixture.LOG_LINE not in log and done == fixture.APPROVED_ORDER,
        "order filed; not yet logged",
    )


def postcondition(step: str, sandbox: Path) -> tuple[bool, str]:
    """The step's intended outcome holds on disk, and nothing outside it changed."""
    k = STEPS.index(step) + 1
    state = fixture.snapshot(sandbox)
    if state == fixture.checkpoints()[k]:
        return True, f"disk equals checkpoint {k}"
    at = fixture.matching_checkpoint(state)
    return False, f"disk equals checkpoint {at}" if at is not None else "disk matches no checkpoint"


def diff(before: dict[str, str], after: dict[str, str]) -> dict[str, list[str]]:
    return {
        "created": sorted(set(after) - set(before)),
        "deleted": sorted(set(before) - set(after)),
        "modified": sorted(k for k in before.keys() & after.keys() if before[k] != after[k]),
    }


def _changed(d: dict[str, list[str]]) -> set[str]:
    return {p.rstrip("/") for p in d["created"] + d["deleted"] + d["modified"]}


def _text(resp: dict[str, Any] | None) -> str | None:
    result = (resp or {}).get("result")
    if not isinstance(result, dict):
        return None
    for block in result.get("content") or []:
        if isinstance(block, dict) and isinstance(block.get("text"), str):
            return str(block["text"])
    return None


def result_problem(resp: dict[str, Any] | None) -> str | None:
    """A structural problem with a *success* result: not a CallToolResult, or off its outputSchema.

    Every tool of this server declares ``outputSchema`` ``{content: string}`` (required).
    """
    result = (resp or {}).get("result")
    if not isinstance(result, dict) or not isinstance(result.get("content"), list):
        return "not a CallToolResult"
    sc = result.get("structuredContent")
    if not isinstance(sc, dict):
        return "structuredContent absent"
    if not isinstance(sc.get("content"), str):
        return "structuredContent.content is not a string"
    return None


def wire_class(resp: dict[str, Any] | None) -> str:
    """What the client was told, in taxonomy terms (no ground truth used)."""
    w = outcome(resp)
    if w == "not-sent (write failed)":
        return "known_not_executed"
    if w.startswith("no-answer"):
        return "execution_unknown"
    if w.startswith("jsonrpc-error"):
        data = (resp or {}).get("error", {}).get("data") or {}
        state = data.get("io.stateguard/executionState") if isinstance(data, dict) else None
        if state == "not_sent":
            return "known_not_executed"
        if state == "unknown":
            return "execution_unknown"
        return "failed_reported"
    if w == "tool-error (isError)":
        text = _text(resp) or ""
        # The relay's refusals carry no machine-readable marker; only this lead text.
        if text.startswith("The arguments were not sent:"):
            return "known_not_executed"
        return "failed_reported"
    return "success"


@dataclass
class StepRecord:
    step: str
    attempt: int
    tool: str
    arg_shape: dict[str, str]
    pre_ok: bool
    wire: str = "not sent"
    told: str = "not_attempted"
    result_problem: str | None = None
    effect: dict[str, list[str]] = field(default_factory=dict)
    out_of_scope: list[str] = field(default_factory=list)
    post_ok: bool | None = None
    post_note: str = ""
    executed: str = "n/a"  # ground truth: "yes" / "no" / "partial-or-other"
    state: str = "not_attempted"
    response_shape: dict[str, Any] = field(default_factory=dict)
    latency_s: float | None = None


def _shape(args: dict[str, Any]) -> dict[str, str]:
    return {k: type(v).__name__ for k, v in args.items()}


def _response_shape(resp: dict[str, Any] | None) -> dict[str, Any]:
    if resp is None or "_timeout" in resp or "_send_failed" in resp:
        return {}
    if "error" in resp:
        e = resp["error"]
        data = e.get("data")
        return {
            "error_code": e.get("code"),
            "error_data_keys": sorted(data) if isinstance(data, dict) else None,
        }
    r = resp.get("result") or {}
    text = _text(resp) or ""
    return {
        "result_keys": sorted(r),
        "isError": bool(r.get("isError")),
        "text_len": len(text),
        "text_head": fixture.redact(text)[:60],
    }


@dataclass
class Run:
    steps: list[StepRecord]
    state: str
    believed_completed: int
    disk_checkpoint: int | None
    sentinel_ok: bool
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "workflow_state": self.state,
            "believed_completed": self.believed_completed,
            "disk_checkpoint": self.disk_checkpoint,
            "sentinel_ok": self.sentinel_ok,
            "note": self.note,
            "steps": [vars(s) for s in self.steps],
        }


def run(
    connect: Callable[[], RawSession],
    sandbox: Path,
    *,
    overrides: dict[str, tuple[str, dict[str, Any]]] | None = None,
    faulty: set[str] | None = None,
    pause_before: dict[str, float] | None = None,
    naive_retry: bool = False,
    timeout: float = 15.0,
) -> Run:
    """
    Run S1..S4 once through ``connect()``'s session, classifying every step.

    *overrides* replaces a step's call (an injected client-side fault); *faulty*
    names the steps whose call was deliberately faulty (so a verified success
    there is ``recovered``). *naive_retry* is a **baseline**: a conventional
    client that, after an unknown or failed step, reconnects and sends the same
    call once more. StateGuard is never asked to retry anything.
    """
    overrides, faulty, pause_before = overrides or {}, faulty or set(), pause_before or {}
    session = connect()
    session.initialize()
    records: list[StepRecord] = []
    believed = 0
    stop_note = ""
    for step in STEPS:
        tool, args = overrides.get(step) or arguments(step, sandbox)
        pre_ok, pre_note = precondition(step, sandbox)
        rec = StepRecord(step, 1, tool, _shape(args), pre_ok)
        records.append(rec)
        if not pre_ok:
            rec.post_note = f"precondition failed: {pre_note}"
            stop_note = f"{step} precondition"
            break
        if step in pause_before:
            time.sleep(pause_before[step])
        _attempt(session, rec, step, tool, args, sandbox, faulty, timeout)
        if naive_retry and rec.state in ("execution_unknown", "failed_reported"):
            session.close()
            session = connect()
            session.initialize()
            retry = StepRecord(step, 2, tool, _shape(args), precondition(step, sandbox)[0])
            records.append(retry)
            _attempt(session, retry, step, tool, args, sandbox, faulty, timeout)
            rec = retry
        if rec.state in ("completed", "recovered"):
            believed += 1
            continue
        stop_note = f"stopped at {step}: {rec.state}"
        break
    session.close()
    state = fixture.snapshot(sandbox)
    at = fixture.matching_checkpoint(state)
    sentinel_ok = state.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT)
    return Run(records, classify(records, believed, state), believed, at, sentinel_ok, stop_note)


def classify(records: list[StepRecord], believed: int, state: dict[str, str]) -> str:
    """The workflow state, from the step records and the final disk state."""
    clean = state == fixture.checkpoints()[believed]
    at = fixture.matching_checkpoint(state)
    last = records[-1].state if records else "not_attempted"
    if believed == len(STEPS) and clean:
        return "recovered" if any(r.state == "recovered" for r in records) else "completed"
    if at is None:
        return "failed_unrecovered"  # a wrong or duplicate effect: no checkpoint to resume from
    if last in ("execution_unknown", "result_invalid", "postcondition_violated"):
        return f"stopped_{last}"  # the disk checkpoint says what reconciliation would find
    if clean:
        return "partially_completed_stopped" if believed else "not_started_stopped"
    return "failed_unrecovered"  # told it failed / never ran, yet the disk moved on


def _attempt(
    session: RawSession,
    rec: StepRecord,
    step: str,
    tool: str,
    args: dict[str, Any],
    sandbox: Path,
    faulty: set[str],
    timeout: float,
) -> None:
    before = fixture.snapshot(sandbox)
    t0 = time.monotonic()
    resp = session.call(tool, args, rid=f"{step}-{rec.attempt}", timeout=timeout)
    rec.latency_s = round(time.monotonic() - t0, 3)
    time.sleep(0.05)  # let a lost-answer server finish its write before inspecting
    after = fixture.snapshot(sandbox)
    rec.wire = outcome(resp)
    rec.told = wire_class(resp)
    rec.response_shape = _response_shape(resp)
    rec.effect = diff(before, after)
    changed = _changed(rec.effect)
    rec.out_of_scope = sorted(changed - ALLOWED[step])
    rec.post_ok, rec.post_note = postcondition(step, sandbox)
    k = STEPS.index(step) + 1
    if step == "S1":
        rec.executed = "n/a (read)" if before == after else "partial-or-other"
    elif before == after:
        rec.executed = "no"
    elif after == fixture.checkpoints()[k] and before == fixture.checkpoints()[k - 1]:
        rec.executed = "yes"
    else:
        rec.executed = "partial-or-other"
    if rec.told == "success":
        rec.result_problem = result_problem(resp)
        # A read is verified against the disk, not trusted.
        if (
            rec.result_problem is None
            and step == "S1"
            and _text(resp) != fixture.read(sandbox, fixture.ORDER)
        ):
            rec.result_problem = "read text differs from the file on disk"
        if rec.result_problem:
            rec.state = "result_invalid"
        elif rec.post_ok and not rec.out_of_scope:
            rec.state = "recovered" if step in faulty else "completed"
        else:
            rec.state = "postcondition_violated"
    else:
        rec.state = rec.told


def audit_events(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]

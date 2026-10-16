"""
The Week 2 workload: the approval workflow under controlled faults, in three configurations.

usage: workload.py SERVER_BIN WORKSPACE OUT_JSON [--configs a,b] [--live-only]
                   [--record PROVENANCE] [--tee] [TEST_ID ...]

``--record PROVENANCE`` records every relayed run (WORKSPACE/rec/, mask
redaction); ``real_capture`` is accepted by the relay in shadow only.
``--tee`` puts a fault-free ``shim.py`` between relay (or client) and server,
logging digests of every line it passes (WORKSPACE/tee/), for fidelity checks.

Configurations (identical workload, identical fault seeds):

``raw``      client -> [shim ->] server               no StateGuard
``shadow``   client -> relay --shadow -> [shim ->] server
``enforce``  client -> relay (auto) -> [shim ->] server, ``--retry off``, no restart
``restart``  as enforce, plus ``--restart 1`` (default ``idle-only`` policy); one test

Enforce mode changes execution behaviour (repairs, refusals, explicit unknown
answers); it runs only here, on the synthetic sandbox, with retries off. The
expected outcome of every test is written below, before execution; the
harness compares it with what the on-disk ground truth shows.

WORKSPACE must be a private scratch directory. Everything mutable lives in
WORKSPACE/sandbox, rebuilt from the fixture before every run.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession, outcome  # noqa: E402

REPO = HERE.parents[2]
RELAY = REPO / "examples" / "mcp" / "stdio_proxy.py"
SHIM = HERE / "shim.py"
PY = sys.executable

ALL = ("raw", "shadow", "enforce")


def case(
    tid: str, category: str, hypothesis: str, *, configs: tuple[str, ...] = ALL, **kw: Any
) -> dict[str, Any]:
    return {"id": tid, "category": category, "hypothesis": hypothesis, "configs": configs, **kw}


def tests(sb: Path, ws: Path) -> list[dict[str, Any]]:
    order, log, done = (str(sb / r) for r in (fixture.ORDER, fixture.LOG, fixture.DONE))
    approve = [{"oldText": "status: pending", "newText": "status: approved"}]
    partial1 = ("partially_completed_stopped", 1)
    partial2 = ("partially_completed_stopped", 2)
    return [
        # ---- valid controls -------------------------------------------------
        case(
            "V1",
            "valid control",
            "the correct workflow completes and the disk equals the oracle",
            repeat=3,
            live=True,
            stage="none",
            fault_method="none",
            expect={c: ("completed", 4, {}) for c in ALL},
        ),
        # ---- recoverable contract faults (client sends a faulty S2) -----------
        case(
            "C1",
            "recoverable contract fault",
            "S2 edits sent as one object, not an array: the server rejects it; "
            "enforce wraps it (single-value wrap) and the step is recovered",
            live=True,
            stage="pre-execution (client arguments)",
            fault_method="override S2 arguments",
            overrides={"S2": ("edit_file", {"path": order, "edits": approve[0]})},
            faulty={"S2"},
            expect={
                "raw": (*partial1, {"S2": "failed_reported"}),
                "shadow": (*partial1, {"S2": "failed_reported"}),
                "enforce": ("recovered", 4, {"S2": "recovered"}),
            },
        ),
        case(
            "C2",
            "recoverable contract fault",
            "S2 dryRun sent as the string 'false': rejected by the server; "
            "enforce coerces it to false and the edit applies",
            live=True,
            stage="pre-execution (client arguments)",
            fault_method="override S2 arguments",
            overrides={"S2": ("edit_file", {"path": order, "edits": approve, "dryRun": "false"})},
            faulty={"S2"},
            expect={
                "raw": (*partial1, {"S2": "failed_reported"}),
                "shadow": (*partial1, {"S2": "failed_reported"}),
                "enforce": ("recovered", 4, {"S2": "recovered"}),
            },
        ),
        case(
            "C3",
            "recoverable contract fault",
            "S2 edits sent as JSON text: rejected by the server; enforce parses it",
            live=True,
            stage="pre-execution (client arguments)",
            fault_method="override S2 arguments",
            overrides={"S2": ("edit_file", {"path": order, "edits": json.dumps(approve)})},
            faulty={"S2"},
            expect={
                "raw": (*partial1, {"S2": "failed_reported"}),
                "shadow": (*partial1, {"S2": "failed_reported"}),
                "enforce": ("recovered", 4, {"S2": "recovered"}),
            },
        ),
        # ---- unsafe / ambiguous transformations -------------------------------
        case(
            "U1",
            "unsafe transformation held",
            "S3 destination sent as a one-element list: enforce must not "
            "render it into a path; it escalates (not sent); the server rejects it elsewhere",
            live=True,
            stage="pre-execution (client arguments)",
            fault_method="override S3 arguments",
            overrides={"S3": ("move_file", {"source": order, "destination": [done]})},
            faulty={"S3"},
            expect={
                "raw": (*partial2, {"S3": "failed_reported"}),
                "shadow": (*partial2, {"S3": "failed_reported"}),
                "enforce": (*partial2, {"S3": "known_not_executed"}),
            },
        ),
        case(
            "U2",
            "semantic ambiguity (schema-valid)",
            "an edit meant for section [b] whose oldText also matches [a]: "
            "the server applies the first match and reports success; StateGuard has no finding",
            live=True,
            stage="execution (server semantics)",
            fault_method="ambiguous oldText",
            single="ambiguous_edit",
            expect={c: ("postcondition_violated", 0, {}) for c in ALL},
        ),
        # ---- known pre-execution failures ------------------------------------
        case(
            "P1",
            "known pre-execution failure",
            "S2 without the required edits: the server's validation rejects it "
            "(as a tool error); enforce refuses it before sending",
            live=True,
            stage="pre-execution (validation)",
            fault_method="override S2 arguments",
            overrides={"S2": ("edit_file", {"path": order})},
            faulty={"S2"},
            expect={
                "raw": (*partial1, {"S2": "failed_reported"}),
                "shadow": (*partial1, {"S2": "failed_reported"}),
                "enforce": (*partial1, {"S2": "known_not_executed"}),
            },
        ),
        case(
            "P2",
            "known pre-execution failure",
            "S3 destination outside the sandbox: the server denies access "
            "before acting; nothing changes inside or outside the sandbox",
            live=True,
            stage="pre-execution (server path check)",
            fault_method="override S3 arguments",
            overrides={
                "S3": ("move_file", {"source": order, "destination": str(ws / "escaped-order.txt")})
            },
            expect={c: (*partial2, {"S3": "failed_reported"}) for c in ALL},
        ),
        case(
            "P3",
            "interruption before send",
            "the server exits idle after S2's answer; S3 is never delivered and "
            "the client can know it (its write fails)",
            live=False,
            stage="before send (idle server death)",
            fault_method="shim exit_after_response:edit_file:1",
            fault="exit_after_response:edit_file:1",
            pause_before={"S3": 1.0},
            expect={c: (*partial2, {"S3": "known_not_executed"}) for c in ALL},
        ),
        # ---- interruption after send -----------------------------------------
        case(
            "I1",
            "interruption after send",
            "S3 is swallowed and the server exits: execution unknown to the client; "
            "ground truth: not executed",
            live=False,
            stage="after send, before execution",
            fault_method="shim swallow_request:move_file",
            fault="swallow_request:move_file",
            expect={c: ("stopped_execution_unknown", 2, {"S3": "execution_unknown"}) for c in ALL},
        ),
        case(
            "I2",
            "interruption after send",
            "S3 executes, its answer is lost and the server exits: execution unknown "
            "to the client; ground truth: executed",
            live=False,
            stage="after execution, before answer",
            fault_method="shim drop_response:move_file",
            fault="drop_response:move_file",
            expect={c: ("stopped_execution_unknown", 2, {"S3": "execution_unknown"}) for c in ALL},
        ),
        case(
            "I3",
            "interruption after send",
            "S4 (non-idempotent append) executes and its answer is lost",
            live=False,
            stage="after execution, before answer",
            fault_method="shim drop_response:edit_file:2",
            fault="drop_response:edit_file:2",
            expect={c: ("stopped_execution_unknown", 3, {"S4": "execution_unknown"}) for c in ALL},
        ),
        # ---- duplicate side effects under a naive-retry baseline --------------
        case(
            "D1",
            "duplicate side effect",
            "baseline client reconnects and re-sends S4 after I3: the server "
            "succeeds again and the log holds the line twice; no answer reveals it",
            live=False,
            stage="after execution, before answer + client re-send",
            fault_method="I3 + naive retry",
            fault="drop_response:edit_file:2",
            naive_retry=True,
            configs=("raw", "enforce"),
            expect={
                c: ("failed_unrecovered", 3, {"S4": "postcondition_violated"})
                for c in ("raw", "enforce")
            },
        ),
        case(
            "D2",
            "duplicate side effect",
            "baseline client re-sends S3 after I2: 'Destination already exists' -- "
            "told the move failed although it happened",
            live=False,
            stage="after execution, before answer + client re-send",
            fault_method="I2 + naive retry",
            fault="drop_response:move_file",
            naive_retry=True,
            configs=("raw", "enforce"),
            expect={
                c: ("failed_unrecovered", 2, {"S3": "failed_reported"}) for c in ("raw", "enforce")
            },
        ),
        case(
            "D3",
            "duplicate side effect",
            "baseline client re-sends S3 after I1 (not executed): the retry is "
            "correct here -- the same wire evidence as D2 with the opposite right answer",
            live=False,
            stage="after send, before execution + client re-send",
            fault_method="I1 + naive retry",
            fault="swallow_request:move_file",
            naive_retry=True,
            configs=("raw", "enforce"),
            expect={c: ("completed", 4, {}) for c in ("raw", "enforce")},
        ),
        # ---- invalid results ---------------------------------------------------
        case(
            "R1",
            "invalid result",
            "S1's answer is rewritten to say 'approved' (schema-valid): only the "
            "independent read-back catches it",
            live=False,
            stage="result (content)",
            fault_method="shim corrupt_text:read_text_file",
            fault="corrupt_text:read_text_file",
            expect={c: ("stopped_result_invalid", 0, {"S1": "result_invalid"}) for c in ALL},
        ),
        case(
            "R2",
            "invalid result",
            "S2 executes but its structuredContent breaks the outputSchema",
            live=False,
            stage="result (structure)",
            fault_method="shim corrupt_schema:edit_file:1",
            fault="corrupt_schema:edit_file:1",
            expect={c: ("stopped_result_invalid", 1, {"S2": "result_invalid"}) for c in ALL},
        ),
        # ---- success-looking postcondition violation ---------------------------
        case(
            "S1",
            "success-looking postcondition violation",
            "S3 answered 'success' without being executed",
            live=False,
            stage="result (fabricated success)",
            fault_method="shim fake_success:move_file",
            fault="fake_success:move_file",
            expect={
                c: ("stopped_postcondition_violated", 2, {"S3": "postcondition_violated"})
                for c in ALL
            },
        ),
        # ---- restart / reconnect (enforce only, isolated, scoped) --------------
        case(
            "RS1",
            "restart (idle)",
            "the server exits idle after S2; with --restart 1 the relay restarts it, "
            "replays initialize, re-lists, and S3/S4 complete",
            live=False,
            stage="before send (idle server death)",
            fault_method="shim exit_after_response:edit_file:1 (once)",
            fault="exit_after_response:edit_file:1",
            pause_before={"S3": 1.5},
            configs=("restart",),
            expect={"restart": ("completed", 4, {})},
        ),
        case(
            "RS2",
            "restart refused (in flight)",
            "S3's answer is lost with the server; idle-only policy: answered "
            "unknown, no restart, nothing re-sent",
            live=False,
            stage="after execution, before answer",
            fault_method="shim drop_response:move_file (once)",
            fault="drop_response:move_file",
            configs=("restart",),
            expect={"restart": ("stopped_execution_unknown", 2, {"S3": "execution_unknown"})},
        ),
    ]


def command(
    config: str,
    sb: Path,
    ws: Path,
    tag: str,
    fault: str | None,
    record: str | None = None,
    tee: bool = False,
) -> list[str]:
    server = [SERVER, str(sb)]
    if fault:
        server = [PY, str(SHIM), "--fault", fault, "--once", str(ws / "once" / tag), "--", *server]
    elif tee:
        server = [PY, str(SHIM), "--tee", str(ws / "tee" / f"{tag}.jsonl"), "--", *server]
    if config == "raw":
        return server
    relay = [PY, str(RELAY), "--audit", str(ws / "audit" / f"{tag}.jsonl"), "--retry", "off"]
    if record:
        relay += ["--record", str(ws / "rec" / f"{tag}.jsonl"), "--record-provenance", record]
    if config == "shadow":
        relay.append("--shadow")
    if config == "restart":
        relay += ["--restart", "1"]
    return [*relay, "--", *server]


def stateguard_view(events: list[dict[str, Any]], rid: str) -> dict[str, Any]:
    """What the relay decided and observed for one request (names and actions only, no values)."""
    view: dict[str, Any] = {}
    for e in events:
        if e.get("id") != rid:
            continue
        ev = e["event"]
        if ev == "call":
            view["call"] = {
                "action": e.get("action"),
                "auto_action": e.get("auto_action"),
                "proposal": e.get("proposal"),
                "would_modify": e.get("would_modify"),
                "decisions": [
                    (d.get("path"), d.get("decision"), d.get("op"), d.get("severity"))
                    for d in e.get("decisions", [])
                ],
            }
        elif ev == "result_check":
            view["result_check"] = {
                k: e.get(k) for k in ("category", "kind", "valid", "problems") if k in e
            }
        else:
            view.setdefault("events", []).append(
                {k: e.get(k) for k in ("event", "category", "reason", "surfaced") if k in e}
            )
    return view


def lifecycle(events: list[dict[str, Any]]) -> list[str]:
    keep = {
        "server_lost",
        "restart_refused",
        "restart_would_attempt",
        "server_restarted",
        "reinitialized",
        "restart_failed",
        "restart_exhausted",
        "not_sent",
        "transport_closed",
        "response_dropped",
        "response_would_drop",
        "internal_error",
        "call_gated",
    }
    return [e["event"] for e in events if e["event"] in keep]


SETTINGS = "config/settings.ini"
INTENDED_SETTINGS = "[a]\n  mode = draft\n[b]\n  mode = live\n"


def ambiguous_edit(connect: Any, sb: Path) -> dict[str, Any]:
    """U2: intent 'set section [b] to live'; the oldText matches both sections."""
    s = connect()
    s.initialize()
    before = fixture.snapshot(sb)
    resp = s.call(
        "edit_file",
        {
            "path": str(sb / SETTINGS),
            "edits": [{"oldText": "mode = draft", "newText": "mode = live"}],
        },
        rid="U-1",
    )
    s.close()
    after = fixture.snapshot(sb)
    text = fixture.read(sb, SETTINGS)
    told = workflow.wire_class(resp)
    post = text == INTENDED_SETTINGS
    which = (
        "section [a] changed" if text == "[a]\n  mode = live\n[b]\n  mode = draft\n" else "other"
    )
    state = ("completed" if post else "postcondition_violated") if told == "success" else told
    rec = {
        "step": "U",
        "wire": outcome(resp),
        "told": told,
        "effect": workflow.diff(before, after),
        "post_ok": post,
        "post_note": which,
        "state": state,
        "attempt": 1,
    }
    return {
        "workflow_state": state,
        "believed_completed": 0,
        "disk_checkpoint": None,
        "sentinel_ok": after.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
        "note": "",
        "steps": [rec],
    }


def main() -> None:
    global SERVER
    SERVER, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), sys.argv[3]
    rest = sys.argv[4:]
    configs = record = None
    live_only = tee = False
    only: set[str] = set()
    while rest:
        arg = rest.pop(0)
        if arg == "--configs":
            configs = set(rest.pop(0).split(","))
        elif arg == "--record":
            record = rest.pop(0)
        elif arg == "--live-only":
            live_only = True
        elif arg == "--tee":
            tee = True
        else:
            only.add(arg)
    sb = ws / "sandbox"
    for sub in ("audit", "once", "rec", "tee"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    env = {"PATH": os.environ["PATH"], "HOME": str(ws)}
    results: list[dict[str, Any]] = []
    for t in tests(sb, ws):
        if (only and t["id"] not in only) or (live_only and not t["live"]):
            continue
        for config in t["configs"]:
            if configs and config not in configs:
                continue
            for rep in range(t.get("repeat", 1)):
                tag = f"{t['id']}-{config}-{rep}"
                stale_paths = [ws / "audit" / f"{tag}.jsonl", ws / "once" / tag]
                stale_paths += [ws / "rec" / f"{tag}.jsonl"] if record else []
                stale_paths += [ws / "tee" / f"{tag}.jsonl"] if tee else []
                for (
                    stale
                ) in stale_paths:  # only what this pass writes: never another pass's evidence
                    stale.unlink(missing_ok=True)
                fixture.reset(sb)
                assert fixture.snapshot(sb) == fixture.initial()
                argv = command(
                    config, sb, ws, tag, t.get("fault"), record if config != "raw" else None, tee
                )
                sessions: list[RawSession] = []

                def connect(
                    argv: list[str] = argv, sessions: list[RawSession] = sessions
                ) -> RawSession:
                    sessions.append(RawSession(argv, env=env, cwd=str(ws)))
                    return sessions[-1]

                t0 = time.monotonic()
                if t.get("single") == "ambiguous_edit":
                    run = ambiguous_edit(connect, sb)
                else:
                    run = workflow.run(
                        connect,
                        sb,
                        overrides=t.get("overrides"),
                        faulty=t.get("faulty"),
                        pause_before=t.get("pause_before"),
                        naive_retry=t.get("naive_retry", False),
                    ).as_dict()
                elapsed = round(time.monotonic() - t0, 3)
                events = workflow.audit_events(ws / "audit" / f"{tag}.jsonl")
                for st in run["steps"]:
                    st["stateguard"] = stateguard_view(events, f"{st['step']}-{st['attempt']}")
                exp_wf, exp_believed, exp_steps = t["expect"][config]
                actual_steps = {st["step"]: st["state"] for st in run["steps"]}  # last attempt wins
                discrepancies = []
                if run["workflow_state"] != exp_wf:
                    discrepancies.append(f"workflow {run['workflow_state']} != expected {exp_wf}")
                if run["believed_completed"] != exp_believed and t.get("single") is None:
                    discrepancies.append(
                        f"believed {run['believed_completed']} != expected {exp_believed}"
                    )
                for step, exp in exp_steps.items():
                    if actual_steps.get(step) != exp:
                        discrepancies.append(f"{step} {actual_steps.get(step)} != expected {exp}")
                if not run["sentinel_ok"]:
                    discrepancies.append("SENTINEL CHANGED")
                if (ws / "escaped-order.txt").exists():
                    discrepancies.append("ESCAPE FILE EXISTS")
                results.append(
                    {
                        "test": t["id"],
                        "category": t["category"],
                        "hypothesis": t["hypothesis"],
                        "config": config,
                        "rep": rep,
                        "observation": "live"
                        if t["live"]
                        else "simulated fault (shim); server live",
                        "fault_method": t["fault_method"],
                        "fault_stage": t["stage"],
                        "expected": {
                            "workflow": exp_wf,
                            "believed": exp_believed,
                            "steps": exp_steps,
                        },
                        **run,
                        "lifecycle_events": lifecycle(events),
                        "elapsed_s": elapsed,
                        "client_wire": [s.wire for s in sessions],
                        "discrepancies": discrepancies,
                    }
                )
                mark = "ok " if not discrepancies else "DIFF"
                steps = " ".join(
                    f"{s['step']}{'r' if s['attempt'] > 1 else ''}={s['state']}"
                    for s in run["steps"]
                )
                cp = run["disk_checkpoint"]
                wf = run["workflow_state"]
                print(f"{mark} {tag:22} {wf:32} cp={cp} {steps} {discrepancies or ''}")
    fixture.reset(sb)
    Path(out).write_text(json.dumps(results, indent=1, default=str), encoding="utf-8")
    print("runs:", len(results), "discrepancies:", sum(1 for r in results if r["discrepancies"]))


SERVER = ""

if __name__ == "__main__":
    main()

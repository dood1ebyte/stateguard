"""
Real-relay verification of Week 3 reconciliation (isolated; production code unchanged).

usage: relay_audit.py SERVER_BIN WORKSPACE OUT_JSON [--reps N] [--only ID,ID...]

Every trial runs the production relay entry point (``examples/mcp/stdio_proxy.py``
``main()``, with its own ``--effects`` loading) as a subprocess. The server
behind it is ``relay_audit_shim.py`` in front of the pinned
``@modelcontextprotocol/server-filesystem`` -- or, for the executor-termination
cases (group ``T``), ``rollback_server.py``, a test server built to break that
assumption.

Ground truth never comes from the relay: the sandbox is hashed on disk right
after the client gets its answer and again at the end of the trial, and the
shim's delivery log says which requests reached which server process.

Configurations (where the conditions are equivalent):

``reconcile``  the effects file declares a ``reconcile`` block for ``move_file``
``raw``        the same relay flags and fault, no ``reconcile`` block
``naive``      ``raw``, then the client re-sends the call once under a new id
"""

from __future__ import annotations

import json
import os
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession, outcome  # noqa: E402

RELAY = HERE.parents[2] / "examples" / "mcp" / "stdio_proxy.py"
SHIM = HERE / "relay_audit_shim.py"
ROLLBACK = HERE / "rollback_server.py"
PY = sys.executable

SRC_ABSENT = {
    "tool": "read_text_file",
    "arguments": {"path": "$source"},
    "expect": "absent",
    "absent_marker": "ENOENT",
}
DST_PRESENT = {
    "tool": "read_text_file",
    "arguments": {"path": "$destination"},
    "expect": "present",
    "absent_marker": "ENOENT",
}


def effects(kind: str, timeout: float = 5.0) -> dict[str, Any]:
    move: dict[str, Any] = {"name": "move_file", "effect": "non_idempotent"}
    probes = {
        "reconcile": [SRC_ABSENT, DST_PRESENT],
        # The source is read twice: a change between the reads is "inconsistent".
        "reconcile_reread": [SRC_ABSENT, DST_PRESENT, SRC_ABSENT],
        # A reference to an argument move_file does not have: binding fails at call time.
        "reconcile_badref": [{**SRC_ABSENT, "arguments": {"path": "$nope"}}],
        # A probe tool declared read_only that the server does not list.
        "reconcile_unlisted": [{**SRC_ABSENT, "tool": "stat_file"}],
    }.get(kind)
    if probes is not None:
        move["reconcile"] = {"timeout_seconds": timeout, "probes": probes}
    return {
        "tools": [
            {"name": "read_text_file", "effect": "read_only"},
            {"name": "edit_file", "effect": "non_idempotent"},
            *(
                [{"name": "stat_file", "effect": "read_only"}]
                if kind == "reconcile_unlisted"
                else []
            ),
            move,
        ]
    }


# Scenario: id, group, title, shim options, setup, expected postcondition, extras.
#   setup "approved" = checkpoint 2 (order approved in inbox); "filed" = checkpoint 3.
SCENARIOS: list[dict[str, Any]] = [
    # 1 executed, response lost
    {
        "id": "S1",
        "group": 1,
        "title": "executed, answer lost",
        "fault": "drop_response",
        "expect": "verified",
        "configs": ["reconcile", "raw", "naive"],
    },
    # 2 never reached the server
    {
        "id": "S2",
        "group": 2,
        "title": "never delivered",
        "fault": "swallow_request",
        "expect": "not_verified",
        "configs": ["reconcile", "raw", "naive"],
    },
    # 3 executed then undone by another actor
    {
        "id": "S3",
        "group": 3,
        "title": "executed, then undone by another actor",
        "fault": "drop_response",
        "actor": "undo",
        "expect": "not_verified",
        "configs": ["reconcile", "raw"],
    },
    # 4 another actor produced the postcondition
    {
        "id": "S4a",
        "group": 4,
        "title": "never delivered; another actor performed the move",
        "fault": "swallow_request",
        "actor": "other_moves",
        "expect": "verified",
        "configs": ["reconcile", "raw"],
    },
    {
        "id": "S4b",
        "group": 4,
        "title": "postcondition already held; original failed (ENOENT)",
        "fault": "drop_response",
        "setup": "filed",
        "expect": "verified",
        "configs": ["reconcile", "raw"],
    },
    {
        "id": "S4c",
        "group": 4,
        "title": "never delivered; actor deleted source, wrote other content",
        "fault": "swallow_request",
        "actor": "wrong_content",
        "expect": "verified",
        "configs": ["reconcile"],
    },
    {
        "id": "S4d",
        "group": 4,
        "title": "executed; actor then overwrote destination content",
        "fault": "drop_response",
        "actor": "conflict",
        "expect": "verified",
        "configs": ["reconcile"],
    },
    # 5 partial effect
    {
        "id": "S5a",
        "group": 5,
        "title": "partial: copied, source kept",
        "fault": "swallow_request",
        "actor": "partial_copy",
        "expect": "not_verified",
        "configs": ["reconcile"],
    },
    {
        "id": "S5b",
        "group": 5,
        "title": "partial: source deleted, nothing written",
        "fault": "swallow_request",
        "actor": "partial_delete",
        "expect": "not_verified",
        "configs": ["reconcile"],
    },
    {
        "id": "S5c",
        "group": 5,
        "title": "executed; actor restored source (both exist)",
        "fault": "drop_response",
        "actor": "restore_source",
        "expect": "not_verified",
        "configs": ["reconcile"],
    },
    # 6 stale / inconsistent / malformed / contradictory / lying probe
    {
        "id": "S6a",
        "group": 6,
        "title": "probe answers JSON-RPC error",
        "fault": "drop_response",
        "probe": "rpc_error",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S6b",
        "group": 6,
        "title": "probe answers malformed result",
        "fault": "drop_response",
        "probe": "malformed",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S6c",
        "group": 6,
        "title": "state changes between probes, re-read config (inconsistent)",
        "fault": "drop_response",
        "probe": "flip:restore_source",
        "kind": "reconcile_reread",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S6d",
        "group": 6,
        "title": "state changes between probes, default config (TOCTOU)",
        "fault": "drop_response",
        "probe": "flip:restore_source",
        "expect": "verified",
        "configs": ["reconcile"],
    },
    {
        "id": "S6e",
        "group": 6,
        "title": "probe server lies (claims post-state); never delivered",
        "fault": "swallow_request",
        "probe": "lie_post",
        "expect": "verified",
        "configs": ["reconcile"],
    },
    # 7 probe timeout or failure
    {
        "id": "S7a",
        "group": 7,
        "title": "probe never answered (timeout 1 s)",
        "fault": "drop_response",
        "probe": "hang",
        "timeout": 1.0,
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S7b",
        "group": 7,
        "title": "probe server dies mid-probe",
        "fault": "drop_response",
        "probe": "die",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    # 8 executor still running / executes late (deadline unknown, server stays up)
    {
        "id": "S8a",
        "group": 8,
        "title": "executes late (delay 3 s, deadline 1 s)",
        "fault": "delay:3",
        "deadline": 1.0,
        "late_wait": 4.0,
        "expect": "deadline",
        "configs": ["reconcile", "raw"],
    },
    {
        "id": "S8b",
        "group": 8,
        "title": "never answered (hang, deadline 1 s)",
        "fault": "hang",
        "deadline": 1.0,
        "expect": "deadline",
        "configs": ["reconcile"],
    },
    # 9 restart fails
    {
        "id": "S9",
        "group": 9,
        "title": "restart fails (every restarted process exits 3)",
        "fault": "drop_response",
        "fail_restart": True,
        "expect": "undetermined",
        "configs": ["reconcile", "raw"],
        "timeout_s": 120.0,
    },
    # 10 shadow under equivalent faults
    {
        "id": "S10a",
        "group": 10,
        "title": "shadow, executed, answer lost",
        "fault": "drop_response",
        "shadow": True,
        "expect": "shadow",
        "configs": ["reconcile"],
    },
    {
        "id": "S10b",
        "group": 10,
        "title": "shadow, never delivered",
        "fault": "swallow_request",
        "shadow": True,
        "expect": "shadow",
        "configs": ["reconcile"],
    },
    {
        "id": "S10c",
        "group": 10,
        "title": "shadow, late (deadline 1 s)",
        "fault": "delay:3",
        "deadline": 1.0,
        "late_wait": 4.0,
        "shadow": True,
        "expect": "shadow",
        "configs": ["reconcile"],
    },
    # 11 verified, then more client traffic on the same session
    {
        "id": "S11",
        "group": 11,
        "title": "verified, then a follow-up edit_file and read",
        "fault": "drop_response",
        "expect": "verified",
        "followup": True,
        "configs": ["reconcile"],
    },
    # 12 edge cases from the audit
    {
        "id": "S12a",
        "group": 12,
        "title": "client request sent while reconciliation runs",
        "fault": "drop_response",
        "expect": "verified",
        "concurrent": True,
        "configs": ["reconcile"],
    },
    {
        "id": "S12b",
        "group": 12,
        "title": "client id uses the probe id prefix",
        "fault": None,
        "expect": "no_fault",
        "colliding_id": True,
        "configs": ["reconcile"],
    },
    {
        "id": "S12c",
        "group": 12,
        "title": "probe references an argument the call lacks",
        "fault": "drop_response",
        "kind": "reconcile_badref",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S12d",
        "group": 12,
        "title": "probe tool not in the server's listing",
        "fault": "drop_response",
        "kind": "reconcile_unlisted",
        "expect": "undetermined",
        "configs": ["reconcile"],
    },
    {
        "id": "S12e",
        "group": 12,
        "title": "restarted server is up but never lists its tools (hold-time bound)",
        "fault": "drop_response",
        "no_listing": True,
        "expect": "undetermined",
        "configs": ["reconcile"],
        "timeout_s": 200.0,
    },
    # T: executor termination (test server, not the pinned filesystem server)
    {
        "id": "T1",
        "group": 8,
        "title": "rollback by detached descendant after the probe",
        "server": "detached",
        "rollback_after": 4.0,
        "late_wait": 6.0,
        "expect": "verified",
        "configs": ["reconcile"],
    },
    {
        "id": "T2",
        "group": 8,
        "title": "move by detached descendant after the probe",
        "server": "detached_late",
        "rollback_after": 4.0,
        "late_wait": 6.0,
        "expect": "not_verified",
        "configs": ["reconcile"],
    },
    {
        "id": "T3",
        "group": 8,
        "title": "direct child survives stdout close (main() stops it)",
        "server": "survivor",
        "rollback_after": 4.0,
        "late_wait": 6.0,
        "expect": "verified",
        "configs": ["reconcile"],
    },
]

LEAK_MARKERS = ("status: approved", "status: pending", "order: 001", "forged")


def _events(path: Path) -> list[dict[str, Any]]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def predicate(state: dict[str, str]) -> bool:
    """The configured postcondition, read from disk: source absent, destination exists."""
    return fixture.ORDER not in state and fixture.DONE in state


def run(server: str, ws: Path, sc: dict[str, Any], config: str, rep: int) -> dict[str, Any]:
    tag = f"{sc['id']}-{config}-{rep}"
    trial = ws / "trials" / tag
    if trial.exists():
        for f in trial.iterdir():
            f.unlink()
    trial.mkdir(parents=True, exist_ok=True)
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    if sc.get("setup") == "filed":
        (sb / fixture.ORDER).rename(sb / fixture.DONE)
    before = fixture.snapshot(sb)
    kind = sc.get("kind", "reconcile") if config == "reconcile" else "raw"
    effects_file = trial / "effects.json"
    effects_file.write_text(json.dumps(effects(kind, sc.get("timeout", 5.0))), encoding="utf-8")
    audit = trial / "audit.jsonl"
    relay = [
        PY,
        str(RELAY),
        "--audit",
        str(audit),
        "--effects",
        str(effects_file),
        "--retry",
        "off",
    ]
    if sc.get("shadow"):
        relay.append("--shadow")
    else:
        relay += ["--restart", "2", "--restart-policy", "after-unknown"]
    if sc.get("deadline"):
        relay += ["--call-deadline", str(sc["deadline"])]
    if sc.get("server"):
        backend = [
            PY,
            str(ROLLBACK),
            "--state",
            str(trial),
            "--sandbox",
            str(sb),
            "--mode",
            sc["server"],
            "--rollback-after",
            str(sc["rollback_after"]),
        ]
        log = trial / "events.jsonl"
    else:
        backend = [PY, str(SHIM), "--state", str(trial), "--sandbox", str(sb)]
        if sc.get("fault"):
            backend += ["--fault", sc["fault"]]
        if sc.get("actor"):
            backend += ["--actor-on-restart", sc["actor"]]
        if sc.get("probe"):
            backend += ["--probe-mode", sc["probe"]]
        if sc.get("fail_restart"):
            backend.append("--fail-restart")
        if sc.get("no_listing"):
            backend.append("--no-listing-on-restart")
        backend += ["--", server, str(sb)]
        log = trial / "deliveries.jsonl"
    cmd = [*relay, "--", *backend]
    session = RawSession(cmd, env={"PATH": os.environ["PATH"], "HOME": str(ws)}, cwd=str(ws))
    session.initialize()
    tool, args = workflow.arguments("S3", sb)
    extra: dict[str, Any] = {}
    t0 = time.monotonic()
    if sc.get("colliding_id"):
        response = session.call(
            "read_text_file",
            {"path": str(sb / fixture.ORDER)},
            rid="stateguard-reconcile:1",
            timeout=15.0,
        )
        extra["colliding_id_answered"] = isinstance(response, dict) and "_timeout" not in response
        response = session.call(tool, args, rid="T-1", timeout=30.0)
    elif sc.get("concurrent"):
        session.send(
            {
                "jsonrpc": "2.0",
                "id": "T-1",
                "method": "tools/call",
                "params": {"name": tool, "arguments": args},
            }
        )
        time.sleep(0.2)
        session.send(
            {
                "jsonrpc": "2.0",
                "id": "T-2",
                "method": "tools/call",
                "params": {"name": "read_text_file", "arguments": {"path": str(sb / fixture.LOG)}},
            }
        )
        got: dict[Any, Any] = {}
        deadline = time.monotonic() + 60
        while len(got) < 2 and time.monotonic() < deadline:
            item = session._lines.get(timeout=max(0.1, deadline - time.monotonic()))
            if item is None:
                break
            if item.get("id") in ("T-1", "T-2") and "method" not in item:
                got[item["id"]] = (item, round(time.monotonic() - t0, 3))
        response = got.get("T-1", (None, 0))[0]
        extra["concurrent_T2"] = outcome(got["T-2"][0]) if "T-2" in got else "no answer"
        extra["concurrent_T2_latency_s"] = got["T-2"][1] if "T-2" in got else None
        extra["T1_latency_s"] = got["T-1"][1] if "T-1" in got else None
    else:
        response = session.call(tool, args, rid="T-1", timeout=sc.get("timeout_s", 60.0))
    elapsed = round(time.monotonic() - t0, 3)
    at_answer = fixture.snapshot(sb)
    if sc.get("followup") and isinstance(response, dict) and "error" in response:
        log_tool, log_args = workflow.arguments("S4", sb)
        r2 = session.call(log_tool, log_args, rid="T-2", timeout=15.0)
        r3 = session.call("read_text_file", {"path": str(sb / fixture.DONE)}, rid="T-3")
        extra["followup_edit"] = outcome(r2)
        extra["followup_read"] = outcome(r3)
    if config == "naive" and isinstance(response, dict) and "error" in response:
        r2 = session.call(tool, args, rid="T-1-resend", timeout=30.0)
        extra["naive_resend"] = outcome(r2)
        extra["naive_resend_is_error"] = workflow.wire_class(r2)
    if sc.get("late_wait"):
        time.sleep(max(0.0, t0 + sc["late_wait"] - time.monotonic()))
    session.close()
    final = fixture.snapshot(sb)
    events = _events(log)
    audit_events = _events(audit)
    audit_text = audit.read_text() if audit.exists() else ""
    data = (response or {}).get("error", {}).get("data", {}) if isinstance(response, dict) else {}
    post = data.get("io.stateguard/postcondition")
    probes = [e for e in audit_events if e.get("event") == "reconcile_probe"]
    probe_failures = [p for p in probes if p.get("status") not in ("satisfied", "unsatisfied")]
    if sc.get("server"):
        delivered = [e for e in events if e["event"] == "moved"]
        processes = sum(1 for e in events if e["event"] == "started")
        later_writes = [e for e in delivered if e.get("gen", 0) >= 1]
        original_deliveries = sum(1 for e in delivered if e.get("gen") == 0) + sum(
            1 for e in events if e["event"] == "moved_late"
        )
        late_effects = [e["event"] for e in events if e["event"] in ("rolled_back", "moved_late")]
        original_answered_ok = any(e["event"] == "moved" and e.get("gen") == 0 for e in delivered)
    else:
        processes = sum(1 for e in events if e["event"] == "started")
        original_deliveries = sum(
            1 for e in events if e["event"] == "delivered" and e.get("id") == "T-1"
        )
        later_writes = [
            e
            for e in events
            if e["event"] == "delivered"
            and e.get("gen", 0) >= 1
            and e.get("tool") not in ("read_text_file",)
            and e.get("id") not in ("T-1-resend", "T-2", "T-3")
        ]
        late_effects = []
        original_answered_ok = any(
            e["event"] == "answered" and e.get("id") == "T-1" and not e.get("is_error")
            for e in events
        )
    resend_deliveries = sum(
        1 for e in events if e.get("event") == "delivered" and e.get("id") == "T-1-resend"
    )
    # A follow-up edit that succeeded takes a correct run on to checkpoint 4.
    intended = fixture.checkpoints()[4 if extra.get("followup_edit") == "success" else 3]
    result = {
        "scenario": sc["id"],
        "group": sc["group"],
        "config": config,
        "rep": rep,
        "wire": outcome(response),
        "error_code": (response or {}).get("error", {}).get("code")
        if isinstance(response, dict)
        else None,
        "postcondition": post,
        "execution_state": data.get("io.stateguard/executionState"),
        "attribution": data.get("io.stateguard/attribution"),
        "reasons": (data.get("io.stateguard/reconciliation") or {}).get("reasons"),
        "latency_s": elapsed,
        "server_processes": processes,
        "probe_count": len(probes),
        "probe_failures": len(probe_failures),
        "probe_statuses": [p.get("status") for p in probes],
        # Ground truth
        "original_deliveries": original_deliveries,
        "original_answered_ok_by_server": original_answered_ok,
        "later_generation_writes": len(later_writes),
        "resend_deliveries": resend_deliveries,
        "late_effects": late_effects,
        "predicate_before": predicate(before),
        "predicate_at_answer": predicate(at_answer),
        "intended_at_answer": at_answer == fixture.checkpoints()[3],
        "predicate_final": predicate(final),
        "intended_final": final == intended,
        "final_checkpoint": fixture.matching_checkpoint(final),
        "sentinel_unchanged": final.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
        "audit_leaks_content": any(m in audit_text.lower() for m in LEAK_MARKERS),
        "audit_has_probe_text": "enoent" in audit_text.lower(),
        **extra,
    }
    result["classification"] = classify(sc, result)
    return result


def classify(sc: dict[str, Any], r: dict[str, Any]) -> str:
    post = r["postcondition"]
    if post == "verified":
        if not r["predicate_at_answer"]:
            return "false_verified_predicate_false_at_answer"
        if not r["predicate_final"]:
            return "false_verified_reverted_after_answer"
        if not r["intended_final"]:
            return "verified_predicate_only_intent_wrong"
        if r["original_deliveries"] == 0 or not r["original_answered_ok_by_server"]:
            return "verified_true_original_not_cause"
        return "true_verified"
    if post == "not_verified":
        if r["late_effects"] == ["moved_late"]:
            return "not_verified_then_original_executed_late"
        return "not_verified_correct" if not r["predicate_at_answer"] else "false_not_verified"
    if post == "undetermined":
        return "unresolved"
    if r["execution_state"] == "unknown":
        return "unknown_no_postcondition"
    return "wire:" + str(r["wire"])


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    keys = sorted({(r["scenario"], r["config"]) for r in results})
    for sid, config in keys:
        rs = [r for r in results if r["scenario"] == sid and r["config"] == config]
        lat = sorted(r["latency_s"] for r in rs)
        classes: dict[str, int] = {}
        for r in rs:
            classes[r["classification"]] = classes.get(r["classification"], 0) + 1
        out[f"{sid}/{config}"] = {
            "trials": len(rs),
            "classes": classes,
            "postconditions": sorted({str(r["postcondition"]) for r in rs}),
            "duplicates": sum(r["original_deliveries"] > 1 for r in rs),
            "relay_resends": sum(r["later_generation_writes"] > 0 for r in rs),
            "client_resends_delivered": sum(r["resend_deliveries"] for r in rs),
            "probes": sum(r["probe_count"] for r in rs),
            "probe_failures": sum(r["probe_failures"] for r in rs),
            "latency_median_s": round(statistics.median(lat), 3),
            "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 3),
            "execution_claims": sum(r["execution_state"] not in (None, "unknown") for r in rs),
            "attribution_claims": sum(r["attribution"] not in (None, "unknown") for r in rs),
            "audit_leaks": sum(r["audit_leaks_content"] for r in rs),
            "sentinel_changed": sum(not r["sentinel_unchanged"] for r in rs),
        }
    return out


def config_validation(server: str, ws: Path) -> list[dict[str, Any]]:
    """Invalid probe configurations must stop main() before any server starts."""
    base = effects("reconcile")
    move = base["tools"][2]

    def variant(**change: Any) -> dict[str, Any]:
        spec = json.loads(json.dumps(base))
        spec["tools"][2]["reconcile"].update(change)
        return dict(spec)

    def probe_variant(**change: Any) -> dict[str, Any]:
        spec = json.loads(json.dumps(base))
        spec["tools"][2]["reconcile"]["probes"][0].update(change)
        return dict(spec)

    not_read_only = json.loads(json.dumps(base))
    not_read_only["tools"][0]["effect"] = "idempotent"
    undeclared = json.loads(json.dumps(base))
    del undeclared["tools"][0]
    self_probe = probe_variant(tool="move_file")
    cases: list[tuple[str, dict[str, Any], list[str]]] = [
        ("valid", base, ["--restart", "2", "--restart-policy", "after-unknown"]),
        ("no_restart", base, []),
        ("idle_only_policy", base, ["--restart", "2"]),
        (
            "probe_tool_not_read_only",
            not_read_only,
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "probe_tool_undeclared",
            undeclared,
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "probe_is_the_call_itself",
            self_probe,
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "literal_argument",
            probe_variant(arguments={"path": "/etc/passwd"}),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "unknown_ref",
            probe_variant(arguments={"path": "$nope"}),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "five_probes",
            variant(probes=[SRC_ABSENT] * 5),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "zero_probes",
            variant(probes=[]),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "timeout_zero",
            variant(timeout_seconds=0),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "timeout_31",
            variant(timeout_seconds=31),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "resend_key",
            variant(resend=True),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "unknown_key",
            variant(compensate="x"),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "absent_without_marker",
            probe_variant(absent_marker=None),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "unknown_expect",
            probe_variant(expect="maybe"),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        (
            "reconcile_on_read_only_tool",
            _reconcile_on_read_only(base),
            ["--restart", "2", "--restart-policy", "after-unknown"],
        ),
        ("shadow_with_reconcile", base, ["--shadow"]),
    ]
    del move
    out = []
    for name, spec, flags in cases:
        path = ws / "config-cases" / f"{name}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec), encoding="utf-8")
        marker = ws / "config-cases" / f"{name}.started"
        marker.unlink(missing_ok=True)
        # The "server" only touches a marker: if it ran, main() started a server.
        server_cmd = [PY, "-c", f"open({str(marker)!r}, 'w').close()"]
        proc = subprocess.run(
            [PY, str(RELAY), "--effects", str(path), "--retry", "off", *flags, "--", *server_cmd],
            input=b"",
            capture_output=True,
            timeout=30,
            env={"PATH": os.environ["PATH"]},
        )
        err = proc.stderr.decode("utf-8", "replace").strip().splitlines()
        out.append(
            {
                "case": name,
                "exit_code": proc.returncode,
                "server_started": marker.exists(),
                "message": next((ln for ln in err if "error:" in ln), "")[-200:],
            }
        )
    return out


def _reconcile_on_read_only(base: dict[str, Any]) -> dict[str, Any]:
    spec = json.loads(json.dumps(base))
    spec["tools"][0]["reconcile"] = spec["tools"][2]["reconcile"]
    return dict(spec)


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    argv = sys.argv[4:]
    reps = int(argv[argv.index("--reps") + 1]) if "--reps" in argv else 5
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    ws.mkdir(parents=True, exist_ok=True)
    results = []
    for sc in SCENARIOS:
        if only and sc["id"] not in only:
            continue
        for config in sc["configs"]:
            for rep in range(reps):
                r = run(server, ws, sc, config, rep)
                results.append(r)
                print(
                    f"{r['scenario']:5} {config:9} r{rep} {r['classification']:42} "
                    f"post={r['postcondition']!s:12} wire={r['wire']:24} "
                    f"t={r['latency_s']}s probes={r['probe_count']} "
                    f"orig={r['original_deliveries']} procs={r['server_processes']}",
                    flush=True,
                )
    validation = config_validation(server, ws) if not only or "CONFIG" in only else []
    for v in validation:
        print(
            f"config {v['case']:28} exit={v['exit_code']} started={v['server_started']} "
            f"{v['message']}"
        )
    fixture.reset(ws / "sandbox")
    out.write_text(
        json.dumps(
            {"results": results, "summary": summarize(results), "config_validation": validation},
            indent=1,
        ),
        encoding="utf-8",
    )
    print("trials:", len(results))


if __name__ == "__main__":
    main()

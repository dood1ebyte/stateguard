"""
Real-relay verification of reconciliation contract v2 (Week 3; isolated tool).

usage: contract_v2_relay.py SERVER_BIN WORKSPACE OUT_JSON [--reps N] [--only ID,...]

Runs the pinned ``@modelcontextprotocol/server-filesystem`` behind the audit's
fault shim (``relay_audit_shim.py``, unchanged), the audit's termination test
server (``rollback_server.py``, unchanged) or ``handoff_server.py``, through:

``cli_refusal``  ``stdio_proxy.py main()`` with ``reconcile`` blocks: must refuse at startup
``cli_raw``      ``main()`` without ``reconcile`` blocks (the raw-execution baseline)
``cli_naive``    ``cli_raw``, then the client re-sends once under a new id (retry baseline)
``emb_direct``   ``embedded_relay.py --scope direct-child``: what ``main()`` can see
``emb_pidns``    ``embedded_relay.py --scope pid-namespace``: a kernel-enforced executor scope

Ground truth is the sandbox on disk (hashed at the answer and at the end) and
the shim's or servers' own logs -- never the relay's answer or audit log. The
client records every line the relay writes until end of stream, so responses
are counted exactly, and an answer that only came when the client closed is
told apart from one that came while the session was open.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import statistics
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import relay_audit  # noqa: E402
import workflow  # noqa: E402

PY = sys.executable
RELAY = relay_audit.RELAY
EMBEDDED = HERE / "embedded_relay.py"
HANDOFF = HERE / "handoff_server.py"
APPROVED_SHA = hashlib.sha256(fixture.APPROVED_ORDER.encode("utf-8")).hexdigest()

CONTENT_PROBES = [
    relay_audit.SRC_ABSENT,
    {
        "tool": "read_text_file",
        "arguments": {"path": "$destination"},
        "expect": {"text_sha256_equals": APPROVED_SHA},
        "absent_marker": "ENOENT",
    },
]


def effects(kind: str, timeout: float = 5.0) -> dict[str, Any]:
    if kind != "reconcile_content":
        return relay_audit.effects(kind, timeout)
    spec = relay_audit.effects("reconcile", timeout)
    spec["tools"][-1]["reconcile"]["probes"] = CONTENT_PROBES
    return spec


EMB = ["emb_direct", "emb_pidns"]


def _scenarios() -> list[dict[str, Any]]:
    out = []
    for sc in relay_audit.SCENARIOS:
        configs = []
        if "reconcile" in sc["configs"]:
            configs += EMB
        configs += [f"cli_{c}" for c in ("raw", "naive") if c in sc["configs"]]
        out.append({**sc, "configs": configs})
    content = {"kind": "reconcile_content", "group": 13, "configs": EMB}
    out += [
        {**content, "id": "C1", "title": "content check: executed, answer lost",
         "fault": "drop_response"},
        {**content, "id": "C2", "title": "content check: actor wrote other content",
         "fault": "swallow_request", "actor": "wrong_content"},
        {**content, "id": "C3", "title": "content check: executed, then content overwritten",
         "fault": "drop_response", "actor": "conflict"},
        {**content, "id": "C4", "title": "content check: state already held, original failed",
         "fault": "drop_response", "setup": "filed"},
        {**content, "id": "C5", "title": "content check: both source and destination exist",
         "fault": "drop_response", "actor": "restore_source"},
        {"id": "X1", "group": 14, "title": "effect handed to an external worker",
         "handoff": True, "configs": EMB, "late_wait": 1.0},
        {"id": "SH", "group": 10, "title": "CLI shadow, executed, answer lost (no reconcile)",
         "fault": "drop_response", "shadow": True, "configs": ["cli_raw"]},
    ]  # fmt: skip
    return out


class Client:
    """Every line the relay writes, with arrival time; until end of stream."""

    def __init__(self, argv: list[str], env: dict[str, str], cwd: str) -> None:
        self.proc = subprocess.Popen(
            argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=env, cwd=cwd,
        )  # fmt: skip
        self.lines: queue.Queue[tuple[float, dict[str, Any] | None]] = queue.Queue()
        self.seen: list[tuple[float, dict[str, Any]]] = []
        threading.Thread(target=self._read, daemon=True).start()
        threading.Thread(target=self._drain_err, daemon=True).start()

    def _read(self) -> None:
        assert self.proc.stdout is not None
        for raw in self.proc.stdout:
            try:
                msg = json.loads(raw)
            except ValueError:
                continue
            self.lines.put((time.monotonic(), msg))
        self.lines.put((time.monotonic(), None))

    def _drain_err(self) -> None:
        assert self.proc.stderr is not None
        for _ in self.proc.stderr:
            pass

    def send(self, msg: dict[str, Any]) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(json.dumps(msg).encode() + b"\n")
        self.proc.stdin.flush()

    def wait_for(self, rid: Any, timeout: float) -> dict[str, Any] | None:
        for _, msg in self.seen:
            if msg.get("id") == rid and "method" not in msg:
                return msg
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                at, item = self.lines.get(timeout=max(0.05, deadline - time.monotonic()))
            except queue.Empty:
                break
            if item is None:
                self.lines.put((at, None))
                return None
            msg = item
            self.seen.append((at, msg))
            if "method" in msg and "id" in msg:  # a server->client request: answer it empty
                self.send({"jsonrpc": "2.0", "id": msg["id"], "result": {}})
            if msg.get("id") == rid and "method" not in msg:
                return msg
        return None

    def request(self, rid: Any, method: str, params: dict[str, Any], timeout: float) -> Any:
        self.send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        return self.wait_for(rid, timeout)

    def close_and_drain(self, timeout: float = 30.0) -> float:
        """Close our side; read everything until EOF. Returns when we closed."""
        closed_at = time.monotonic()
        if self.proc.stdin is not None:
            self.proc.stdin.close()
        deadline = closed_at + timeout
        while time.monotonic() < deadline:
            try:
                at, msg = self.lines.get(timeout=0.5)
            except queue.Empty:
                if self.proc.poll() is not None and self.lines.empty():
                    break
                continue
            if msg is None:
                break
            self.seen.append((at, msg))
        try:
            self.proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        return closed_at


def predicate(state: dict[str, str], kind: str) -> bool:
    """The configured predicate, evaluated on disk."""
    moved = fixture.ORDER not in state and fixture.DONE in state
    if kind == "reconcile_content":
        return moved and state.get(fixture.DONE) == fixture.digest(fixture.APPROVED_ORDER)
    return moved


def _iso(ts: str | None) -> float | None:
    return datetime.fromisoformat(ts).timestamp() if ts else None


def trial(server: str, ws: Path, sc: dict[str, Any], config: str, rep: int) -> dict[str, Any]:
    tag = f"{sc['id']}-{config}-{rep}"
    tdir = ws / "trials" / tag
    tdir.mkdir(parents=True, exist_ok=True)
    for f in tdir.iterdir():
        if f.is_file():
            f.unlink()
    sb = ws / "sandbox"
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    if sc.get("setup") == "filed":
        (sb / fixture.ORDER).rename(sb / fixture.DONE)
    before = fixture.snapshot(sb)
    kind = "raw" if config.startswith("cli_") else sc.get("kind", "reconcile")
    effects_file = tdir / "effects.json"
    effects_file.write_text(json.dumps(effects(kind, sc.get("timeout", 5.0))), encoding="utf-8")
    audit = tdir / "audit.jsonl"
    if config.startswith("cli_"):
        relay = [PY, str(RELAY), "--audit", str(audit), "--effects", str(effects_file),
                 "--retry", "off"]  # fmt: skip
        relay += ["--shadow"] if sc.get("shadow") else [
            "--restart", "2", "--restart-policy", "after-unknown"]  # fmt: skip
    else:
        scope = "pid-namespace" if config == "emb_pidns" else "direct-child"
        relay = [PY, str(EMBEDDED), "--scope", scope, "--audit", str(audit),
                 "--effects", str(effects_file)]  # fmt: skip
        if sc.get("shadow"):
            relay.append("--shadow")
    if sc.get("deadline"):
        relay += ["--call-deadline", str(sc["deadline"])]
    worker = None
    if sc.get("handoff"):
        backend = [PY, str(HANDOFF), "serve", "--state", str(tdir), "--sandbox", str(sb)]
        worker = subprocess.Popen([PY, str(HANDOFF), "worker", "--state", str(tdir)])
        log = tdir / "events.jsonl"
    elif sc.get("server"):
        backend = [PY, str(relay_audit.ROLLBACK), "--state", str(tdir), "--sandbox", str(sb),
                   "--mode", sc["server"],
                   "--rollback-after", str(sc["rollback_after"])]  # fmt: skip
        log = tdir / "events.jsonl"
    else:
        backend = [PY, str(relay_audit.SHIM), "--state", str(tdir), "--sandbox", str(sb)]
        for flag, key in (("--fault", "fault"), ("--actor-on-restart", "actor"),
                          ("--probe-mode", "probe")):  # fmt: skip
            if sc.get(key):
                backend += [flag, sc[key]]
        if sc.get("fail_restart"):
            backend.append("--fail-restart")
        if sc.get("no_listing"):
            backend.append("--no-listing-on-restart")
        backend += ["--", server, str(sb)]
        log = tdir / "deliveries.jsonl"
    client = Client([*relay, "--", *backend], {"PATH": os.environ["PATH"], "HOME": str(ws)},
                    str(ws))  # fmt: skip
    init = client.request(
        "init",
        "initialize",
        {"protocolVersion": "2025-11-25", "capabilities": {},
         "clientInfo": {"name": "sg-contract-v2", "version": "0"}},
        15.0,
    )  # fmt: skip
    client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
    tool, args = workflow.arguments("S3", sb)
    call = {"name": tool, "arguments": args}
    ids = ["T-1"]
    extra: dict[str, Any] = {}
    t0 = time.monotonic()
    if sc.get("colliding_id"):
        ids.insert(0, "stateguard-reconcile:1")
        got = client.request("stateguard-reconcile:1", "tools/call",
                             {"name": "read_text_file",
                              "arguments": {"path": str(sb / fixture.ORDER)}}, 15.0)  # fmt: skip
        extra["colliding_id_answered_while_open"] = bool(got and "result" in got)
        t0 = time.monotonic()
    client.send({"jsonrpc": "2.0", "id": "T-1", "method": "tools/call", "params": call})
    if sc.get("concurrent"):
        ids.append("T-2")
        time.sleep(0.2)  # scenario timing: the second request lands mid-reconciliation
        client.send({"jsonrpc": "2.0", "id": "T-2", "method": "tools/call",
                     "params": {"name": "read_text_file",
                                "arguments": {"path": str(sb / fixture.LOG)}}})  # fmt: skip
    response = client.wait_for("T-1", sc.get("timeout_s", 90.0))
    latency = round(time.monotonic() - t0, 3)
    if sc.get("concurrent"):
        second = client.wait_for("T-2", 30.0)
        extra["concurrent_T2"] = workflow.wire_class(second)
    at_answer = fixture.snapshot(sb)
    error = response.get("error") if isinstance(response, dict) else None
    if sc.get("followup") and error:
        ids += ["T-2", "T-3"]
        log_tool, log_args = workflow.arguments("S4", sb)
        r2 = client.request("T-2", "tools/call", {"name": log_tool, "arguments": log_args}, 15.0)
        r3 = client.request("T-3", "tools/call", {"name": "read_text_file",
                            "arguments": {"path": str(sb / fixture.DONE)}}, 15.0)  # fmt: skip
        extra["followup"] = [workflow.wire_class(r2), workflow.wire_class(r3)]
    if config == "cli_naive" and error:
        ids.append("T-1-resend")
        r2 = client.request("T-1-resend", "tools/call", call, 30.0)
        extra["naive_resend"] = workflow.wire_class(r2)
    if sc.get("handoff"):
        (tdir / "go").touch()  # only now may the external worker act
        deadline = time.monotonic() + 15
        while not (tdir / "worker_done").exists() and time.monotonic() < deadline:
            time.sleep(0.02)
    if sc.get("late_wait"):
        time.sleep(max(0.0, t0 + float(sc["late_wait"]) - time.monotonic()))
    closed_at = client.close_and_drain()
    if worker is not None:
        worker.wait(timeout=30)
    final = fixture.snapshot(sb)
    events = relay_audit._events(log)
    audit_events = relay_audit._events(audit)
    audit_text = audit.read_text() if audit.exists() else ""
    answers = {i: [at for at, m in client.seen if m.get("id") == i and "method" not in m]
               for i in ids}  # fmt: skip
    data = (error or {}).get("data") or {}
    observation = data.get("io.stateguard/observation") or {}
    window = None
    if observation.get("observed_from") and observation.get("observed_to"):
        window = round(_iso(observation["observed_to"]) - _iso(observation["observed_from"]), 3)  # type: ignore[operator]
    probes = [e for e in audit_events if e.get("event") == "reconcile_probe"]
    termination = [e for e in audit_events if e.get("event") == "executor_termination"]
    if sc.get("server") or sc.get("handoff"):
        moved_now = [e for e in events if e["event"] in ("moved",) and e.get("gen") == 0]
        original_ok = bool(moved_now)
        deliveries = len(moved_now) + sum(1 for e in events if e["event"] == "queued")
        late = [e["event"] for e in events if e["event"] in ("rolled_back", "moved_late",
                                                               "worker_moved")]  # fmt: skip
        later_writes = [e for e in events if e["event"] == "moved" and e.get("gen", 0) >= 1]
    else:
        deliveries = sum(1 for e in events if e["event"] == "delivered" and e.get("id") == "T-1")
        original_ok = any(e["event"] == "answered" and e.get("id") == "T-1"
                          and not e.get("is_error") for e in events)  # fmt: skip
        late = []
        later_writes = [e for e in events if e["event"] == "delivered" and e.get("gen", 0) >= 1
                        and e.get("tool") != "read_text_file"
                        and e.get("id") not in ("T-1-resend", "T-2", "T-3")]  # fmt: skip
    actors = [e for e in events if e.get("event") == "actor"]
    kind_pred = sc.get("kind", "reconcile")
    r: dict[str, Any] = {
        "scenario": sc["id"], "group": sc["group"], "config": config, "rep": rep,
        "initialized": bool(init and "result" in init),
        "wire": "none" if response is None else ("error" if error else "result"),
        "error_code": (error or {}).get("code"),
        "postcondition": data.get("io.stateguard/postcondition"),
        "contract": data.get("io.stateguard/reconciliationContract"),
        "claims": {k: data.get(f"io.stateguard/{k}") for k in
                   ("executionState", "attribution", "durability", "operationOutcome")},
        "content_checked": observation.get("content_checked"),
        "reasons": (data.get("io.stateguard/reconciliation") or {}).get("reasons"),
        "latency_s": latency,
        "observation_window_s": window,
        "probe_count": len(probes),
        "probe_failures": sum(p.get("status") not in ("satisfied", "unsatisfied") for p in probes),
        "termination": [(t.get("kind"), t.get("applies")) for t in termination],
        # ground truth
        "original_deliveries": deliveries,
        "original_ok_by_server": original_ok,
        "actor_ops": [a.get("op") for a in actors],
        "later_generation_writes": len(later_writes),
        "late_effects": late,
        "predicate_before": predicate(before, kind_pred),
        "predicate_at_answer": predicate(at_answer, kind_pred),
        "predicate_final": predicate(final, kind_pred),
        # Independent of the configured predicate: the intended state, content included.
        "intended_at_answer": predicate(at_answer, "reconcile_content"),
        "intended_final": predicate(final, "reconcile_content"),
        "final_checkpoint": fixture.matching_checkpoint(final),
        "answers": {i: len(v) for i, v in answers.items()},
        "answers_after_close": {i: sum(at >= closed_at for at in v) for i, v in answers.items()},
        "sentinel_unchanged": final.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
        "audit_leaks_content": any(m in audit_text.lower() for m in relay_audit.LEAK_MARKERS),
        "audit_reconcile_paths": sum(
            1 for e in audit_events if "reconcile" in e.get("event", "")
            and str(sb) in json.dumps(e)),  # fmt: skip
        **extra,
    }  # fmt: skip
    r["classification"] = classify(r)
    return r


def classify(r: dict[str, Any]) -> str:
    post = r["postcondition"]
    if post == "observed_satisfied":
        return "satisfied_correct" if r["predicate_at_answer"] else "satisfied_false_at_answer"
    if post == "observed_not_satisfied":
        return (
            "not_satisfied_false_at_answer"
            if r["predicate_at_answer"]
            else ("not_satisfied_correct")
        )
    if post == "undetermined":
        return "undetermined"
    if r["claims"]["executionState"] == "unknown":
        return "unknown_no_reconciliation"
    return f"wire_{r['wire']}"


def refusal(ws: Path, reps: int) -> list[dict[str, Any]]:
    """main() with reconcile blocks, in each mode: refused before any server starts."""
    out: list[dict[str, Any]] = []
    spec = ws / "refusal-effects.json"
    spec.write_text(json.dumps(effects("reconcile")), encoding="utf-8")
    for flags in ([], ["--restart", "2", "--restart-policy", "after-unknown"], ["--shadow"],
                  ["--passthrough"]):  # fmt: skip
        for rep in range(reps):
            marker = ws / f"refusal-started-{len(out)}"
            proc = subprocess.run(
                [PY, str(RELAY), "--effects", str(spec), *flags, "--", PY, "-c",
                 f"open({str(marker)!r}, 'w').close()"],
                input=b"", capture_output=True, timeout=30, env={"PATH": os.environ["PATH"]},
            )  # fmt: skip
            err = proc.stderr.decode("utf-8", "replace")
            out.append({
                "flags": flags, "rep": rep, "exit_code": proc.returncode,
                "server_started": marker.exists(),
                "actionable": "not supported by this entry point" in err
                and "TerminationEvidence.scope_ended" in err and "direct child" in err,
            })  # fmt: skip
    return out


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in sorted({(r["scenario"], r["config"]) for r in results}):
        rs = [r for r in results if (r["scenario"], r["config"]) == key]
        lat = sorted(r["latency_s"] for r in rs)
        classes: dict[str, int] = {}
        for r in rs:
            classes[r["classification"]] = classes.get(r["classification"], 0) + 1
        out[f"{key[0]}/{key[1]}"] = {
            "trials": len(rs),
            "classes": classes,
            "probes": sum(r["probe_count"] for r in rs),
            "latency_median_s": round(statistics.median(lat), 3),
            "latency_p95_s": round(lat[min(len(lat) - 1, int(0.95 * len(lat)))], 3),
        }
    return out


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    argv = sys.argv[4:]
    reps = int(argv[argv.index("--reps") + 1]) if "--reps" in argv else 5
    only = set(argv[argv.index("--only") + 1].split(",")) if "--only" in argv else None
    ws.mkdir(parents=True, exist_ok=True)
    results = []
    for sc in _scenarios():
        if only and sc["id"] not in only:
            continue
        for config in sc["configs"]:
            for rep in range(reps if sc["id"] not in ("S12e",) else min(reps, 3)):
                r = trial(server, ws, sc, config, rep)
                results.append(r)
                print(
                    f"{r['scenario']:5} {config:10} r{rep} {r['classification']:30} "
                    f"post={r['postcondition']!s:22} t={r['latency_s']}s "
                    f"probes={r['probe_count']} answers={r['answers']} "
                    f"late={r['late_effects']} cp={r['final_checkpoint']}",
                    flush=True,
                )
    refused = refusal(ws, reps) if not only or "REFUSAL" in only else []
    fixture.reset(ws / "sandbox")
    out.write_text(
        json.dumps(
            {"results": results, "refusal": refused, "summary": summarize(results)}, indent=1
        ),
        encoding="utf-8",
    )
    print("trials:", len(results), "refusal runs:", len(refused))


if __name__ == "__main__":
    main()

"""
Week 3 experiment: can a declared read-only probe safely reconcile a lost response?

usage: reconcile_experiment.py SERVER_BIN WORKSPACE OUT_JSON [--reps N] [SCENARIO ...]

No relay is involved and no runtime code changes: the harness plays the client,
injects faults with ``reconcile_shim.py`` in front of the real server, and judges
every verdict against on-disk ground truth (``fixture.snapshot`` / checkpoints)
and the shim's delivery ledger.

Configurations, identical scenario and fault seed for each:

``raw``       the client gets no answer and stops: outcome unknown
``naive``     the client reconnects and re-sends the call once, no probe
``declared``  probe + ``reconcile.assess`` under the Week 2 handoff policy
              (``exclusive: true`` accepted); resend once if eligible, then verify
``strict``    probe + ``reconcile.assess`` under the Week 3 rules (a declaration
              is not evidence); resend once if eligible, then verify

Every operation is declared ``exclusive: true, resend_if_not_executed: true`` --
the most permissive declaration -- so the policies alone decide.

WORKSPACE must be a private scratch directory. Results hold digests and envelopes
only, never file contents.
"""

from __future__ import annotations

import contextlib
import json
import os
import statistics
import subprocess
import sys
import time
from collections import Counter, defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import fixture  # noqa: E402
import reconcile as rc  # noqa: E402
import workflow  # noqa: E402
from rawclient import RawSession, outcome  # noqa: E402

PY = sys.executable
SHIM = HERE / "reconcile_shim.py"
CONFIGS = ("raw", "naive", "declared", "strict")
DECLARATION = rc.Declaration(exclusive=True, resend_if_not_executed=True)
FORGED = "order: 001\nstatus: forged\nitems: 9\n"


@dataclass
class Scenario:
    sid: str
    title: str
    step: str  # "S3" (move) or "S4" (append)
    fault: str  # reconcile_shim fault spec for the original call
    call_timeout: float = 10.0
    actor: str | None = None  # concurrent writer, after the loss and before the probe
    probe_fault: str | None = None  # fault on the probe server
    probe_unavailable: bool = False
    stale_probe: bool = False  # use the pre-image as the "probe" (a cached view)
    hook_between_reads: str | None = None  # actor run after the probe's first read
    late_wait: float = 0.0  # seconds to keep a live executor before closing it
    reps: int = 3
    expect: str = ""


SCENARIOS = [
    Scenario(
        "R1",
        "never reaches the server; executor stopped",
        "S3",
        "swallow_request:move_file",
        expect="original not executed",
    ),
    Scenario(
        "R2", "executes; response lost", "S3", "drop_response:move_file", expect="original executed"
    ),
    Scenario(
        "R3",
        "append executes; response lost (blind retry duplicates)",
        "S4",
        "drop_response:edit_file",
        expect="original executed",
    ),
    Scenario(
        "R4",
        "server executes after the probe saw the pre-state",
        "S4",
        "delay_request:edit_file:1:3.0",
        call_timeout=1.0,
        late_wait=3.5,
        expect="original executes late",
    ),
    Scenario(
        "R5",
        "concurrent writer produces the desired post-state",
        "S3",
        "swallow_request:move_file",
        actor="move",
        expect="original not executed",
    ),
    Scenario(
        "R6",
        "concurrent writer produces a conflicting state",
        "S3",
        "swallow_request:move_file",
        actor="conflict",
        expect="original not executed",
    ),
    Scenario(
        "R6b",
        "executes; another actor undoes it (ABA)",
        "S4",
        "drop_response:edit_file",
        actor="revert_log",
        expect="original executed",
    ),
    Scenario(
        "R7a",
        "stale probe (a view from before the loss)",
        "S3",
        "drop_response:move_file",
        stale_probe=True,
        expect="original executed",
    ),
    Scenario(
        "R7b",
        "inconsistent probe (state changes between reads)",
        "S3",
        "swallow_request:move_file",
        hook_between_reads="move",
        expect="original not executed",
    ),
    Scenario(
        "R7c",
        "incomplete probe (probe server dies mid-probe)",
        "S3",
        "drop_response:move_file",
        probe_fault="drop_response:read_text_file:2",
        expect="original executed",
    ),
    Scenario(
        "R7d",
        "probe unavailable (probe server cannot start)",
        "S3",
        "drop_response:move_file",
        probe_unavailable=True,
        expect="original executed",
    ),
    Scenario(
        "R8",
        "postcondition absent while the executor stays alive",
        "S4",
        "hang_request:edit_file",
        call_timeout=1.0,
        late_wait=0.5,
        expect="original never executes",
    ),
    Scenario(
        "R10",
        "server killed right after delivery; probe on a restarted server",
        "S3",
        "kill_after_deliver:move_file",
        reps=5,
        expect="either (race)",
    ),
]


@dataclass
class Ctx:
    server: str
    ws: Path
    tag: str
    gen: int = 0
    calls: int = 0
    sessions: list[RawSession] = field(default_factory=list)

    @property
    def sb(self) -> Path:
        return self.ws / "sandbox"

    @property
    def deliveries(self) -> Path:
        return self.ws / "deliveries" / f"{self.tag}.jsonl"

    def command(self, fault: str | None = None, *, broken: bool = False) -> list[str]:
        self.gen += 1
        cmd = [PY, str(SHIM), "--gen", str(self.gen), "--deliveries", str(self.deliveries)]
        if fault:
            cmd += ["--fault", fault, "--once", str(self.ws / "once" / f"{self.tag}-{self.gen}")]
        server = [str(self.ws / "no-such-server")] if broken else [self.server, str(self.sb)]
        return [*cmd, "--", *server]

    def session(self, cmd: list[str]) -> RawSession:
        env = {"PATH": os.environ["PATH"], "HOME": str(self.ws)}
        s = RawSession(cmd, env=env, cwd=str(self.ws))
        self.sessions.append(s)
        return s


# ---------------------------------------------------------------------------
# State setup, actors, probe
# ---------------------------------------------------------------------------


def setup(sb: Path, step: str) -> int:
    """Bring the sandbox to the checkpoint just before *step* by plain file operations."""
    fixture.reset(sb)
    (sb / fixture.ORDER).write_text(fixture.APPROVED_ORDER, encoding="utf-8")
    k = 3 if step == "S3" else 4
    if step == "S4":
        (sb / fixture.ORDER).rename(sb / fixture.DONE)
    assert fixture.snapshot(sb) == fixture.checkpoints()[k - 1], (
        "setup did not reach the checkpoint"
    )
    return k


def actor(name: str, sb: Path, pre_texts: dict[str, str]) -> None:
    """Another writer, outside the invocation under test."""
    if name == "move":
        (sb / fixture.ORDER).rename(sb / fixture.DONE)
    elif name == "conflict":
        (sb / fixture.DONE).write_text(FORGED, encoding="utf-8")
    elif name == "revert_log":
        (sb / fixture.LOG).write_text(pre_texts[fixture.LOG], encoding="utf-8")


def probe(
    ctx: Ctx,
    paths: list[str],
    *,
    fault: str | None = None,
    broken: bool = False,
    hook: Callable[[int], None] | None = None,
) -> tuple[list[rc.Read], dict[str, str], dict[str, Any]]:
    """Read *paths* through a fresh server process. Returns reads, texts (memory only), cost."""
    t0 = time.monotonic()
    gen = ctx.gen + 1
    cost: dict[str, Any] = {"calls": 0, "server_starts": 1}
    reads: list[rc.Read] = []
    texts: dict[str, str] = {}

    def missing(rest: list[str]) -> list[rc.Read]:
        return [rc.Read(p, "missing", None, time.monotonic(), None) for p in rest]

    s = ctx.session(ctx.command(fault, broken=broken))
    try:
        init = s.initialize()
    except OSError:  # the probe server died at once (unavailable): nothing to read
        init = None
    cost["calls"] += 1
    if init is None or "_timeout" in init or "_send_failed" in init or "result" not in init:
        s.close()
        cost["latency_s"] = round(time.monotonic() - t0, 3)
        return missing(paths), texts, cost
    dead = False
    for i, rel in enumerate(paths):
        if dead:
            reads += missing([rel])
            continue
        r = s.call("read_text_file", {"path": str(ctx.sb / rel)}, rid=f"P{gen}-{i}", timeout=5.0)
        cost["calls"] += 1
        now = time.monotonic()
        if r is None or "_timeout" in r or "_send_failed" in r:
            dead = True
            reads.append(rc.Read(rel, "missing", None, now, None))
        elif "error" in r:
            reads.append(rc.Read(rel, "error", None, now, gen))
        else:
            res = r["result"]
            text = str((res.get("content") or [{}])[0].get("text", ""))
            if res.get("isError"):
                status = "enoent" if "ENOENT" in text else "error"
                reads.append(rc.Read(rel, status, None, now, gen))
            else:
                texts[rel] = text
                reads.append(rc.Read(rel, "ok", rc.digest(text), now, gen))
        if hook:
            hook(i)
    s.close()
    cost["latency_s"] = round(time.monotonic() - t0, 3)
    return reads, texts, cost


def ledger(ctx: Ctx) -> list[dict[str, Any]]:
    if not ctx.deliveries.exists():
        return []
    return [
        json.loads(x) for x in ctx.deliveries.read_text(encoding="utf-8").splitlines() if x.strip()
    ]


def pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# ---------------------------------------------------------------------------
# One run
# ---------------------------------------------------------------------------


def run_one(server: str, ws: Path, sc: Scenario, config: str, rep: int) -> dict[str, Any]:
    tag = f"{sc.sid}-{config}-{rep}"
    ctx = Ctx(server, ws, tag)
    ctx.deliveries.unlink(missing_ok=True)
    k = setup(ctx.sb, sc.step)
    cps = fixture.checkpoints()
    tool, args = workflow.arguments(sc.step, ctx.sb)
    rec: dict[str, Any] = {
        "scenario": sc.sid,
        "title": sc.title,
        "config": config,
        "rep": rep,
        "step": sc.step,
        "fault": sc.fault,
        "expect": sc.expect,
    }

    # Pre-image, captured before the call (all configurations: identical scenario).
    pre_paths = [fixture.ORDER, fixture.DONE] if sc.step == "S3" else [fixture.LOG]
    pre_reads, pre_texts, pre_cost = probe(ctx, pre_paths)
    if sc.step == "S3":
        pre, post, probe_paths = rc.move_spec(fixture.ORDER, fixture.DONE, pre_texts[fixture.ORDER])
    else:
        pre, post, probe_paths = rc.append_spec(
            fixture.LOG, pre_texts[fixture.LOG], "END", fixture.LOG_LINE
        )

    # The original call, under the fault.
    orig_gen = ctx.gen + 1
    orig = ctx.session(ctx.command(sc.fault))
    orig.initialize()
    t_call = time.monotonic()
    resp = orig.call(tool, args, rid="T-1", timeout=sc.call_timeout)
    loss_at = time.monotonic()
    rec["original_wire"] = outcome(resp)

    # Executor status: observed, not assumed.
    executor = rc.Executor.UNKNOWN
    if resp is None:
        with contextlib.suppress(subprocess.TimeoutExpired):
            orig.proc.wait(timeout=5)
        reaped = [e for e in ledger(ctx) if e["event"] == "server_reaped" and e["gen"] == orig_gen]
        if orig.proc.poll() is not None and reaped and not pid_alive(reaped[0]["pid"]):
            executor = rc.Executor.TERMINATED
    elif resp.get("_timeout"):
        executor = rc.Executor.ALIVE if orig.proc.poll() is None else rc.Executor.UNKNOWN
    rec["executor"] = executor.value

    # Ground truth for the original call, before any other actor touches the state.
    snap_loss = fixture.snapshot(ctx.sb)
    gt_at_loss = fixture.matching_checkpoint(snap_loss)
    if sc.actor:
        actor(sc.actor, ctx.sb, pre_texts)

    belief: str = "unknown"
    assessment: rc.Assessment | None = None
    resend: dict[str, Any] = {"attempted": False}
    probe_cost: dict[str, Any] = {}
    snap_probe: dict[str, str] | None = None
    if config == "naive":
        rs = ctx.session(ctx.command())
        rs.initialize()
        r2 = rs.call(tool, args, rid="T-2", timeout=10.0)
        w = outcome(r2)
        belief = {"success": "completed", "tool-error (isError)": "failed"}.get(w, "unknown")
        resend = {"attempted": True, "eligible": False, "wire": w}  # no evidence: never eligible
        rs.close()
    elif config in ("declared", "strict"):
        if sc.stale_probe:
            reads, probe_cost = pre_reads, {"calls": 0, "server_starts": 0, "latency_s": 0.0}
        else:
            hook: Callable[[int], None] | None = None
            if sc.hook_between_reads:
                between = sc.hook_between_reads

                def hook(i: int) -> None:
                    if i == 0:
                        actor(between, ctx.sb, pre_texts)

            reads, _, probe_cost = probe(
                ctx, probe_paths, fault=sc.probe_fault, broken=sc.probe_unavailable, hook=hook
            )
        snap_probe = fixture.snapshot(ctx.sb)
        evidence = rc.Evidence(reads, loss_at, executor, orig_gen)
        assessment = rc.assess(pre, post, evidence, DECLARATION, rc.Policy(config))
        ok, why = rc.resend_eligible(assessment, DECLARATION, resends_made=0, executor=executor)
        resend = {"attempted": False, "eligible": ok, "why": why}
        if assessment.verdict is rc.Verdict.SATISFIED:
            belief = "completed"
        elif ok:
            rs = ctx.session(ctx.command())
            rs.initialize()
            r2 = rs.call(tool, args, rid="T-2", timeout=10.0)
            answered_at = time.monotonic()
            resend_gen = ctx.gen
            rs.close()
            vreads, _, vcost = probe(ctx, probe_paths)
            # Verification of the resend: its answer is known, so the executor's
            # state does not matter for reading the postcondition.
            verify = rc.assess(
                pre,
                post,
                rc.Evidence(vreads, answered_at, rc.Executor.TERMINATED, resend_gen),
                DECLARATION,
                rc.Policy(config),
            )
            resend.update(
                {
                    "attempted": True,
                    "wire": outcome(r2),
                    "verify": verify.verdict.value,
                    "verify_calls": vcost["calls"],
                }
            )
            belief = "completed" if verify.verdict is rc.Verdict.SATISFIED else "unknown"
        else:
            belief = "escalated"

    # Let a live executor do whatever it will, then end it.
    if sc.late_wait:
        time.sleep(max(0.0, t_call + sc.late_wait - time.monotonic()))
    for s in ctx.sessions:
        s.close()
    final = fixture.snapshot(ctx.sb)
    events = ledger(ctx)

    def answered_ok(rid: str) -> bool:
        return any(
            e["event"] == "answered" and e["id"] == rid and not e["is_error"] for e in events
        )

    if executor is rc.Executor.TERMINATED:
        orig_executed: bool | None = (
            {k - 1: False, k: True}.get(gt_at_loss) if gt_at_loss is not None else None
        )
    else:
        orig_executed = answered_ok("T-1")
    resend_executed = answered_ok("T-2")
    effective = int(bool(orig_executed)) + int(resend_executed)
    final_cp = fixture.matching_checkpoint(final)
    rec.update(
        {
            "gt": {
                "original_executed": orig_executed,
                "state_at_loss_checkpoint": gt_at_loss,
                "state_at_probe_is_post": None if snap_probe is None else snap_probe == cps[k],
                "final_checkpoint": final_cp,
                "final_is_post": final == cps[k],
                "effective_executions": effective,
                "duplicate_effects": max(0, effective - 1),
                "sentinel_ok": final.get("<sentinel>") == fixture.digest(fixture.SENTINEL_TEXT),
                "delivered_ids": sorted(
                    {
                        str(e["id"])
                        for e in events
                        if e["event"] == "delivered" and str(e["id"]).startswith("T-")
                    }
                ),
            },
            "belief": belief,
            "resend": resend,
            "assessment": None if assessment is None else assessment.to_dict(),
            "evidence_fingerprint": None
            if assessment is None
            else sorted(assessment.observed.items()),
            "probe_cost": probe_cost,
            "pre_image_cost": pre_cost,
            "server_processes": ctx.gen,
        }
    )
    rec["judgement"] = judge(rec, k)
    return rec


def judge(rec: dict[str, Any], k: int) -> dict[str, Any]:
    """Score one run against ground truth. Unresolved verdicts are never counted as false claims."""
    gt, a = rec["gt"], rec["assessment"]
    out: dict[str, Any] = {
        "false_satisfied": False,
        "false_not_executed": False,
        "relabelled_executed": False,
        "verdict_correct": None,
    }
    if a:
        v = a["verdict"]
        out["relabelled_executed"] = a["execution_state"] != "unknown"
        if v == "satisfied":
            out["verdict_correct"] = bool(gt["state_at_probe_is_post"])
            out["false_satisfied"] = not out["verdict_correct"]
        elif v == "not_executed":
            out["verdict_correct"] = gt["original_executed"] is False
            out["false_not_executed"] = not out["verdict_correct"]
        elif v == "unsatisfied_open":
            out["verdict_correct"] = (not gt["state_at_probe_is_post"]) and rec[
                "executor"
            ] != "terminated"
        else:
            out["verdict_correct"] = True  # inconclusive claims nothing
    unresolved = rec["belief"] in ("unknown", "escalated")
    completed_ok = (
        rec["belief"] == "completed" and gt["final_is_post"] and gt["duplicate_effects"] == 0
    )
    out.update(
        {
            "unresolved": unresolved,
            "false_completion": rec["belief"] == "completed" and not completed_ok,
            "false_failure": rec["belief"] == "failed" and gt["final_is_post"],
            "completed_correctly": completed_ok,
            "unauthorized_resend": rec["resend"].get("attempted", False)
            and not rec["resend"].get("eligible", False),
        }
    )
    return out


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def summarize(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_cfg: dict[str, Counter[str]] = defaultdict(Counter)
    lat: dict[str, list[float]] = defaultdict(list)
    calls: dict[str, list[int]] = defaultdict(list)
    for r in results:
        c, j, gt = by_cfg[r["config"]], r["judgement"], r["gt"]
        c["runs"] += 1
        for key in (
            "false_satisfied",
            "false_not_executed",
            "relabelled_executed",
            "unresolved",
            "false_completion",
            "false_failure",
            "completed_correctly",
            "unauthorized_resend",
        ):
            c[key] += int(bool(j[key]))
        c["duplicate_effects"] += gt["duplicate_effects"]
        c["resends_attempted"] += int(bool(r["resend"].get("attempted")))
        c["sentinel_changed"] += int(not gt["sentinel_ok"])
        if r["assessment"]:
            c[f"verdict:{r['assessment']['verdict']}"] += 1
            c["verdict_correct"] += int(bool(j["verdict_correct"]))
            lat[r["config"]].append(r["probe_cost"].get("latency_s", 0.0))
            calls[r["config"]].append(
                r["probe_cost"].get("calls", 0)
                + r["pre_image_cost"]["calls"]
                + r["resend"].get("verify_calls", 0)
            )
    determinism: dict[str, bool] = {}
    groups: dict[tuple[str, str], set[str]] = defaultdict(set)
    for r in results:
        if r["assessment"]:
            key = json.dumps(
                [r["assessment"]["verdict"], r["assessment"]["reasons"], r["evidence_fingerprint"]]
            )
            groups[(r["scenario"], r["config"])].add(key)
    for (sid, cfg), keys in groups.items():
        determinism[f"{sid}/{cfg}"] = len(keys) == 1
    return {
        "by_config": {k: dict(v) for k, v in by_cfg.items()},
        "probe_latency_s_median": {k: round(statistics.median(v), 3) for k, v in lat.items() if v},
        "extra_calls_median": {k: statistics.median(v) for k, v in calls.items() if v},
        "verdict_deterministic": determinism,
    }


def main() -> None:
    server, ws, out = sys.argv[1], Path(sys.argv[2]).resolve(), Path(sys.argv[3])
    rest = sys.argv[4:]
    reps_override = None
    if rest[:1] == ["--reps"]:
        reps_override, rest = int(rest[1]), rest[2:]
    only = set(rest)
    for sub in ("deliveries", "once"):
        (ws / sub).mkdir(parents=True, exist_ok=True)
    results = []
    for sc in SCENARIOS:
        if only and sc.sid not in only:
            continue
        for rep in range(reps_override or sc.reps):
            for config in CONFIGS:
                r = run_one(server, ws, sc, config, rep)
                results.append(r)
                a = r["assessment"]
                j = r["judgement"]
                flags = [
                    k
                    for k in (
                        "false_satisfied",
                        "false_not_executed",
                        "false_completion",
                        "false_failure",
                        "unauthorized_resend",
                    )
                    if j[k]
                ]
                print(
                    f"{sc.sid:4} {config:8} r{rep} exec={r['gt']['original_executed']!s:5} "
                    f"verdict={(a or {}).get('verdict', '-'):16} belief={r['belief']:9} "
                    f"resend={r['resend'].get('attempted', False)!s:5} "
                    f"dup={r['gt']['duplicate_effects']} "
                    f"final_cp={r['gt']['final_checkpoint']} {' '.join(flags)}",
                    flush=True,
                )
    fixture.reset(ws / "sandbox")
    summary = summarize(results)
    out.write_text(
        json.dumps({"results": results, "summary": summary}, indent=1, default=str),
        encoding="utf-8",
    )
    print(json.dumps(summary["by_config"], indent=1))
    print("probe latency (median s):", summary["probe_latency_s_median"])
    print("extra calls per reconciliation (median):", summary["extra_calls_median"])
    nondet = [k for k, v in summary["verdict_deterministic"].items() if not v]
    print("non-deterministic verdict groups:", nondet or "none")


if __name__ == "__main__":
    main()

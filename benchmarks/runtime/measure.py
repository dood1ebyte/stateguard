"""
Before/after measurement of MCP invocation failures, per failure category.

    python benchmarks/runtime/measure.py [--seed N] [--per-fault N] [--json OUT]

Every scenario (see ``scenarios.py``) runs once in each arm, each time in a
fresh in-process rig (client <-> stdio relay <-> scripted server):

=============  =============================================================
``raw``        The relay with StateGuard off (``--passthrough``): baseline.
``naive``      Raw, plus a client that retries once on an error or a
               timeout. Not a safe strategy; it shows what blind retry does.
``shadow``     StateGuard observing. Must match raw exactly.
``auto``       StateGuard acting, default configuration.
``auto_ops``   StateGuard acting with an operator configuration: a 0.3 s
               call deadline (clients here wait 0.6 s), effects declared
               for the corpus tools (``SIMULATED_EFFECTS`` -- *simulated*
               declarations), and ``--retry auto``. A configuration, not a
               default: the deadline cuts legitimately slow calls short,
               and retry is only as safe as the declarations.
=============  =============================================================

Repeated execution of a tool the configuration declares READ_ONLY or
IDEMPOTENT is recorded as ``repeated_declared``, not as a failure: that is
what the declaration says is harmless. It is reported, so an operator can
see what their declarations allowed.

An invocation **succeeds** when it ends as its ground truth says:

* intended call -- the server executed it exactly once, with the intended
  arguments (server defaults applied), and the client was told it
  succeeded;
* nothing intended -- the server executed nothing, and the client was not
  told it succeeded.

Anything else is a failure, recorded with its kinds:

* ``not_executed`` -- an intended call that never ran;
* ``wrong_execution`` -- something ran that should not have (different
  arguments, or nothing was intended);
* ``duplicate_execution`` -- the intended call ran more than once;
* ``phantom_success`` -- the client was told it succeeded when the intended
  call did not run;
* ``no_response`` -- the client got no answer at all;
* ``executed_reported_failed`` -- the intended call ran, and the client was
  told it failed: the client's view and the server's state now disagree;
* ``duplicate_response`` -- the client received more than one response for
  one request id;
* ``result_contract_violation`` -- the client was told the call succeeded and
  its ``structuredContent`` breaks the tool's ``outputSchema`` (ground truth
  from the injection, not from StateGuard's checker). StateGuard only
  *detects* this (Shadow-only by design), so it stays a failure in every
  arm; the report counts detections separately;
* ``surfaced_unknown`` -- the client was told, truthfully, that the outcome is
  unknown (it may or may not have executed). Still a failure -- the call
  did not complete -- but not a *hidden* one.

``no_response``, ``phantom_success`` and ``duplicate_response`` are
**hidden** failures: the client is left without the truth. The report counts
them separately, because turning a hidden failure into a surfaced one is
progress that the success rate alone does not show.

The server's execution ledger is the ground truth for side effects, so
duplicates and phantom successes are counted where they happen, not
inferred from what the client saw.

**Synthetic.** These numbers describe the scenario corpus, not production
failure rates. The report says so, with the corpus provenance and the
injected-fault distribution.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from stateguard.adapters.mcp import EffectPolicy  # noqa: E402

from .harness import (  # noqa: E402
    RelayMode,
    Rig,
    ScriptedServer,
    ServerAction,
    ServerCall,
    ServerTool,
)
from .scenarios import (  # noqa: E402
    CATEGORIES,
    RAW_LINE_ID,
    SIMULATED_EFFECTS,
    Scenario,
    build,
    drift_corpus,
    equivalent,
)

ARMS = ("raw", "naive", "shadow", "auto", "auto_ops")
DEADLINE_ARM_SECONDS = 0.3
HIDDEN_KINDS = frozenset(
    {"no_response", "phantom_success", "duplicate_response", "result_contract_violation"}
)
FAILURE_KINDS = (
    "not_executed",
    "wrong_execution",
    "duplicate_execution",
    "phantom_success",
    "no_response",
    "executed_reported_failed",
    "duplicate_response",
    "surfaced_unknown",
    "repeated_declared",
    "result_contract_violation",
)


@dataclass
class Result:
    scenario: str
    category: str
    injector: str
    arm: str
    success: bool
    kinds: list[str]
    executions: int
    #: What StateGuard did, from the relay's audit: forward / refuse / escalate /
    #: hold / unvalidated / none.
    action: str
    #: True when StateGuard changed the arguments it forwarded.
    rewrote: bool
    internal_errors: int
    tools_call_sent: int
    own_listings: int
    latency_ms: float | None
    retried: bool = False
    #: Retries the relay actually sent (acted retry decisions).
    relay_retries: int = 0
    #: Result-integrity findings the relay logged (category H).
    result_findings: int = 0


@dataclass
class Report:
    seed: int
    per_fault: int
    provenance: str
    results: list[Result] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Running one invocation
# ---------------------------------------------------------------------------


def run_one(scenario: Scenario, arm: str, *, listing_wait: float = 1.0) -> Result:
    plan = dict(scenario.plan)

    def faults(call: ServerCall) -> ServerAction:
        return plan.get(call.index, ServerAction.NORMAL)

    server = ScriptedServer([ServerTool(t) for t in scenario.tools], faults=faults)
    mode = {
        "raw": RelayMode.PASSTHROUGH,
        "naive": RelayMode.PASSTHROUGH,
        "shadow": RelayMode.SHADOW,
        "auto": RelayMode.AUTO,
        "auto_ops": RelayMode.AUTO,
    }[arm]
    ops = arm == "auto_ops"
    rig = Rig(
        server,
        mode,
        listing_wait=listing_wait,
        call_deadline=DEADLINE_ARM_SECONDS if ops else None,
        effects=EffectPolicy(SIMULATED_EFFECTS) if ops else None,
        retry="auto" if ops else "shadow",
    )
    try:
        rig.request("server/discover")
        if mode is not RelayMode.PASSTHROUGH:
            rig.wait_synced()
        if scenario.tools_after is not None:
            server.set_tools([ServerTool(t) for t in scenario.tools_after])

        first_call_id = len(rig.sent_ids)
        started = time.perf_counter()
        retried = False
        if scenario.raw_line is not None:
            rig._client_w.write(scenario.raw_line + b"\n")
            rig._client_w.flush()
            response = rig.wait_for(RAW_LINE_ID, timeout=scenario.timeout)
        else:
            response = rig.call(scenario.tool, scenario.arguments, timeout=scenario.timeout)
            if arm == "naive" and not _client_ok(response):
                retried = True
                response = rig.call(scenario.tool, scenario.arguments, timeout=scenario.timeout)
        latency = (time.perf_counter() - started) * 1000 if response is not None else None
        time.sleep(0.05)  # let any late or duplicate response arrive before judging

        tool = next((t for t in scenario.tools if t["name"] == scenario.tool), {})
        call_ids = rig.sent_ids[first_call_id:] if scenario.raw_line is None else [RAW_LINE_ID]
        repeatable = ops and SIMULATED_EFFECTS.get(scenario.tool) in ("read_only", "idempotent")
        return _judge(
            scenario, arm, server, rig, response, tool, latency, retried, call_ids, repeatable
        )
    finally:
        rig.close()


def _client_ok(response: dict[str, Any] | None) -> bool:
    if response is None or "result" not in response:
        return False
    result = response["result"]
    return isinstance(result, dict) and result.get("isError") is not True


def _judge(
    scenario: Scenario,
    arm: str,
    server: ScriptedServer,
    rig: Rig,
    response: dict[str, Any] | None,
    tool: dict[str, Any],
    latency: float | None,
    retried: bool,
    call_ids: list[Any],
    repeatable: bool = False,
) -> Result:
    executed = list(server.executed)
    client_ok = _client_ok(response)
    unknown = _surfaced_unknown(response)
    kinds: list[str] = ["surfaced_unknown"] if unknown else []
    if client_ok and ServerAction.BAD_STRUCTURED in scenario.plan.values():
        kinds.append("result_contract_violation")
        client_ok = False  # told "success" with a result that breaks the contract
    if scenario.intended is not None:
        matching = [c for c in executed if equivalent(tool, c.arguments, scenario.intended)]
        if not matching:
            kinds.append("not_executed")
        if len(matching) > 1:
            kinds.append("repeated_declared" if repeatable else "duplicate_execution")
        if len(executed) > len(matching):
            kinds.append("wrong_execution")
        if client_ok and not matching:
            kinds.append("phantom_success")
        if response is None:
            kinds.append("no_response")
        elif (
            not client_ok and matching and not unknown and "result_contract_violation" not in kinds
        ):
            kinds.append("executed_reported_failed")
        success = (
            bool(matching)
            and (len(matching) == 1 or repeatable)
            and len(executed) == len(matching)
            and client_ok
        )
    else:
        if executed:
            kinds.append("wrong_execution")
        if client_ok:
            kinds.append("phantom_success")
        success = not executed and not client_ok and response is not None
        if response is None:
            kinds.append("no_response")

    for identifier in call_ids:
        answers = [
            m
            for m in rig.client_received
            if isinstance(m, dict) and m.get("id") == identifier and "method" not in m
        ]
        if len(answers) > 1:
            kinds.append("duplicate_response")
            success = False

    calls = [e for e in rig.audit if e.get("event") == "call"]
    action = calls[-1].get("action", "none") if calls else "none"
    sent = [json.loads(line) for line in server.received if _method(line) == "tools/call"]
    rewrote = (
        any(m.get("params", {}).get("arguments") != scenario.arguments for m in sent)
        and scenario.raw_line is None
    )
    own = sum(1 for line in server.received if _is_own_listing(line))
    return Result(
        scenario=scenario.id,
        category=scenario.category,
        injector=scenario.injector,
        arm=arm,
        success=success,
        kinds=sorted(set(kinds)),
        executions=len(executed),
        action=action,
        rewrote=rewrote,
        internal_errors=sum(1 for e in rig.audit if e.get("category") == "Z"),
        tools_call_sent=len(sent),
        own_listings=own,
        latency_ms=latency,
        retried=retried,
        relay_retries=sum(
            1 for e in rig.audit if e.get("event") == "retry_decision" and e.get("acted")
        ),
        result_findings=sum(
            1 for e in rig.audit if e.get("event") == "result_check" and e.get("category") == "H"
        ),
    )


def _surfaced_unknown(response: dict[str, Any] | None) -> bool:
    error = response.get("error") if isinstance(response, dict) else None
    data = error.get("data") if isinstance(error, dict) else None
    return isinstance(data, dict) and data.get("io.stateguard/executionState") == "unknown"


def _method(line: bytes) -> str | None:
    with contextlib.suppress(ValueError):
        message = json.loads(line)
        if isinstance(message, dict):
            method = message.get("method")
            return method if isinstance(method, str) else None
    return None


def _is_own_listing(line: bytes) -> bool:
    with contextlib.suppress(ValueError):
        message = json.loads(line)
        return isinstance(message, dict) and str(message.get("id", "")).startswith(
            "stateguard-relay:"
        )
    return False


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round(q * (len(ordered) - 1))))
    return ordered[index]


def summarise(report: Report) -> dict[str, Any]:
    by: dict[tuple[str, str], Result] = {(r.scenario, r.arm): r for r in report.results}
    scenarios = sorted({r.scenario for r in report.results})
    categories: dict[str, dict[str, Any]] = {}

    for cat in CATEGORIES:
        ids = [s for s in scenarios if by[(s, "raw")].category == cat]
        row: dict[str, Any] = {"name": CATEGORIES[cat], "n": len(ids)}
        if not ids:
            row["exercised"] = False
            categories[cat] = row
            continue
        row["exercised"] = True
        row["injectors"] = dict(Counter(by[(s, "raw")].injector for s in ids))
        for arm in ARMS:
            failures = [s for s in ids if not by[(s, arm)].success]
            kinds: Counter[str] = Counter()
            for s in failures:
                kinds.update(by[(s, arm)].kinds)
            row[arm] = {
                "failures": len(failures),
                "hidden": sum(1 for s in failures if HIDDEN_KINDS & set(by[(s, arm)].kinds)),
                "kinds": dict(kinds),
            }

        raw_fail = {s for s in ids if not by[(s, "raw")].success}
        auto = {s: by[(s, "auto")] for s in ids}
        naive = {s: by[(s, "naive")] for s in ids}
        row["auto_vs_raw"] = {
            "failures_before": len(raw_fail),
            "failures_after": sum(1 for r in auto.values() if not r.success),
            "prevented": sum(1 for s in raw_fail if auto[s].success and not auto[s].retried),
            "recovered": sum(1 for s in raw_fail if auto[s].success and auto[s].retried),
            "escalated": sum(1 for r in auto.values() if r.action == "escalate"),
            "refused": sum(1 for r in auto.values() if r.action == "refuse"),
            "unresolved": sum(1 for s in raw_fail if not auto[s].success),
            "stateguard_induced": sum(
                1 for s in ids if by[(s, "raw")].success and not auto[s].success
            ),
            "stateguard_induced_auto_ops": sum(
                1 for s in ids if by[(s, "raw")].success and not by[(s, "auto_ops")].success
            ),
            "incorrect_repair": sum(
                1 for r in auto.values() if r.rewrote and "wrong_execution" in r.kinds
            ),
            "incorrect_refusal": sum(
                1
                for s in ids
                if auto[s].action in ("refuse", "escalate") and by[(s, "raw")].success
            ),
            "incorrect_retry": 0,  # auto has no retry primitive yet
            "incorrect_recovery": 0,
            "duplicate_execution": sum(
                1 for r in auto.values() if "duplicate_execution" in r.kinds
            ),
            "crashes": sum(r.internal_errors for r in auto.values()),
            "round_trips_avoided": sum(
                by[(s, "raw")].tools_call_sent - auto[s].tools_call_sent for s in ids
            ),
            # Each scenario runs in a fresh session, so every auto run pays
            # the relay's initial tools/list once. That is a per-session cost,
            # reported separately; this counts the per-call extras (the
            # refresh before an intervention).
            "round_trips_added": sum(max(0, r.own_listings - 1) for r in auto.values()),
            "session_listings": sum(min(1, r.own_listings) for r in auto.values()),
        }
        ops = {s: by[(s, "auto_ops")] for s in ids}
        row["auto_ops_vs_raw"] = {
            "failures_after": sum(1 for r in ops.values() if not r.success),
            "prevented": sum(1 for s in raw_fail if ops[s].success and ops[s].relay_retries == 0),
            "recovered": sum(1 for s in raw_fail if ops[s].success and ops[s].relay_retries > 0),
            "retries_sent": sum(r.relay_retries for r in ops.values()),
            "stateguard_induced": sum(
                1 for s in ids if by[(s, "raw")].success and not ops[s].success
            ),
            "repeated_declared": sum(1 for r in ops.values() if "repeated_declared" in r.kinds),
            "duplicate_execution": sum(1 for r in ops.values() if "duplicate_execution" in r.kinds),
        }
        row["result_findings"] = {
            arm: sum(by[(s, arm)].result_findings for s in ids) for arm in ARMS
        }
        row["shadow_matches_raw"] = all(
            by[(s, "shadow")].success == by[(s, "raw")].success
            and by[(s, "shadow")].executions == by[(s, "raw")].executions
            for s in ids
        )
        row["naive_retry"] = {
            "failures": sum(1 for r in naive.values() if not r.success),
            "duplicate_execution": sum(
                1 for r in naive.values() if "duplicate_execution" in r.kinds
            ),
            "retried": sum(1 for r in naive.values() if r.retried),
        }
        categories[cat] = row

    latency: dict[str, dict[str, float | None]] = {}
    for arm in ARMS:
        values = [
            r.latency_ms
            for r in report.results
            if r.arm == arm and r.category == "A" and r.latency_ms is not None
        ]
        latency[arm] = {
            "p50_ms": _percentile(values, 0.5),
            "p99_ms": _percentile(values, 0.99),
            "mean_ms": statistics.fmean(values) if values else None,
        }
    totals = {
        "invocations": len(scenarios),
        "failures_before": sum(
            c.get("auto_vs_raw", {}).get("failures_before", 0) for c in categories.values()
        ),
        "failures_after": sum(
            c.get("auto_vs_raw", {}).get("failures_after", 0) for c in categories.values()
        ),
        "stateguard_induced": sum(
            c.get("auto_vs_raw", {}).get("stateguard_induced", 0) for c in categories.values()
        ),
        "stateguard_induced_auto_ops": sum(
            c.get("auto_vs_raw", {}).get("stateguard_induced_auto_ops", 0)
            for c in categories.values()
        ),
        "hidden": {
            arm: sum(c[arm]["hidden"] for c in categories.values() if c.get("exercised"))
            for arm in ARMS
        },
        "result_findings": {
            arm: sum(c["result_findings"][arm] for c in categories.values() if c.get("exercised"))
            for arm in ARMS
        },
    }
    return {
        "provenance": report.provenance,
        "seed": report.seed,
        "per_fault": report.per_fault,
        "synthetic": True,
        "categories": categories,
        "untested": [c for c, row in categories.items() if not row["exercised"]],
        "latency_category_A": latency,
        "totals": totals,
        "limitations": LIMITATIONS,
    }


LIMITATIONS = [
    "Synthetic corpus and a scripted in-process server; not production traffic, "
    "not a production failure rate.",
    "The server's argument validator is deliberately simple (required, closed "
    "properties, primitive types, enum, min/max). Real servers are stricter or "
    "looser; a lax server turns some refusals into wrong executions.",
    "One fault per invocation, on the first server call; retries see a healthy server.",
    "Transport faults are pipe closes and hangs inside one process, not real "
    "process crashes or network partitions.",
    "Latency is in-process wall time on one machine; it measures added overhead, "
    "not network latency.",
]


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def render(summary: dict[str, Any]) -> str:
    lines = [
        "MCP runtime failures: before/after StateGuard (SYNTHETIC CORPUS)",
        f"corpus: {summary['provenance']}",
        f"seed {summary['seed']}, {summary['per_fault']} invocations per injected fault, "
        f"{summary['totals']['invocations']} invocations per arm",
        "",
        f"{'cat':<4}{'name':<30}{'N':>4} {'raw':>4} {'naive':>5} {'shadow':>6} {'auto':>5}"
        f" {'ops':>5} {'prev':>5} {'esc':>4} {'unres':>5} {'Z':>3} {'hid raw':>7}"
        f" {'hid auto':>8} {'hid ops':>8} {'rec ops':>7} {'dup(naive)':>10} {'dup(auto)':>9}",
        "-" * 140,
    ]
    for cat, row in summary["categories"].items():
        if not row["exercised"]:
            lines.append(f"{cat:<4}{row['name']:<30}{0:>4}  -- not exercised")
            continue
        avr = row["auto_vs_raw"]
        lines.append(
            f"{cat:<4}{row['name']:<30}{row['n']:>4} {row['raw']['failures']:>4}"
            f" {row['naive']['failures']:>5} {row['shadow']['failures']:>6}"
            f" {row['auto']['failures']:>5} {row['auto_ops']['failures']:>5}"
            f" {avr['prevented']:>5} {avr['escalated']:>4}"
            f" {avr['unresolved']:>5} {avr['stateguard_induced']:>3}"
            f" {row['raw']['hidden']:>7} {row['auto']['hidden']:>8} {row['auto_ops']['hidden']:>8}"
            f" {row['auto_ops_vs_raw']['recovered']:>7}"
            f" {row['naive_retry']['duplicate_execution']:>10} {avr['duplicate_execution']:>9}"
        )
    t = summary["totals"]
    lines += [
        "-" * 140,
        f"failures before (raw) {t['failures_before']}, after (auto) {t['failures_after']}, "
        f"StateGuard-induced {t['stateguard_induced']} "
        f"(auto_ops: {t['stateguard_induced_auto_ops']})",
        "hidden failures: " + ", ".join(f"{arm} {n}" for arm, n in t["hidden"].items()),
        "result-integrity findings logged (detection only, results never altered): "
        + ", ".join(f"{arm} {n}" for arm, n in t["result_findings"].items()),
        "auto_ops totals: "
        + ", ".join(
            f"{key} "
            + str(
                sum(
                    c["auto_ops_vs_raw"][key]
                    for c in summary["categories"].values()
                    if c["exercised"]
                )
            )
            for key in ("recovered", "retries_sent", "repeated_declared", "duplicate_execution")
        ),
        "columns: raw/naive/shadow/auto/ops = failing invocations per arm (ops: auto with an "
        f"operator configuration -- {DEADLINE_ARM_SECONDS}s deadline, simulated effect "
        "declarations, retry on); prev = prevented by auto; esc = escalated; "
        "unres = still failing under auto; Z = succeeded raw, failed auto; hid = failures "
        "hidden from the client (no answer, phantom success, duplicate answer); dup = "
        "duplicate executions; rec ops = failures recovered by a relay retry in auto_ops",
        "latency (category A, in-process): "
        + ", ".join(
            f"{arm} p50 {v['p50_ms']:.1f} ms / p99 {v['p99_ms']:.1f} ms"
            for arm, v in summary["latency_category_A"].items()
            if v["p50_ms"] is not None
        ),
        f"untested categories: {', '.join(summary['untested']) or 'none'}",
        "",
        "Limitations:",
        *[f"  - {item}" for item in summary["limitations"]],
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Before/after MCP failure measurement.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-fault", type=int, default=8)
    parser.add_argument("--json", metavar="OUT", help="Write the full report as JSON.")
    parser.add_argument("--arms", default=",".join(ARMS))
    args = parser.parse_args(argv)

    corpus = drift_corpus()
    report = Report(seed=args.seed, per_fault=args.per_fault, provenance=corpus.provenance)
    arms = [a for a in args.arms.split(",") if a]
    for scenario in build(args.seed, corpus=corpus, per_fault=args.per_fault):
        for arm in arms:
            report.results.append(run_one(scenario, arm))
    summary = summarise(report)
    print(render(summary))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "results": [asdict(r) for r in report.results]},
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

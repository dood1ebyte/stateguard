"""
Strict Residual Invocation Failure Rate (RIFR), per category, with uncertainty.

    python benchmarks/runtime/rifr.py [--seed N] [--per-fault N] [--control N]
                                      [--arms raw,auto,...] [--json OUT]

The unit is one in-session ``tools/call`` invocation, from the client's
request to the client's one answer (or the end of its wait). Each runs in a
fresh in-process rig (``measure.run_one``) and is judged by ``measure``'s
strict rule against ground truth decided before it ran: the server's own
execution ledger and what the client was told.

**Strict.** An invocation succeeds only when the intended call ran exactly
once with the intended arguments and the client was told it succeeded (or,
with nothing intended, nothing ran and the client was not told success).
UNKNOWN, "not sent", detected-but-delivered bad results, silence, duplicates
and StateGuard-induced failures are all failures. A reconciliation verdict is
never a success here.

What it reports, per arm:

* **Conditional residual** r_c per fault category c: failures / invocations
  *given that fault was injected*, with a Wilson 95% interval. This is what
  StateGuard changes; it is measured.
* **Control** r_0: the same, on fault-free calls -- StateGuard-induced
  failures on healthy traffic.
* **Unsafe outcomes**, kept apart from RIFR: duplicate effects, wrong
  effects, false success, false failure, invalid results reported as
  success.
* **Paired comparison** with the raw arm: recovered (raw failed, arm
  succeeded) and induced (raw succeeded, arm failed).
* **Mixture RIFR** = w_0 r_0 + sum_c w_c r_c for *assumed* weights: how
  often each fault occurs in real traffic is unknown, so the weights are
  labelled assumptions (fault rate x category shares from
  docs/runtime/STATEGUARD_FAILURE_REDUCTION_ESTIMATE.md, central). Failure
  sources the corpus does not model at all are added at residual 1.0 in
  every arm, never left out.

Synthetic: a scripted in-process server and a hand-labelled corpus. These
numbers bound StateGuard's effect on these faults; they are not a production
failure rate.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import random
import sys
from collections import defaultdict
from dataclasses import asdict
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
    __package__ = "runtime"

from .measure import Result, run_one  # noqa: E402
from .scenarios import CATEGORIES, Scenario, build, drift_corpus  # noqa: E402

DEFAULT_ARMS = (
    "raw",
    "naive",
    "shadow",
    "auto",
    "auto_ops",
    "ops_reread",
    "ops_scoped",
    "ops_keyed",
)

#: Failure kinds that are unsafe outcomes (counted apart from RIFR as well as in it).
UNSAFE = {
    "duplicate_execution": "duplicate_effect",
    "wrong_execution": "wrong_effect",
    "phantom_success": "false_success",
    "executed_reported_failed": "false_failure",
    "result_contract_violation": "invalid_result_reported_success",
}

#: ASSUMED share of all in-session invocation failures per internal category,
#: from the estimate report's central vector (user category in brackets). The
#: "unmodelled" share is failure sources this corpus does not exercise at all
#: (in-session configuration, protocol faults the client cannot see, session
#: loss): residual 1.0 in every arm. Assumptions, never measurements.
ASSUMED_FAILURE_SHARES = {
    "A": 0.20,  # [D] input schema / contract
    "B": 0.07,  # [C] stale discovery
    "C": 0.04,  # [B] protocol (client-side malformed envelope only)
    "E": 0.10,  # [A] server availability / process death
    "F": 0.15,  # [F] timeouts, transient errors
    "G": 0.15,  # [E] tool execution errors
    "H": 0.04,  # [I] result integrity
    "I": 0.04,  # [G] lost responses / unknown outcomes
    "J": 0.03,  # [J] runtime state (duplicate responses)
    "K": 0.08,  # [H] auth
    "unmodelled": 0.10,  # [B part, K, session] not exercised here
}
#: ASSUMED fault rates: the share of invocations that meet any fault.
ASSUMED_FAULT_RATES = (0.02, 0.05, 0.10)


def wilson(failures: int, n: int, z: float = 1.959964) -> tuple[float, float]:
    """95% Wilson score interval for a proportion; (0, 1) when n is 0."""
    if n == 0:
        return 0.0, 1.0
    p = failures / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def control_scenarios(n: int, seed: int) -> list[Scenario]:
    """Fault-free valid calls: what StateGuard does to healthy traffic."""
    corpus = drift_corpus()
    rng = random.Random(seed + 1)
    tools = [copy.deepcopy(t) for t in corpus.tools.values()]
    valid = [
        c
        for c in corpus.calls
        if c["class"] in ("unchanged", "repairable") and c.get("forward") and not c.get("known_gap")
    ]
    scenarios = []
    for i in range(n):
        case = copy.deepcopy(rng.choice(valid))
        scenarios.append(
            Scenario(
                id=f"N:no_fault:{i}",
                category="N",
                injector="no_fault",
                tools=tools,
                tool=case["tool"],
                arguments=case["forward"],
                intended=case["forward"],
            )
        )
    return scenarios


def _rate(failures: int, n: int) -> dict[str, Any]:
    low, high = wilson(failures, n)
    return {
        "n": n,
        "failures": failures,
        "rate": failures / n if n else None,
        "ci95": [round(low, 4), round(high, 4)],
    }


def score(results: list[Result], arms: list[str]) -> dict[str, Any]:
    by = {(r.scenario, r.arm): r for r in results}
    scenarios = sorted({r.scenario for r in results})
    category_of = {r.scenario: r.category for r in results}
    injector_of = {r.scenario: r.injector for r in results}
    out: dict[str, Any] = {"arms": {}}
    for arm in arms:
        rows = [by[(s, arm)] for s in scenarios if (s, arm) in by]
        faulted = [r for r in rows if r.category != "N"]
        control = [r for r in rows if r.category == "N"]
        per_category: dict[str, Any] = {}
        per_injector: dict[str, Any] = {}
        grouped: dict[str, list[Result]] = defaultdict(list)
        grouped_inj: dict[str, list[Result]] = defaultdict(list)
        for r in rows:
            grouped[r.category].append(r)
            grouped_inj[f"{r.category}:{r.injector}"].append(r)
        for key, items in sorted(grouped.items()):
            per_category[key] = _rate(sum(1 for r in items if not r.success), len(items))
        for key, items in sorted(grouped_inj.items()):
            entry = _rate(sum(1 for r in items if not r.success), len(items))
            kinds: dict[str, int] = defaultdict(int)
            for r in items:
                if not r.success:
                    for kind in r.kinds:
                        kinds[kind] += 1
            entry["failure_kinds"] = dict(sorted(kinds.items()))
            per_injector[key] = entry
        unsafe: dict[str, int] = {label: 0 for label in UNSAFE.values()}
        for r in rows:
            for kind, label in UNSAFE.items():
                if kind in r.kinds:
                    unsafe[label] += 1
        paired: dict[str, Any] = {}
        if arm != "raw":
            raw_rows = {s: by[(s, "raw")] for s in scenarios if (s, "raw") in by}
            recovered = [
                r.scenario
                for r in rows
                if r.scenario in raw_rows and r.success and not raw_rows[r.scenario].success
            ]
            induced = [
                r.scenario
                for r in rows
                if r.scenario in raw_rows and not r.success and raw_rows[r.scenario].success
            ]
            paired = {
                "recovered_vs_raw": len(recovered),
                "induced_vs_raw": len(induced),
                "induced_scenarios": induced,
            }
        out["arms"][arm] = {
            "faulted": _rate(sum(1 for r in faulted if not r.success), len(faulted)),
            "control": _rate(sum(1 for r in control if not r.success), len(control)),
            "per_category": per_category,
            "per_injector": per_injector,
            "unsafe": unsafe,
            "surfaced_unknown": sum(1 for r in rows if "surfaced_unknown" in r.kinds),
            "hidden": sum(
                1
                for r in rows
                if {"no_response", "phantom_success", "duplicate_response"} & set(r.kinds)
            ),
            "internal_errors": sum(r.internal_errors for r in rows),
            "replayed": sum(r.replayed for r in rows),
            **paired,
        }
        out["arms"][arm]["mixture"] = _mixture(per_category, out["arms"][arm]["control"])
    out["injectors"] = sorted({f"{category_of[s]}:{injector_of[s]}" for s in scenarios})
    return out


def _mixture(per_category: dict[str, Any], control: dict[str, Any]) -> list[dict[str, Any]]:
    """RIFR under assumed fault rates and shares; point and upper (95% Wilson) bounds."""
    rows = []
    r0 = control["rate"] or 0.0
    r0_hi = control["ci95"][1] if control["n"] else 1.0
    for fault_rate in ASSUMED_FAULT_RATES:
        point = (1 - fault_rate) * r0
        upper = (1 - fault_rate) * r0_hi
        for category, share in ASSUMED_FAILURE_SHARES.items():
            if category == "unmodelled":
                residual = residual_hi = 1.0
            else:
                entry = per_category.get(category)
                if entry is None or not entry["n"]:
                    residual = residual_hi = 1.0  # not measured: counted as failing
                else:
                    residual, residual_hi = entry["rate"], entry["ci95"][1]
            point += fault_rate * share * residual
            upper += fault_rate * share * residual_hi
        rows.append(
            {
                "assumed_fault_rate": fault_rate,
                "rifr": round(point, 5),
                "rifr_upper95": round(min(1.0, upper), 5),
            }
        )
    return rows


def render(summary: dict[str, Any], arms: list[str]) -> str:
    lines = [
        "Strict RIFR (SYNTHETIC: scripted in-process server, hand-labelled corpus)",
        f"seed {summary['seed']}, {summary['per_fault']} per fault, "
        f"{summary['control_n']} fault-free control calls per arm",
        "",
        "Conditional residual per category (failures/n, Wilson 95%)",
    ]
    cats = sorted({c for arm in arms for c in summary["arms"][arm]["per_category"]})
    header = f"{'cat':<4} {'name':<30}" + "".join(f"{a:>17}" for a in arms)
    lines += [header, "-" * len(header)]
    for c in cats:
        name = CATEGORIES.get(c, "no fault (control)")
        cells = []
        for arm in arms:
            e = summary["arms"][arm]["per_category"].get(c)
            cells.append(f"{e['failures']:>4}/{e['n']:<4}({e['ci95'][1]:.2f})" if e else "-")
        lines.append(f"{c:<4} {name:<30}" + "".join(f"{x:>17}" for x in cells))
    lines += ["", "Per arm"]
    for arm in arms:
        a = summary["arms"][arm]
        mix = ", ".join(
            f"f={m['assumed_fault_rate']:.0%}: {m['rifr']:.2%} (<= {m['rifr_upper95']:.2%})"
            for m in a["mixture"]
        )
        unsafe = ", ".join(f"{k} {v}" for k, v in a["unsafe"].items() if v) or "none"
        lines.append(
            f"  {arm:<11} faulted {a['faulted']['failures']}/{a['faulted']['n']}"
            f"  control {a['control']['failures']}/{a['control']['n']}"
            f" (<= {a['control']['ci95'][1]:.2%})"
            f"  unsafe: {unsafe}"
            + (
                f"  recovered {a['recovered_vs_raw']} induced {a['induced_vs_raw']}"
                if arm != "raw"
                else ""
            )
        )
        lines.append(f"  {'':<11} mixture RIFR (ASSUMED weights): {mix}")
    lines += ["", "Limitations:", *[f"  - {x}" for x in LIMITATIONS]]
    return "\n".join(lines)


LIMITATIONS = [
    "Synthetic corpus and scripted in-process server; not production traffic.",
    "Mixture weights (fault rate, category shares) are ASSUMED; production prevalence is unknown.",
    "One fault per invocation, on the first server call; real faults cluster.",
    "Wilson intervals treat scenarios as independent; injected faults are deterministic, so the "
    "intervals bound this corpus, not the world.",
    "Effect declarations in the ops arms are simulated (scenarios.SIMULATED_EFFECTS).",
]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Strict RIFR with uncertainty.")
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--per-fault", type=int, default=8)
    parser.add_argument("--control", type=int, default=40)
    parser.add_argument("--arms", default=",".join(DEFAULT_ARMS))
    parser.add_argument("--json", metavar="OUT")
    args = parser.parse_args(argv)
    arms = [a for a in args.arms.split(",") if a]
    scenarios = build(args.seed, per_fault=args.per_fault) + control_scenarios(
        args.control, args.seed
    )
    results = [run_one(s, arm) for s in scenarios for arm in arms]
    summary = {
        "seed": args.seed,
        "per_fault": args.per_fault,
        "control_n": args.control,
        "assumed_failure_shares": ASSUMED_FAILURE_SHARES,
        "assumed_fault_rates": ASSUMED_FAULT_RATES,
        **score(results, arms),
    }
    print(render(summary, arms))
    if args.json:
        Path(args.json).write_text(
            json.dumps(
                {"summary": summary, "results": [asdict(r) for r in results]},
                indent=2,
                default=str,
            )
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())

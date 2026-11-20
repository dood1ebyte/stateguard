# StateGuard — external-reproduction experiment (MCP issue #3394, lost-response mutating call)

**Date:** 2026-10-19. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Objective:** does StateGuard give a measurable, defensible advantage over an unprotected MCP client and over ordinary retry, for the externally documented lost-response failure of a mutating `tools/call`?
**Nature:** evaluation only. **No production code changed.** All experiment code is a disposable harness outside the repository (`/root/stateguard-captures/public-failure-evidence/experiment/`). Nothing committed/pushed/stashed/reset. Based on `docs/runtime/PUBLIC_FAILURE_EVIDENCE_RESEARCH.md` (report R1).

## 1. External source and provenance
- **Report:** [modelcontextprotocol/modelcontextprotocol#3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394) — "Lost-response retry on a mutating `tools/call` re-executes external effects."
- **Reproduction repo:** `github.com/arjun2075/mcp-b1-lost-response-repro`, cloned at commit **`6373793aed3c98c281d6fa695afe9fb6fa541586`** (the exact commit referenced by #3394; MIT; 3 files). `external_commit.txt` records the hash.
- **Mechanism (author's own words, verified by reading the script):** a mutating tool commits its effect, the attempt-1 `tools/call` response is dropped *after* the handler returns, the client's read-timeout fires, and one scripted application retry (fresh JSON-RPC id, identical args) runs the tool again → 2 external effects for 1 logical operation. The SDK does no automatic retry; the retry is scripted application policy.

## 2. Environment, dependencies, exact steps, deviations
- **Direct runs:** isolated venv, **`mcp==2.2.0`** (the repo's pin), Python **3.13.16**. Steps: `pip install -r requirements.txt` (`mcp==2.2.0`); `python b1_lost_response_repro.py`; `python b1_positive_control.py`. Captured verbatim in `direct_mcp220.txt`.
- **Deviations from the original:** (i) Python 3.13.16 vs the repo's tested 3.12 — the scripts ran unmodified and produced the documented output, so the deviation did not matter here; (ii) the failure **also reproduces under StateGuard's own pinned `mcp==2.3.0`** (`direct_mcp230.txt`), so it is not specific to 2.2.0.
- **Direct result (unmodified external scripts):** baseline → `authoritative effect count: 2` ("duplicate execution observed"); positive control (application-level idempotency key) → `1`. This matches #3394 exactly.

## 3. Experimental arms, ground truth, trials, raw results
Four arms share one scenario, fault, initial state, and ground-truth instrumentation. Harness: `experiment/xr_arms.py`; raw per-trial + aggregate JSON: `experiment/results.json`. **Ground truth** = rows in the append-only effect ledger (the authoritative effect count, same definition as upstream).

- **A** unprotected, no retry.
- **B** ordinary retry (identical to the external baseline's scripted retry).
- **C** StateGuard **Shadow** — real `stateguard.adapters.mcp.reconcile` code: classify the lost-response outcome UNKNOWN, never resend, make no claim.
- **D** StateGuard **reconcile** — real reconcile code: never resend the mutating call; run one **read-only** probe (`order_exists`) via the real `observe()`/`decide()`; return a verdict.

**Faithfulness / adaptation (labeled):** the mutating tool, the after-commit response-drop fault, the in-memory MCP transport, and the ledger are faithfully adapted from the external repo; arms A/B here agree with the verbatim direct runs (§2). **Added** for StateGuard: a read-only `order_exists` probe tool and a `logical_operation_id` arg (the upstream positive control already uses that id). Arms C/D invoke StateGuard's **real** reconcile functions unmodified. **Limitation:** `executor_replaced=True` is *supplied* in-harness to model a confirmed restart; the termination mechanism itself (`scope_ended`) is evidenced separately (WEEK3 243 trials; WEEK4), not re-established here. Arm D therefore evaluates StateGuard's *decision + probe* behavior, not its termination mechanism.

**Trials:** 20 per arm. The scenario is **deterministic by construction** (the fault always drops attempt-1 after commit), so 20/20 identical outcomes expose nondeterminism by its absence, not statistical spread. **Repeated trials of one in-memory mock are not independent real-world deployments** and do not estimate prevalence.

### Raw aggregate (20 trials/arm; full data in `results.json`)

| Metric | A no-retry | B ordinary retry | C SG Shadow | D SG reconcile |
|---|---|---|---|---|
| Actual side-effect executions / trial | 1 | **2** | 1 | 1 |
| Duplicate side effects (total) | 0 | **20** | 0 | 0 |
| Original calls resent/replayed (total) | 0 | **20** | 0 | 0 |
| Completed-without-duplicate | 0 | 0 | 0 | 0 |
| Outcomes left explicit UNKNOWN | 20 | 0 | 20 | 0 |
| False "exactly-once success" claims | 0 | **20** | 0 | 0 |
| False "did-not-execute" claims | 0 | 0 | 0 | 0 |
| Reconcile verdict correct vs ground truth | — | — | — | **20/20** |
| Keeps execution/attribution/durability/outcome separate | — | — | — | **20/20** |
| StateGuard-induced failures (Z) | 0 | 0 | 0 | 0 |
| Round trips / trial | 1 | 2 (2nd = mutating resend) | 1 | 2 (2nd = **read-only** probe) |
| Latency p50 (s) | 1.505 | 1.507 | 1.505 | 1.508 |

Latency across all arms is dominated by the injected 1.5 s read-timeout; StateGuard adds no measurable per-call overhead beyond the one read-only probe round trip in arm D.

## 4. Side-effect safety and reconciliation findings
- **B (ordinary retry) is the harm, every trial:** it produces a real duplicate side effect (2 ledger rows) and reports success to the client with no indication a duplicate occurred — a false "exactly-once success" on 20/20 trials.
- **A (no retry) is safe but blind:** no duplicate, but the outcome is an ambiguous timeout; an unprotected app that treats it as failure would either lose a completed order or escalate into B. (Not scored as a false claim because the bare client asserts nothing; the risk is in how the app interprets the timeout.)
- **C (StateGuard Shadow) is safe and honest:** 0 duplicates, 0 resends, outcome **explicitly UNKNOWN** — the same safety as A, but labeled rather than silently abandoned, and adds **no** extra round trip.
- **D (StateGuard reconcile) is safe and recovers knowledge:** 0 duplicates, 0 resends; one read-only probe establishes the postcondition correctly on 20/20 trials; and the verdict **keeps the concepts separate** — `execution_state=unknown`, `attribution=unknown`, `durability=not_established`, `operation_outcome=unknown` — i.e. it reports "the order exists when read" without ever claiming the client's call executed, caused it, is durable, or succeeded. No UNKNOWN was converted to success.

## 5. Latency / round-trip comparison
D and B both use 2 round trips; the decisive difference is **what the second round trip is**: B's is a **mutating resend** (causes the duplicate), D's is a **read-only probe** (causes nothing). A and C use 1. No arm shows meaningful latency difference here (all gated by the read-timeout).

## 6. Comparison against ordinary retry — the defensible claim
On this externally documented failure, **StateGuard (D) strictly dominates both baselines:**
- **vs ordinary retry (B):** eliminates the duplicate side effect (20→0) and the false exactly-once success (20→0), replacing a mutating resend with a read-only probe. A clear **safety** win, not merely "fewer errors."
- **vs unprotected no-retry (A):** equal safety (0 duplicates) **and** resolves the ambiguity correctly (20/20), which A cannot.
- **C vs A:** equal safety, but C's explicit UNKNOWN is more honest than A's silent give-up and costs no extra round trip.

This is a genuine advantage on **both** safety and useful recovery — the bar the brief set.

## 7. Evidence separation (as required)
1. **Directly reproduced external behavior:** arm B / the baseline script = 2 effects (direct runs `direct_mcp220.txt`, `direct_mcp230.txt`; matches #3394).
2. **Locally adapted scenario:** arms C/D (StateGuard's real reconcile code on the adapted server with an added read-only probe; `executor_replaced` supplied).
3. **Existing internal controlled results (separate, not re-run here):** WEEK3 real-relay verification (243 trials, 0 resends/dupes/false claims) and the Week 4 scope/reconcile suites.
4. **Inference / untested:** everything about prevalence, demand, production readiness, non-mutating-but-observable coverage in the wild, and behavior under a real (non-mock) server and transport.

## 8. Limitations and remaining unknowns
- One deterministic in-memory mock; **not** a real deployment, not a prevalence or demand signal. No failure-reduction percentage is extrapolated to production.
- Arm D's recovery **requires a read-back predicate** (a read-only tool that can observe the effect). Where none exists, StateGuard falls back to arm C's behavior: safe UNKNOWN, **no** recovery. The advantage over B is then "no duplicate + honest UNKNOWN," not "resolved."
- `executor_replaced=True` was supplied; the termination mechanism is evidenced elsewhere, not here. A full end-to-end test (StateGuard's stdio relay + real restart/scope + a real server exhibiting this fault) remains the stronger, unproven step (§10).
- SDK/runtime: in-memory transport, `mcp` 2.2.0/2.3.0, single logical operation. Streamable-HTTP and concurrent/interleaved operations were not exercised here (modsuperagi's comment on #3394 reproduced it on streamable HTTP; we did not).

## 9. Decision against predefined criteria

| # | Criterion | Result |
|---|---|---|
| C1 | External failure reproduced (ordinary retry → duplicate) | **PASS** (2 effects, 20/20; matches #3394 + direct runs) |
| C2 | StateGuard never replays an unknown-outcome call | **PASS** (C, D: 0 resents) |
| C3 | StateGuard avoids the duplicate side effect | **PASS** (C, D: 0 duplicates vs B: 20) |
| C4 | No false success; no UNKNOWN→success | **PASS** (D operation_outcome unknown; 0 false exactly-once; C explicit UNKNOWN) |
| C5 | Reconcile verdict correct vs ground truth | **PASS** (20/20) |
| C6 | Execution/attribution/durability/outcome kept separate | **PASS** (20/20) |
| C7 | No StateGuard-induced failures | **PASS** (0) |
| C8 | Defensible advantage over ordinary retry on safety **and** useful recovery | **PASS** on safety; recovery **conditional** on a read-back predicate |

**Overall: PASS** — StateGuard shows a measurable, defensible safety advantage over both an unprotected client and ordinary retry on the externally documented lost-response failure, with correct reconciliation when a read-back predicate exists, and no false claims or induced failures. The PASS is bounded by §8 (one mock; recovery needs a predicate; termination mechanism supplied, not re-established; no prevalence/demand/production-readiness claim).

## 10. Single most informative next step
Run this **through StateGuard's real stdio relay end-to-end** — the production entry point with `--executor-scope` + `--restart --restart-policy after-unknown` + an `--effects` reconcile block — against a real (non-mock) MCP server instrumented with this exact after-commit lost-response fault and a read-only probe tool. That removes the one supplied assumption (`executor_replaced`) by establishing termination with the real scope, and converts arm D from "StateGuard's decision logic" to "StateGuard's full production path," on an externally-derived failure. It is the smallest step that would make the PASS unconditional for the mutating-call lost-response case.

Do **not** (per the standing closure) build new features, contact maintainers, or extrapolate these counts to production from this result.

# StateGuard — end-to-end validation through the real stdio relay (MCP #3394 lost-response)

**Date:** 2026-10-19. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Objective:** does StateGuard's **real stdio relay** safely handle the externally documented lost-response failure of a mutating `tools/call` (MCP #3394) — **establishing executor termination/replacement through the real runtime**, not by supplying `executor_replaced=True` as the prior harness did?
**Nature:** evaluation only. **No production code changed.** All experiment code is a disposable harness outside the repository (`/root/stateguard-captures/public-failure-evidence/e2e/`). Nothing committed/pushed/stashed/reset. Builds on `PUBLIC_FAILURE_EVIDENCE_RESEARCH.md` (R1), `EXTERNAL_REPRODUCTION_EXPERIMENT.md`, `WEEK4H_REGRESSION_AND_HARDENING_GATE.md`, and the Week 3 / Week 4 reconciliation + executor-scope docs.

## 1. Environment and exact configuration
- Host: Linux 6.18.44 (Firecracker microVM), StateGuard repo at HEAD `b6f7ba0f` with the inherited uncommitted Week 1–4 tree. Relay run with the repo venv (`mcp==2.3.0`).
- Real relay entry point: `examples/mcp/stdio_proxy.py`, **unmodified**.
- Arm D configuration (the intended production path): `--executor-scope pid-namespace-unprivileged --restart 2 --restart-policy after-unknown --effects effects.json`. The unprivileged executor scope (Week 4G) was chosen per the brief's preference and runs the server as namespaced root (mapped host uid 65534); no security setting was weakened and no new sandbox was built.
- `effects.json` reconcile block: `create_order` → `effect: non_idempotent` with one read-only probe `order_exists(logical_operation_id=$logical_operation_id) expect present (absent_marker NOT_FOUND)`; `order_exists` declared `effect: read_only`. This is the supported effects-file schema (`reconcile.ReconcilePolicy.from_file`).

## 2. External provenance and documented adaptations
- **Behavioral reference:** `github.com/arjun2075/mcp-b1-lost-response-repro` @ `6373793` (companion to [modelcontextprotocol#3394](https://github.com/modelcontextprotocol/modelcontextprotocol/issues/3394)); verbatim direct runs captured in the prior experiment (`../experiment/direct_mcp220.txt`, `direct_mcp230.txt`): baseline = 2 effects, positive control = 1.
- **Adaptation (labeled):** the external repo drives the `mcp`-SDK **in-memory** transport; StateGuard's production surface is a **stdio** JSON-RPC relay, so the integration uses a stdio server (`order_server.py`) that reproduces the same **sequence** — a mutating `create_order` commits exactly one on-disk ledger row, then (drop mode, generation 0) exits without answering (lost response + connection failure) — plus a read-only `order_exists` probe. Protocol shape mirrors the repo's own proven scope-test server (`tests/examples/test_cli_executor_scope.py`). This adaptation is required because the relay is a stdio relay, not an mcp-SDK client; the failure sequence is preserved.
- **Removed assumption:** nothing supplies `executor_replaced`. Arm D establishes termination through the real executor scope (§3).

## 3. Actual lifecycle evidence (real runtime, not supplied)
Representative arm-D audit sequence (`lifecycle_trace_armD.txt`), with on-disk ground truth `ledger rows: 1`, `calls: [gen0 create_order, gen1 order_exists]`:
```
relay_started executor_scope=pid-namespace-unprivileged
call                         (client create_order)
reconcile_pending
server_lost                  (response lost; connection failed)
executor_termination kind=scope_ended applies=True executor=pidns-4026532265-645-0
server_restarted generation=1
reinitialized
reconcile_probe tool=order_exists        (READ-ONLY; not a resend)
reconcile_verdict postcondition=observed_satisfied
outcome_unknown
reconcile_answered
```
The `executor_termination` event is `kind=scope_ended, applies=True` for the real unprivileged PID-namespace executor — i.e. the old server generation was **provably gone before the probe**, established by the real scope, not asserted by the harness. The only call the restarted generation received was the **read-only** probe; `create_order` was never resent (`calls.log`: one `create_order` on gen 0, one `order_exists` on gen 1).

## 4. Experimental design and raw results
Four arms share one scenario, fault, initial state, and independent ground truth (ledger rows + per-generation `calls.log`, written by the server, counted by the harness — never by StateGuard). **N = 10 trials/arm.** The scenario is **deterministic by construction**, so 10/10 identical outcomes expose regressions by absence, not statistical spread; **repeated trials of one stdio mock are not independent real-world deployments** and estimate no prevalence. Harness: `e2e_driver.py`; raw per-trial + aggregate: `results.json`.

| Metric (10 trials/arm) | A no-retry | B ordinary retry | C SG relay Shadow | D SG relay auto+scope+reconcile |
|---|---|---|---|---|
| Actual side-effect executions / trial | 1 | **2** | 1 | 1 |
| Duplicate side effects (total) | 0 | **10** | 0 | 0 |
| Original mutating calls resent (total) | 0 | **10** | 0 | **0** |
| Executor termination/replacement confirmed (scope_ended) | n/a | n/a | 0 | **10/10** |
| Server restarted | n/a | n/a | 0 | **10/10** |
| Reconciliation probe executed | n/a | n/a | 0 | **10/10** |
| Reconcile verdict correct vs ground truth | — | — | — | **10/10** |
| Keeps execution/attribution/durability/outcome separate; treatAsSuccess false | — | — | — | **10/10** |
| Client correct result returned | 0 | 10 (but duplicated) | 0 | 0 |
| Explicit UNKNOWN to client | 10 | 0 | 10 | 10 |
| False "exactly-once success" claims | 0 | **10** | 0 | 0 |
| False "did-not-execute" claims | 0 | 0 | 0 | 0 |
| StateGuard-induced failures | 0 | 0 | 0 | 0 |
| Round trips / trial | 1 | 2 (2nd = **mutating resend**) | 1 | 2 (2nd = **read-only probe**) |
| Latency p50 (s) | 0.034 | 0.064 | 0.155 | 0.508 |

## 5. Safety, reconciliation, latency, integration findings
- **Safety.** Arm D never resent the uncertain mutating call (0/10) and produced zero duplicate side effects (0/10). The client received an explicit JSON-RPC error — *"Outcome unknown … Do not resend it."* with `treatAsSuccess: false` — never a fabricated success. No UNKNOWN was converted to success. 0 StateGuard-induced failures.
- **Reconciliation.** The read-only `order_exists` probe correctly observed the postcondition on 10/10 trials, and the verdict kept the concepts separate: `postcondition=observed_satisfied` while `executionState=unknown`, `attribution=unknown`, `durability=not_established`, `operationOutcome=unknown`. A read-back match was explicitly **not** treated as proof of causation or durability.
- **Shadow (arm C).** Observes only: no restart, no probe, no resend, outcome left UNKNOWN. Correctly **not** an enforced recovery path.
- **Latency / round trips.** Arm D's p50 ≈ 0.51 s vs 0.03 s unprotected — the integration cost of a real executor-scope create + server restart + one read-only probe on this tiny server. D uses 2 round trips like B, but D's second is a **read-only probe** (causes nothing) where B's is a **mutating resend** (causes the duplicate).
- **Integration.** The scenario ran on the real, unmodified relay via its documented CLI; the unprivileged scope worked as root in this environment; `effects.json` expressed the reconcile probe with no invented fields.

## 6. Baseline comparison
- **vs ordinary retry (B):** D eliminates the duplicate side effect (10→0) and the false exactly-once success (10→0), replacing a mutating resend with a read-only probe. A safety win, not merely fewer errors.
- **vs unprotected no-retry (A):** equal safety (0 duplicates) and additionally resolves the ambiguity correctly (10/10), which A cannot.
- **vs Shadow (C):** D adds the recovery (termination + probe + verdict) that Shadow deliberately does not perform.

## 7. Evidence separation
1. **Directly reproduced external behavior:** B / baseline = 2 effects (prior experiment's verbatim runs).
2. **This end-to-end run through the real relay:** arms C/D via `examples/mcp/stdio_proxy.py` with the real scope + restart + reconcile; executor replacement established by the real runtime (§3).
3. **Existing internal controlled results (separate, not re-run here):** WEEK3 real-relay verification (243 trials, 0 resends/dupes/false claims); Week 4 scope suites; W4H full suite 6075 passed.
4. **Inference / untested:** prevalence, demand, production readiness, real (non-mock) servers, concurrent/interleaved operations, streamable-HTTP transport, and any environment other than this one.

## 8. Decision against the criteria
- Real relay **confirmed the required executor lifecycle** (scope_ended, applies=True) 10/10 — the supplied assumption of the prior experiment is now removed. ✓
- **Did not resend** the uncertain mutating call (0/10). ✓
- **Zero duplicate side effects** (0/10). ✓
- **Reconciled the observed state correctly** (10/10) **without** unsupported execution/attribution/durability/success claims (treatAsSuccess false; concepts separate). ✓
- No duplicate, unsafe resend, false claim, or StateGuard-induced regression occurred. ✓

**Decision: PASS.** The real StateGuard stdio relay safely handles the #3394 lost-response mutating-call failure in this tested environment: it establishes executor termination through the real unprivileged scope, never resends the uncertain call, causes zero duplicate effects, and reconciles the observable postcondition correctly while making no unsupported claims. **This validates only this specific failure path under this environment** — it establishes no production reliability, general MCP failure reduction, prevalence, or demand.

## 9. Limitations and the single most informative next step
- One deterministic stdio mock server; not a real server, not concurrency, not streamable-HTTP, not a deployment. Arm D's recovery still depends on a read-only probe existing for the effect (absent one, StateGuard falls back to Shadow-like safe UNKNOWN with no resolution). Latency ~0.5 s here reflects a trivial server; real restart/probe cost will differ.
- The unprivileged scope ran as root-in-userns in this environment; a non-root relay in a real container end-to-end remains the open deployment question (WEEK4H prerequisites).
- **Most informative next step:** repeat this exact end-to-end validation against a **real, third-party MCP server** (e.g. the Week 2 pinned filesystem server) exhibiting a genuine lost-response on a mutating tool, with concurrent in-flight calls, to test the lifecycle under realistic I/O and interleaving rather than a single deterministic mock. That is the smallest step that would extend the PASS beyond a mock without expanding scope.

## 10. Files changed
No repository files were modified. The only repo addition is this report (`docs/runtime/EXTERNAL_REPRODUCTION_END_TO_END.md`). The relay, reconciliation, ledger, and executor-scope sources are byte-identical to the W4H state (verified: `reconcile.py` sha unchanged). All harness/evidence lives under `/root/stateguard-captures/public-failure-evidence/e2e/`. Because no production code was touched, no repository tests required re-running; the unchanged code is covered by the W4H full suite (6075 passed, 0 failed). The harness itself ran with 0 StateGuard-induced failures across 40 arm-trials.

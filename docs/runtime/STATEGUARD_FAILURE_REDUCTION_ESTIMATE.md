# StateGuard — total MCP runtime failure-reduction estimate (current state)

**Date:** 2026-10-25. **Branch:** `claude/serene-bohr-o28snr`. **HEAD:** `b6f7ba0faece14cd6efa3a219dfedddcb0e06930`.
**Working tree:** 5 modified tracked files (`benchmarks/runtime/harness.py`, `examples/mcp/stdio_proxy.py`, `src/stateguard/adapters/mcp/__init__.py`, `src/stateguard/adapters/mcp/ledger.py`, `tests/adapters/jsonschema/test_2020_12_conformance.py`) and 57 untracked files, all inherited and left as they were. This report is one of two files added; nothing else was changed. It is uncommitted.
**Nature:** audit and estimate only. No production code, tests, docs or configuration were changed. Every benchmark rerun wrote only to the session scratchpad.
**Companion:** `docs/runtime/STATEGUARD_1_PERCENT_FAILURE_ROADMAP.md` (Phase B).

> **Labels used throughout.** **[M]** measured in this repository, or re-measured in this audit. **[X]** observed in external evidence. **[I]** inferred from code or evidence. **[A]** assumed: a scenario parameter, not a fact. **[U]** unknown.
> No MCP failure distribution observed in production exists in any source this audit could reach. **Every category share below is [A].** The shares are scenario inputs, not measurements.

---

## 1. Executive summary

**The question:** what share of total MCP runtime failures can the current StateGuard prevent, repair or safely recover from?

**Short answer.** Measured against a strictly defined in-session invocation-failure denominator and a realistic baseline, the operator-configured relay plausibly removes about **3% / 16% / 35%** (conservative / central / favorable) of baseline failures, after subtracting failures StateGuard itself causes. With the shipped **default flags** (no restart, retry only proposed, no executor scope) the central figure is about **11%**. These are relative reductions of failures in the denominator, not a residual rate. They also assume a stdio deployment, because the relay speaks stdio only. For HTTP-hosted MCP servers today's coverage is **0%**.

**Confidence:**
- Low in the magnitude. The shares are [A], and the effectiveness factors are synthetic-measured [M] and then discounted [I].
- Moderate in the direction: StateGuard removes some failures and on the measured paths adds almost none.
- High in the bounded safety result: on the lost-response path of a mutating call, StateGuard performed **0 unsafe resends** where the ordinary-retry baseline did harm in **40 of 40** paired trials. That is 20 duplicates in the in-memory #3394 harness, plus 10 unsafe resends with false failures against the real third-party filesystem server and 10 in the end-to-end order server. These are repeated trials in three harnesses, **not** independent replications in production.

**What StateGuard credibly reduces today, and under what conditions:**
1. **Input-contract (schema-drift) failures** that its repair strategies cover: type coercion, near-miss property renames, enum case and separator. On a synthetic corpus the drift-caused failures fell from 21 to 1 [M]. Calibration precision was 41/41 applied repairs correct [M], with an inferred low-single-digit false-repair risk on unseen real schemas [I].
2. **Calls against a stale tool listing.** The relay never repairs or refuses against an unconfirmed listing (5/5 incorrect interventions avoided [M]), and it re-lists on `tools/list_changed`.
3. **stdio server process death**, when the operator enables `--restart` and `--restart-policy after-unknown`. In the synthetic recovery workload the relay recovered 82 of the 116 failed calls; 20 stayed explicitly UNKNOWN and were contained, not recovered [M].
4. **Runtime state inconsistency** after lifecycle events: synthetic 8 → 0 [M].

**What it does not reduce under strict counting:**
- **Unknown outcomes.** It converts an unsafe blind resend into an explicit UNKNOWN. That is a large safety gain, but the invocation is still not a correct outcome.
- **Result-integrity problems.** These are detected (8/8 in synthetic runs, 0 false findings [M]) but never corrected.
- **Auth failures.** These are classified, not fixed.
- **Genuine tool-execution errors, configuration errors and model reasoning errors.** These are outside its control.

**Largest contributors to the remaining gap** (central, as points of baseline failure):
- tool execution: 14.6
- input schema not covered: 13.0
- timeouts / lost responses: 12.8
- init / protocol / transport: 8.3
- auth: 8.0
- server availability: 7.5

See §5.

**Suitability (§11):**
- **Internal planning:** ranges only, with labels.
- **Customer conversations:** no percentage; quote only the bounded scenario results.
- **Public claims:** no percentage at all.

---

## 2. Defined denominator

### 2.1 Unit of analysis

The **primary unit** is a **runtime `tools/call` invocation**: one JSON-RPC `tools/call` request that a client issues in an MCP session whose initialization completed, through a deployment that StateGuard is (or would be) placed in front of. Excluded from the primary unit:
- failures before the session exists (server binary missing, bad launch configuration, a failed initialize); these count in category K or B as a *session-level* metric (§2.4)
- whole-workflow failures caused by model planning; these are category L, a *workflow-level* metric

The exclusions are deliberate, and they are disclosed with every number. A 1% figure on this denominator is **not** a 1% total workflow failure rate.

### 2.2 What counts as a failure (against ground truth, not the client's view)

An invocation **fails** when its client-visible outcome is not the correct outcome for the request. Specifically:

| Outcome | Counts as failure? | Notes |
|---|---|---|
| Correct result, or a correct domain error (not found, a correct permission denial, a correct validation refusal of an invalid request) | **No** | A correct "no" is a correct outcome. |
| Wrong or incorrect error for a request that should have succeeded | Yes | Includes schema drift the server rejects. |
| No answer, hang, connection closed | Yes | |
| Explicit **UNKNOWN** outcome | **Yes** | Safer than a lie, but not correct. Never counted as a success. |
| Read-only reconcile verdict `observed_satisfied` | **Yes** under the strict metric. Counted as completion only in the separately reported **policy variant** (`RIFR-P`), where an operator has explicitly opted in. | StateGuard itself sets `treatAsSuccess: false`. |
| Duplicate side effect | Yes, and **unsafe** | |
| False success (a phantom or wrong result presented as success) | Yes, and **unsafe** | |
| False failure (the effect happened but the client is told it failed) | Yes, and **unsafe** | |
| Repaired call that executes the wrong intent (false repair) | Yes, unsafe, and **StateGuard-induced** | |

### 2.3 Primary classification, A–L (mutually exclusive)

Each failed invocation is assigned the **first** failure on its causal path as its primary category. Later contributing factors are recorded as secondary labels: for example, a timeout (F) whose retry duplicated an effect is primary F with secondary G. For overlapping kinds, the precedence is: K before B before A before C before D before H before F before G before E before I before J.

| User category | Definition used here | Internal TASKMAP/`measure.py` letter |
|---|---|---|
| **A** Server availability / crash / process lifecycle | the server process dies, or is unavailable, during the session | E (transport death), M10 workload |
| **B** Initialization, protocol, transport | framing errors, version mismatch, non-conformant messages, session or capability state lost or wrong | C, D (D never exercised), E (framing) |
| **C** Discovery / stale schema / stale tool state | the call is built or judged against a listing that is no longer current | B |
| **D** Input-schema / contract violation | the arguments do not match the current input schema | A |
| **E** Tool execution error | the tool itself fails incorrectly: a bug, an upstream outage, a transient fault | G |
| **F** Timeout, cancellation, lost response | no timely or complete answer, whatever the server-side effect | F, I |
| **G** Unknown outcome / unsafe retry | after F or A, the outcome is unknown, or a retry duplicates or misreports the effect | I (secondary), reconciliation |
| **H** AuthN / AuthZ / security | expired or missing credentials, a scope that should have been granted | K |
| **I** Output-schema / result integrity | the result violates the declared `outputSchema`, or is malformed, partial, phantom or mismatched | H |
| **J** State inconsistency / workflow reconciliation | a call executed against inconsistent runtime state, or a workflow step diverges from actual state | J |
| **K** Client/framework configuration and integration | wrong configuration, wrong client wiring (in-session part only in RIFR) | — (outside StateGuard) |
| **L** Model reasoning / tool selection / planning | the model picks the wrong tool or argument intent, or plans badly | — (outside StateGuard; excluded from RIFR) |

### 2.4 Separated quantities (never merged)

| Quantity | Definition |
|---|---|
| **Failed invocations (RIFR)** | the north-star metric (§2.5) |
| **Failed workflows** | the share of user tasks not completed correctly; dominated by L in public benchmarks [X] |
| **Unsafe side effects** | duplicate, unintended or wrong-intent effects per 10,000 invocations, and per lost-response mutation event |
| **False success / false failure claims** | counted separately from unsafe effects; each is a failure in RIFR |
| **Detection-only events** | a failure StateGuard flags (result integrity, auth class, protocol issue) without changing its outcome; still a failure |
| **Unresolved outcomes** | explicit UNKNOWNs, plus failures that pass through unchanged |
| **StateGuard-induced (Z)** | invocations that would have succeeded without StateGuard and fail with it, plus false repairs |

### 2.5 North-star metric

**Residual Invocation Failure Rate (RIFR)** = failed in-session `tools/call` invocations ÷ all in-session `tools/call` invocations, with failures judged against ground truth per §2.2 and with StateGuard-induced failures included.

Why this metric:
- It is the unit StateGuard can observe and control.
- It cannot be gamed by converting failures into "explicit" failures: UNKNOWN and detection-only events stay failures.
- It includes harm StateGuard causes.

It is paired with a **hard safety gate** that is never traded against RIFR: unsafe outcomes, meaning duplicate effects, false success and false failure, per 10,000 invocations and per lost-response mutation event. The workflow-level and session-level rates are reported next to it, so that the exclusion of L and of pre-session K is visible and never hidden.

---

## 3. Current implementation coverage matrix

**Classes:**
1. Demonstrated effective for a bounded scenario.
2. Implemented but insufficiently validated.
3. Partial detection or mitigation only.
4. Planned or absent.
5. Not safely addressable by current StateGuard.

**Action:**
- **P** prevent
- **R** repair
- **Rc** recover
- **D** detect
- **C** contain
- **Rep** report

**Mode:**
- **auto**: default flags
- **pol**: operator-enabled policy flag
- **op**: operator-supplied declaration (effects, reconcile probes)
- **sh**: Shadow, which observes only

Default flags verified in `examples/mcp/stdio_proxy.py:2348–2456`:
- `--restart 0`
- `--restart-policy idle-only`
- `--retry shadow` (proposes only)
- `--executor-scope none`
- `--call-deadline` unset
- listing refresh on

All code is production library or relay code unless marked otherwise. `examples/mcp/stdio_proxy.py` is the shipped relay, not test-only.

| Cat. | Subcategory | Code | Tests | Class | Action / mode | FP / FN risk | Existing alternatives | Incremental value | Evidence | Residual after StateGuard |
|---|---|---|---|---|---|---|---|---|---|---|
| **A** | stdio server death (idle, or between calls) | `stdio_proxy.py` `_on_server_lost` :1756; restart factory in `main` :2348 | `tests/examples/test_relay_restart.py`, `test_relay_restart_race.py`, `test_recovery_workload.py` | **1** (synthetic) / **2** (real spawn) | Rc / pol (`--restart N`) | FN: restart fails (14 of 225 in M10 `restart_fails`); FP: restart storms (no circuit breaker) | Client reconnect (manual `/mcp` in some clients), supervisors (systemd, k8s) | Restarts *with* the handshake replayed and stale state gated, inside the session | M10 [M]: restart_after_unknown recovered 82 of the 116 failed calls; replays 0, duplicates 0, Z 0 | restart failure; HTTP-hosted servers (no transport); death mid-call becomes G |
| A | Death mid-call | `_on_server_lost`, ledger UNKNOWN | `test_relay_lifecycle.py`, `test_recovery_workload.py` (`death_after_dispatch`, `two_in_flight`) | **1** for containment | C (never resent) / auto | none observed | Naive retry (unsafe) | Contains rather than duplicates | M10: 20 UNKNOWN, 0 replays [M] | stays UNKNOWN (a failure) |
| **B** | Lifecycle gating of the relay's own requests | `_refresh_listing` :664, session gate | `test_stdio_relay_protocol.py` | **1** (avoids self-induced protocol errors) | P (of Z) / auto | — | — | Avoids *causing* B | F1 fix and tests [M] | — |
| B | Protocol non-conformance | `failures.py` `classify_response` :242, `jsonrpc` helpers | `tests/adapters/mcp/test_jsonrpc.py`, `test_failures.py` | **3** | D/Rep / auto | — | SDK parsing | Classification only | synthetic C: 8 injected, 0 failing raw (protocol faults were tolerated) [M] | all real protocol failures pass through |
| B | Session / capability state loss | handshake replay after restart | `test_relay_restart.py` | **2** | Rc (after restart only) / pol | — | — | — | internal D **never exercised** by `measure.py` [M] | unknown |
| B | Non-stdio transports (streamable HTTP, SSE) | **absent** | — | **4** | — | — | — | — | grep: stdio only [I] | 100% for HTTP deployments |
| **C** | Stale listing at intervention | refresh-before-intervening (T3) `_gate_call` :860, `_refresh_listing` :664 | `test_relay_staleness.py`, `tests/adapters/mcp/test_registry.py`, `test_contract_drift.py` | **1** | P (of wrong repair or refusal) / auto | Extra `tools/list` per intervention; on refresh timeout it forwards unchanged (fail-open) | Clients re-list on `list_changed` (varies; codex#19155 shows one that doesn't [X]) | Never acts on an unconfirmed listing | M3: 5/5 [M] | the **client's** stale view is not corrected; the 2026-07-28 `subscriptions/listen` stream is not held (docstring :44) |
| C | `list_changed` handling | forwarded + relay re-lists (docstring :14) | `test_relay_staleness.py` | **1** (legacy protocol) / **4** (2026-07-28 listen stream) | P / auto | — | Client handling | Relay-side registry stays current | [M] tests | client still calls the old shape (then D-repair may apply) |
| **D** | Type drift, near-miss rename, enum case and separator | `core/engine.py`, `trust.py`, `strategies/*`, `core/validator.py`; relay `_on_call` :1542 | `tests/adapters/mcp/test_drift_corpus.py`, `test_unsafe_repair_boundary.py`, `test_false_positives.py`, `test_hostile_payloads.py`, `tests/integration/test_full_repair_flow.py`, `benchmarks/calibrate.py` | **1** (synthetic + calibration) / **2** (real schemas) | R / auto | FP: false repair (opposite-name guard: 103 → 0 [M]); 1 known abstain gap (`enum_members_collide_on_separator_only`) [M]; FN: semantic errors not repairable | The server rejects, then the **LLM self-corrects** on the error text; some clients validate inputs | Saves a round trip and the model's retry; fixes drift the model repeats | M9 synthetic A: 21 → 1 [M]; calibration 202/203, 41/41 precision, ECE 0.071 [M, re-run] | semantic/intent errors, missing required data, uncovered drift kinds |
| D | Ambiguous / unsafe repairs | `trust.py` AMBIGUOUS/REJECT → escalate/refuse | `test_unsafe_repair_boundary.py` | **1** | C/Rep / auto | refusal of a call a lax server would have accepted (a possible Z) | — | Fail-closed on ambiguity | synthetic: 2 escalated [M] | escalation is **not** recovery |
| **E** | Tool returns an error | `failures.py` classification; `effects.py` `decide_retry` :210 | `test_relay_retry.py`, `tests/adapters/mcp/test_effects.py` | **3** | Rep / auto; retry of declared idempotent or read-only tools: pol + op | FP: retry of a misdeclared tool | SDK and framework retries, application retries | Retry gated by the declared effect model | synthetic G: 8 → 8 (auto), auto_ops recovered 3 [M] | almost all real tool errors |
| E | Upstream rate limit (429 / Retry-After) | **absent** | — | **4** | — | — | client backoff | — | grep [I] | all |
| **F** | Timeout of a declared read-only/idempotent call | `--call-deadline`, `decide_retry` | `test_relay_retry.py` | **2** | Rc / pol + op | **Z: deadline cuts slow-but-healthy calls** (auto_ops Z 8 of 155 synthetic [M]) | SDK request timeout and retry | Retries only what is declared safe | synthetic F: raw 16 → auto_ops recovered 4 [M] | mutating timeouts become UNKNOWN |
| F | Lost response | ledger `InvocationLedger` :215; `_resolve_unknown` :1004 | `test_relay_ledger.py`, `tests/adapters/mcp/test_ledger.py` | **1** (containment) | C / auto | — | naive retry | explicit UNKNOWN instead of silence | synthetic I: hidden 8 → 0 under auto_ops [M] | stays a failure |
| **G** | Unsafe resend of an unknown mutation | ledger + `decide_retry` (undeclared tools never resent) | `test_relay_retry.py`, `test_outcome_truth_table.py`, `test_relay_fail_open.py` | **1** | P (of the unsafe outcome) / auto | — | framework fix (agent-framework#9204 patch [X]), idempotency keys | Transport-level and framework-agnostic | 0 resends in all harnesses: in-memory #3394 20/20 harm under baseline vs 0; e2e 10/10 vs 0; third-party fs 10/10 vs 0; WEEK3 243 trials 0 resends [M/X] | the invocation remains UNKNOWN (a failure under strict counting) |
| G | Reconciliation of an unknown mutation | `reconcile.py` (`decide`, `observe`, `Probe`), `_run_reconciliation` :1136, `executor_scope.py` | `tests/adapters/mcp/test_reconcile.py`, `tests/examples/test_reconciliation_contract_v2.py`, `test_reconciliation_lifecycle.py`, `test_relay_reconciliation*.py`, `test_executor_scope*.py` | **1** (bounded) | D (observes a postcondition) / pol + op (scope, restart, probe declaration) | FN: no predicate means no resolution; attribution unknowable by design | application reconciliation, idempotency keys | Probe runs only after `scope_ended`; never claims execution | fs 15/15 correct verdicts vs disk; harness 20/20; e2e 10/10 [M] | strict: still UNKNOWN; policy variant: completion |
| G | Second mutation in flight at the moment of loss | ledger handles several in flight | `test_recovery_workload.py` (`two_in_flight`, synthetic) | **2** | C / auto | **untested deterministically against a real server** (S2: the read resolved first 5/5) | — | — | partial [M] | open safety uncertainty |
| **H** | Auth error classification | `failures.py`; `benchmarks/runtime/auth.py` | `tests/examples/test_relay_auth.py` | **3** | D/Rep / auto | misclassification | SDK OAuth flows | clear class in audit | synthetic K: 8 → 8 [M] | all auth failures |
| H | Token refresh, OAuth | **absent** | — | **4** | — | — | SDKs do this | — | — | all |
| **I** | Output-schema violation | `results.py` `check_tool_result` :103; `_check_result` :1455 | `tests/adapters/mcp/test_results.py`, `tests/examples/test_relay_results.py` | **3** (detection only; results never altered) | D / auto | needs a declared `outputSchema`; FN otherwise | client validation (spec SHOULD) | audit finding | 8/8 synthetic, 0 false findings [M] | every bad result still reaches the client |
| **J** | Call against inconsistent runtime state (during or after restart) | ledger gating, request-during-restart handling | `test_relay_restart_race.py`, `test_relay_lifecycle.py` | **1** (synthetic) | P / auto | — | — | — | synthetic J: 8 → 0 [M] | workflow-level divergence beyond one session |
| **K** | Configuration / integration | `--env`, `--server-cwd`, `--env-passthrough` (deployment hygiene only) | `tests/examples/test_cli_environment_policy.py` | **5** | — | — | — | — | — | all |
| **L** | Model reasoning / tool selection | — | — | **5** | — | — | — | — | — | all |
| **Z** | Self-induced | Shadow mode (`--shadow`), fail-open recorder, listing-refresh fallback | `test_relay_fail_open.py`, `test_relay_proposals.py` | **1** (synthetic auto Z = 0) | C / auto | deadline Z; false repair | — | — | auto Z 0 (M9, M10, M11) [M] | real-traffic Z not measured |
| Obs. | Recording / Shadow `real_capture` | `recording.py`, `evidence.py` | `tests/examples/test_real_capture.py`, `test_relay_recording.py`, `tests/adapters/mcp/test_recording.py` | **2** | Rep / sh | redaction loss affects replay fidelity | OTel tracers | ground-truth capture for evaluation | M11 fidelity 59/59 (as documented), this audit 68/68 answers, 58/58 decisions [M] | no telemetry export |

**Verified absent:**
- circuit breaker
- streamable-HTTP/SSE transport
- holding a `subscriptions/listen` stream
- 429/Retry-After handling
- durable invocation identity across relays or sessions
- runtime telemetry export
- OAuth refresh

---

## 4. Evidence inventory and limitations

### 4.1 Re-verified from the current checkout in this audit (scratch output only)

| Check | Result | Matches recorded? |
|---|---|---|
| Full test suite (isolated Hypothesis DB, no cache) | **6075 passed** in 368 s; the preserved `.hypothesis` DB is unchanged | yes (W4H: 6075) |
| `benchmarks/runtime/measure.py --per-fault 8 --seed 0` | raw 117 → auto 89, Z 0 (auto_ops 8); hidden 45/21/45/33/9; per category: A 21→1, J 8→0, H detected 8/16; auto p50 3.6 ms vs raw 0.4 ms | yes (TASKMAP M9) |
| `benchmarks/runtime/recovery.py` | 225 invocations per arm. raw: 109 ok, 116 closed. restart_idle: 58 recovered. restart_after_unknown: 82 recovered, 20 UNKNOWN, 14 closed. Replays 0, duplicates 0, Z 0 | yes (M10) |
| `benchmarks/runtime/replay.py` | raw 31 → auto 11 → auto_ops 8; 17 of 20 auto-prevented are counterfactual; fidelity 68/68 answers, 58/58 decisions; determinism 40/40 | yes (M11; fidelity reported per answer/decision) |
| `benchmarks/calibrate.py` | 202/203 (1 known gap), 41/41 applied correct, ECE 0.071 | yes (M1) |
| Production file hashes | `reconcile.py` f8d62839…, `stdio_proxy.py` bb51b982… | unchanged during this audit |

### 4.2 External-validation artifacts (presence and counts verified earlier in this audit)

| Evidence | Trials | Kind | Verified |
|---|---|---|---|
| In-memory #3394 reproduction (`EXTERNAL_REPRODUCTION_EXPERIMENT.md`) | 20 per arm; baseline B 20 duplicates; D 0 resends, 20/20 reconciled | repeated trials, one harness | `results.json` counts match; manifest passes; external clone at commit 6373793 present |
| End-to-end through the real relay (`EXTERNAL_REPRODUCTION_END_TO_END.md`) | 10 per arm; D `scope_ended` 10/10 | repeated trials, one synthetic server | counts match |
| Third-party filesystem 2026.8.31 (`THIRD_PARTY_MCP_RECOVERY_VALIDATION.md`) | S1 10 per arm (B: 10 unsafe resends, 10 false failures; D 10/10 on every safety metric); S2 5 | repeated trials, one real server, **injected** fault | counts match; S2 concurrency **partial** |
| WEEK3 reconciliation batches | 243 trials, 0 resends | repeated trials | manifests pass for week3-real-relay, week3-audit-fixes, week3-contract-v2; the weeks 1–3 verifier passed 39/39 |
| week3-reconcile, week3-implementation, domain2-fs, capture-1/1b/zt/1-rerun | — | — | **presence only**; no manifest to verify |

**Replications versus repetitions:**
- **Independent replications** of the lost-response harm exist **in public**: #3394 was reproduced by two other people [X], and agent-framework#9204 is a separate report [X].
- Inside this repository there are **three distinct harnesses**: in-memory, synthetic stdio and real third-party. Each was run as **repeated deterministic trials**.
- Trials within one harness are not independent samples of the world. The three harnesses provide **breadth across 3 setups**, not a prevalence measurement.

### 4.3 External prevalence research (secondary only)

| Source | What it is | Usable as |
|---|---|---|
| Taraghi, Morovati, Khomh, arXiv:2603.05637 | 407 MCP-related issues out of 3,282 closed issues, 279 repos, survey of 41 | **Research-corpus frequency** of *reported* issues. A secondary summary (Pith) gives about 27% server setting, 32% server/tool configuration and 29% server/host configuration, which would make it configuration-dominated (our K and B, mostly pre-session). **Primary PDF not verified** (403). |
| Owotogbe et al., arXiv:2606.05339 | 837 runtime fault threads, 473 repos; 11 categories, 27 subcategories, 73 leaf types; survey of 55 developers (all categories observed; about 20 of 27 subcategories per respondent) | Confirms the **breadth** of the taxonomy. **No frequencies retrievable.** |
| LiveMCP-101, arXiv:2508.15760 | frontier models below 60% task success, about 5.4 tool calls per task | **Workflow-level** failure dominated by L (model and orchestration); outside RIFR |
| MCPToolBench++ (secondary write-up) | failures attributed to parameter errors, unreliable APIs, runtime issues | qualitative support for a material D share and E share |
| Observability posts (last9, oneuptime, dev.to) | alert thresholds of 1% and 5%; one anecdotal 0.8% error rate | **Not** a production prevalence figure |

**Separation required by the brief:**
- **Public issue counts:** #3394, #9204, codex#19155 and others are evidence that failures *exist*, not of how often.
- **Research-corpus frequencies:** the issue-tracker taxonomies above are biased toward setup and configuration problems people file issues about.
- **Production prevalence: [U]. No source found.**
- **StateGuard-addressable share:** [A] (§5).
- **Measured intervention effectiveness:** synthetic and bounded-real [M] (§3, §4.1).

### 4.4 Limitations of the evidence

- Every effectiveness number is synthetic, or from one real server under an injected fault.
- Real-traffic captures (Shadow `real_capture`) are few and small. They establish fidelity, not effectiveness.
- One fault per invocation, on the first call. Real failures cluster.
- Restarts in `measure.py` and `recovery.py` are in-process; real process spawns are tested separately.
- Latency figures are in-process wall time; the real-server reconcile path costs about 1.2 s p50 versus 0.69 s for ordinary retry [M].
- The privileged executor scope was run as root. A non-root container end-to-end run is still not validated.

---

## 5. Conservative, central and favorable calculations

### 5.1 Model

For each category *c*:
- **s_c** is its share of in-scope baseline failures [A]
- **a_c** is the fraction of that category the operator-configured stdio relay can act on [A/I]
- **e_c** is the incremental effectiveness on that fraction, over the realistic baseline [M-discounted/I]

The baseline includes the model self-correcting after an error, framework and SDK retries, and client reconnects.

- Relative reduction R = Σ_c s_c · a_c · e_c − z, where z is StateGuard-induced failures as a fraction of baseline failures.
- Residual rate = f0 · (1 − R), where f0 is a **hypothetical** baseline RIFR.

**Overlap:** primary categories are mutually exclusive, so the per-category terms add without double counting. A call that is repaired (D) and then fails in execution (E) stays a D residual: that is why e_D is discounted below the synthetic 20/21. Capabilities interact: restart (A) creates G cases that reconciliation then contains. That containment adds **no** RIFR credit under strict counting.

### 5.2 Category shares (all [A])

- **Conservative** assumes failures are dominated by tool execution and auth, where StateGuard is weak.
- **Favorable** assumes failures are dominated by schema drift and staleness; MCPToolBench++'s emphasis on parameter errors supports this qualitatively [X].
- **Central** sits between the two.

All three exclude L and pre-session K.

| Cat. | Conservative s | Central s | Favorable s |
|---|---|---|---|
| A availability | 7 | 10 | 11 |
| B init/protocol/transport | 7 | 9 | 7 |
| C stale discovery | 4 | 7 | 9 |
| D input schema | 10 | 20 | 26 |
| E tool execution | 33 | 15 | 12 |
| F timeout/lost response | 13 | 15 | 15 |
| G unknown/unsafe retry | 3 | 4 | 5 |
| H auth | 11 | 8 | 4 |
| I result integrity | 4 | 4 | 4 |
| J state/workflow | 1 | 3 | 4 |
| K in-session configuration | 7 | 5 | 3 |
| **Total** | 100 | 100 | 100 |

(An earlier draft of this analysis used a vector that also included L at 7/5/2. It is superseded by this one because L is outside RIFR. With L included, every reduction below scales by 1 − s_L; see §8.)

### 5.3 Effectiveness factors a·e (strict; operator-configured relay)

| Cat. | Cons. | Central | Fav. | Basis for the central value |
|---|---|---|---|---|
| A | 0.10 | 0.25 | 0.45 | a≈0.5 stdio deaths where restart applies [A]; e≈0.5: M10 recovered 82 of 116 (71%) [M], discounted for real spawn failure and for baseline client reconnect [I] |
| B | 0.02 | 0.08 | 0.15 | mostly detection; recovery only via restart and handshake replay; session category D never exercised [M/I] |
| C | 0.15 | 0.35 | 0.55 | a≈0.7 relay-visible staleness; e≈0.5, because the client's own stale view is not corrected [I]; M3 5/5 [M] |
| D | 0.15 | 0.35 | 0.60 | a≈0.6 drift kinds covered [I]; e≈0.6: synthetic 20/21 [M] discounted for real-schema variety and for LLM self-correction in the baseline [I] |
| E | 0 | 0.03 | 0.08 | only declared idempotent or read-only transient retries [M: 3 of 8 synthetic under auto_ops] |
| F | 0.05 | 0.15 | 0.30 | safe retry of declared read-only/idempotent timeouts; mutating timeouts become UNKNOWN, which is not counted [M/I] |
| G | 0 | 0 | 0.05 | strict: UNKNOWN is a failure. The favorable case allows a small policy-completion share [I] |
| H | 0 | 0 | 0.02 | classification only |
| I | 0 | 0 | 0 | detection only |
| J | 0.20 | 0.40 | 0.60 | synthetic 8→0 [M]; a≈0.5, because workflow-level divergence is not covered [I] |
| K | 0 | 0 | 0 | out of control |

### 5.4 Result (points of in-scope baseline failure)

| Cat. | Cons. s·ae | Central s·ae | Fav. s·ae |
|---|---|---|---|
| A | 0.70 | 2.50 | 4.95 |
| B | 0.14 | 0.72 | 1.05 |
| C | 0.60 | 2.45 | 4.95 |
| D | 1.50 | 7.00 | 15.60 |
| E | 0 | 0.45 | 0.96 |
| F | 0.65 | 2.25 | 4.50 |
| G | 0 | 0 | 0.25 |
| H | 0 | 0 | 0.08 |
| I | 0 | 0 | 0 |
| J | 0.20 | 1.20 | 2.40 |
| K | 0 | 0 | 0 |
| **Gross prevented/recovered** | **3.8%** | **16.6%** | **34.7%** |
| z (StateGuard-induced, §7) [A] | −1.0 | −0.2 | −0.0 |
| **Net relative reduction R** | **≈2.8%** | **≈16.4%** | **≈34.7%** |

**Shipped default flags** (no restart, retry only proposed, no scope, no deadline): A and F recovery are 0, so central ≈ C 2.45 + D 7.0 + J 1.2 + B 0.72 ≈ **11.4%** gross.

**Residual-rate illustration (hypothetical f0, not measured):**

| Hypothetical baseline RIFR f0 | Central residual f0·(1−0.164) | Points removed |
|---|---|---|
| 2% | 1.67% | 0.33 pp |
| 5% | 4.18% | 0.82 pp |
| 10% | 8.36% | 1.64 pp |

Read correctly: the central estimate reduces failures **by about 16% of baseline failures**. It does **not** bring any baseline anywhere near a residual rate **of** 1%, except one that was already below 1.2%.

### 5.5 Required separations (central, as % of in-scope baseline failures)

| Quantity | Central | Notes |
|---|---|---|
| **Prevented or recovered** | **≈16.6%** (net 16.4%) | D 7.0, A 2.5, C 2.45, F 2.25, J 1.2, B 0.7, E 0.45 |
| **Detected but not prevented or recovered** | **≈9%** [I] | result integrity flagged when `outputSchema` is declared (≈2), auth classified (≈6), protocol issues classified (≈1). These are still failures. |
| **Converted to explicit UNKNOWN (contained, unresolved)** | **≈5–8%** [I] | G (4) plus part of F and of A. M10: 20 of 116 failures stayed UNKNOWN [M] |
| **Unsafe outcomes prevented** | on the measured path: **all** blind resends of unknown mutations (0 versus 40/40 harmful baseline trials) [M]. Production share: G·(fraction of baselines that blindly retry) = **4 × [U]** | A safety metric, not an RIFR reduction. Undeclared tools are never resent (synthetic duplicates across non-declared tools: 0 [M]) |
| **Left unresolved** | **≈83%** of in-scope failures | includes the detected-only and UNKNOWN rows above |
| **Outside current scope** | K (5 of in-scope) plus **all of L and pre-session K**, excluded from the denominator; for HTTP-only deployments, **100%** | disclosed, never hidden |
| **Potentially introduced by StateGuard** | ≈0.2% of baseline failures central [A]; measured synthetic auto Z = 0; auto_ops deadline Z = 8 of 155 invocations [M] | see §7 |

### 5.6 Comparison against the real baseline

| Baseline mechanism | What it does on these failures | StateGuard's position |
|---|---|---|
| Ordinary retry (SDK, framework, application) | Recovers transient E and F on idempotent tools; on non-idempotent tools after a lost response it **duplicates or reports a false failure** (40/40 harmful in our harnesses [M]; #3394, #9204 [X]) | Strictly safer on G; roughly equal on transient E and F, where it only retries what is declared safe |
| Framework fix (agent-framework#9204 patch disables replay [X]) | Same "never replay" rule inside one framework | Redundant for that framework's users; StateGuard's value is in being framework-agnostic and in reconciliation |
| Idempotency keys or a server-side dedup ledger | Eliminates duplicate effects at the effect boundary | **StateGuard is redundant** where they exist; it adds value only where they do not |
| LLM self-correction after a validation error | Fixes many D failures at the cost of a round trip and tokens; can loop | StateGuard's incremental value on D is the part the model fails to fix, plus latency and tokens; this is why e_D is discounted |
| Client `list_changed` handling | Varies by client (codex#19155 shows a gap [X]) | The relay keeps its own view current but cannot fix the client's view |
| Supervisors (systemd, k8s) | Restart dead servers, but outside the session | The relay restarts **inside** the session with the handshake replayed and unknown calls contained |
| Client output-schema validation (a spec SHOULD) | May reject bad results | StateGuard only detects; no incremental recovery |
| SDK OAuth | Refreshes tokens | StateGuard adds nothing today |

---

## 6. Detection-only versus prevention and recovery

| Capability | Prevents | Repairs | Recovers | Contains | Detects / reports only |
|---|---|---|---|---|---|
| Schema-drift repair | — | **yes** | — | escalates ambiguity (not recovery) | — |
| Refresh-before-intervening | **yes** (wrong interventions) | — | — | — | — |
| Restart after server death | — | — | **yes** (pol) | — | — |
| Ledger + never-resend | **yes** (unsafe outcomes only) | — | — | **yes** (UNKNOWN) | — |
| Reconciliation probe | — | — | **no** under strict counting; completion only under RIFR-P | — | **observes** a postcondition |
| Safe retry (declared tools) | — | — | **yes** (pol + op) | — | — |
| Output-schema check | — | — | — | — | **detect only** |
| Auth classification | — | — | — | — | **report only** |
| Protocol classification | — | — | — | — | **report only** |
| Shadow mode | — | — | — | — | **observe only** |

None of the detection-only rows is counted in §5's prevented/recovered figure.

---

## 7. StateGuard-induced failure risks

| Risk | Mechanism | Evidence | Status |
|---|---|---|---|
| False repair | A repair maps to the wrong intent (for example an opposite-named property) | 103 → 0 after the opposite-name guard [M]; 41/41 calibration precision [M]; real-schema hold-out not measured | residual [I]: low single-digit % of applied repairs on unseen schemas |
| Deadline cut | `--call-deadline` too tight; a slow but healthy call becomes UNKNOWN | auto_ops Z 8 of 155 invocations at 0.3 s [M] | opt-in; no progress-aware deadline |
| Refusal of a call a lax server would accept | fail-closed on REJECT/AMBIGUOUS | [I] | possible Z on lax servers |
| Restart side effects | A restart re-runs server initialization (re-auth, re-index) | real spawn tested; side effects unmeasured | [U] |
| Restart storms | no circuit breaker | absent | [U] |
| Extra latency | 2–3.6 ms p50 in-process overhead [M]; reconcile path about 1.2 s p50 [M] | measured | can push calls past client timeouts [I] |
| Refresh round trip | one extra `tools/list` per intervention | [M] | bounded; fail-open on timeout |
| Relay as a single point of failure | relay crash takes down the session | fail-open recorder [M]; relay crash not measured | [U] |

Assumed z for the calculation, as a fraction of baseline failures: 1.0% / 0.2% / 0. That corresponds to about 5 / 1 / 0 per 10,000 invocations at f0 = 5%. **No real-traffic measurement of Z exists.**

---

## 8. Sensitivity analysis

| Parameter varied (central otherwise) | Change | R (gross) |
|---|---|---|
| s_D (schema share) | 10 → 30 (others rescaled pro rata) | ≈14.3% → ≈18.9% |
| e_D | 0.6 → 0.3 (strong LLM self-correction baseline) | 16.6% → 13.2% |
| Operator flags off (defaults) | A, F recovery = 0 | 16.6% → ≈11.4% |
| Count reconcile `observed_satisfied` as completion (RIFR-P) | adds ≈0.6 G + ≈0.75 F | 16.6% → ≈18% |
| L included in the denominator at share s_L | multiply by (1 − s_L) | s_L = 0.2 → 13.3%; 0.5 → 8.3%; 0.8 → 3.3% |
| Population coverage p_stdio (share of invocations through stdio servers) | multiply by p_stdio | p = 0.5 → 8.3%; p = 0.2 → 3.3% |
| z (induced) | 0 → 2% of baseline failures (aggressive deadlines, bad repairs) | net 16.6% → 14.6% |

**The three dominant sensitivities:**
1. the schema-drift share and how good LLM self-correction already is
2. the transport population (stdio versus HTTP)
3. whether L is in the denominator

Each one can move the answer by a factor of two or more. None is measured.

---

## 9. Current versus plausible expanded scope

| Scope | Central relative reduction | Basis |
|---|---|---|
| Today, default flags | ≈11% | §5.4 |
| Today, operator-configured stdio | ≈16% | §5.4 |
| Plausible relay-only ceiling (passive middleware, every relay-side lever built) | ≈40–50% | per-category ceilings: A .55, B .5, C .7, D .65, E .3, F .45 (.6 with policy), G .6 (.8 with policy), H .3, I .25, J .6, K .1 [I/A] |
| Expanded architecture (relay + server-SDK cooperation + client diagnostics + managed deployment) | ≈65–70% | roadmap §5 [A] |

What bounds the ceiling:
- Tool-internal bugs and upstream outages (E) are not fixable from the wire.
- Correct auth denials are not failures, and incorrect ones need the identity provider.
- Mutations with no idempotency key and no observable postcondition stay UNKNOWN.
- K and L are outside the path.

---

## 10. Confidence and uncertainty

| Claim | Confidence | Why |
|---|---|---|
| On the lost-response path of a mutating call through the relay, StateGuard does not blindly resend and does not claim success | **High (bounded)** | 3 harnesses, real third-party server, 0 versus 40/40, 243 WEEK3 trials, code and tests read |
| It reduces input-contract failures for covered drift kinds without measurable false repairs on the calibration corpus | **Moderate** | synthetic plus calibration; no real-schema hold-out |
| Restart recovers most stdio-death failures in a synthetic workload | **Moderate** (synthetic) / **low** (real) | in-process restarts |
| The overall in-scope reduction is about 16% (3–35%) | **Low** on magnitude, **moderate** on sign | shares [A]; factors discounted [I] |
| StateGuard-induced failures are negligible | **Low–moderate** | auto Z = 0 synthetic; deadline Z measured; real-traffic Z unmeasured |
| Production prevalence of any category | **None** | [U] |

---

## 11. Suitability of any percentage

| Audience | Suitable statement | Not suitable |
|---|---|---|
| **Internal planning** | "Scenario estimate: about 3–35% relative reduction of in-scope invocation failures, central about 16% (operator-configured stdio), low confidence; about 11% with default flags; shares assumed." Always with labels and the denominator. | any single number without its range, denominator and labels |
| **Customer conversations** | The bounded scenario results only. For example: "against a pinned third-party server with an injected lost response, ordinary retry produced 10/10 false failures; the relay produced 0 resends and 10/10 correct reconciliations." Plus the honest boundaries in `STATEGUARD_DEVELOPER_DEMO.md`. | **any** "reduces MCP failures by X%" |
| **Public claims** | none beyond reproducible scenario results with their scope | any failure-reduction percentage, any "1%" statement, any prevalence claim |

The 1% residual target is a strategic objective. It is **not** a current capability, and it is not reachable by the current relay at any plausible baseline above about 1.2% (§5.4). The roadmap that addresses it is in `STATEGUARD_1_PERCENT_FAILURE_ROADMAP.md`.

---

## Addendum (2026-10-25, later the same day): measured follow-up

The estimate above is preserved as written. The engineering that followed it is recorded in `docs/runtime/AUTONOMOUS_RIFR_ENGINEERING.md`. Changes to the inputs used above:

- **Category F/G (reads).** Unknown outcomes of operator-declared READ_ONLY tools are now re-read: after a deadline (first answer wins) and after a lost connection (on the restarted server). Measured on three pinned real servers (filesystem, memory, sqlite) with injected faults: reads failed 216/216 in raw and 0/216 with StateGuard (Wilson upper 1.7%), with 0 unsafe outcomes and 0 StateGuard-induced failures.
- **Writes.** These remain explicitly UNKNOWN and are never resent: 216/216 failures under strict counting. Blind retry "recovers" some of them only by luck. In the same runs it produced 36 duplicate effects and 36 false failures.
- **StateGuard-induced deadline failures.** A deadline scoped to calls the relay can recover removed them entirely in the synthetic corpus (16 → 0).
- **Idempotency keys** (roadmap C1) are implemented as an opt-in contract that servers must advertise. No third-party server supports it, so its measured effect on third-party traffic is 0. In the synthetic corpus, for servers that adopt it, lost-response failures fell from 18/32 to 0/32.
- **No figure in this addendum is a production failure rate,** and none supports an "RIFR < 1%" claim.

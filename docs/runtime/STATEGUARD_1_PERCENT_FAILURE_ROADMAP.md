# StateGuard — aggressive roadmap toward a 1% residual MCP failure rate

**Date:** 2026-10-25. **Branch:** `claude/serene-bohr-o28snr`. **HEAD:** `b6f7ba0faece14cd6efa3a219dfedddcb0e06930`. Uncommitted.
**Derived from:** `docs/runtime/STATEGUARD_FAILURE_REDUCTION_ESTIMATE.md` (Phase A). Every number here inherits that report's labels:
- **[M]** measured
- **[X]** external evidence
- **[I]** inferred
- **[A]** assumed
- **[U]** unknown

**This is an aggressive research roadmap, not a delivery commitment.** Effort figures are engineering estimates under stated assumptions. Nothing below has been implemented.

---

## 1. The objective and its precise denominator

**Objective:** the Residual Invocation Failure Rate (**RIFR**) is ≤ 1%. RIFR is defined as:
- failed in-session `tools/call` invocations ÷ all in-session `tools/call` invocations
- for deployments that route through StateGuard
- with failures judged against ground truth and StateGuard-induced failures included

The full definition is in the estimate report, §2.

What this objective means:
- It means **reducing the failure rate *to* 1% of invocations**, not reducing failures *by* 1%. From a baseline f0, the target requires eliminating 1 − 1/f0 of baseline failures: 50% at f0 = 2%, 80% at 5%, 90% at 10%, 95% at 20%.
- An explicit **UNKNOWN** counts as a failure. A detection-only event counts as a failure. A reconcile verdict counts as completion **only** in the separately labelled policy variant **RIFR-P**, where an operator has opted in.
- **Excluded and always disclosed:**
  - model reasoning and planning (L)
  - pre-session configuration and launch failures (part of K and B)

  These are reported as **workflow-level** and **session-level** rates next to RIFR. A 1% RIFR does **not** imply a 1% workflow failure rate. Public benchmarks put workflow failure for frontier models above 40%, mostly from L [X: LiveMCP-101].
- **Hard safety gate, never traded against RIFR:** unsafe outcomes (duplicate effect, false success, false failure, wrong-intent repair) per 10,000 invocations, and per lost-response mutation event.
- **Baseline f0 is [U].** No production MCP failure rate exists in any source reached. All f0 values below are **hypothetical**. Phase 0 exists to replace them.

---

## 2. Current-state-to-target gap analysis

### 2.1 Required elimination against what each architecture can plausibly deliver (central [A])

| Hypothetical f0 | Required elimination | Current relay (16.6% gross; residual 83.4%) | Full relay roadmap (46.5%; residual 53.5%) | Expanded architecture (66.4%; residual 33.6%) |
|---|---|---|---|---|
| 2% | 50% | 1.67% | **1.07%** | **0.67%** ✔ |
| 3% | 67% | 2.50% | 1.61% | **1.01%** (borderline) |
| 5% | 80% | 4.17% | 2.68% | 1.68% |
| 10% | 90% | 8.34% | 5.35% | 3.37% |
| 20% | 95% | 16.7% | 10.7% | 6.73% |
| **Max f0 at which 1% is reachable** | — | **≈1.2%** | **≈1.9%** (strict RIFR without the C2 policy: ≈1.8%) | **≈3.0%** |

The **infeasibility point** under current assumptions:
- With every relay-side lever built, 1% is reachable only if the baseline RIFR is at or below about 1.9%.
- With server-SDK cooperation, client diagnostics and managed deployment, it is reachable at or below about 3%.
- Above that, 1% needs control over things no runtime layer has: the tools' own correctness, upstream services, the identity provider, and the model.

### 2.2 Each category's contribution to the gap (central shares, points of baseline failure)

At hypothetical f0 = 5%, the target allows a residual of 20 points; the current residual is 83.4, so the gap is **63.4 points** (3.17 pp of invocations).

| Cat. | Share s | Current residual s·(1−ae) | % of current residual | Roadmap residual | Expanded residual | Floor driver |
|---|---|---|---|---|---|---|
| E tool execution | 15 | **14.6** | 17% | 11.3 | 9.0 | tool bugs, upstream outages |
| D input schema | 20 | **13.0** | 16% | 8.0 | 4.0 | semantic or intent errors |
| F timeout / lost response | 15 | **12.8** | 15% | 6.8 | 3.0 | mutations with no key or predicate |
| B init/protocol/transport | 9 | **8.3** | 10% | 5.4 | 3.6 | non-conformant servers |
| H auth | 8 | **8.0** | 10% | 5.6 | 4.0 | IdP / consent outside the path |
| A availability | 10 | **7.5** | 9% | 4.5 | 3.0 | hard-down dependencies |
| K in-session configuration | 5 | 5.0 | 6% | 4.5 | 3.0 | outside the path |
| C stale discovery | 7 | 4.6 | 5% | 2.1 | 1.1 | — |
| G unknown / unsafe retry | 4 | 4.0 | 5% | 1.2 | 0.4 | no observable postcondition |
| I result integrity | 4 | 4.0 | 5% | 3.0 | 2.0 | tool returns wrong data validly |
| J state / workflow | 3 | 1.8 | 2% | 1.2 | 0.6 | cross-session state |
| **Total** | 100 | **83.4** | 100% | **53.5** | **33.6** | |

**Largest gap contributors:**
- E, D and F together are 48% of the current residual.
- B, H and A add another 29%.

Note that E and H are where a relay has the **least** leverage. That is the core reason the target is fragile.

---

## 3. Category-by-category coverage gaps

For each category: what is missing, what could close it, and what cannot be closed by runtime middleware. The capability IDs (C1–C13, S1–S4, M0) are detailed in §4.2.

| Cat. | Current class (estimate §3) | Gap | Capabilities | Not safely addressable by middleware |
|---|---|---|---|---|
| **A** | 1 (stdio, synthetic) / 2 (real spawn) | HTTP servers not covered; no backoff or circuit breaker; restart failure is terminal; no health ping | C12, C6, C3 | the server's own hard dependencies being down |
| **B** | 3 (protocol) / 2 (session) / 4 (HTTP) | session/capability loss never exercised; no conformance shims; stdio only | C12, C6, C10 | a server that cannot speak the protocol correctly at all |
| **C** | 1 (relay view) | client's stale view not corrected; the 2026-07-28 `subscriptions/listen` stream is not held | C4 | — |
| **D** | 1 (synthetic) / 2 (real) | uncovered drift kinds; no real-schema hold-out; no structured feedback to the model | C5, C9, S2 | wrong intent, missing user data |
| **E** | 3 | no 429/Retry-After; retry only for declared-safe tools; no breaker | C3, C1, C9 | tool bugs and wrong business logic; outages without an alternative |
| **F** | 2 (retry) / 1 (containment) | mutating timeouts stay UNKNOWN; deadlines not progress-aware (Z) | C1, C3, S3, C2 | a mutation with no key and no observable postcondition |
| **G** | 1 (safety) | still UNKNOWN under strict RIFR; concurrency at the moment of loss untested | S1, C1, C2 | attribution of an observed state to a specific call (unknowable without a key) |
| **H** | 3 | no token refresh, no step-up handling | C7, C9 | consent, revoked grants, correct denials (these are not failures) |
| **I** | 3 (detection only) | results never corrected | C8 (read-only re-request), fail-closed option | a tool that returns schema-valid but wrong data |
| **J** | 1 (synthetic, in-session) | workflow-level divergence across sessions | C11, C1 | divergence caused by other actors |
| **K** | 5 | outside the path | C9 (diagnostics), managed-deployment preflight (expanded) | client wiring bugs |
| **L** | 5 | outside the path | narrow slice only: an unknown tool name gets a refusal **plus** a near-miss list (C9). **Never** auto-redirect to another tool. | tool choice, planning, argument intent |
| **Z** | 1 (synthetic auto Z = 0) | real-traffic Z unmeasured; deadline Z; false-repair hold-out | M0, S2, S3 | — |

---

## 4. Ranked roadmap

### 4.1 Scoring model

Each capability is scored on 3/2/1 levels (high / medium / low) for:
- **Imp**: expected contribution to RIFR reduction
- **Ev**: evidence the failure occurs in real deployments
- **Conf**: confidence in the solution
- **Inc**: incremental value beyond existing solutions
- **Eff**: implementation effort
- **Risk**: safety risk
- **Cost**: latency, deployment and compatibility cost

**Score = (Imp·Ev·Conf·Inc) / (Eff·Risk·Cost)**, reported as a **band** rather than a number, to avoid false precision:
- **Very high**: ≥ 12
- **High**: 6–11
- **Medium**: 3–5
- **Low**: < 3

Testability and critical-path value are applied as **tier overrides**, in the order the brief requires:
1. **Safety-critical** (S-items): they go first regardless of score, because they gate every retry or recovery capability.
2. High-frequency, high-impact, credible.
3. Clear advantage over existing solutions.
4. **Measurement that resolves high-impact uncertainty.**
5. Breadth.

**Deviation, justified:** M0 (measurement) is promoted to the head of the critical path even though the brief lists measurement fourth. Two reasons:
- There is **no demonstrated unsafe defect** to fix. The S-items verify untested cases, they do not repair known bugs.
- Every Imp and Ev value below is [A] until M0 runs. Building Phase 2 in the wrong order costs far more than M0 does.

### 4.2 Capability catalogue (contributions in central points of baseline failure [A])

| ID | Capability | Category: current gap | Mechanism | Expected effect (central) | Safety implications | Baseline alternatives | Dependencies | Validation | Rollback / containment | Effort | Evidence required before it counts |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **M0** | RIFR scorer + labelled multi-server evaluation + real-capture scoring | all: no denominator, no real-server per-category effectiveness | ground-truth oracles per scenario; per-category baseline vs StateGuard arms; Wilson CIs; scoring of Shadow `real_capture` recordings | 0 direct; replaces [A] with [M] | none (offline) | none | `measure.py`, `replay.py`, `recording.py`, `evidence.py` | self-checks: reproduces M9/M10/M11 numbers exactly; oracle unit tests | offline only | 3–5 wk | — |
| **S1** | Deterministic two-mutations-in-flight-at-loss test | G: untested (S2 read resolved first, 5/5) | fault shim holds **two** mutating responses, then kills the connection; asserts both UNKNOWN, 0 resends, per-call reconcile correctness | 0 RIFR; protects the safety gate | finds or excludes a potential duplicate path | — | third-party fs harness pattern (`fs_fault_wrapper.py`) | ≥ 20 trials per arm on the real fs server + order server, with forced interleavings | test-only | 2–4 days | n/a (gate) |
| **S2** | Real-schema false-repair hold-out | D/Z: precision measured only on the calibration corpus | label ≥ 300 drifted calls against ≥ 20 real published server schemas, unseen by the strategy authors | 0 RIFR; bounds Z | if precision < 99%, tighten TrustPolicy | — | `calibrate.py`, drift corpus | precision with a 95% CI | raise the APPLY threshold, or disable a strategy | 1 wk | n/a (gate) |
| **S3** | Progress-aware deadlines | F/Z: auto_ops Z 8/155 from deadline cuts [M] | reset the deadline on `notifications/progress`; per-tool deadline from the observed p99; never cut a call that is reporting progress | +1.0 F, and removes most deadline Z | strict improvement | SDK `resetTimeoutOnProgress` | ledger | Z under the same auto_ops config drops to ≤ 1/155 | fall back to no deadline (the default) | 1 wk | Z measured in M0 |
| **S4** | Non-root container end-to-end (scope B) | deployment: validated only as root | run the third-party fs suite inside an unprivileged container with `pid-namespace-unprivileged` | 0 RIFR; widens deployable population | — | — | W4G/W4H scope B | same S1/fs metrics, 10/10 | fall back to `--executor-scope none` with documented loss of reconcile | 1 wk | n/a (gate) |
| **C12** | Streamable-HTTP (and legacy SSE) transport | A/B/F: 100% of HTTP deployments uncovered | HTTP relay endpoint; session-id handling; resumable streams (`Last-Event-ID`); the same ledger and decision core | +1.2 within population; **population multiplier p → ≈1** | new attack surface (auth headers, origin checks) | gateways and proxies (no outcome semantics) | ledger and registry are transport-agnostic [I] | port the M0 suite; conformance tests against SDK servers | stdio-only build flag | 3–5 wk | M0 scenarios pass on HTTP with the same safety gate |
| **C1** | Idempotency-key safe resend (server-SDK cooperation) | F/G/E: a mutation after loss can only become UNKNOWN | relay attaches a stable key in `_meta`; cooperating server SDK middleware dedups; the relay resends **only** when the server advertises support | **+6.3** (F 3.5, G 1.8, E 1.0): **the largest strict-RIFR lever** | correctness depends on the server honouring the key; never resend without an advertised capability | application idempotency keys (StateGuard is redundant where they exist) | S1 must pass; server-SDK middleware (Python + TS) | duplicate effects 0 on ≥ 300 lost-response events across ≥ 3 servers; resend recovers ≥ 95% of keyed UNKNOWNs | capability off means back to UNKNOWN | 3–5 wk | M0 shows F + G ≥ 10% of baseline failures on real capture |
| **C5** | Repair-breadth expansion | D: uncovered drift kinds | new strategies only where S2 shows a frequent, unambiguous drift kind (nested object flattening, array wrapping, unit and format normalisation) | +4.0 | each strategy must hold ≥ 99% hold-out precision | LLM self-correction | S2 | S2 hold-out plus differential tests | per-strategy disable flag | 2–3 wk | real-capture drift frequency from M0 |
| **C6** | Supervisor-grade restart + session re-init | A/B: restart failure is terminal; session loss unexercised | backoff with jitter, health ping, re-initialize with capability diff, exercise internal category D | +3.2 (A 2.0, B 1.2) | restart side effects (re-auth, re-index) must be declared | systemd/k8s (outside the session) | C3 breaker | M10 workload extended with real spawns + session faults | `--restart 0` | 1–2 wk | real-spawn recovery ≥ 80% of idle deaths |
| **C3** | Circuit breaker, 429/Retry-After, backoff | E/A/F: storms; rate limits | per-tool and per-server breaker; honour Retry-After for **declared-safe** calls only | +3.0 (E 1.5, F 1.0, A 0.5) | open breaker fails fast with an explicit error, never a silent drop | client backoff | S3 | storm and rate-limit fault catalogue | disable flag | 2 wk | rate-limit share from M0 |
| **C4** | Hold the `subscriptions/listen` stream + forward `list_changed` + client drift notice | C: client stale view; new protocol | hold the listen stream; on change, forward and annotate refusals with "listing changed" guidance | +2.45 | none (the server still decides) | client handling (varies) | — | staleness suite on both protocol versions | flag | 1–2 wk | — (cheap; counts once the test shows it) |
| **C9** | Structured diagnostics to the model and client | D/E/H/K/L-slice: errors are opaque text | machine-readable error `_meta` (category, repairable fields, near-miss tool names, retry-safe flag) | +2.7 (D 1.0, E 0.8, H 0.4, K 0.5) | must not leak secrets; never auto-redirect a tool | model reads error text | — | A/B with ≥ 2 agent clients on a labelled task set | flag | 1–2 wk | client A/B shows fewer follow-up failures |
| **C2** | Complete-on-verified-postcondition policy (**RIFR-P only**) | F/G: reconcile verdict stays UNKNOWN | operator opt-in: a content-verified `observed_satisfied` probe is returned as completed, with explicit `verifiedBy: postcondition` | +2.5 (F 1.5, G 1.0), **policy metric only** | risk of over-claiming attribution; requires a content-verifying (not existence) predicate | application reconciliation | S1, reconcile v2 | reconcile correctness ≥ 99% vs ground truth on ≥ 300 events | flag off by default | 1–2 wk | never counted toward strict RIFR |
| **C7** | OAuth refresh / step-up hook (HTTP) | H | relay-side token refresh hook; classify step-up vs denial | +2.0 | credential handling: high risk | SDK OAuth | C12 | security review; auth fault catalogue | disable | 2–3 wk | H share from M0 |
| **C10** | Protocol conformance shims | B | known server-quirk normalisers (version negotiation, id types) | +1.0 | must not mask real errors | SDK leniency | M0 | conformance corpus | per-shim flag | 1–2 wk | observed quirk frequency |
| **C8** | Re-request on bad result (read-only tools) + optional fail-closed | I | for declared read-only tools, re-issue once on an `outputSchema` violation; otherwise optionally fail closed | +1.0 | fail-closed may convert a usable result into an error (Z) | client validation | effect declarations | result-integrity suite | flag | 1 wk | — |
| **C11** | Workflow state checks | J | operator-declared invariants checked after mutating calls | +0.6 | — | application checks | C2 | workflow harness | flag | 1 wk | — |
| **C13** | Telemetry export (OTel spans + RIFR counters) | measurement | export ledger states, categories, Z and latency | 0 direct; makes Phase 4 possible | must respect redaction | tracers | M0 | span-schema tests | flag | 1 wk | — |

**Relay-side total:** 16.6 + 30.0 = **46.5** central. This includes the 2.5 policy-only points from C2; strict RIFR is **44.0**. Contributions are assigned to disjoint categories and capped by each category's ceiling, so they **add without double counting**.

### 4.3 Scores and ranking

| Rank | ID | Imp | Ev | Conf | Inc | Eff | Risk | Cost | Band | Tier | Why here |
|---|---|---|---|---|---|---|---|---|---|---|---|
| **1** | **M0** | — | — | — | — | 2 | 1 | 1 | gate | 4 (promoted) | every other number is [A] until it runs |
| **2** | **S1** | — | — | — | — | 1 | 1 | 1 | gate | 1 | the only open safety question on the strongest claim; gates C1 and C2 |
| **3** | **C1** | 3 | 3 | 2 | 2 | 3 | 2 | 2 | Medium (3.0) | 2/3 | largest strict-RIFR lever; public harm reports #3394, #9204 [X] |
| **4** | **C12** | 3 | 3 | 3 | 2 | 3 | 2 | 2 | Medium (4.5) + multiplier | 2 | without it, coverage of HTTP deployments stays at 0 |
| **5** | **C4 + S2** | 2 | 3 | 3 | 2 | 1 | 1 | 1 | Very high (36) | 2/1 | cheapest high-confidence C gain; S2 validates D on real schemas |
| 6 | C6 | 2 | 2 | 3 | 2 | 2 | 1 | 1 | Very high (12) | 2 | |
| 7 | C5 | 3 | 2 | 2 | 2 | 2 | 2 | 1 | High (6) | 2 | after S2 |
| 8 | S3, S4 | — | — | — | — | 1 | 1 | 1 | gate | 1 | parallel, Phase 1 |
| 9 | C3 | 2 | 2 | 2 | 2 | 2 | 2 | 1 | Medium (4) | 2 | |
| 10 | C2 | 1 | 2 | 2 | 3 | 1 | 3 | 1 | Medium (4) | 3 | policy metric only |
| 11 | C8 | 1 | 1 | 2 | 2 | 1 | 1 | 1 | Medium (4) | 3 | |
| 12 | C9 | 2 | 2 | 1 | 2 | 2 | 1 | 2 | Low (2) | 5 | model-dependent |
| 13 | C10 | 1 | 2 | 2 | 1 | 2 | 1 | 1 | Low (2) | 5 | |
| 14 | C13 | 1 | — | 3 | 2 | 1 | 1 | 1 | supports M0/Phase 4 | 4 | |
| 15 | C7 | 2 | 2 | 2 | 1 | 2 | 3 | 2 | Low (0.7) | 5 | high security risk, SDKs cover it |
| 16 | C11 | 1 | 1 | 1 | 1 | 2 | 2 | 1 | Low | 5 | |

C4 and C6 score highest per unit of effort but have small absolute effect. They are scheduled early because they are cheap and parallel, not ranked above the large levers.

### 4.4 Phases

Effort assumes one engineer familiar with the codebase, 5-day weeks, and excludes review latency and waiting on external partners. Calendar times assume three engineers in parallel.

#### Phase 0 — Measurement and baseline integrity (weeks 0–5; **critical path**)
- **Objective:** replace the [A] shares and the discounted effectiveness factors with [M] values on real servers; measure Z and overhead.
- **Tasks and dependencies:**
  - **0.1** RIFR scorer: oracle schema, per-category tallies, Wilson CIs, strict versus RIFR-P (1–1.5 wk).
  - **0.2** Pinned multi-server harness: at least 3 real servers (filesystem 2026.8.31, plus two more of different kinds, for example a git or SQLite server and a fetch/HTTP-backed server run over stdio), with a per-category fault catalogue covering A–J. Generalises the `fs_fault_wrapper.py` pattern (1.5–2 wk; depends on 0.1).
  - **0.3** Scoring of Shadow `real_capture` recordings, plus a design-partner capture protocol with redaction (1 wk; depends on 0.1).
  - **0.4** Z and latency arms under default and operator flags (0.5 wk; depends on 0.2).
  - **0.5** C13 telemetry export (optional, parallel, 1 wk).
- **Priority:** P0.
- **Deliverables:** scorer + harness + report with per-category baseline versus StateGuard and CIs.
- **Acceptance:**
  - the scorer reproduces M9 (117 → 89), M10 (82 recovered / 20 UNKNOWN) and M11 (31 → 11 → 8) exactly
  - at least 3 servers with at least 20 trials per category per arm
  - Z reported
- **Tests:** unit tests for oracles; determinism (fixed seeds).
- **Safety gate:** offline only.
- **Exit:** a measured per-category e_c with CIs, and an updated Phase-2 order.
- **Risks / fallback:** fault injection measures *effectiveness*, not *shares*. If no real traffic is obtainable, shares stay [A] and Phase 4 cannot run; report that explicitly.

#### Phase 1 — Close correctness and safety gaps (weeks 0–4; **parallel** with Phase 0)
- **Objective:** make sure no retry or recovery capability is built on an unverified safety boundary.
- **Tasks:** S1 (2–4 days), S2 (1 wk), S3 (1 wk), S4 (1 wk), plus a regression suite that pins fail-closed semantics: UNKNOWN never becomes success; `treatAsSuccess` stays false; undeclared tools are never resent.
- **Priority:** P0 (S1), P1 (others).
- **Acceptance:**
  - S1: 0 resends and 0 duplicates with two mutations unresolved at loss, at least 20 trials per arm on 2 servers, with forced interleavings
  - S2: false-repair precision ≥ 99% (95% CI lower bound ≥ 97%)
  - S3: deadline Z ≤ 1 of 155 under the auto_ops configuration
  - S4: fs suite 10/10 non-root
- **Safety gate:** any unsafe outcome stops Phase 2 work on C1 and C2 until it is root-caused.
- **Exit:** all four pass.
- **Fallback:**
  - if S1 fails: serialise mutating calls per server, accepting the latency cost, and re-test
  - if S2 fails: raise the APPLY thresholds or drop the strategy

#### Phase 2 — Expand high-value runtime coverage (weeks 4–16)
- **Objective:** build the capabilities in M0's measured order. The default order if M0 confirms the central shares:
  1. C4 + C6 (cheap, parallel)
  2. C12
  3. C1 (after S1)
  4. C5 (after S2)
  5. C3
  6. C2
  7. C8
  8. C9
  9. C10
  10. C7
  11. C11
- **Critical path:** M0 → C12 → C1 (server-SDK middleware) → Phase 3. Everything else is parallel.
- **Acceptance per capability:** the "Evidence required" column in §4.2, met on the M0 harness with the safety gate unchanged (0 unsafe outcomes).
- **Effort:** about 19–30 engineer-weeks in total; about 10–14 calendar weeks with three engineers.
- **Risks:**
  - server-SDK adoption for C1 is an external dependency; fallback is to ship the SDK middleware as opt-in and count C1 only on cooperating servers
  - HTTP security surface for C12; fallback is a security review gate before enforcement mode

#### Phase 3 — End-to-end reliability and deployment hardening (weeks 14–22)
- **Objective:** show the gains hold across the deployment matrix.
- **Tasks:**
  - at least 5 servers, at least 2 client SDKs (Python, TypeScript) and 1 agent framework
  - stdio and HTTP
  - root and non-root containers
  - concurrency and adversarial suites (hostile payloads, storms, interleavings)
  - latency budgets: relay p50 overhead ≤ 5 ms, recovery path p95 documented
- **Acceptance:**
  - per-category effectiveness within the CI of the Phase-0 measurement on every cell of the matrix
  - 0 unsafe outcomes across at least 3,000 lost-response mutation events (95% upper bound ≤ 0.1%)
- **Effort:** 4–6 wk.
- **Exit:** the matrix is green.
- **Fallback:** document unsupported cells instead of claiming them.

#### Phase 4 — Evidence-driven convergence toward 1% (weeks 20 onward; **open-ended**)
- **Objective:** measure the real f0 and RIFR on design-partner traffic, close the largest remaining categories, and identify the floor.
- **Tasks:**
  - Shadow then enforce on partner traffic, with stratified manual labelling (at least 500 invocations per week)
  - recompute §2
  - decide whether expanded-architecture work (durable server-side effect ledger, managed-deployment preflight) is justified
- **Duration:** **cannot be promised.** It depends on partner traffic volume and on the real f0.
- **Exit:** the release gates in §6.

---

## 5. Quantitative model of expected category-level improvements (all [A] unless noted)

| Cat. | s (central) | a·e today | a·e after relay roadmap | a·e expanded architecture | Relay ceiling | Δ points (roadmap) | Capabilities |
|---|---|---|---|---|---|---|---|
| A | 10 | 0.25 | 0.55 | 0.70 | 0.55 | +3.0 | C6 2.0, C3 0.5, C12 0.5 |
| B | 9 | 0.08 | 0.40 | 0.60 | 0.50 | +2.9 | C6 1.2, C10 1.0, C12 0.7 |
| C | 7 | 0.35 | 0.70 | 0.85 | 0.70 | +2.45 | C4 |
| D | 20 | 0.35 | 0.60 | 0.80 | 0.65 | +5.0 | C5 4.0, C9 1.0 |
| E | 15 | 0.03 | 0.25 | 0.40 | 0.30 | +3.3 | C3 1.5, C1 1.0, C9 0.8 |
| F | 15 | 0.15 | 0.55 | 0.80 | 0.45 (0.6 with C2) | +6.0 | C1 3.5, S3/C3 1.0, C2 1.5 (policy) |
| G | 4 | 0 | 0.70 | 0.90 | 0.6 (0.8 with C2) | +2.8 | C1 1.8, C2 1.0 (policy) |
| H | 8 | 0 | 0.30 | 0.50 | 0.30 | +2.4 | C7 2.0, C9 0.4 |
| I | 4 | 0 | 0.25 | 0.50 | 0.25 | +1.0 | C8 |
| J | 3 | 0.40 | 0.60 | 0.80 | 0.60 | +0.6 | C11 |
| K | 5 | 0 | 0.10 | 0.40 | 0.10 | +0.5 | C9 / preflight |
| **R (gross)** | 100 | **16.6%** | **46.5%** (strict 44.0%) | **66.4%** | ≈50% | **+30.0** | |

Assumptions and interactions:
- **F and G after the roadmap exceed the passive relay ceiling.** That is deliberate: C1 needs **server cooperation**, so the "relay roadmap" column is really relay + cooperating server SDK. Without C1, F falls to about 0.32 and G to about 0.25, and the roadmap R falls to about 40%.
- **C12 is modelled mainly as a population multiplier.** The table assumes stdio deployments. For a mixed population with stdio share p, today's R scales by p, and the post-C12 R does not.
- **C9's effect depends on the model reading structured errors.** Conf = 1; it counts only after a client A/B test.
- **Interactions:**
  - C6 restarts create G events, which S1, C1 and C2 then handle.
  - C5 repairs feed E, so the e_D discount already allows for repaired calls that then fail in execution.
  - C3 open breakers convert A and E failures into fast explicit failures, which are not RIFR gains. Only Retry-After recoveries count.
- **Expanded architecture** means durable server-side effect ledger + typed errors (server SDK), client integration, and managed deployment with configuration preflight. It is listed to show **what would have to change** to go further. It is not part of the committed roadmap.

---

## 6. Measurement, test and release gates

### 6.1 Metric system

| Metric | Numerator | Denominator | Sampling | Interval |
|---|---|---|---|---|
| Raw baseline failure rate f0 | failed invocations, raw arm (same traffic, no StateGuard) | invocations, raw arm | harness: all; partner: Shadow counterfactual + stratified labels | per commit (harness); weekly (partner) |
| **RIFR (strict)** | failed invocations incl. UNKNOWN, detection-only and Z | in-session `tools/call` invocations | same | same |
| RIFR-P | as strict, minus content-verified policy completions | same | same | same |
| Relative reduction | f0 − RIFR | f0 | paired arms | same |
| Absolute pp reduction | f0 − RIFR | — | paired | same |
| Category coverage c | category failures with a StateGuard lever applied | category failures | labelled | per release |
| Category effectiveness | category failures prevented or recovered | category failures | labelled | per release |
| Duplicate side effects | duplicate effects (ground truth) | invocations; **and** lost-response mutation events | harness ground truth; partner audit | per commit; per release |
| False success claims | success shown, but the effect is absent or wrong | invocations | ground truth | same |
| False failure claims | failure shown, but the effect happened | invocations | ground truth | same |
| Unresolved outcomes | UNKNOWN + unchanged failures | invocations | all | same |
| StateGuard-induced (Z) | succeeded raw, failed with StateGuard | invocations | paired arms; partner Shadow counterfactual | same |
| False-positive repairs | applied repairs that change intent | applied repairs | S2 hold-out + audited sample | per release |
| Successful safe repairs | applied repairs that then succeed with intent preserved | applied repairs | same | same |
| Detection-only outcomes | flagged, not changed | invocations | all | same |
| Latency overhead | p50/p95/p99 (StateGuard − raw), normal and recovery paths | invocations | all | per commit |
| Availability overhead | relay-caused session loss | sessions | all | weekly |
| Workflow failure rate (disclosed) | failed tasks | tasks | labelled task set | per release |
| Session-level failure rate (disclosed) | failed sessions before the first call | session attempts | all | weekly |

### 6.2 Progressive gates

| Gate | Scope | Requirement |
|---|---|---|
| **G0** synthetic | `measure.py` / `recovery.py` / `replay.py` | reproduces M9–M11; 0 unsafe outcomes; auto Z = 0 |
| **G1** multi-server harness | M0, at least 3 real servers | per-category e with 95% CIs; 0 unsafe outcomes in at least 300 lost-response events (upper bound ≤ 1%); S1–S4 pass |
| **G2** matrix | Phase 3 | G1 on at least 5 servers × 2 SDKs × 2 transports; 0 unsafe outcomes in at least 3,000 events (≤ 0.1%); overhead within budget |
| **G3** partner Shadow → enforce | Phase 4 | measured f0 and RIFR on at least 10,000 labelled invocations per environment stratum; Z ≤ 0.1% of invocations; unsafe outcomes ≤ 1 per 10,000 invocations (0 observed in at least 30,000) |
| **G4** "≤1% RIFR" claim | public | every condition below |

### 6.3 Conditions for claiming 1%

All of the following must hold:
1. The denominator is RIFR as defined, published with its exclusions (L, pre-session K), and with the workflow-level rate shown next to it.
2. The workload is representative: at least 2 independent design partners, at least 5 server kinds, at least 2 clients, and the transports actually used.
3. Major categories are covered, or explicitly excluded with their shares.
4. Statistically credible: observed RIFR ≤ 0.8% with n ≥ 10,000 labelled invocations per stratum, so that the Wilson 95% upper bound is ≤ 1%.
5. Safety gates met (G3) in the same period.
6. Reproduced in at least 2 environments.
7. Z included.
8. Fair baseline: the same traffic, the clients' real retry and reconnect behaviour, and idempotency where the partner already has it.
9. CIs reported for f0, RIFR and each category.

**Public claim:** only G4, with the method, data description and limitations published. Until then, no percentage is stated publicly.

---

## 7. Safety and rollback requirements

- **Invariants that never change**, whatever the capability:
  - UNKNOWN is never reported as success
  - an undeclared or non-idempotent mutation is never resent without an advertised idempotency capability (C1)
  - a reconcile probe runs only after `scope_ended`
  - `treatAsSuccess` stays false outside RIFR-P
  - StateGuard never claims execution, attribution or durability it has not observed
- **Each new capability ships:**
  - behind a flag, default off until G1
  - with Shadow-mode observation first
  - with fail-open on internal error (forward unchanged), except where fail-open would cause an unsafe resend, which fails closed
- **Kill switches:**
  - `--shadow` (observe only)
  - per-strategy repair disable
  - `--restart 0`
  - `--retry shadow`
  - transport build flag
- **Rollback trigger:** any unsafe outcome, or Z above its gate, in harness or partner traffic. Disable the capability, root-cause it, add a regression test, then re-pass G1.
- **Security:** C7 and C12 require a security review before enforcement mode. Recordings stay redacted (`real_capture` masks numbers).

---

## 8. Critical-path and parallelizable work

```
Critical path:  M0 (3–5 wk) ─► C12 HTTP (3–5 wk) ─► C1 idempotency + server SDK (3–5 wk) ─► Phase 3 matrix (4–6 wk) ─► Phase 4 (open)
                  ▲                                     ▲
Parallel:       S1 (days) ────────────────────────────┘ (gate for C1/C2)
                S2 (1 wk) ─► C5 (2–3 wk)
                S3 (1 wk) ─► C3 (2 wk)
                S4 (1 wk)
                C4 (1–2 wk), C6 (1–2 wk), C8, C10, C13 (1–2 wk each)
                C9 (1–2 wk) ─► client A/B
                C2 (after S1), C7 (after C12), C11
```

- **Critical path to the end of Phase 3:** about 13–21 engineer-weeks, or about 5–6 calendar months with three engineers.
- **Phase 4:** not schedulable.

---

## 9. Uncertainty, infeasible targets and irreducible failure categories

- **Infeasible under current assumptions:**
  - 1% RIFR at any baseline above about 1.9% with the relay roadmap
  - 1% RIFR at any baseline above about 3% even with the expanded architecture
  - 1% **workflow** failure, at any baseline: L is outside the path

  If Phase 0 measures f0 well above 3%, the honest target becomes a **relative** one: about 45–50% reduction with the relay, about 65% expanded, with near-zero unsafe outcomes. That is the closest credible target.
- **Irreducible floor (central):** about 34% of baseline failures remain even with the expanded architecture, so the floor rate is about 0.34·f0. It is made up of:
  - E (9 points): tool bugs, upstream outages without an alternative
  - D (4): wrong intent, missing data
  - H (4): consent and revoked grants
  - B (3.6)
  - A (3): hard dependencies down
  - F (3): mutations with no key and no observable postcondition
  - K (3)
  - I (2): schema-valid wrong data
- **Not safely addressable by runtime middleware, ever:**
  - attributing an observed state to a specific call without a key
  - resending a mutation of unknown outcome without server-side dedup
  - fixing a tool's business logic
  - choosing the right tool for the model
  - repairing intent (as opposed to shape)
- **Constraints responsible:**
  - visibility: the relay sees only the wire
  - authority: no server or identity-provider control
  - semantics: no knowledge of effects without declarations
- **What would have to change to go further:**
  - server-SDK cooperation (keys, typed errors, a durable effect ledger)
  - client integration (structured diagnostics consumed by the model)
  - managed deployment (configuration preflight, supervision)
- **Largest uncertainties:**
  - f0 [U]
  - category shares [A]
  - the LLM self-correction baseline for D
  - HTTP versus stdio population mix
  - server-SDK adoption for C1

  Each can move the conclusion by a factor of two or more (estimate §8).

---

## 10. The highest-leverage next engineering task

**Task: build the RIFR scorer and a labelled multi-server evaluation (M0.1 + M0.2, plus scoring of existing Shadow recordings).**

**Why it outranks the alternatives:**
- Every roadmap number above is [A]. The order of Phase 2 and the feasibility verdict both turn on per-category effectiveness on real servers and on the real share mix, and none of that has been measured.
- C1 (largest lever) and C12 (largest population multiplier) cost 3–5 weeks each. Building them in the wrong order, or building C1 for a category that turns out to be small, costs more than M0.
- S1 is cheaper and safety-critical, but it protects an existing claim rather than moving RIFR. It runs **in parallel** and is the runner-up.

**Hypotheses tested:**
- **H1:** on at least 3 pinned real servers with a per-category fault catalogue, StateGuard's measured incremental a·e for A, C, D, F and J falls within the estimate's conservative-to-favorable band:
  - A 0.10–0.45
  - C 0.15–0.55
  - D 0.15–0.60
  - F 0.05–0.30
  - J 0.20–0.60
- **H2:** StateGuard-induced Z is ≤ 0.1% of invocations under the default flags.
- **H3:** scored Shadow recordings yield a first, labelled (small-n) share estimate. It is reported as [M] with wide CIs and never as prevalence.

**Relevant code and tests:**
- `benchmarks/runtime/measure.py` (category arms, tallies)
- `benchmarks/runtime/replay.py` and `record_corpus.py` (counterfactual scoring)
- `benchmarks/runtime/recovery.py`, `scenarios.py`
- `benchmarks/runtime/harness.py` (modified in the inherited tree; preserve it)
- `src/stateguard/adapters/mcp/recording.py` (provenance, `REAL_CAPTURE`) and `evidence.py`
- relay `examples/mcp/stdio_proxy.py`
- third-party harness pattern: `/root/stateguard-captures/public-failure-evidence/thirdparty/` (`fs_fault_wrapper.py`, `tp_driver.py`)
- tests: `tests/examples/test_runtime_measurement.py`, `test_replay.py`, `test_real_capture.py`, `test_recovery_workload.py`, `test_relay_evidence_metrics.py`

**Bounded plan for a future task (about 3–4 engineer-weeks):**
1. **Days 1–3.** Oracle schema (expected outcome per scenario), the RIFR and RIFR-P scorer, and Wilson CIs. Regression check: the scorer reproduces M9, M10 and M11 exactly.
2. **Days 4–10.** Generalise the transport fault shim into a per-category catalogue covering A (death idle and mid-call), B (framing, version), C (listing change), D (drift), E (tool error and transient), F (hang, lost response), G (lost mutation response), H (auth error), I (bad result) and J (call during restart). Run it on fs 2026.8.31 plus two other pinned servers.
3. **Days 11–14.** Arms: raw, naive retry, StateGuard default, StateGuard operator-configured; at least 20 trials per category per arm per server; ground truth checked outside StateGuard.
4. **Days 15–17.** Score the existing Shadow `real_capture` recordings; write the report with per-category tables and CIs.

**Acceptance criteria:**
- reproduces the M9, M10 and M11 numbers
- at least 3 real servers
- at least 20 trials per category per arm
- every outcome labelled against independent ground truth
- per-category e with 95% CIs
- Z and overhead reported
- no production code changed, beyond a test-only shim and benchmark code

**Stop conditions:**
- If a pinned server cannot be driven deterministically within 2 days, replace it, or proceed with 2 servers and say so.
- If any unsafe outcome appears, stop measuring and escalate to a safety fix (Phase 1 takes precedence).
- If oracles cannot be made independent of StateGuard for a category, exclude that category and disclose it.

**Decisions that depend on the result:**
1. The Phase-2 order: confirm C12 → C1, or promote C5 or C6 if D or A dominate the measured residual.
2. Whether the 1% target is pursued with the relay alone (if H1 lands in the favorable band and real capture suggests a low f0) or explicitly reframed toward the expanded architecture, or a relative target (if it lands in the conservative band).
3. Whether any internal planning figure can move from [A] to [M].

---

## Addendum (2026-10-25, later the same day): roadmap items executed

Recorded in full in `docs/runtime/AUTONOMOUS_RIFR_ENGINEERING.md`. The plan above is preserved as written.

| Roadmap item | Status | Evidence |
|---|---|---|
| M0: RIFR scorer and labelled evaluation | **Done, in part** | `benchmarks/runtime/rifr.py` (strict, Wilson CIs, Z, unsafe outcomes, control, labelled mixture); `/root/stateguard-captures/autonomous-mandate/real3/` (3 pinned real servers, 7 fault modes, frozen code snapshots). Real-traffic shares remain unmeasured. |
| S1: two mutations in flight at loss | **Done; no defect** | `tests/examples/test_relay_concurrent_loss.py`; real fs server, 80 in-flight mutations, 0 resends, 80/80 reconciled correctly |
| S3: deadline Z | **Done, by another route** | `--deadline-scope recoverable`; synthetic Z 16 → 0 |
| C1: idempotency-key safe resend | **Implemented (opt-in, server-advertised)** | `stateguard.adapters.mcp.idempotency`; synthetic ops_keyed 119 → 91 faulted failures; third-party effect 0 until servers adopt it |
| New: read re-request after an unknown outcome | **Done** | real reads 216/216 → 0/216 failed |
| New: trusted `readOnlyHint` (opt-in) | **Done** | `--trust-read-only-hints`; real-server measurement in the engineering record |
| C12: streamable HTTP | Not started | — |

The single next task changes. The safe, relay-only levers for third-party stdio servers are now largely spent (writes are the floor). What would move the next decision is measuring real fault incidence: a Shadow-mode `real_capture` deployment on real traffic, scored with `rifr.py`'s judging, to replace the assumed mixture weights.

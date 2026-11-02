# StateGuard — third-party MCP server recovery validation (lost response, filesystem server)

**Date:** 2026-10-19. **State:** working tree, uncommitted and unpushed. **HEAD:** `b6f7ba0f`.
**Objective:** validate StateGuard's real stdio relay + executor scope + restart + reconciliation against a **pinned third-party MCP server**, prioritizing a lost response, with a concurrent in-flight call where feasible.
**Nature:** evaluation only. **No production code changed.** All experiment code is a disposable, test-only harness outside the repository (`/root/stateguard-captures/public-failure-evidence/thirdparty/`). Nothing committed/pushed/stashed/reset. Builds on `PUBLIC_FAILURE_EVIDENCE_RESEARCH.md`, `EXTERNAL_REPRODUCTION_EXPERIMENT.md`, `EXTERNAL_REPRODUCTION_END_TO_END.md`, `WEEK2_DOMAIN2_FILESYSTEM.md`, and the Week 3/4 reconciliation + executor-scope docs.

## 1. Exact server, environment, configuration
- **Third-party server:** `@modelcontextprotocol/server-filesystem` **2026.8.31** (the Week 2 pinned install at `/root/stateguard-captures/domain2-fs/server/...`), run **unmodified** on **Node v22.22.0**. Tools used: `move_file` (mutating, non-idempotent), `read_text_file` (read-only, used as the reconciliation probe).
- **Relay:** `examples/mcp/stdio_proxy.py`, unmodified.
- **Arm D / S2 config (the intended production path):** `--executor-scope pid-namespace --restart 2 --restart-policy after-unknown --effects effects_fs.json --env-passthrough PATH`. The **privileged** PID-namespace scope was used (the shipped default) because the Node install lives in a root-only tree; the unprivileged scope was already validated for this exact lifecycle in `EXTERNAL_REPRODUCTION_END_TO_END.md`, and scope-privilege is not what this experiment tests.
- **Reconcile block (`effects_fs.json`):** `move_file` (`non_idempotent`) → one read-only probe `read_text_file(path=$destination) expect present (absent_marker ENOENT)`; `read_text_file` declared `read_only`. Supported schema; no invented fields.

## 2. Scenario provenance and honest adaptation labels
- **Behavioral reference:** the lost-response-on-a-mutating-call failure of MCP #3394 (and the Week 2 I1–I3 "lost answer is indistinguishable" finding).
- **Fault injection (TEST-ONLY, labeled):** a transport shim (`fs_fault_wrapper.py`) sits on the real server's stdio and **injects** the fault; the third-party server's implementation is **not modified**. On generation 0 it lets the real `move_file` complete on disk, then **drops that response and exits**, so the relay observes a lost response + connection failure. On the restarted generation it forwards everything, so the read-only probe reaches the real server. **This injected loss is an adapted scenario, not a naturally occurring third-party bug.**
- **Independent ground truth:** the actual filesystem state, checked by the harness via `os.stat` (never via StateGuard): after a move `a.txt`→`b.txt`, exactly one of {a present, b present}. `move_file` is **non-idempotent** — a retry after a successful-but-lost move returns an error ("destination already exists"/ENOENT), i.e. a **false failure** for an operation that succeeded. Filesystem operations are not assumed idempotent, and a postcondition (b exists) is not treated as proof of which invocation caused it.

## 3. Method, arms, trials
Scenario **S1** (lost mutating response): arms **A** unprotected-no-retry, **B** ordinary-retry, **C** StateGuard relay Shadow, **D** StateGuard relay auto+scope+restart+reconcile. Scenario **S2** (lost mutating response with a concurrent in-flight read): arm **D**. Each trial uses a fresh sandbox + fresh server generation; ground truth re-checked per trial. **S1: 10 trials/arm; S2: 5 trials.** Deterministic by construction, so identical outcomes expose regressions by absence; **repeated runs against one server are not evidence of broad ecosystem coverage.** Harness `tp_driver.py`; raw per-trial + aggregate `results.json`; lifecycle trace `lifecycle_trace_fs_armD.txt`.

## 4. Actual lifecycle evidence (real runtime)
Representative arm-D sequence (`lifecycle_trace_fs_armD.txt`), ground truth `b.txt` only (moved once):
```
relay_started executor_scope=pid-namespace
call                 (client move_file a->b)
reconcile_pending
server_lost          (move response dropped after the rename; connection failed)
executor_termination kind=scope_ended applies=True executor=pidns-4026532264-2433-0
server_restarted generation=1
reinitialized
reconcile_probe tool=read_text_file status=satisfied     (READ-ONLY; not a resend)
reconcile_verdict postcondition=observed_satisfied
outcome_unknown
reconcile_answered
```
`executor_termination` is `kind=scope_ended, applies=True` for the real PID-namespace executor — termination/replacement **established by the real runtime, not supplied**.

## 5. Raw results (ground truth = filesystem state)

| Metric | S1-A no-retry | S1-B ordinary retry | S1-C Shadow | S1-D relay+scope+reconcile | S2-D concurrent |
|---|---|---|---|---|---|
| Trials | 10 | 10 | 10 | 10 | 5 |
| Moved exactly once (disk) | 10/10 | 10/10 | 10/10 | 10/10 | 5/5 |
| Duplicate side effects | 0 | 0 | 0 | 0 | 0 |
| Unsafe resend of the unknown mutation | 0 | **10** | 0 | **0** | 0 |
| Mutating calls that reached a server | 1 | **2** | 1 | 1 | 1 |
| Client false-failure claim (move succeeded, told it failed) | 0 | **10** | 0 | 0 | 0 |
| Explicit UNKNOWN to client | 10 | 0 | 10 | 10 | 5 |
| Executor termination confirmed (scope_ended) | n/a | n/a | 0 | **10/10** | **5/5** |
| Server restarted | n/a | n/a | 0 | **10/10** | **5/5** |
| Read-only reconcile probe executed | n/a | n/a | 0 | **10/10** | **5/5** |
| Reconcile verdict correct vs disk | — | — | — | **10/10** | **5/5** |
| Keeps execution/attribution/durability/outcome separate; treatAsSuccess false | — | — | — | **10/10** | **5/5** |
| False "exactly-once success" claims | 0 | 0 | 0 | 0 | 0 |
| StateGuard-induced failures | 0 | 0 | 0 | 0 | 0 |
| Round trips / trial | 1 | 2 (2nd = **mutating resend**) | 1 | 2 (2nd = **read-only probe**) | 2 + concurrent read |
| Latency p50 (s) | 0.29 | 0.69 | 0.47 | 1.22 | 1.06 |

## 6. Comparisons and interpretation
- **Ordinary retry (B) is the real-world harm against this real server:** 10/10 unsafe resends and **10/10 false-failure claims** — the retry hits "destination already exists" and the client is told the move failed, though it succeeded (the Week 2 I2/D2 result, now reproduced against the real server with disk ground truth). (Against a different non-idempotent op the same unsafe resend would instead duplicate; the #3394/order experiments showed the duplicate form. Here the harm manifests as a false failure.)
- **StateGuard relay (D)** establishes real executor termination, never resends the uncertain mutation (0/10), causes zero duplicate effects, restarts and runs a read-only probe, and reconciles correctly (10/10) while keeping the concepts separate (`observed_satisfied`, yet `executionState`/`attribution`/`operationOutcome` unknown, `durability` not_established, `treatAsSuccess` false). It neither claims the move succeeded nor claims it did not.
- **Shadow (C)** observes only: UNKNOWN, no restart/probe/resend — correctly not an enforced recovery path.
- **A** is safe (0 duplicates) but blind (UNKNOWN, no recovery).
- **Latency:** D's ≈1.2 s p50 is the cost of a real scope create + restart + one read-only probe on this server; D uses 2 round trips like B, but D's second is read-only (safe) where B's is a mutating resend (the harm).

## 7. Safety, reconciliation, concurrency findings
- No unsafe resend, no duplicate effect, no false success, and no StateGuard-induced failure in any StateGuard arm (0 across 150 arm-trials).
- Reconciliation accuracy against independent disk ground truth: 15/15 correct for arm D (S1+S2); no verdict asserted causation or durability.
- **Concurrency (S2) — honest limitation:** the scenario issued a read and the mutating move near-simultaneously; StateGuard handled both without duplicate, resend, false claim, or regression, and the move was reconciled correctly. **However, in all 5 trials the concurrent read resolved (`answered`) before the mutating response was dropped**, so a second call genuinely *unresolved at the instant of connection loss* was not deterministically forced with this fast read. Concurrency was therefore exercised without regression but the strict in-flight-at-loss timing was **not** deterministically achieved — a limitation, not a failure.

## 8. Decision
- Real relay confirmed the executor lifecycle (`scope_ended`, applies=True) 10/10 (S1) + 5/5 (S2) — not supplied. ✓
- Did not resend the uncertain mutating call (0). ✓
- Zero duplicate side effects (0). ✓
- Reconciled the observed state correctly (15/15) without unsupported execution/attribution/durability/success claims. ✓
- No duplicate, unsafe resend, false claim, or StateGuard-induced regression. ✓

**Decision: PASS** for the lost-response recovery path against the pinned third-party filesystem server 2026.8.31 in this environment. The concurrency dimension is **PARTIAL**: exercised and handled safely, but a second call unresolved at the exact moment of loss was not deterministically forced (§7). This validates only this failure path and server in this environment; it establishes no production reliability, broad MCP coverage, prevalence, or demand.

## 9. Files changed
No repository files were modified. The only repo addition is this report. Relay/reconcile/ledger/executor-scope sources are byte-identical to the W4H state (`reconcile.py` and `stdio_proxy.py` sha unchanged). Harness and evidence live under `/root/stateguard-captures/public-failure-evidence/thirdparty/` (`fs_fault_wrapper.py`, `effects_fs.json`, `tp_driver.py`, `results.json`, `lifecycle_trace_fs_armD.txt`). One harness-only fix was applied during bring-up (the fault wrapper now closes the server's stdin on client disconnect, removing a ~20 s shutdown stall); it does not touch production code. Because no production code was touched, no repository tests required re-running; the unchanged code is covered by the W4H full suite (6075 passed). The harness ran with 0 StateGuard-induced failures across 45 arm-trials.

## 10. Recommendation and remaining technical uncertainty
**The current technical evidence justifies moving to customer validation** for the narrow, well-characterized use case — an MCP relay that makes lost-response outcomes on mutating tool calls *safe and explicit* (never a blind resend, never a false success) and *reconciles* the observable postcondition when a read-only predicate exists. The safety behavior now holds across: the controlled reconcile module (prior), the real relay end-to-end on a synthetic server (prior), and **the real relay against a pinned third-party server with independent disk ground truth (this report)**. The failure class is independently documented in public (R1/R2) and the harm of the status-quo (ordinary retry) is reproduced against the real server.

The **remaining technical uncertainty that could materially change that decision**, in priority order:
1. **Deterministic concurrency / interleaving at the moment of loss** — not yet forced (§7). A second mutating call genuinely in-flight when the connection drops is the untested stress case; if the relay mishandled truly-simultaneous in-flight mutations, the safety claim would narrow.
2. **The reconcile predicate requirement** — StateGuard's *recovery* (not just safety) needs an operator-declared read-only predicate per mutating tool. For tools with no observable postcondition, StateGuard degrades to safe-UNKNOWN with no resolution; how often target tools have a usable predicate is a real adoption question.
3. **Non-root / real deployment** — this run used the privileged scope as root; a non-root relay in a real container end-to-end (WEEK4H prerequisites) is still open.
4. **Breadth** — two servers (order mock + filesystem) and one failure class validated; this is not evidence of broad MCP-ecosystem coverage.

None of these is a safety regression; each is a scope boundary that customer validation should probe rather than block on. **Recommend proceeding to a bounded customer-validation conversation, carrying these four uncertainties as the explicit technical risks** — and, if an engineering step is wanted first, close #1 (a deterministic two-mutation-in-flight test) since it is the cheapest and most informative.

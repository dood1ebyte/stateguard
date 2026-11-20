# Week 3 — Reconciliation contract v2: runtime hardening and independent verification

**Date:** 2026-10-16. **Branch:** `claude/serene-bohr-o28snr`, HEAD `b6f7ba0` (= `origin/main`). Everything is uncommitted; nothing was committed, pushed or merged.

**Contract:** `docs/runtime/RECONCILIATION_CONTRACT_V2.md`.

**New evidence:** `/root/stateguard-captures/week3-contract-v2/`, hashed in `EVIDENCE_SHA256.txt`.

**Labels used below:**
- **[measured]** independently measured on the real relay, with ground truth from disk and server logs;
- **[test]** test results;
- **[claim]** implementation claims — what the code is designed to do;
- **[assumption]** unresolved assumptions.

## 1. Verdict

**Retain the capability — as an embedding-only, fail-closed observation contract.** It is not a CLI recovery feature in this release.

- **[measured]** Through the real relay, under the new contract, every outcome described only what the evidence established. Across 426 trials and 20 CLI-refusal runs:
  - 0 resends, 0 duplicate deliveries of the original;
  - 0 claims of execution, attribution, durability or success;
  - every request answered exactly once.
- **[measured]** The standard CLI cannot supply termination evidence, so it now **refuses** `reconcile` blocks at startup: 20/20 runs, with no server started and an actionable message. Its ordinary behaviour is unchanged.
- **[measured]** Reconciliation does useful, honest work only where an embedder can assert that the executor's scope ended. With a kernel-enforced PID-namespace scope (a benchmark embedder, not a product configuration):
  - 115 of 188 trials gave an observation; 53 were `undetermined`; 20 got no postcondition (shadow pass-through 15, no-fault 5);
  - the audit's detached-descendant counterexample became a true, lasting `observed_satisfied`, 5/5.
- **[measured]** The weaknesses the audit found are still real. A presence check passes on wrong content, another actor's work satisfies a predicate, and state can change between probes. v2 labels each of these instead of hiding it; it does not remove them.

## 2. Initial working tree and baseline

**State at start:**
- branch `claude/serene-bohr-o28snr`, HEAD `b6f7ba0faece14cd6efa3a219dfedddcb0e06930` (= `origin/main`);
- 4 tracked files modified and 19 untracked files — the Week 3 implementation, audit and fixes;
- the ignored files are caches, `.venv`, `.coverage` and two old `benchmarks/results/run_*.json`.

**Frozen copy.** The full tracked diff (1,180 lines), copies of all 22 changed or untracked source and doc files, and their hashes are in `baseline/`. Every hash matched `WEEK3_AUDIT_FIXES.md`, for example relay `f0dce73d2ed1c494` and library `c91a9ac94b584ab3`.

**[test] Baseline checks:**
- the focused reconciliation, restart, recording and replay suites: 204 passed;
- `ruff check`, `ruff format --check`, `mypy src` and strict mypy: clean.

**Pre-fix code reconstructed byte-exactly.** The audit-fix patch script had been kept, so reverse-applying it reproduced the original files, verified by hash:

| File | Hash |
|---|---|
| relay | `466f9a7fa342e598` |
| library | `11208ec6d4db3c66` |
| exports | `bebcea566519dbe1` |
| harness | `55fc97d105d37c2f` |
| `test_relay_reconciliation.py` | `af64fbdf18abc31c` |

They are stored in `prefix-code/`, so the claim "these tests fail on the pre-fix code" is demonstrated rather than asserted (§6.4).

**The three original regressions still had meaningful tests:** A (termination), B (shutdown answer), C (probe-id collision). They were strengthened, not weakened (§6).

**[measured] Prior evidence is unchanged.** `verify_prior.sh` checks 39 items, and all pass at start and at end:
- Week 1 recordings;
- the Week 2 manifest and results;
- Week 3 experiment, implementation, audit and audit-fix evidence;
- historical reports and benchmark inputs;
- the committed replay tapes (git-clean).

## 3. Contract and state-machine changes

The full specification, including the state diagram, is in `RECONCILIATION_CONTRACT_V2.md`.

**Outcomes say only what was observed. [claim, test, measured]**

| v1 | v2 | Meaning |
|---|---|---|
| `verified` | `observed_satisfied` | Every declared check held when it was read |
| `not_verified` | `observed_not_satisfied` | The checks answered and the predicate did not hold; this is not "not executed" |
| `undetermined` | `undetermined` | The evidence could not decide |

**What a reconciled answer now carries.** `error.data` states explicitly what no observation establishes:
- `executionState: unknown`
- `attribution: unknown`
- `durability: not_established`
- `operationOutcome: unknown`

It also carries `reconciliationContract: "2"`, and an `observation` object with the window (`observed_from`/`observed_to`, UTC milliseconds), each predicate with its **strength** (`presence` or `content`), and `content_checked`. `reconciliation.establishes` and `does_not_establish` are spelled out too.

**Envelope unchanged.** It is still JSON-RPC `-32000`. Both SDKs raise on it, which is the safe reading of an unknown outcome, and both expose `data` (§7).

**Messages are reworded.** A satisfied presence-only answer reads: "observed the declared presence-only predicate (content was not checked) holding at … That does not show this request executed or caused it, that it still holds, or that the operation succeeded. Do not resend it."

**Time bound corrected and enforced. [claim, test]** Discovery is now one 20 s budget, however many restarts are allowed. Before, it waited per restart: (N+2) × up to 30 s.
- The worst case is `ReconcileSpec.worst_case_seconds`: 10 s termination wait + 20 s discovery + Σ probe timeouts, at most **150 s**.
- Measured: a never-listing server took **40.3 s** under v1 and takes **20.05 s** under v2 (`S12e`, 3/3).

## 4. Termination evidence and CLI behaviour

**CLI. [measured, test]**
- `stdio_proxy.py main()` rejects any effects file with `reconcile` blocks, in auto, restart, shadow and passthrough modes: exit 2, before any server starts.
- The message names the missing capability (it can only reap its direct child; detached descendants and external systems could still act) and the remedy (embed `StdioRelay` with a restart factory returning `Restarted(..., TerminationEvidence.scope_ended(scope, executor=...), executor=...)`).
- Measured: 20/20 runs, 0 servers started, 20/20 with the actionable message.
- Without `reconcile` blocks the CLI relays as before, including an effects file with a repeated key, which is read exactly as before.

**Embedders: a trust boundary, bound to one executor. [claim, test]**

`scope_ended(scope, *, executor)` now requires the identity of the executor it is about.

*Where identities come from:*
- every server generation gets an identity from the embedder: `StdioRelay(..., executor=…)` for the first, `Restarted(..., executor=…)` for each restart;
- identities are validated (1–120 safe characters) and must be unique per relay;
- a malformed or reused identity is rejected and logged, and that generation can never be attested.

*When the relay probes:* only if the evidence names exactly the identity of the generation the lost call was sent to. These are all refused:
- evidence naming another executor;
- the same evidence object returned again by a later restart;
- evidence for a generation with no identity;
- anything other than `scope_ended`: `not_reported`, `direct_child_reaped`, `process_group_empty`.

*What the assertion means:* it is documented as the embedder's assertion, which StateGuard cannot verify and logs. It explicitly excludes external systems and other actors.

**[claim] Review of the defect-A fix.** The audit-fix version had a reusable, unbound `scope_ended(scope)`. A factory could return one constant object forever and pass. In v2 that object applies to at most one generation. A lazy embedder that echoes back whichever identity it is asked about can still lie; that stays the embedder's responsibility and is stated as such.

**[measured] Reference embedder** (`benchmarks/domains/mcp_server_filesystem/embedded_relay.py`; benchmark only, not shipped):
- **`--scope direct-child`** reports what `main()` can see: `direct_child_reaped`. That is insufficient, so it never probes: 158/158 restart reports were insufficient, 0 probes.
- **`--scope pid-namespace`** runs each generation as init of a new PID namespace. The kernel kills every member when init exits; the embedder confirms through `/proc` that the namespace is empty, then asserts `scope_ended` for that executor: 168/168 established.

## 5. Predicate semantics and response routing

**Probe and predicate contract. [claim, test]**
- **Content check against a known value.** New `expect: {"text_sha256_equals": "<hex>"}` checks content against a declared constant, for tools whose effect's content is not one of their arguments (`move_file`). `text_equals_argument` remains for `write_file`-like tools.
- **Ambiguity refused.** A `text_equals_argument` that names one of the probe's own arguments is refused as ambiguous.
- **Repeated keys.** In files with `reconcile` blocks, a repeated JSON key is refused; files without them load as before.
- **Unchanged:** read-only declaration, `$ref`-only arguments, ≤ 4 probes, timeouts in (0, 30], no resend-like keys, malformed and unknown results fail closed.
- **Strength never upgraded.** `content_checked` is true only when every positive check reads content; presence-only answers say so.

**Routing. [claim, test, measured]**
- **Defect D (found in this audit).** The relay-owned `tools/list` ids and retry ids were still routed by prefix, the same class as defect C:
  - a client request with id `stateguard-relay:…` or `stateguard-retry:…` lost its response;
  - a client `tools/list` with such an id could be synced as the relay's own.
- **Fix.** Both are now matched against the exact issued-id tables the relay already keeps (`_epoch_of`, `_client_of`). The id formats did not change, so the replay tapes are unchanged and still pass.
  - Settled own ids drop late or duplicate answers, unless a client request with that id is in flight, in which case the answer is the client's.
  - The relay never issues an own, retry or probe id that a client has already used.
- **Residual exposure.** If a client reuses an id the relay issued while that relay request is still pending, the first answer goes to the relay (logged `response_id_collision`). JSON-RPC ids are per-sender, so only rewriting client ids would remove this.
- **Shutdown.** The answer lifecycle (`held → answering → removed`) and the 2 s bounded settle are unchanged from the audit fix. `reconcile_answered.written` is renamed `written_to_stream`, meaning flushed into the client's stream, not known to be read.

## 6. Adversarial test matrix (actual outcomes)

**[test] Full suite: 5,968 passed, 0 failed, 0 skipped.** Of these, 132 are reconciliation tests (90 before this package).

### 6.1 Termination and lifecycle

| Case | Test | Outcome |
|---|---|---|
| Positive attestation, in-process executor | `test_a_scope_ended_evidence_is_what_allows_a_probe` | pass: `observed_satisfied`; execution and attribution `unknown` |
| Positive attestation, real kernel scope | `test_a_pid_namespace_scope_is_positive_evidence_and_the_descendant_cannot_act` | pass: `observed_satisfied`; the detached descendant died with the namespace and never rolled back |
| Missing attestation | `test_a_custom_factory_without_evidence_never_probes`, `test_a_factory_that_leaves_the_old_executor_running_…` | pass: `undetermined`, 0 probes |
| Attestation not for this call's executor | `test_evidence_naming_another_executor_never_allows_a_probe` | pass: `undetermined` ("names a different executor"), 0 probes |
| Stale attestation reused | `test_evidence_from_an_earlier_restart_does_not_carry_over` | pass: the first loss gives `observed_satisfied`, the second `undetermined` |
| Reused executor identity | `test_a_reused_executor_identity_is_rejected_and_never_attests` | pass: rejected and logged; that generation has no identity |
| Restart failure | `test_a_restart_that_fails_records_no_evidence_and_never_probes` | pass: `undetermined`; exactly one answer |
| Direct-child exit with a detached descendant alive | `test_a_direct_child_exit_with_a_detached_descendant_alive_is_never_probed` | pass: `undetermined`, 0 probes; the descendant then rolled back (ground truth on disk) |
| Process group emptied, descendant alive | `test_a_process_group_emptied_by_the_restart_does_not_establish_termination` | pass: the group is verifiably empty, the `setsid` descendant is still alive, `undetermined` |
| External side effect | `test_an_external_side_effect_is_outside_any_termination_evidence` | pass: `observed_not_satisfied` saying it "does not show the request did not execute"; the external worker executed afterwards |
| CLI with reconciliation | `test_main_refuses_reconciliation_at_startup`, `test_main_refuses_unsafe_or_unusable_configuration` | pass: exit 2 in every mode, no server, actionable message |
| CLI without reconciliation | `test_main_relays_normally_without_reconciliation`, plus the existing relay suites | pass |

### 6.2 Predicate semantics

| Case | Test | Outcome |
|---|---|---|
| Satisfied by the original | `test_satisfied_by_the_original_is_observation_not_proof` | pass: `observed_satisfied`, presence-only, not proof |
| Already satisfied before the original (which failed) | `test_a_predicate_that_already_held_is_satisfied_without_attribution` | pass |
| Satisfied by another actor | `test_another_actors_state_is_observed_satisfied_without_claiming_execution` | pass |
| Wrong content, presence check | `test_wrong_content_passes_a_presence_check_and_says_so` | pass: satisfied, labelled "content was not checked" |
| Wrong content, content check | `test_wrong_content_fails_a_content_check` | pass: `observed_not_satisfied` |
| Right content, content check | `test_the_right_content_passes_a_content_check_and_is_still_only_an_observation` | pass |
| State changes after observation | `test_a_state_that_changes_after_the_observation_is_not_reported_durable` | pass: `durability: not_established`; the message says it may no longer hold |
| Misleading probe server | `test_a_misleading_probe_server_can_only_mislead_the_observation` | pass: a wrong `observed_satisfied` (outside the trust model), with no execution or success claim |
| Malformed, error, timeout or dead probe server | `test_no_outcome_is_ever_worded_as_proof` (10 combinations), plus the existing failing-probe tests | pass: `undetermined`; never worded as proof |
| Weak predicate never stronger | unit `test_presence_checks_are_labelled_as_such`, `test_every_outcome_states_what_it_does_not_establish` | pass |
| Bounded discovery | `test_the_discovery_wait_is_one_budget_however_many_restarts` (`max_restarts=8`) | pass: under 4 s with a 1 s budget |

### 6.3 Concurrency and routing

These use events and thread joins, never sleeps, and count answers at end-of-stream.

| Case | Test | Outcome |
|---|---|---|
| Shutdown races a worker mid-answer | `test_a_pending_reconciliation_is_answered_before_the_relay_stops` | pass: 1 answer, `written_to_stream: true` |
| Worker finishes just before shutdown | `test_a_worker_that_finishes_just_before_shutdown_is_answered_once` | pass |
| Worker blocked beyond the bound | `test_an_answer_that_cannot_be_written_in_time_is_reported_unconfirmed` | pass: the pump stops within the bound; `reconcile_answer_unconfirmed`; later `written_to_stream: false`; 0 answers claimed |
| Repeated shutdown; 8 concurrent finishers | `test_a_repeated_shutdown_…`, `test_concurrent_finishers_answer_exactly_once` | pass: exactly one answer |
| Duplicate, late and out-of-order probe answers; unknown ids | `test_duplicate_…`, `test_late_and_out_of_order_…`, `test_a_response_for_an_unknown_id_…` | pass |
| Client ids resembling probe, own and retry ids | `test_a_client_id_with_the_probe_prefix_…`, `test_client_ids_that_look_like_relay_owned_ids_are_answered_normally` | pass: the real result, exactly one answer |
| No original ever resent | `_never_resent` in every unknown-outcome test | pass |

### 6.4 Shown to fail on the code each test targets

`tests/examples/test_reconciliation_prefix_regressions.py` uses only interfaces common to all three versions:

| Version | A | B | C | D (own id) | D (retry id) |
|---|---|---|---|---|---|
| Pre-audit-fix, reconstructed byte-exactly (relay `466f9a7f`) | **fail** | **fail** | **fail** | **fail** | **fail** |
| Post-audit-fix baseline (relay `f0dce73d`) | pass | pass | pass | **fail** | **fail** |
| Current (relay `a6f2b00f`) | pass | pass | pass | pass | pass |

**[measured] One test-validity bug was caught and corrected.** The first version of the id-collision tests passed on the old code for the wrong reason: the relay's "outcome unknown" answer sent at close counted as the one answer. The tests now require the server's real result while the session is open. The other new tests were written against the current code; I did not run each one against the old trees, since many use v2 interfaces.

## 7. Real-relay results (all denominators)

**Testbed (unchanged):** the pinned `@modelcontextprotocol/server-filesystem` 2026.8.31 (TS SDK 1.32.1, Node 22.22.0); the Week 2 fixture; the audit's fault shim and termination test server, both unchanged (hashes verified).

**New for this package:**
- `handoff_server.py`, whose effect is performed by an external worker outside the executor scope;
- content-check scenarios C1–C5.

**Runner:** `contract_v2_relay.py`. Its client records every line until end-of-stream.

**Ground truth:** sandbox hashes at the answer and at the end, plus the shim's and servers' logs.

**Batch:** 426 trials plus 20 refusal runs, 5 repetitions (S12e: 3). All production code is byte-identical to `batch/code-under-test.sha256`.

| Measure | `emb_pidns` (n = 188) | `emb_direct` (n = 188) | `cli_raw` (n = 40) | `cli_naive` (n = 10) |
|---|---|---|---|---|
| Originals executed (ground truth) | 128 | 133 | 25 | 5 |
| `observed_satisfied` / `observed_not_satisfied` / `undetermined` | 60 / 55 / 53 | 0 / 0 / 168 | — | — |
| Postcondition correct vs disk at answer | satisfied 50/60; not satisfied 55/55 | — | — | — |
| `observed_satisfied` false at answer | **10** (S6d concurrent writer between probes 5; S6e lying probe server 5) | 0 | — | — |
| `observed_satisfied` with wrong intent (content or state) | **20** (S4c, S4d presence-only wrong content 10; S6d, S6e 10) | 0 | — | — |
| `observed_satisfied` not caused by the original | **35** (other actor, pre-existing state, failed original) | 0 | — | — |
| `observed_not_satisfied`, then executed later | **5** (X1, external worker) | 0 | — | — |
| Execution / attribution claims | 0 | 0 | 0 | 0 |
| Durability / success claims | 0 | 0 | 0 | 0 |
| Duplicate original deliveries / relay resends | 0 / 0 | 0 / 0 | 0 / 0 | 0 / 0 |
| Exactly one response per request (EOF count) | 188/188 | 188/188 | 40/40 | 10/10 |
| Termination established | 168/168 restart reports, in 158 trials (30 trials had no restart) | 0 of 158 restart reports | — | — |
| Probes (failures) | 290 (45, all in probe-fault scenarios) | 0 | 0 | 0 |
| Latency, median / p95 / max | 0.40 / 3.03 / 20.05 s | 0.036 / 1.05 / 3.04 s | 0.036 / 1.04 / 1.06 s | 0.032 / 0.038 s |
| Probed latency, median / p95 (n = 145) | 0.40 / 2.39 s | — | — | — |
| Observation window, median / p95 / max | 0.011 / 0.019 / 2.00 s | — | — | — |
| Audit content leaks / reconcile events with paths / sentinel changed | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 | 0 / 0 / 0 |

**Notes on the table:**
- **Undetermined is never success.** The 53 `emb_pidns` undetermined trials are S6a–c, S7a–b, S8a–b (deadline: never probed), S9 (restart fails), S12c–e. They are not counted as recoveries anywhere. The 168 `emb_direct` undetermined trials are the configuration doing its job — correctly refused probes — not recoveries.
- **Content checks.** With content checks (C1–C5), the wrong-content cases that passed a presence check (S4c, S4d) became `observed_not_satisfied`: 10/10. C4 (state already held, original failed) is a correct `observed_satisfied` that the original did not cause, and it is labelled with attribution unknown.
- **T1–T3 under `emb_pidns`:**
  - **T1** (detached descendant would roll back): `observed_satisfied` 5/5, and the state lasted (no rollback). Under `emb_direct`: `undetermined` 5/5, rolled back 5/5.
  - **T2** (descendant would execute late): `observed_not_satisfied` 5/5, and it never executed (killed). Under `emb_direct`: `undetermined`, executed late 5/5.
  - **T3** (direct child survives stdout close): `observed_not_satisfied` at 4.05 s, 5/5. `unshare` keeps the server's stdout open, so the loss was seen only when the survivor exited after its rollback. This is a liveness limit of the reference embedder, not a false claim.
- **S12a.** In the main batch, the runner closed before the concurrent T-2 was answered (5 `emb_direct` trials), so T-2's single answer arrived after close. After fixing the runner to wait for T-2, a separate re-run (`batch-s12a/`, 10 trials) answered T-2 with its real result while open, 10/10.
- **Comparisons with earlier batches:**
  - **Raw and naive (CLI, equivalent conditions):** unchanged behaviour. Raw gave an unknown answer and no resolution. Naive: S1's resend reported a failure (ENOENT) for a move that had succeeded, 5/5; S2's resend performed the move, 5/5.
  - **The audit's v1 `verified` counts** (45/163) and **the audit-fix CLI `undetermined` counts** (148/168) are **not numerically comparable**. The semantics changed (verified → observed), the CLI no longer accepts reconcile blocks at all, and the PID-namespace path did not exist. Scenario by scenario, the observed predicate truth under `emb_pidns` matches the audit's v1 observations wherever the old termination assumption happened to hold. The difference is that v2 never claims more than the observation, and T1, T2 and X1 now come out right.

**Other checks:**
- **[measured] Shadow byte-fidelity** (Week 2 `workload.py` and `fidelity.py`, unchanged; CLI shadow): 10/10 byte-identical in both directions; recordings had no leaks. The only privacy hits are server-command paths in `relay_started`, as before. Shadow never probes: S10 is 30/30 with 0 probes.
- **[measured] Client semantics** (Python `mcp` 2.3.0, TS SDK 1.32.1, through `emb_pidns`): both raise `-32000`; `data` carries all nine v2 keys; follow-up calls succeed.

## 8. Test, lint, format and type status

| Command | Result |
|---|---|
| `pytest -q -o addopts="" --junitxml=…` (full suite) | exit 0, 217 s: **5,968 passed**, 0 failed, 0 errors, 0 skipped |
| `ruff check src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime` | All checks passed |
| `ruff format --check` (same paths) | 218 files already formatted |
| `mypy src` | no issues in 66 source files |
| `mypy --strict` on the relay, harness, 5 reconciliation test files and 6 new tools | no issues in 12 source files |
| Cross-version demonstration (§6.4) | pre-audit 5 fail; baseline 2 fail, 3 pass; current 5 pass |

After the full suite ran, `test_reconciliation_contract_v2.py` received typing-only edits (a `_message()` helper). Its 22 tests were re-run and pass. The batch runner received the T-2 wait used for the S12a re-run.

Repo-wide `ruff check .` still reports the pre-existing issues in `benchmarks/runner.py` and `CORE_HARDENING_PLAN.md`, both untouched.

## 9. Evidence and hash verification

Everything is under `/root/stateguard-captures/week3-contract-v2/`; `EVIDENCE_SHA256.txt` has the full hashes.

| File | sha256 (16) |
|---|---|
| `batch/results.json` (426 trials + 20 refusal runs) | `391f2651f3f24b64` |
| `batch/summary.json` | `63f7598cda668943` |
| `batch/run.log` | `6267b50cc966b857` |
| `batch/code-under-test.sha256` | `d917bb25fafec245` |
| `batch-s12a/results.json` | `f893ef0bb0c12f24` |
| `client_semantics_v2.json` | `7c4aac85405ad2b9` |
| `shadow-fidelity/fidelity.json` | `bce40a37c89ab8cf` |
| `prefix-demo/pre-audit.txt`, `baseline.txt`, `current.txt` | `e721d5bb023b89dd`, `ce8c345cf17dc59e`, `d006b97890c20e16` |
| `prefix-code/` (reconstructed pre-fix code) | relay `466f9a7fa342e598`, library `11208ec6d4db3c66`, harness `55fc97d105d37c2f`, exports `bebcea566519dbe1`, helper tests `af64fbdf18abc31c` |
| `baseline/tracked.diff` / `tree.sha256` | `bdb603e255e0eb65` / `0312a02ee25d438e` |
| `gates/junit.xml` / `pytest.txt` / `mypy-strict.txt` | `c08f191c716b9e04` / `bfc10239081b1870` / `d851ebf031d6b324` |
| `verify_prior.sh` | `cda7e5261b126837` |

**Historical evidence is preserved.** `verify_prior.sh` passes 39/39 at start and at end. `WEEK3_AUDIT_FIXES.md` (`5fbd62c650ebbd17`) and the audit tools are unchanged; the audit tools could not run under v2, so new tools were added beside them. The real-relay report received a dated §14, with its earlier sections unchanged.

## 10. Remaining limitations and risks

1. **[assumption] `scope_ended` is trusted.** An embedder that asserts it falsely re-creates the audit's false-claim class. The relay can only require the claim to be explicit, named and bound to one executor.
2. **[measured] External effects are uncovered.** X1: the scope ended and the work still happened afterwards. The answer was correctly `observed_not_satisfied`, "not 'not executed'", but a client that acts on it, for example by resending, could duplicate the effect.
3. **[measured] Concurrency within one observation.** A writer between two probes (S6d) produces an `observed_satisfied` that was false by the time it was answered. Re-reading the same key catches only changes to that key.
4. **[assumption] Probe honesty.** A misleading probe server (S6e) yields a wrong observation. No relay-side check can detect it.
5. **[claim] Presence checks stay weak.** They are labelled weak. Content checks need either a call argument or a known constant.
6. **[measured] Hold time.** Up to 150 s by configuration; 20.05 s measured for a server that never lists. A client timeout shorter than that turns into a plain client-side timeout.
7. **[measured] Reference-embedder liveness.** A server that closes stdout but keeps running is seen as lost only when it exits (T3: 4.05 s).
8. **[claim] Routing residual.** A client reusing a still-pending relay-issued id is ambiguous (logged); JSON-RPC ids are per-sender.
9. **[claim] Shutdown.** An answer still being written 2 s into a stop is logged as unconfirmed, and then the process exits. That occurred in 0 of 306 relay-held reconciliations in the batch: all 306 were logged `written_to_stream: true`.
10. **Not run:** non-Linux platforms (the PID-namespace path is Linux-only, and the process-group test is skipped without `killpg`); multiple simultaneous reconciliations at shutdown, beyond the 8-thread exactly-once test; `--record` during probing through an embedder.

## 11. Should the capability be retained in this release?

**Retain, revised, and scoped to embedders**, with these conditions:
1. The CLI keeps refusing `reconcile` blocks. That is the release behaviour.
2. The feature is documented as an *observation* service for embedders that can truthfully assert `scope_ended`. It is not "recovery", and must never be presented as success.
3. Integrations should prefer content predicates wherever the expected content is known.

Disabling it would lose the one capability the measurements show to be both safe and informative under a real executor scope:
- T1: a true, lasting observation where v1 produced a false one;
- C2 and C3: wrong content correctly reported as not satisfied.

Keeping the v1 semantics would not be defensible.

## 12. Exact changed-file list (this package; relative to the frozen baseline)

| Status | File | Before → after (sha256 16) |
|---|---|---|
| modified | `examples/mcp/stdio_proxy.py` | `f0dce73d2ed1c494` → `a6f2b00f85c66b9a` |
| modified | `src/stateguard/adapters/mcp/reconcile.py` | `c91a9ac94b584ab3` → `65a0ff19c0f9be75` |
| modified | `src/stateguard/adapters/mcp/ledger.py` | `4fcc6e1840aaa11e` → `791a4edca3b2d44c` |
| modified | `benchmarks/runtime/harness.py` | `154138b5245a7fb9` → `83b589e4f5ef3403` |
| modified | `tests/adapters/mcp/test_reconcile.py` | `ddffcb433c8849be` → `7e716e66021c9de8` |
| modified | `tests/examples/test_relay_reconciliation.py` | `1a5a8179603b0108` → `b98f1520fb4dfea8` |
| modified | `tests/examples/test_relay_reconciliation_audit.py` | `18e5dfaec758f379` → `fe27f4efd1cbb0f2` |
| new | `tests/examples/test_reconciliation_contract_v2.py` | `612cd59e3be7887b` |
| new | `tests/examples/test_reconciliation_prefix_regressions.py` | `84fa326c72d3ff59` |
| new | `benchmarks/domains/mcp_server_filesystem/embedded_relay.py` | `56073364f40d1f1b` |
| new | `benchmarks/domains/mcp_server_filesystem/handoff_server.py` | `a413220254564c6e` |
| new | `benchmarks/domains/mcp_server_filesystem/contract_v2_relay.py` | `7e02a334e30d55b8` (differs from `batch/code-under-test.sha256` only by the T-2 wait) |
| new | `benchmarks/domains/mcp_server_filesystem/contract_v2_summary.py` | `1903b9309814d569` |
| new | `benchmarks/domains/mcp_server_filesystem/relay_client_semantics_v2.py` | `6a31d29e000eedda` |
| new | `docs/runtime/RECONCILIATION_CONTRACT_V2.md` | `2f73f92c0c5239ce` |
| new | `docs/runtime/WEEK3_RECONCILIATION_CONTRACT_V2.md` | this file |
| modified | `docs/runtime/WEEK3_REAL_RELAY_VERIFICATION.md` | dated §14 appended |

`src/stateguard/adapters/mcp/__init__.py` is unchanged in this package (`6496534e16b64e0e`).

## 13. Highest-priority next engineering task

**Executor confinement for the CLI.** Add an opt-in `--executor-scope` to `stdio_proxy.py`, starting with Linux PID namespaces, and later containers or job objects. The CLI would start each server generation inside a kernel-enforced scope, confirm the scope is empty after it ends, and only then assert `scope_ended` for that executor's identity.

The reference embedder already demonstrates the mechanism on the real server: 168/168 established, and T1 and T2 made correct. Productising it requires:
1. holding the server's stdio outside the wrapper, so that stdout-close liveness (T3) is preserved;
2. an explicit platform-support decision, with fail-closed refusal elsewhere;
3. tests on each supported platform.

It still would not, and must not claim to, cover external systems.

## 14. Metric audit and corrected wording (2026-10-17, Week 4A)

This section is appended. Nothing above it was changed: the bytes above are identical to sha256 `0e1ad042ce952e57…`, preserved in `/root/stateguard-captures/week4a/baseline/tree/`.

**How it was audited.** Every figure in §1 and §7 was re-derived from raw evidence only, by `benchmarks/domains/mcp_server_filesystem/contract_v2_audit.py`. Its sources are:
- `batch/results.json`;
- the relay's own audit log for each of the 426 trials;
- `batch-s12a/results.json`.

Output: `/root/stateguard-captures/week4a/metric-audit/audit-final.json`. Two runs gave byte-identical output.

**Result: all 18 printed figures match the raw evidence.** No number was wrong. Several were worded so that they could be misread. Corrected wording:

| Printed | What it actually counts | Corrected statement |
|---|---|---|
| "168/168 established" (§3, §13) | **restart reports** with `scope_ended` that applied, not trials. 188 `emb_pidns` trials: 158 had a restart, 10 of them two (S7b and S9; 168 reports in all), and 30 had none (deadline S8a–b, shadow S10a–c, no-fault S12b). | "168 of 168 restart reports established termination, in the 158 of 188 `emb_pidns` trials that restarted" |
| "188 vs 168" (the discrepancy questioned) | 188 = `emb_pidns` trials. 168 = 60 + 55 + 53 trials **with a postcondition**; the other 20 had none (S10a/b/c shadow: 15; S12b no fault: 5). That 168 restart reports also exist is a coincidence. | "Of 188 trials, 168 received a postcondition (60 / 55 / 53); 20 received none" |
| "every request answered exactly once" (§1); "Exactly one response per request 188/188" (§7) | Counted as "no request answered twice". 25 trials had `T-1` **unanswered by design**: shadow pass-through of a closed pipe, where the client sees end of stream exactly as with no relay. These are S10a/S10b × `emb_pidns`/`emb_direct` (20) and SH × `cli_raw` (5). | **At most one** response per request: 426/426 trials. **Exactly one** response per request: 401/426 overall (`emb_pidns` 178/188, `emb_direct` 178/188, `cli_raw` 35/40, `cli_naive` 10/10); the remaining 25 are the pass-through cases above |
| "Probes (failures) 290 (45)" | 290 probe events; 245 completed with `satisfied`/`unsatisfied`; 45 did not, all in probe-fault scenarios | unchanged, now with the completed count: 245/290 |
| S12a `emb_direct` | 5 trials whose `T-2` answer arrived only after the client closed, in the main batch, from an older runner without the T-2 wait (§10) | The S12a re-run (10 trials) answered `T-2` while open in 10/10. The main-batch rows are kept as they are, and the re-run does not replace them. |

**Unavailable figures:** none. Every printed figure could be reconstructed.

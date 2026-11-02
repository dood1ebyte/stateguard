# Week 3 — Fixes for real-relay audit defects A, B and C

**Date:** 2026-10-16. **Branch:** `claude/serene-bohr-o28snr`, HEAD `b6f7ba0` (= `origin/main`). The work is uncommitted, as instructed.

**Findings fixed:** `docs/runtime/WEEK3_REAL_RELAY_VERIFICATION.md` §1 and §12.

**Scope.** Only defects A, B and C. There are:
- no new recovery features;
- no resends, retries, compensation or persistent state;
- no change to the meaning of `verified`;
- no change to the client response shape: still JSON-RPC `-32000`, with the same `data` keys.

## Summary

| | Defect | Fix | Result |
|---|---|---|---|
| **A** | `verified` could be returned while a detached descendant of the old server was still able to act. The relay assumed termination (`executor_replaced=True`). | Probing now requires positive `TerminationEvidence`, reported by the restart for the generation it replaced. Only `scope_ended` is sufficient. The standard entry point can only show `direct_child_reaped`, which is insufficient, so it **never probes**. | T1 counterexample: before, **5/5 false `verified`**. After, **5/5 `undetermined`, 0 probes**; the rollback still happened in 5/5, and no claim was made. |
| **B** | A reconciliation answer could be lost when the relay stopped: the worker popped the entry before writing, and the stopping relay found nothing to wait for. | Explicit lifecycle `held → answering → removed`. Only the held → answering transition may answer. Shutdown answers everything still held, then waits a bounded time (`RECONCILE_STOP_WAIT_SECONDS`, 2 s) for answers being written. Anything still unwritten is logged `reconcile_answer_unconfirmed`. | S9 (restart failure): before, **23/25 answered**. After, **25/25 answered**, and the client-side count is exactly one answer per request. |
| **C** | Any response with an id starting `stateguard-reconcile:` was consumed as a probe answer, so a client request with such an id was never answered. | Exact correlation. Probe ids carry a per-relay random nonce, and are checked against ledger ids before use. A response enters probe handling only if its id is one the relay issued. Unknown ids take the normal path; late, duplicate and other-generation answers to issued probes are dropped and logged. | S12b: before, **5/5 unanswered** (15 s client timeout). After, **5/5 answered** in about 0.03 s. |

**Coverage cost.** Through the standard entry point, reconciliation now gives `verified` in **0/168** real-server trials, against 45/163 before. This is intended: `main()` cannot establish that every executor has ended (see "Termination guarantee and its limits"). `verified` remains possible only for embedders whose restart code reports `scope_ended`. The in-process test harness is one such embedder.

## 1. Root causes

**A — termination assumed, not established.**
- `_run_reconciliation` passed `decide(..., executor_replaced=True)` unconditionally.
- The `StdioRelay(restart=...)` contract was `Callable[[], (stdin, stdout)]`. Nothing in it said what the restart established about the old executor, so every factory, careless or not, was trusted.
- `main()`'s factory terminates and reaps only its **direct** child. A descendant that called `setsid` survives: it is reparented, outside the process group, and invisible to `waitpid`.

**B — "answered" was conflated with "taken".**
- `_finish_reconciliation` removed the pending entry under the lock *before* recording and writing.
- If the pump thread reached `pump_server`'s `finally` in that window, `_abandon_reconciliations` saw nothing pending and set `_stopped`.
- `main()` then returned and called `os._exit`, killing the daemon worker before its write.
- There was no state for "being answered", so there was nothing to wait for.

**C — routing by prefix.**
- `_pump_server` sent every response whose id string started with `RECONCILE_ID_PREFIX` to `_on_probe_response`.
- That method dropped any id it was not waiting for, logging `reconcile_probe_late`. A client's own response with that prefix was therefore swallowed.
- Probe ids were also predictable: `stateguard-reconcile:1`, `:2`, …

## 2. Implementation changes

**Library: `src/stateguard/adapters/mcp/reconcile.py` and `__init__.py`.**
- New `TerminationKind` and `TerminationEvidence`, exported from `stateguard.adapters.mcp`.
- Evidence can only be built through named constructors:
  - `not_reported()`
  - `direct_child_reaped(returncode)`
  - `process_group_empty()`
  - `scope_ended(scope)` — `scope` is required, non-empty, and truncated to 120 characters.
- The library, not the caller, decides sufficiency: `established` is True only for `scope_ended`. Each insufficient kind carries a fixed `limit` sentence.
- The module docstring documents the rule. `decide()` is unchanged; it still receives `executor_replaced`.

**Relay: `examples/mcp/stdio_proxy.py`.**

*A — termination evidence*
- New `Restarted(stdin, stdout, termination)` NamedTuple. A plain `(stdin, stdout)` pair is still accepted for restarts and is normalised to `not_reported`.
- `_on_server_lost` records the evidence for the replaced generation (`_record_termination`, first report wins) before `_connect`. It logs an `executor_termination` event: kind, established, detail, limit.
- A factory that returns anything else counts as a failed restart (`TypeError`).
- `_run_reconciliation` first calls `_await_termination(origin)`, bounded by `RECONNECT_WAIT_SECONDS`.
  - If there is no evidence, or it is insufficient, the answer is `undetermined` with "executor termination not established: <limit>; nothing was probed", or "no probe channel: no restart replaced …".
  - It does not wait for discovery and does not probe. `decide` receives `executor_replaced=termination.established`.
- The verdict's ledger entry and its `reconcile_verdict` audit event now carry `termination` (kind, established, detail, limit). These are statuses only.
- `main()`'s restart returns `Restarted(..., TerminationEvidence.direct_child_reaped(old.returncode))`; `old` has been reaped first. `main()` logs `reconcile_limited` at startup when reconcile blocks are configured outside shadow and passthrough.
- Process spawning in `main()` is **unchanged**: no `start_new_session` and no `killpg`. A process-group kill would still not be evidence, and starting a new session would change how terminal signals reach the server.

*B — answer lifecycle*
- Each pending entry has a `state`. `_finish_reconciliation` moves it from `held` to `answering` under `_reconcile_lock`, or returns False. It then records the verdict (a recording error is logged, and the client is still answered) and writes.
- The entry is removed only afterwards, under `_reconcile_cond`, which then notifies. A `reconcile_answered` event records `written` — handed to the client stream, not known to be read.
- `_surface_unknown` now returns whether its writes succeeded; before, it suppressed the `OSError`.
- `_settle_reconciliations(reason)` replaces `_abandon_reconciliations`. It is idempotent:
  1. answer every `held` entry (`undetermined`);
  2. wait on the condition until none are pending, at most `RECONCILE_STOP_WAIT_SECONDS`;
  3. log `reconcile_answer_unconfirmed` for each entry left.
- The worker checks `_held()` before each probe, so after a stopping relay takes over, the worker stops probing.

*C — exact correlation*
- Probe ids are `stateguard-reconcile:<16 hex nonce>:<n>`, with the nonce from `secrets.token_hex(8)`, one per relay. An id already known to the ledger is skipped.
- `_probe_waiters` holds `[event, response, generation]`. `_settled_probes` is a bounded OrderedDict (4,096 entries) of issued ids that are no longer awaited.
- `_take_probe_response`:
  - **First answer** on the waiter's generation: delivered to the waiter.
  - **Duplicate, late, or other-generation answer to an issued probe:** dropped; logged `reconcile_probe_dropped` with the reason.
  - **Any other id:** returns False, so the response takes the normal path.

**Test harness: `benchmarks/runtime/harness.py`.**
- `Rig(..., attest_termination=False)` is the default, so a factory that proves nothing reports nothing.
- With `attest_termination=True`, the restart **joins the replaced server's serve thread**, which is its whole executor in-process. It reports `scope_ended("in-process server: serve thread joined")` only if the join succeeded.
- `_restart` returns `Restarted`.

**Tests**
- `tests/examples/test_relay_reconciliation.py`:
  - `_world` gains `attest_termination=True`, because the in-process servers there are joinable.
  - `server_cls` lets tests script server behaviour.
- `tests/examples/test_relay_reconciliation_audit.py` was rewritten. It held the three strict-xfail reproductions; it now holds 16 regression tests (§4).

**Tools (isolated, new):** `benchmarks/domains/mcp_server_filesystem/relay_answer_count.py`. It is a client-side exactly-once counter: it reads every line the relay writes until EOF. The audit runner, shim, test server and client-semantics script are **unchanged** from the audit, so the before and after numbers are comparable.

## 3. Termination guarantee and its exact limits

**What the relay guarantees now.** It sends no probe for an unknown call unless the restart that replaced the call's server generation reported `TerminationEvidence` of kind `scope_ended`. It checks that evidence per generation, after the restart and before any wait for discovery.

**What each source of evidence can actually show:**

| Source | Evidence | Sufficient? | Why |
|---|---|---|---|
| Plain `(stdin, stdout)` factory, or any custom factory that reports nothing | `not_reported` | no | Nothing was established. |
| `main()` (standard entry point) | `direct_child_reaped` (exit status from `waitpid`) | **no** | A descendant in its own session or group is not the child, is not in the group, and cannot be enumerated portably once reparented. Live T1: such a descendant undid the move about 4 s after the answer, in 5/5 trials. |
| A factory that kills a process group and verifies `killpg(pgid, 0)` → `ESRCH` | `process_group_empty` | **no** | Group membership is escapable with `setsid`. Live regression test: the group was empty and the `setsid` descendant was still alive. |
| An integrator whose every executor ran inside one scope it confirmed ended (an in-process server whose thread was joined; a container or VM whose exit was awaited; a cgroup it emptied) | `scope_ended(scope)` | yes | That claim belongs to the integrator. It is made explicitly through a named constructor and is logged. The relay cannot verify it. |

**Limits, stated plainly**
- The relay cannot check a `scope_ended` claim. An integrator who calls it falsely re-creates defect A. What changed is that this can no longer happen *silently*: the default reports nothing, and the claim is named and logged.
- The standard entry point gives **no** reconciliation coverage. Its answers are `undetermined` and unprobed, by design.
- No subreaper, cgroup or job-object mechanism was added, per the platform decision.
- The evidence covers the executor of the generation whose outcome is unknown. A shared external backend (a database, a remote API, a queue worker) that keeps working after the server process is gone is outside any process evidence, and outside `scope_ended` unless the integrator's scope includes it.

## 4. Regression tests and actual results

**Defect reproduction before the fix** (strict-xfail tests run with `--runxfail` on the unmodified code; hashes `466f9a7fa342e598` relay, `11208ec6d4db3c66` reconcile, `55fc97d105d37c2f` harness):

- A: `AssertionError: assert 'verified' == 'undetermined'`
- B: `AssertionError: the client got no answer`
- C: `AssertionError: the client's own request was never answered`

The new tests below that go beyond those three reproductions were written after the fix. They were not separately run against the pre-fix code, because that code was never committed and was not kept.

**`tests/examples/test_relay_reconciliation_audit.py`** (16 tests, all pass, 5 consecutive runs):

| Requirement | Test | Checks |
|---|---|---|
| A: custom factory leaves the executor running (the original reproduction) | `test_a_factory_that_leaves_the_old_executor_running_is_never_verified` | `undetermined`; 0 probes; the evidence is `not_reported`; the ground-truth rollback happens; never resent |
| A: custom factory without evidence | `test_a_custom_factory_without_evidence_never_probes` | Reason "executor termination not established … nothing was probed"; the ledger and audit hold `termination.established: false` |
| A: positive control | `test_a_scope_ended_evidence_is_what_allows_a_probe` | A joined in-process server gives `scope_ended`, then `verified`; execution and attribution stay `unknown` |
| A: restart fails | `test_a_restart_that_fails_records_no_evidence_and_never_probes` | No evidence recorded; `undetermined` ("no probe channel"); exactly one answer |
| A: process group emptied, descendant survives | `test_a_process_group_emptied_by_the_restart_does_not_establish_termination` | **Real processes:** the group leader is killed with `SIGKILL`; `killpg(pgid, 0)` raises `ProcessLookupError`; the `setsid` descendant is still alive (`kill(pid, 0)`). The relay answers `undetermined` and sends 0 probes. |
| A: original server exits normally (through `main()`) | `test_main_never_probes_after_a_server_that_exited_normally` | A real subprocess relay. The evidence is `direct_child_reaped` with "exit status 0" and not established; `reconcile_limited` is logged; no later generation receives any `tools/call`; the move did happen |
| A: detached descendant acts later (through `main()`; audit counterexample T1) | `test_main_never_verifies_when_a_detached_descendant_acts_later` | As above. The descendant waits for a go-file created after the answer, then undoes the move; the rollback is observed; no `verified` |
| B: the original race | `test_a_pending_reconciliation_is_answered_before_the_relay_stops` | The worker is pre-empted for 0.5 s after taking the entry, and the stop is delayed 0.2 s. The client is answered exactly once, with `written: true` |
| B: timeout and fallback | `test_an_answer_that_cannot_be_written_in_time_is_reported_unconfirmed` | Bound set to 0.3 s and the worker blocked. The pump thread finishes (no deadlock or unbounded wait); `reconcile_answer_unconfirmed` (state `answering`) is logged. The released worker then logs `written: false`, and the client gets no answer — nothing is claimed delivered |
| B: repeated shutdown | `test_a_repeated_shutdown_answers_nothing_twice` | Settled twice more after the stop: still one answer, one `reconcile_answered` |
| B: exactly once | `test_concurrent_finishers_answer_exactly_once` | 8 threads behind a barrier: exactly one `True`, exactly one extra answer |
| C: ordinary ids that resemble the prefix | `test_a_client_id_with_the_probe_prefix_is_still_answered` | `stateguard-reconcile:1` and `stateguard-reconcile:0123456789abcdef:1` are both answered; nothing dropped |
| C: genuine probe responses | `test_genuine_probe_responses_use_exact_issued_ids` | Ids match `stateguard-reconcile:<16 hex>:<n>` with one nonce; `verified`; never relayed to the client |
| C: late and out-of-order | `test_late_and_out_of_order_probe_responses_are_dropped` | Probe 1's answer is held until after probe 2's. Probe 1 is `unavailable`, then `undetermined`; the late answer is dropped (`why: late`) and not relayed |
| C: duplicates | `test_duplicate_probe_responses_are_dropped` | Two duplicates dropped (`why: duplicate`); `verified` from the first answers |
| C: unknown id | `test_a_response_for_an_unknown_id_takes_the_normal_path` | A stray `stateguard-reconcile:ffff…:1` response is not consumed as a probe and reaches the client by the normal path |

The 23 existing relay reconciliation tests and 51 unit tests pass unchanged, apart from `_world` opting in to attestation. The restart, race and recording suites also pass. Full-suite, lint, format and type results are in §8.

## 5. Real-relay measurements, before and after

**Method.** Same runner (`relay_audit.py`), shim, test server, configurations and repetitions as the audit, against the production `stdio_proxy.py main()`. Ground truth comes from disk snapshots and the delivery logs.

The "after" column is the **final** run (`final/`), made on the code exactly as left in the tree. A first post-fix run, before one last change, gave the same classifications in every row and is kept as `run*/`. That last change limited `executor_termination` audit events to relays with reconcile blocks, after the replay golden-tape test caught it.

**Volume.**
- Before: 243 trials (228 real-server, 15 test-server).
- After: 248 trials (233 real-server, 15 test-server); S12e is now in the main run as well.

| Measure | Before | After |
|---|---|---|
| Real-server reconcile trials | 163 | 168 |
| `verified` | 45 (15 caused by the original; 10 other actor or pre-existing; 10 wrong content; 10 false at answer) | **0** |
| `not_verified` | 25 | 0 |
| `undetermined` | 71 | 148 |
| Shadow pass-through | 15 | 15 (unchanged) |
| Client got no answer (S9) | **2 / 25** | **0 / 25** |
| Colliding client id unanswered (S12b) | **5 / 5** | **0 / 5** |
| T1: detached descendant rolls back after the answer | **5/5 `verified`, then reverted** | 5/5 `undetermined`, 0 probes; rollback still observed 5/5 |
| T2: descendant executes after the answer | 5/5 `not_verified`, then executed | 5/5 `undetermined`, 0 probes |
| T3: direct child survives stdout close | 5/5 `verified` (true) | 5/5 `undetermined` (evidence: exit status −15, reaped) — coverage loss |
| Probes sent | 230 | **0** |
| Original delivered twice / relay resends / execution or attribution claims | 0 / 0 / 0 | 0 / 0 / 0 |
| Reconciliations held, then answered and written | (not instrumented) | 148 / 148 `written: true`; 0 unconfirmed |
| Client-side answers per request (`relay_answer_count.py`, 5 cases × 5) | (not measured) | exactly 1 in 20/20 relay-answered trials (drop, restart failure, detached descendant, colliding id plus its call); shadow 0/5, as with no relay |
| Answer latency, S1 (median / p95) | 0.42 / 0.44 s (probed) | 0.039 / 0.052 s |
| Hold with a server that never lists (S12e) | **40.3 s** | 0.048 s median (n = 8) |
| Deadline unknowns (S8, 1 s) | ≈1.04 s, 0 probes | 1.05 s median, 0 probes |
| Audit-log content leaks / sentinel changes | 0 / 0 | 0 / 0 |
| Invalid configurations refused before any server starts | 15 / 15 | 15 / 15 |
| Shadow byte-fidelity (Week 2 `workload.py` and `fidelity.py`, unchanged) | 10/10 (implementation report) | **10/10** byte-identical in both directions; recordings had no leaks. The only privacy hits are the server-command paths in `relay_started`, as before |
| Python `mcp` 2.3.0 / TS SDK 1.32.1 | raise `MCPError` / `McpError` −32000; `data` reachable | unchanged; postcondition now `undetermined` |

**Why "undetermined" here is safe, not merely quiet.** For each of the 148 undetermined real-server answers and 15 test-server answers, the evidence shows:
- 0 probes sent;
- no later generation received a `tools/call` from the relay;
- the original was delivered at most once;
- exactly one answer reached the client (audit, and the client-side counter);
- execution and attribution stayed `unknown`;
- each reason names termination ("executor termination not established: only the direct child process was reaped …"), except S12c ("probe 0: the call has no string argument 'nope'") and S8 ("the original executor may still be running").

## 6. Unresolved platform limitations and remaining risks

1. **No reconciliation through the standard entry point.** Process-only evidence cannot prove that every descendant ended. Without a subreaper, cgroup or job object (out of scope), the CLI answers every reconciled call `undetermined`.
2. **`scope_ended` is trusted, not verified.** It is a named, logged integrator claim. A false claim re-creates defect A.
3. **Shared external state.** Effects that run outside the server's processes (remote APIs, databases, job queues) are not covered by any process evidence.
4. **Probe-level limits from the audit, still true where probing happens.**
   - A presence-only predicate cannot see content.
   - A concurrent writer can change state between two probes.
   - A lying probe server cannot be detected.
   - The message still says "intended postcondition" (wording change deferred: client contract).
5. **The worst-case hold is now short where termination is not established.** Where it is established, the earlier bound still applies: (N + 2) × 30 s + Σ probe timeouts.
6. **Shutdown bound.** An answer still being written 2 s after the stop begins is logged as unconfirmed. In `main()` the process then exits, so that client gets no answer, but the failure is no longer silent in the audit log. This did not occur in any live trial (0/148).
7. **`OWN_ID_PREFIX` (relay-owned `tools/list`).** It still routes by prefix. This is the same class of defect as C, but it predates Week 3 and was out of scope.
8. **Not re-tested here:**
   - non-Linux platforms (the process-group test is skipped where `os.killpg` is missing);
   - multiple concurrent reconciliations at shutdown, beyond the 8-thread exactly-once test;
   - `--record` during reconciliation, which now records no probe traffic through `main()` because nothing is probed.

## 7. Evidence

New evidence is under `/root/stateguard-captures/week3-audit-fixes/`, with full hashes in `EVIDENCE_SHA256.txt`.

**Final run** (`final/`, the code as left in the tree; the numbers above):

| File | sha256 (16) |
|---|---|
| `final/results.json` (200 trials + 18 configuration cases) | `28c4f8228045eed4` |
| `final/results-s9x20.json` | `ff2f11e5f088918f` |
| `final/results-s12e.json` | `5d7aaab2e2c936c5` |
| `final/answer_count.json` (client-side answers per id) | `47ac1bf817ab537d` |
| `final/client_semantics.json` | `6c2392c617d1b76d` |
| `final/shadow-fidelity/fidelity.json` | `40a4eff391cac09e` |
| `final/run.log` / `run-s9.log` / `run-s12e.log` / `answer-count.log` | `e497731565ecf62e` / `8752590f4ba04383` / `1a79bb3038ce9d68` / `f725ad8495eab595` |

**First post-fix run** (top level), before the `executor_termination` logging change. Classifications are identical to the final run.

| File | sha256 (16) |
|---|---|
| `results.json` / `results-s9x20.json` / `results-s12e.json` | `31c943dd9310b67c` / `9624dfce86c0793a` / `16295af4d4c0e87c` |
| `answer_count.json` / `client_semantics.json` | `47ac1bf817ab537d` / `6c2392c617d1b76d` (byte-identical to the final run) |
| `shadow-fidelity/fidelity.json` | `ea06dc4448ccd3d5` |

**Prior evidence, verified unchanged after all of the above:**
- **Week 1 recordings:** `5952c1898ede45ce`, `44d4798335097769`, `636880d507888b38`, `bdc65c64ecc0de41`.
- **Week 2:**
  - recording manifest `cef453720aac8b7e`;
  - `results-raw` / `results-shadow` / `results-tee` / `fidelity`: `28a9beb529b42d9b` / `097a7763becf561a` / `d701de99240a782d` / `57697bcd7beaaaf2`.
- **Week 3 experiment** `results.json`: `24304f58c991964f`.
- **Week 3 implementation:** `live-final` `82c0131815c56285`; `shadow-fidelity` `81de74780f9faaf9`.
- **Week 3 audit evidence:** `week3-real-relay/` — `sha256sum -c EVIDENCE_SHA256.txt` gives 10/10 OK; `results.json` is `8fd4132c628acfc4`.
- **Reports:** `WEEK2_DOMAIN2_FILESYSTEM.md` `9386533c92597fbd`, `WEEK3_RECONCILIATION_EXPERIMENT.md` `7914662a4fa7bfe5`, `WEEK3_RECONCILIATION_IMPLEMENTATION.md` `19dc0b1b9ba67b9f`.
- **Testbed and Week 3 experiment sources:** all unchanged — `fixture`, `rawclient`, `workflow`, `shim`, `workload`, `fidelity`, `probe`, `reconcile`, `reconcile_shim`, `reconcile_experiment`, `reconcile_live`, `test_week3_reconciliation`, `test_domain2_filesystem_testbed`, `test_reconcile`.

**Changed by this package** (sha256, first 16 hex):

| File | Before | After |
|---|---|---|
| `examples/mcp/stdio_proxy.py` | `466f9a7fa342e598` | `f0dce73d2ed1c494` |
| `src/stateguard/adapters/mcp/reconcile.py` | `11208ec6d4db3c66` | `c91a9ac94b584ab3` |
| `src/stateguard/adapters/mcp/__init__.py` | `bebcea566519dbe1` | `6496534e16b64e0e` |
| `benchmarks/runtime/harness.py` | `55fc97d105d37c2f` | `154138b5245a7fb9` |
| `tests/examples/test_relay_reconciliation.py` | `af64fbdf18abc31c` | `1a5a8179603b0108` |
| `tests/examples/test_relay_reconciliation_audit.py` | `b66228de9fccaaf7` | `18e5dfaec758f379` |
| `benchmarks/domains/mcp_server_filesystem/relay_answer_count.py` | (new) | `d2b897e77b590017` |
| `docs/runtime/WEEK3_AUDIT_FIXES.md` | (new) | this file |
| `docs/runtime/WEEK3_REAL_RELAY_VERIFICATION.md` | `2047c439a3a87b4d` | status note + §13 appended; earlier sections unchanged |

`ledger.py` is unchanged (`4fcc6e1840aaa11e`), and so are the audit tools `relay_audit.py`, `relay_audit_shim.py`, `rollback_server.py` and `relay_client_semantics.py`.

## 8. Checks actually run

| Command | Result |
|---|---|
| `pytest tests/examples/test_relay_reconciliation_audit.py --runxfail` on the **pre-fix** code (3 strict-xfail reproductions) | 3 failed, reproducing A, B and C (§4) |
| `pytest tests/examples/test_relay_reconciliation_audit.py -q`, 5 consecutive runs | 16 passed each run |
| `pytest test_replay.py test_relay_reconciliation_audit.py test_relay_reconciliation.py test_relay_recording.py` | all passed. The replay golden-tape test had failed once in the full suite, and that failure led to the logging change in §5. |
| `pytest -q --junitxml=…` (full suite, final code) | exit 0, 211 s: **5,926 passed**, 0 failed, 0 errors, 0 skipped. That is the 5,910 baseline plus the 16 new tests, which replace the 3 xfails. |
| `ruff check src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime` | All checks passed |
| `ruff format --check` (same paths) | 211 files already formatted |
| `mypy src` | Success: no issues found in 66 source files |
| `MYPYPATH=benchmarks:benchmarks/domains/mcp_server_filesystem mypy --strict` on the relay, harness, the 3 reconcile test files and the 5 audit tools | Success: no issues found in 10 source files |
| Real-relay re-run: `relay_audit.py --reps 5`, `--only S9 --reps 20`, `--only S12e --reps 3`; `relay_answer_count.py --reps 5` | §5 |
| Shadow fidelity: Week 2 `workload.py` (raw, shadow + real_capture, tee) and `fidelity.py` | `fidelity ok: True`, 10/10 byte-identical |
| Client semantics: `relay_client_semantics.py` | Python and TS SDKs: −32000, `data` reachable, follow-up call succeeds |

Repo-wide `ruff check .` still reports the pre-existing issues in `benchmarks/runner.py` and `CORE_HARDENING_PLAN.md`; they are out of scope and unchanged. Formatting was applied only to files this package changed. Historical benchmark files were hash-checked afterwards and are unchanged.

## 9. Readiness

**Ready for another review, as a fail-closed safety correction.** It is not ready as a recovery feature for the standard entry point.

- **Fixed and verified:**
  - Defect A can no longer produce a silent false `verified`. The live counterexample is now `undetermined` and unprobed in 5/5.
  - Defect B: 25/25 answered, exactly once.
  - Defect C: 5/5 ordinary prefix ids answered; late, duplicate and unknown answers are handled.
  - Every verified-or-escalate invariant held in all 248 trials and in the test suite: 0 resends; 0 duplicate answers; 0 execution or attribution claims; no probe in shadow, after a deadline, or without established termination; no content in audit or ledger.
- **What reviewers should decide:**
  1. Is a named, logged `scope_ended` claim the right contract for embedders, given that the relay cannot verify it?
  2. Should `main()` keep accepting `reconcile` blocks, which now always answer `undetermined` and log `reconcile_limited`, or refuse them at startup?
  3. Is the 2 s shutdown bound right?
- **Not recommended yet:** advertising reconciliation as a recovery capability. Through the CLI it now gives no `verified` outcomes, and that is the honest result of the portability constraint, not a defect.

# Week 3 — Reconciliation in the runtime: verified satisfied, or escalate

**Status:** implementation for review. Not committed, pushed or opened as a PR.
**Base:** `main` at `b6f7ba0`, which includes PR #18.
**Evidence base:** `docs/runtime/WEEK3_RECONCILIATION_EXPERIMENT.md`, left unchanged.

**What it adds.** When a `tools/call` with operator-declared probes loses its connection mid-flight and the relay restarts the server, the relay holds the client's answer. It runs read-only probes against the **restarted** server, then answers "outcome unknown" with a postcondition verdict.

| Verdict | Meaning |
|---|---|
| `verified` | The intended state was observed. Execution and attribution stay unknown. |
| `not_verified` | The intended state is absent. Escalated. |
| `undetermined` | The evidence could not decide. Escalated. |

Nothing is ever resent. Shadow mode never probes.

---

## 1. Preflight

All checks below were executed.

| Check | Result |
|---|---|
| PR #18 merged; branch contains it | Yes. `merged_at` 2026-10-16 09:59:13Z, merge `b6f7ba0`; HEAD = `origin/main` = `b6f7ba0`. |
| Working tree before changes | Only the Week 3 experiment files, untracked. |
| Baseline suite | **5,836 passed**, 0 failed, 0 skipped (204 s), matching the experiment report. |
| Week 2 / Week 3 artifacts | Hashes unchanged before and after this work (§9). |

**Review of the experiment.** I re-read the verdict logic, the ground-truth derivation and the scoring, and found **no material flaw**. The safety conclusion holds: no resend is justified.

Points carried forward (none changes the conclusion):

1. *"0 execution-state relabels"* is true **by construction**: the experiment's `Assessment.execution_state` is a constant. It is not an empirical finding. The runtime enforces the same thing structurally (§3, invariant I2).
2. R10 never observed the branch where the kill lands after the call executed (20 of 20 runs were not executed), as the report states.
3. The experiment let `satisfied` stand while the executor was alive, with a `late_execution_possible` flag. **The runtime is stricter: it never probes while the executor may be alive.**
4. The experiment checked against a **pre-image** captured before the call. The runtime has no pre-call reads, so its predicates can only reference the call's own arguments. This is weaker; see §7 and decision D2.
5. The latent ledger issue from Week 2 is **confirmed and latent**. `_answer_not_sent` ignored `answered_locally`'s outcome. All three current callers run before `ledger.sent`, so it was correct only because of call ordering. It is fixed here, with a regression test.

## 2. Files changed and why

**Production code**

| File | Change |
|---|---|
| `src/stateguard/adapters/mcp/reconcile.py` (**new**, 505 lines) | Pure, no I/O: configuration parsing and validation (`ReconcilePolicy`), argument binding, probe classification (`observe`), verdict (`decide`). It cannot express "executed", "not executed" or a resend. |
| `src/stateguard/adapters/mcp/ledger.py` | Adds `ResultValidity` and `Postcondition` enums. `Invocation` gains `result_validity`, `postcondition`, `attribution` and an append-only `reconciliation` list. New methods: `record_result_check`, `record_reconciliation` (UNKNOWN only, append-only, state unchanged), and `unknown()`. `answered_locally` now **returns whether the transition was accepted**. |
| `src/stateguard/adapters/mcp/__init__.py` | Exports the new names. |
| `examples/mcp/stdio_proxy.py` | Reconciliation path, `not_sent` fix, result-validity recording, `--effects` reconcile loading and validation in `main()`, docstring section. |

**Tests and harness**

| File | Change |
|---|---|
| `tests/adapters/mcp/test_reconcile.py` (**new**, 51 tests) | Configuration safety, binding, observation, verdict rules (including an exhaustive no-execution-claim test), ledger model. |
| `tests/examples/test_relay_reconciliation.py` (**new**, 23 tests) | End-to-end through the relay with a stateful in-process server; ground truth is the server's shared filesystem dict. |
| `benchmarks/runtime/harness.py` | `Rig` passes a `reconcile` argument through (+2 lines; backward compatible). |
| `benchmarks/domains/mcp_server_filesystem/reconcile_live.py` (**new**) | Live check of the production `main()` relay against the real pinned server, using the existing `reconcile_shim.py`. |

**Unchanged:** every Week 2 file, every Week 3 experiment file, and all historical captures.

## 3. State model and transitions

The **execution state machine is unchanged**: PENDING → ANSWERED_LOCALLY | SENT → COMPLETED | UNKNOWN. Four independent fields sit beside it, and none of them ever moves the state.

| Field | Values | Set when |
|---|---|---|
| `result_validity` | `not_checked`, `envelope_valid` (a CallToolResult; no outputSchema), `valid`, `invalid`, `no_result` | COMPLETED and the result was checked; `no_result` on UNKNOWN |
| `postcondition` | `not_checked`, `pending`, `verified`, `not_verified`, `undetermined` | Only on UNKNOWN invocations, by reconciliation. A final value is never overwritten; an attempt is recorded as an anomaly. |
| `attribution` | `None`; `"unknown"` once reconciled | Never anything else. A probe sees state, not who produced it. |
| `reconciliation` | Append-only list of `{event: pending}` then `{event: verdict, probe_generation, postcondition, execution_state: "unknown", attribution: "unknown", reasons, probes: [statuses]}` | Probe arguments and results are never stored. |

**Invariants enforced in code:**

- **I1.** `record_reconciliation` refuses any state but UNKNOWN.
- **I2.** No code path moves UNKNOWN → COMPLETED. `Verdict.execution_state` and `attribution` are constants.
- **I3.** No code path re-sends the original. Probes use ids prefixed `stateguard-reconcile:` and tools declared `read_only`; the original payload is only parsed for its arguments.
- **I4.** The client gets exactly one answer: a pending reconciliation is popped under a lock before answering.

**Relay flow for an UNKNOWN call to a tool with a declared probe:**

| Situation | Behaviour |
|---|---|
| Shadow mode | Logs `reconcile_skipped` ("shadow mode never probes"). The unknown is handled exactly as before: logged, not surfaced. |
| Deadline expiry (`--call-deadline`) | The executor may still run, so **no probe**. Answered `undetermined`. |
| Transport closed, restart refused, or restart disabled | No probe channel. Answered `undetermined`. |
| Transport closed, and the relay will restart (`--restart N --restart-policy after-unknown`) | The answer is held and a worker waits for the new generation's link and listing. Then: (1) each probe tool must be listed and must not be annotated `readOnlyHint:false` or `destructiveHint:true`; (2) each probe goes only to that generation (`_send_decided`), with a timeout; (3) `decide`; (4) record and answer. |

Waiting is bounded: the relay's reconnect wait plus its listing wait (10 s each by default) plus the probe timeouts. A relay that stops answers every pending reconciliation as `undetermined`.

**Client answer.** The existing `-32000` error. `data` keeps `io.stateguard/executionState: "unknown"` and adds:
- `io.stateguard/postcondition`;
- `io.stateguard/attribution: "unknown"`;
- `io.stateguard/reconciliation`: reasons plus per-probe statuses.

For `verified`, the message says the postcondition was verified "but not that this request produced it. Do not resend it."

**The `not_sent` fix.** `_answer_not_sent` says "not sent, did not execute" only if `answered_locally` returned True. Otherwise it logs `not_sent_unconfirmed` with the ledger state, then:
- if the request was SENT: resolves it UNKNOWN and answers "unknown";
- if it was already finished: sends nothing.

## 4. Configuration and operation

The probes live in the existing `--effects` file, on the entry of the tool being reconciled:

```json
{"tools": [
  {"name": "read_text_file", "effect": "read_only"},
  {"name": "move_file", "effect": "non_idempotent",
   "reconcile": {"timeout_seconds": 5, "probes": [
     {"tool": "read_text_file", "arguments": {"path": "$source"},
      "expect": "absent", "absent_marker": "ENOENT"},
     {"tool": "read_text_file", "arguments": {"path": "$destination"},
      "expect": "present", "absent_marker": "ENOENT"}]}}]}
```

```
stdio_proxy.py --effects effects.json --restart 2 --restart-policy after-unknown -- <server>
```

**Rejected at load: the relay refuses to start** (tested):
- the probe tool is not explicitly declared `read_only` in the same file (undeclared, `idempotent` or `unknown` all fail);
- keys asking for a resend: `exclusive`, `resend`, `resend_if_not_executed`, `retry`, `replay`, `on_not_executed`;
- unknown keys;
- zero probes, or more than 4;
- `timeout_seconds` outside (0, 30];
- a tool probing itself;
- reconciling a `read_only` tool;
- duplicate entries;
- a probe argument that is not a `$reference` to the call's arguments, which blocks literal paths such as `/etc/passwd`;
- an `absent` probe without an `absent_marker`;
- in auto mode, reconcile blocks without `--restart N --restart-policy after-unknown`.

**Checked at probe time:**
- The probe tool must be in the restarted server's listing, and not annotated as non-read-only. Annotations can only disqualify a tool, never qualify one.
- Referenced call arguments must be strings.
- Any error, timeout, malformed answer, server change or inconsistency between repeated reads gives `undetermined`.

**Privacy.** The audit log and the ledger hold probe statuses and coarse forms (`present` / `absent` / `error` / `none`) only. They hold no arguments, paths or contents; this is tested.

## 5. Tests and actual results

All of the following were executed.

| Check | Result |
|---|---|
| Full suite (`pytest -q`) | **5,910 passed, 0 failed, 0 skipped** (204 s): the 5,836 baseline plus 74 new |
| New unit tests (`tests/adapters/mcp/test_reconcile.py`) | 51 passed |
| New relay tests (`tests/examples/test_relay_reconciliation.py`) | 23 passed; the file was repeated 3× with no failures |
| Week 2 testbed tests + Week 3 experiment tests | Passed, as part of the full suite (31 + 29) |
| `ruff check src tests examples/mcp/stdio_proxy.py benchmarks/...` | clean |
| `ruff format --check src tests` | clean (182 files) |
| `mypy src` | clean (66 files) |
| `mypy --strict` on the relay, the new tests, the testbed and the live script (`MYPYPATH=benchmarks`) | clean |
| Live check (`reconcile_live.py`, real server, production `main()`), 3 reps × 4 scenarios | **12 of 12 checks passed** (see below) |
| Shadow fidelity re-check with the modified relay | byte-identical in both directions (10 of 10); 27/27 tool calls forwarded unchanged; recordings 0 leaks |

The shadow re-check used the Week 2 `workload.py` and `fidelity.py`, unchanged, in a new workspace. Its only privacy hits are the paths in the audit log's `relay_started` event, as in Week 2.

**Live results.** Ground truth comes from the disk and the shim's delivery ledger. The client answer took 0.34–1.06 s.

| Scenario | Postcondition | Disk checkpoint | Server processes | Later processes received | Verdict |
|---|---|---|---|---|---|
| L1 executed, answer lost | `verified` ×3 | 3 (executed) | 2 | `read_text_file` only | ✔ |
| L2 never executed | `not_verified` ×3 | 2 (not executed) | 2 | `read_text_file` only | ✔ |
| L3 executes late (deadline 1 s, server executes at 3 s) | `undetermined` ×3, no probe | 3 (late execution) | 1 | — | ✔ |
| L4 shadow | no postcondition, no probe, no restart | 3 | 1 | — | ✔ |

**Required cases mapped to tests:**

| # | Case | Test(s) |
|---|---|---|
| 1 | Executes, response lost → verified | `test_executed_and_lost_is_verified_but_stays_unknown`; live L1 |
| 2 | Never reaches the server | `test_never_reached_is_not_verified_and_escalated`; live L2 |
| 3 | Executes, then undone by another actor | `test_executed_then_undone_is_not_verified_and_never_resent` |
| 4 | Another actor produces the postcondition | `test_another_actors_state_is_verified_without_claiming_execution` |
| 5 | Stale / contradictory evidence | `test_server_lost_during_the_probe_is_undetermined`, `test_inconsistent_reads_during_one_probe_are_undetermined`, `test_a_contradictory_state_is_not_verified`, unit stale/inconsistent tests |
| 6 | Probe fails / times out / malformed | `test_a_failing_probe_is_undetermined[rpc_error, malformed, hang]`, unit `test_observe` |
| 7 | Executor may still run or execute later | `test_a_live_executor_is_never_probed[hang, delayed]`; live L3 |
| 8 | Configuration missing / invalid / not read-only | `test_unsafe_or_invalid_configuration_is_rejected_at_load` (21 cases), `test_without_a_reconcile_policy_nothing_changes`, `test_without_a_probe_channel_…`, `test_a_restart_that_never_succeeds_…`, `test_a_probe_tool_the_server_does_not_list_…`, `…annotates_as_not_read_only…`, `test_main_refuses_unsafe_or_unusable_configuration` |
| 9 | Shadow never probes | `test_shadow_never_probes`; live L4; shadow fidelity re-check |
| 10 | No automatic resend | `_never_resent` asserted in every relay scenario: the original id reached a server at most once, restarted servers received only `read`/`peek` probes, `move` executed at most once, `resent: false`. Also `test_identical_scenarios_give_identical_verdicts`. |
| 11 | Execution and attribution stay unknown | `_assert_unknown_record` in every relay scenario; unit `test_no_combination_yields_an_execution_claim` (exhaustive) |
| 12 | Result validity separate | `test_a_completed_call_has_result_validity_and_no_postcondition`, unit `test_result_validity_is_separate_from_the_postcondition` |
| 13 | Ledger rejects; relay does not report it | `test_not_sent_is_never_claimed_for_a_request_the_ledger_holds_as_sent`, unit `test_answered_locally_reports_whether_the_transition_was_accepted` |
| 14 | Week 2 / experiment tests still valid | full suite |

The relay tests assert the full ledger record (state, history, postcondition, attribution, result validity, reconciliation entries) and the audit events (`reconcile_pending`, `reconcile_probe` statuses and generation, `reconcile_verdict`). They do not stop at the client's answer.

## 6. Safety invariants verified

| Invariant | How |
|---|---|
| No automatic resend | No code path; `_never_resent` in every scenario; live: later processes received only `read_text_file`. |
| UNKNOWN never relabelled executed or not executed | Ledger refuses; `Verdict` constants; exhaustive unit test. |
| No attribution claim | `attribution` is always `"unknown"`; the answer says so. |
| Shadow never probes, traffic unchanged | Relay test, live L4, byte-fidelity re-check. |
| Uncertainty escalates | Every unusable, stale, inconsistent or unchecked case → `undetermined`; tested per kind. |
| "Not sent" only when the ledger accepted it | Regression test. |
| No values in evidence | Test: no paths or contents in reconcile audit events or ledger entries. |
| Existing behaviour compatible | 5,836 baseline tests pass unchanged. Without `reconcile` blocks nothing changes (tested). |

**Two documented behaviour changes:**

- `answered_locally` now returns `bool` instead of `None`.
- `relay_started` audit events gain `reconcile_tools`, a list of names.

## 7. Limitations and assumptions

- **A1 — the restart factory reaps the old process.** "Executor gone" is asserted by the restart factory. `main()`'s factory terminates and waits for the old process. A custom `StdioRelay` factory must do the same, which is documented and not verified by the relay. Detached server children are assumed absent.
- **A2 — argument-only predicates.** Without a pre-image, a probe can check only what the call's own arguments name: present, absent, or text equal to an argument.
  - `write_file`-style calls (text equals `$content`) are strongly checkable.
  - `move_file` is checked structurally: source absent, destination present. Its content is not checked.
  - **Append-style edits cannot be verified** and must not be declared. No `contains` predicate is provided, deliberately.
- **A3 — the probe trusts the restarted server's read path.**
- **A4 — one probe round.** No repeated probing.
- **A5 — when the client's answer arrives.** The answer to a reconciled call arrives after the restart and probes, at most about 20 s plus the probe timeouts. In tests and live runs it took ≤ 1.1 s.
- **A6 — id-prefix collision.** A client that itself uses ids beginning `stateguard-reconcile:` would have those responses consumed. This is the same caveat as the relay's existing own-id prefix.
- **Not implemented, by design:** resend, retries, compensation, cross-process persistence, reconciliation of deadline unknowns, probes in shadow mode.
- **Not tested:** a probe server that lies; a multi-path transaction; a concurrent writer that changes state during the probe on the *real* server (in-process only); servers other than the filesystem one.
- **No claims** about prevalence or other MCP servers.

## 8. Differences from the experiment report

| Experiment | Runtime |
|---|---|
| Probe through a separate server process | Probe through the relay's restarted server (same `_send_decided` generation gating) |
| `satisfied` allowed with a live executor (flagged) | **No probe** while the executor may be alive (deadline or send-failed unknowns) |
| Pre-image correlation (exact post-state digests, including append) | Argument-only predicates; no pre-call reads; append not verifiable |
| `declared` and `strict` policies compared | Only the strict contract exists; resend/exclusivity keys are rejected at load |
| Verdict names `satisfied` / `inconclusive` / `unsatisfied_open` | Ledger/answer vocabulary: `verified` / `not_verified` / `undetermined` (no separate "open" state: not probing a live executor makes it unnecessary) |

## 9. Evidence hashes

sha256, first 16 hex.

**New and changed source:**

| File | sha256 (16) |
|---|---|
| `reconcile.py` (src) | `11208ec6d4db3c66` |
| `ledger.py` | `4fcc6e1840aaa11e` |
| `stdio_proxy.py` | `466f9a7fa342e598` |
| `test_reconcile.py` | `ddffcb433c8849be` |
| `test_relay_reconciliation.py` | `af64fbdf18abc31c` |
| `reconcile_live.py` | `0d190f1af6856dcd` |
| `harness.py` | `55fc97d105d37c2f` |

**Preserved artifacts (unchanged, verified):**

| File | sha256 (16) |
|---|---|
| `WEEK2_DOMAIN2_FILESYSTEM.md` | `9386533c92597fbd` |
| `WEEK3_RECONCILIATION_EXPERIMENT.md` | `7914662a4fa7bfe5` |
| `fixture.py` | `ee2e73d167fb86fe` |
| `rawclient.py` | `6589c75d3ee6d726` |
| `workflow.py` | `7da087a2bce1411a` |
| `shim.py` | `c9e936fecaa3fea0` |
| `workload.py` | `40a4ada1d0ab0268` |
| `fidelity.py` | `50a7744a5309961c` |
| `probe.py` | `b6a8f349c057b5e0` |
| `reconcile.py` (benchmark) | `99653bf21bcfa13a` |
| `reconcile_shim.py` | `73b9b03f6b832bfb` |
| `reconcile_experiment.py` | `3b70d1fb884fc6f2` |
| `test_week3_reconciliation.py` | `b30ab2c86c054617` |
| `test_domain2_filesystem_testbed.py` | `717ff67dbde43b69` |

**New private evidence**, under `/root/stateguard-captures/week3-implementation/`:

| File | sha256 (16) |
|---|---|
| `live-final/results.json` | `82c0131815c56285` |
| `shadow-fidelity/fidelity.json` | `81de74780f9faaf9` |

## 10. Decisions that need review

- **D1 — answer shape.** `verified` is delivered as the existing `-32000` *error* with `postcondition: verified`, not as a synthesized tool result. A client that only checks for an error will treat a verified outcome as a failure. That is safe, but it may waste the verification. The alternative, a synthesized `CallToolResult`, would look like the tool's own answer, so I rejected it.
- **D2 — no pre-image.** I chose no pre-call reads. The alternative adds latency and a race window to every declared call. The consequence is the weaker, argument-only predicates (A2). An opt-in pre-image for selected tools would be the next increment if append verification is needed.
- **D3 — reconciliation requires `after-unknown` restarts.** The probe channel is the restarted server, so enabling reconciliation means accepting restarts while unknowns are in flight. The existing policy already permits that, and it never replays.
- **D4 — config location.** I reused the `--effects` file rather than adding a new `--reconcile FILE`, because probes depend on its `read_only` declarations.

## 11. Next recommended step

Measure value on the Week 2/3 testbed workload with the new runtime path, using the relay rather than the harness. Rerun the experiment's lost-response scenarios (R1–R10 analogues for `move_file`) through `stdio_proxy.py`, and report:
- how many of the unknowns become `verified`, with zero false `verified` against disk ground truth;
- the added latency.

Then decide D1 and D2. The decision on D2 should come only if append-style operations prove important enough to justify pre-call reads.

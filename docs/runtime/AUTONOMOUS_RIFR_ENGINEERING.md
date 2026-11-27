# StateGuard — autonomous RIFR engineering record (2026-10-25)

**Objective (governing):** drive the strict in-session Residual Invocation Failure Rate (RIFR) below 1%, measured against independent ground truth, without weakening any execution-safety invariant.
**Branch:** `claude/serene-bohr-o28snr`. **HEAD:** `b6f7ba0faece14cd6efa3a219dfedddcb0e06930`. All work is uncommitted and unpushed.
**Inputs:** `STATEGUARD_FAILURE_REDUCTION_ESTIMATE.md` and `STATEGUARD_1_PERCENT_FAILURE_ROADMAP.md`. Both are verified against the code here, and §9 corrects them where needed.

> **Bottom line.** No sub-1% claim is made, and none would be supportable. This work produced:
> - **Measured, on three pinned real third-party servers:** StateGuard now recovers every injected read failure tested. Writes stay explicitly UNKNOWN (never resent), so they count as failures.
> - **Synthetic:** the strict residual fell, and StateGuard-induced failures went to 0.
> - **The remaining residual** is made up of unknown-outcome *writes*, auth, genuine tool errors and failed restarts. A relay cannot fix the first without server cooperation. An idempotency-key contract that would close it for adopting servers is implemented and tested, but **no third-party server supports it today**.

---

## 1. Baseline (inherited state, preserved)

Recorded before any change, in `…/scratchpad/am_baseline/`:
- branch, HEAD and `git status --short`: 5 modified tracked files, 57 untracked
- SHA-256 of every inherited modified and untracked file
- the inherited tracked diff
- an aggregate hash of `.hypothesis/` (`8bae78e3…`)
- the full test-suite result: 6075 passed

The inherited relay was reconstructed from HEAD plus the saved diff. Its hash matches the recorded inherited hash (`bb51b982`), so the changes in this record are exactly the diff in §8.

## 2. Measurement loop

**Invocation boundary.** One client `tools/call` request, from the moment it is written to the relay until the client's single answer arrives or the client's wait expires. In-session only: the session has initialized.

**Strict success.**
- A read succeeds when it is answered with a non-error result whose content matches the seeded ground truth.
- A mutation succeeds when its effect exists exactly once in independent ground truth **and** the client was told it succeeded.
- Everything else is a failure: UNKNOWN, no answer, error, garbage, a wrong result delivered as success (detection-only), duplicates, and StateGuard-induced failures.
- Reconciliation verdicts are never successes.

**Tools.**

| Tool | What it is | Ground truth |
|---|---|---|
| `benchmarks/runtime/rifr.py` (new) | strict scorer over `measure.run_one`, per category and per injector, with Wilson 95% intervals, unsafe outcomes kept apart, paired Z (raw succeeded, arm failed) and recovered counts, a fault-free control arm, and a mixture model whose weights are **labelled assumptions** (fault-free and unmodelled shares included at residual 1.0) | scripted server's execution ledger + client messages |
| `/root/stateguard-captures/autonomous-mandate/real3/` (new, outside the repo) | 3 pinned **real** servers, a test-only fault shim, 7 fault modes × read/write × 5–7 arms × 12 trials, run against a **frozen code snapshot** | files on disk (fs); the server's JSONL store (memory); the SQLite file via `sqlite3` |
| `/root/stateguard-captures/autonomous-mandate/s1_concurrency/` (new) | two mutations genuinely in flight at loss, real fs server | files on disk |

**Real servers (pinned, unmodified):**
- `@modelcontextprotocol/server-filesystem` 2026.8.31
- `@modelcontextprotocol/server-memory` 2026.8.31
- `mcp-server-sqlite` 2025.4.25, in a new venv pinning `mcp==1.9.4` and `pydantic==2.11.7`. The inherited candidate venv pairs it with `mcp` 2.3.0, which cannot start the server (`'Server' object has no attribute 'list_resources'`). That venv was left untouched.

**Fault modes (injected by the shim; not server bugs):**
- `lost_exit`: executed, response lost, connection closed
- `kill_before`: connection closed, never executed
- `hang`: executed, never answered
- `hang_before`: never executed, never answered
- `garbage`: truncated response bytes
- `transient`: an `isError` "temporarily unavailable" answer, not executed
- `none`: control

## 3. Safety-critical test first: two mutations in flight at loss (roadmap S1)

The previous third-party run could not force this case: its concurrent read always resolved first.
- **Deterministic in-process test.** New file `tests/examples/test_relay_concurrent_loss.py`, 7 tests. The server processes calls serially and dies only on the second mutation, so both are unresolved at loss *by construction*. Scenarios covered:
  - both executed
  - first executed, second never executed
  - first never executed, second executed
  - conflicting writes to one path: the earlier write's predicate reads *not satisfied* even though it ran, and is never credited with the other call's state
  - a read alongside a mutation
  - without reconciliation
  - determinism across 5 repeats
- **Real server.** `@modelcontextprotocol/server-filesystem` 2026.8.31, 20 trials per mode, ground truth on disk:

| Arm | Mode | Mutations in flight at loss | Unsafe resends | False failures | Reconcile correct | Duplicate answers |
|---|---|---|---|---|---|---|
| B ordinary retry | both executed | 40 | **40** | **40** | — | — |
| B ordinary retry | 1 executed, 1 not | 40 | **40** | **20** | — | — |
| D StateGuard (scope + restart + reconcile) | both executed | 40 | **0** | 0 | **40/40** | 0 |
| D StateGuard | 1 executed, 1 not | 40 | **0** | 0 | **40/40** | 0 |

`scope_ended` termination was confirmed in 40/40 StateGuard trials. **Result: no defect found.** The relay already handled several in-flight mutations correctly, and this is now regression-tested. These are repeated trials against one server, not independent replications.

## 4. Change 1 — re-read unknown outcomes of declared reads

**Mechanism.** `decide_retry(reread_unknown=True)` (`src/stateguard/adapters/mcp/effects.py`) makes one exception to "never resend an unknown outcome": a tool the operator **declared** `read_only` is requested once more.
- **After a deadline:** the re-read goes to the same server, and **the first answer wins.** That is the original's late success, or the re-read's answer; never both. A late *error* does not pre-empt the re-read.
- **After a lost connection:** a worker waits, bounded by `DISCOVERY_BUDGET_SECONDS`, for the restarted server *whose own listing is synced*, then re-reads.
- **Idle-only restart:** `idle-only` now restarts when **every** in-flight request is re-readable. Reads left running beside a new server change nothing.
- **Exactly one answer, on every path:** a re-read whose client was already answered is dropped (`reread_superseded`), and its later UNKNOWN is suppressed (`unknown_superseded`). A relay stopping, or a restart budget spent, answers any held re-read "unknown" (`_settle_rereads`).

**Why it is safe.** A read has no side effect to repeat. This is not "replaying an operation whose outcome may be unknown" in the side-effect sense the invariant protects. The declaration is the operator's, annotations never count (except Change 2's explicit opt-in), and IDEMPOTENT is deliberately excluded: idempotent ≠ safe to run twice *concurrently*.

**Default and rollback.** Enabled by `--retry auto` (the CLI flag that already enables retries); `--no-reread-unknown` restores the old behaviour exactly. The library default is off.

**Tests:** `tests/examples/test_relay_reread.py`, 26 tests covering:
- the rule itself
- deadline and transport paths
- first answer wins, and late errors
- only declared reads; never IDEMPOTENT, NON_IDEMPOTENT or undeclared
- the attempt budget
- Shadow never re-reads
- restart failure, and a re-read lost again
- an idle-only restart refused when a mutation is also in flight

## 5. Change 2 — two opt-ins: trusted read-only hints, and a recoverable-only deadline

- **`--trust-read-only-hints`** (`EffectPolicy.of(..., trust_read_only_hints=True)`): the server's `readOnlyHint: true` counts as a READ_ONLY declaration, unless the operator declared otherwise. Every decision records `source: server_hint`. No other hint is trusted. This is the operator's statement of trust, because a wrong hint lets a mutating call be re-sent. It makes re-reads work with **zero effect configuration** on servers that annotate (filesystem and memory do; sqlite does not).
- **`--deadline-scope recoverable`**: `--call-deadline` applies only to calls the relay could resend. For any other call, a deadline can only turn success into failure: it cuts a slow but healthy call into UNKNOWN. In the synthetic corpus this was the **entire** remaining StateGuard-induced failure class (16 of 267 → 0; §7). Implemented with `InvocationLedger.expire(select=…)` (additive). The default stays `all`.

**Tests:** `tests/examples/test_relay_hints_and_deadline_scope.py`, 11 tests.

## 6. Change 3 — idempotency-key contract (server cooperation)

`src/stateguard/adapters/mcp/idempotency.py` (new). Contract, all names namespaced:
- **Advertisement:** a tool's own listing `_meta` carries `io.stateguard/idempotency: {"version": 1}`. Evidence therefore comes from each server generation's own listing and is never carried over across a restart.
- **Key:** the relay adds `io.stateguard/idempotencyKey` (a UUID) to `params._meta` of calls to such tools. It never does this for declared reads or in Shadow mode, and keeps a key the client sent.
- **Resend:** after an unknown outcome the relay resends **the same request with the same key**, once. It goes to the same server after a deadline, or to the restarted server only if that server's listing still advertises support.
- **Server side** (`IdempotencyStore`, SQLite, `synchronous=FULL`):
  1. *Started* is committed before executing.
  2. A *done* key replays its recorded result.
  3. A *started but not done* key answers `in_doubt` and **executes nothing**.
  4. The same key with other arguments is an error.

A resend therefore can never be a second execution: it is a replay or an explicit unknown. The relay turns `in_doubt` into "outcome unknown". After a deadline, though, an in-doubt answer can mean the original is still running, so the relay waits **one more deadline** for the original's late answer, which is still the client's under first-answer-wins.

**Honest limits:**
- No third-party MCP server implements this contract.
- A call whose process died between acting and recording stays unknown.
- A server that advertises support and doesn't honour it can duplicate effects: the advertisement is the server's promise.
- The test server models the conservative case: the key is recorded as started *before* any fault, so a crash or hang before execution is in doubt and never recovered.

**Rollback:** `--no-idempotency-keys`. Servers that do not advertise are unaffected.
**Tests:** `tests/examples/test_relay_idempotency.py`, 15 tests. They include two keyed mutations in flight at loss, each executed exactly once, and a slow keyed call whose late answer still wins after an in-doubt resend.

## 7. Results

### 7.1 Synthetic (scripted server, hand-labelled corpus: bounds StateGuard's effect on these faults; not a production rate)

**Iteration 1** (seed 0, 8 per fault, 155 faulted + 40 control per arm):

| Arm | Faulted failures | Control failures | Z (induced vs raw) | Unsafe beyond raw |
|---|---|---|---|---|
| raw | 117 | 0/40 | — | — |
| auto (defaults) | 89 | 0/40 | 0 | none |
| auto_ops (operator effects, 0.3 s deadline, retry auto) | 82 | 0/40 | 8 | none |
| **ops_reread** (+ Change 1) | **72** | 0/40 | 6 | none |

**Iteration 2** (seed 1, 16 per fault, 267 faulted + 200 control per arm):

| Arm | Faulted failures | Control (Wilson upper) | Z | Unsafe outcomes (all arms ≤ raw) |
|---|---|---|---|---|
| raw | 213 | 0/200 (≤ 1.88%) | — | wrong 5, false-success 5, false-failure 16, invalid-result-as-success 16 |
| auto | 177 | 0/200 | 0 | wrong 1, false-success 1, false-failure 16, invalid 16 |
| auto_ops | 167 | 0/200 | **16** | wrong 1, false-success 1, false-failure 10, invalid 16 |
| ops_reread | 138 | 0/200 | 12 | same as auto_ops |
| **ops_scoped** (+ Change 2 deadline scope) | **126** | 0/200 | **0** | same as auto_ops |

Duplicate effects were 0 in every StateGuard arm. The `naive` blind-retry arm produced 24 duplicates in iteration 1.

**Which failures are left?** Every read-only call that can be recovered now is: in iteration 1 the remaining read-only failures were a restart that fails (2) and expired auth (4). The residual is:
- unknown outcomes of non-read calls (needs Change 3 plus server adoption)
- tool execution errors after a side effect (outside StateGuard's control)
- output-schema violations (detected, not repairable)
- auth (outside control)
- servers that cannot be restarted

**Mixture RIFR** at *assumed* fault rates and shares, `ops_scoped`, iteration 2:

| Assumed fault rate | Point | Upper bound |
|---|---|---|
| 2% | 0.94% | ≤ 3.04% |
| 5% | 2.34% | ≤ 4.79% |
| 10% | 4.67% | ≤ 7.71% |

This is **not** a sub-1% claim. The weights are assumptions, the upper bound is above 1% at every assumed rate, and the 2% point estimate depends entirely on the assumed fault rate.

*(Iteration 3, with the idempotency arm: §7.3.)*

### 7.2 Real servers (three pinned third-party servers, injected faults, ground truth outside StateGuard)

**Iteration 1** was run against the frozen code snapshot `snapshots/iter1` (Change 1), with 12 trials per cell. Totals per arm: 432 faulted invocations (216 reads, 216 writes) and 72 fault-free controls. The operator-configuration arms use `--effects` (each server's read tool `read_only`, its write tool `non_idempotent`), `--retry auto`, `--restart 2` and `--call-deadline 1.0`.

| Arm | Reads failed (Wilson 95%) | Writes failed | Controls failed | Unsafe outcomes | Z | Recovered-read latency p50 |
|---|---|---|---|---|---|---|
| raw (passthrough) | 216/216 | 216/216 | 0/72 | 0 | — | — |
| naive (client retries once) | 0/216 | 72/216 | 0/72 | **36 duplicate effects (sqlite), 36 false failures (fs)** | 0 | 3.63 s |
| StateGuard defaults | 216/216 | 216/216 | 0/72 | 0 | 0 | — |
| StateGuard ops, re-reads off | 180/216 | 216/216 | 0/72 | 0 | 0 | 0.65 s |
| **StateGuard ops + re-reads** | **0/216 (≤ 1.7%)** | 216/216 | 0/72 | **0** | **0** | 1.52 s |

Notes:
- **Faults per arm:** every faulted invocation meets one of six injected faults, so the raw arm fails 100% by construction. Control p50 latency is 0.62–0.65 s in every arm, which is process start-up included.
- **Writes:** StateGuard never resends a write whose outcome is unknown. It answers "outcome unknown", which counts as a strict failure.
- **Naive retry looks better on writes, but only by luck.** Its write "successes" are cases where:
  - the first attempt never executed (`kill_before`, `hang_before`, `transient`), or
  - the server de-duplicates (memory `add_observations`).

  The relay cannot tell either case apart from an executed write. The same blind resend produced 36 duplicate rows in SQLite and 36 false "move failed" reports on the filesystem server, which is the harm the safety invariant forbids.
- **Strict RIFR on this workload:** StateGuard's best safe arm is 216/432 = 50.0% of faulted invocations; naive is 16.7%, with 72 unsafe outcomes. Without server cooperation, the writes in this fault mix are the floor.

**Iteration 2** was run against the frozen snapshot `snapshots/iter2` (Changes 1–3; the audit-record tweak in §8 came after it and does not affect outcomes). 12 trials per cell, 1512 invocations:

| Arm | fs reads | memory reads | sqlite reads | writes (all) | Controls | Unsafe | Z |
|---|---|---|---|---|---|---|---|
| raw | 72/72 | 72/72 | 72/72 | 216/216 | 0/72 | 0 | — |
| **sg_hints**: `--trust-read-only-hints`, no effects file | **0/72** | **0/72** | 72/72 (no annotations) | 216/216 | 0/72 | 0 | 0 |
| sg_scoped: effects file + `--deadline-scope recoverable` | 0/72 | 0/72 | 0/72 | 216/216 | 0/72 | 0 | 0 |

- Trusting `readOnlyHint` recovers reads with **zero effect configuration** on servers that annotate their tools (filesystem, memory). It does nothing on servers that don't (sqlite).
- With the recoverable deadline scope, a hung *write* gets no relay deadline. It waits for the client's own timeout (a failure either way, now a hidden one rather than an explicit UNKNOWN). That is the documented cost of never cutting a slow mutation short.
- The real-server fault set contains no slow-but-healthy calls, so the deadline scope's Z benefit shows only in the synthetic corpus (§7.1).

### 7.3 Synthetic iteration 3 (seed 2, 16 per fault, 267 faulted + 400 control per arm; final code)

| Arm | Faulted failures | Control (Wilson upper) | Z | Duplicates | Mixture RIFR, assumed f=2% / 5% / 10% (upper 95%) |
|---|---|---|---|---|---|
| raw | 213 | 0/400 (≤ 0.95%) | — | 0 | 1.51 (2.60) / 3.77 (5.08) / 7.54 (9.21) % |
| naive blind retry | 181 | 0/400 | 0 | **48** | 1.31 / 3.27 / 6.54 % |
| auto (defaults) | 177 | 0/400 | 0 | 0 | 1.23 / 3.08 / 6.15 % |
| auto_ops | 165 | 0/400 | 16 | 0 | 1.10 / 2.75 / 5.51 % |
| ops_reread (Change 1) | 130 | 0/400 | 11 | 0 | 0.97 / 2.42 / 4.84 % |
| ops_scoped (+ Change 2) | 119 | 0/400 | **0** | 0 | 0.90 (2.11) / 2.25 (3.84) / 4.50 (6.73) % |
| **ops_keyed** (+ Change 3, *servers that adopted keys*) | **91** | 0/400 | **0** | 0 | 0.83 (2.04) / 2.07 (3.67) / 4.15 (6.39) % |

**Per category, ops_keyed vs ops_scoped:**
- Ambiguous completion (I): 18/32 → **0/32**. The lost answers of executed, recorded calls are replayed.
- Result integrity (H): 26 → 16. Truncated answers of keyed calls are now replayed. The 16 left are output-schema violations, which are detected only.
- Transport (E) is unchanged at 27:
  - 16 are servers that cannot be restarted.
  - 11 are deaths *before* execution, which are in doubt under the conservative store model and never recovered.

**What remains in ops_keyed (91):**
- auth: 16
- output-schema violations: 16 (detected, not repairable)
- restart impossible: 16
- in-doubt pre-execution deaths: 11
- timeouts and transient errors of undeclared tools: 22
- tool errors after a side effect: 9
- one known repair gap: 1

Unsafe outcomes never exceed raw in any StateGuard arm. The 9 false failures in every ops arm are server-reported tool errors after a side effect: the scripted server's own lie, delivered unchanged, and also present in raw (16).

**Reading the mixture column.** Point estimates below 1% appear only at an *assumed* 2% fault rate. The 95% upper bounds are 2.0–2.1%. Even the best arm cannot support "RIFR < 1%", and the fault rate itself is an assumption.

## 7.4 Category labels (after this work)

| Category | Label | Conditions / evidence |
|---|---|---|
| Input-contract / schema drift | **Repairable safely** under conditions | covered drift kinds, fail-closed on ambiguity; synthetic 21→1, calibration 41/41 (unchanged this session) |
| Tool discovery / stale listing | **Preventable** (StateGuard's own interventions) | refresh-before-intervening; the client's own stale view is **unknown pending evidence** |
| Protocol / malformed exchanges | **Detectable**, not recoverable | classification only; client-side malformed envelopes pass through unchanged |
| Initialization / session / capability | **Recoverable** after restart (handshake replay) | tested synthetically; session loss without process death is **unknown pending evidence** |
| stdio transport, process death | **Recoverable** for reads (re-read on the restarted server) and keyed calls; **detectable** (explicit UNKNOWN) otherwise | real 3-server: reads 0/216 failed; writes UNKNOWN |
| HTTP transport | **Outside current scope** | stdio only |
| Timeouts | **Recoverable** for declared reads (deadline re-read, first answer wins); a deadline is *not* applied to unrecoverable calls with `--deadline-scope recoverable` | synthetic Z 16→0 |
| Lost responses / ambiguous completion | **Recoverable** for reads; **recoverable for keyed calls on servers that adopt the contract**; otherwise **detectable, not safely recoverable** | real: writes 216/216 UNKNOWN; synthetic keyed: §7.3 |
| Unsafe retry / duplicates | **Preventable** | 0 resends of unknown mutations, 0 duplicates in every StateGuard arm (synthetic, real 3-server, S1 concurrency 80/80) |
| Result integrity | **Detectable**, not repairable | 16/16 detections synthetic; results never altered |
| Tool execution errors | **Recoverable** only for declared reads/idempotent tools on a knowing error; **outside control** otherwise | real `transient` reads 36/36 recovered by retry |
| Auth | **Outside control** (classification only) | never retried |
| Concurrent calls at loss | **Preventable** (no unsafe resend) | S1: 80 mutations in flight, 0 resends |
| Restart races | **Preventable** | existing T14 gates; unchanged |
| StateGuard-induced | **Measured**: 0 in every StateGuard arm on real servers; 0 with the recoverable deadline scope (synthetic) | §7.1–7.2 |
| Configuration, model reasoning | **Outside control**; not in the in-session denominator; reported separately | — |

## 8. Files changed by this work

**Modified.** Each file marked † was already modified in the inherited tree; my edits are additive on top of the inherited changes, verified by diffing against HEAD + the inherited diff.

| File | Change |
|---|---|
| `examples/mcp/stdio_proxy.py` † | re-read worker, first-answer-wins delivery, never-two-answers guards, idle-only relaxation for re-sendable in-flight calls, trusted hints, deadline scope, idempotency keys and in-doubt handling, three CLI flags, module docs |
| `src/stateguard/adapters/mcp/effects.py` | `decide_retry(reread_unknown=…, keyed=…)`; `EffectPolicy.of(trust_read_only_hints=…)`; `EffectDeclaration.source`. Audit records are byte-identical unless a hint is trusted. |
| `src/stateguard/adapters/mcp/ledger.py` † | `expire(select=…)` |
| `benchmarks/runtime/harness.py` † | `Rig` pass-throughs; opt-in `ScriptedServer` idempotency store |
| `benchmarks/runtime/measure.py` | arms `ops_reread`, `ops_scoped`, `ops_keyed` (not in `ARMS`: the historical report is unchanged) |
| `docs/runtime/TASKMAP.md`, `STATEGUARD_FAILURE_REDUCTION_ESTIMATE.md`, `STATEGUARD_1_PERCENT_FAILURE_ROADMAP.md` | dated addenda appended; earlier text untouched |

**New:**
- `src/stateguard/adapters/mcp/idempotency.py`
- `benchmarks/runtime/rifr.py`
- `tests/examples/test_relay_concurrent_loss.py`
- `tests/examples/test_relay_reread.py`
- `tests/examples/test_relay_hints_and_deadline_scope.py`
- `tests/examples/test_relay_idempotency.py`
- `tests/examples/test_rifr_scorer.py`
- this file

**Outside the repo:** `/root/stateguard-captures/autonomous-mandate/` (harnesses, frozen snapshots, raw rows, summaries, `MANIFEST.sha256`, baseline copy, sqlite venv).

**Rollback** (each runtime change is behind a flag):
- `--no-reread-unknown`
- `--no-idempotency-keys`
- leave `--trust-read-only-hints` unset (the default)
- `--deadline-scope all` (the default)

`--retry shadow`, the default, acts on none of them.

## 10. Remaining residual and what would move it

On the measured workloads the remaining strict failures are:
1. **Writes whose outcome is unknown.** These are 100% of the real-server residual. Only a server-side contract fixes them; the keys of Change 3 are one. In the synthetic corpus they took ambiguous-completion failures from 18 to 0 for adopting servers. **External dependency:** server adoption.
2. **Auth, tool errors after side effects, output-schema violations.** Outside a relay's control, or detection-only.
3. **Servers that cannot be restarted.**

**Unmeasured:**
- production fault incidence and category shares, so the mixture weights remain assumptions
- HTTP transport
- client behaviour with junk lines (`garbage` mode forwards the truncated bytes; a re-read then answers)
- non-root container deployment of the new paths

**The single highest-leverage next task** is to measure real fault incidence: run the relay in Shadow mode with `--record-provenance real_capture` on real agent traffic, across the three pinned servers or a design partner, and score the capture with `rifr.py`'s judging. Until then, whether the safe residual is above or below 1% depends entirely on an assumed fault rate, and every sub-1% statement would rest on that assumption. A runner-up that needs no new evidence is a server-side reference adapter (Python MCP SDK middleware around `IdempotencyStore`), so a real server can adopt Change 3 and be measured.

## 10a. Gates on the final tree

| Gate | Result |
|---|---|
| Full suite | **6138 passed / 6138 collected**, rc 0 (6075 inherited + 63 new). An earlier run failed `test_replay.py::test_recording_again_gives_the_same_content` because new audit fields changed recordings. Fixed by keeping audit records byte-identical for unchanged configurations, not by regenerating the inherited tapes. |
| `.hypothesis/` DB (Week-4G counterexample) | unchanged, `8bae78e3…` before and after; suites ran with an isolated `HYPOTHESIS_STORAGE_DIRECTORY` |
| `ruff check src/ tests/`, `ruff format --check src/ tests/` | pass |
| `mypy --strict src/` (CI gate), plus mypy on the relay and the changed benchmark scripts | pass |
| Inherited state | 57 untracked + 5 modified files preserved: all unchanged except the three † files and the appended addenda (§8); verified by hash against the recorded baseline |

## 11. Reproduce

```
cd /home/user/stateguard
# tests (Hypothesis DB isolated so the preserved .hypothesis counterexample is untouched)
HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp .venv/bin/python -m pytest -p no:cacheprovider -q
.venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/ && .venv/bin/mypy src/
# synthetic strict RIFR (iteration 3)
.venv/bin/python benchmarks/runtime/rifr.py --seed 2 --per-fault 16 --control 400 \
  --arms raw,naive,shadow,auto,auto_ops,ops_reread,ops_scoped,ops_keyed --json out.json
# real servers (frozen snapshot), per server: fs | mem | sqlite
cd /root/stateguard-captures/autonomous-mandate
SNAPSHOT=$PWD/snapshots/iter2 /home/user/stateguard/.venv/bin/python real3/driver.py fs 12 "" raw,sg_hints,sg_scoped > rows.jsonl
python3 real3/summarize.py rows.jsonl
# two mutations in flight at loss, real fs server
/home/user/stateguard/.venv/bin/python s1_concurrency/s1_driver.py 20 > results.json
```

## 9. Corrections to the Phase A/B reports

- The estimate report credited reads with no recovery after lost responses. That is now implemented (Change 1), and measured on three real servers (§7.2).
- The roadmap's S3 ("progress-aware deadlines") assumed deadline Z needed progress tracking. Measurement shows a simpler dominant fix: do not apply a deadline to calls that cannot be recovered (Change 2). Z fell 16 → 0 in iteration 2.
- The roadmap's C1 (idempotency keys) is implemented as an opt-in, server-advertised contract (Change 3). Its effect on third-party traffic is 0 until servers adopt it.
- The sqlite candidate from Week 2 was unusable as installed (dependency mismatch). A pinned working venv now exists.

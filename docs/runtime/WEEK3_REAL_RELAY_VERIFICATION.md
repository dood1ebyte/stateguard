# Week 3 — Real-relay verification and safety audit of reconciliation

**Date:** 2026-10-16. **Branch:** `claude/serene-bohr-o28snr`, HEAD `b6f7ba0` (= `origin/main`).
**Scope.** This audit independently checks the uncommitted Week 3 reconciliation implementation. It runs the production relay entry point against the pinned filesystem server, with ground truth taken from disk.
**Constraints honoured.**
- No production runtime code was changed.
- Nothing was committed, pushed or merged.
- Prior evidence is unchanged (§2).

> **Status update (2026-10-16, after approval).** Defects A, B and C are fixed and re-verified. See §13 and `docs/runtime/WEEK3_AUDIT_FIXES.md`. Sections 1–12 are kept as written: they record the audit's findings on the pre-fix code.

## 1. Executive conclusion

**Needs specific correction. Do not publish the Week 3 runtime yet.** The design holds wherever its assumptions hold. Against the real server, with the old executor provably gone:

- every `verified` matched the configured predicate on disk;
- every `not_verified` was correct;
- every faulty, stale, inconsistent or unavailable probe escalated to `undetermined`;
- across 243 trials there were **0 resends, 0 duplicate deliveries, and 0 claims of execution or attribution**.

Three production defects block publication. Each has a reproducible failing case: a strict-xfail test plus live evidence.

| # | Defect | Severity | Evidence |
|---|---|---|---|
| **A** | **Executor termination is trusted, not established.** `_run_reconciliation` passes `executor_replaced=True` unconditionally (`examples/mcp/stdio_proxy.py:1081`). The `StdioRelay(restart=...)` API accepts any factory without attestation. `main()`'s factory (`:2079`) stops only its *direct* child. A descendant that outlives it can change state after the probe. | **Material safety gap** | T1, through `main()`: **5/5 `verified` answers, then the order was rolled back** about 4 s later. The in-process custom factory reproduces it too: `test_a_factory_that_leaves_the_old_executor_running_is_never_verified` (xfail strict). |
| **B** | **A pending reconciliation can go unanswered when the relay stops.** The worker pops the pending entry in `_finish_reconciliation` (`:1128`) before writing. If `pump_server`'s `finally` (`:1516`) runs in between, it finds nothing to answer and sets `_stopped`. `main()` then calls `os._exit` (`:2165`) before the worker writes. | Liveness: breaks "never silence instead of an answer" | S9 (restart fails), through `main()`: **2/25 trials, the client got no answer at all**; raw mode answered 25/25. The deterministic reproduction is `test_a_pending_reconciliation_is_answered_before_the_relay_stops` (xfail strict). |
| **C** | **Client ids with the reserved probe prefix are swallowed.** Every response whose id starts with `stateguard-reconcile:` is routed to probe handling (`:1708`). A client request using such an id is never answered while the session lasts. | Minor: a new silence path; same class as the existing `OWN_ID_PREFIX` caveat | S12b: **5/5 unanswered** until the 15 s client timeout. Reproduction: `test_a_client_id_with_the_probe_prefix_is_still_answered` (xfail strict). |

Per the instructions, **no fix was applied.** §12 gives the smallest safe fix for each, for approval.

Two semantic findings need correction in docs and wording, not in the decision logic.

1. **`verified` means "the configured predicate held when probed".** It does not mean the original executed, caused the state, or completed correctly. In the 45 real-server `verified` answers:
   - the original produced the state in **15**;
   - another actor or a pre-existing state produced it in **10**;
   - the destination content was wrong in **10**, because a move probe only checks presence;
   - the predicate was already false by the time the client read the answer in **10**: 5 from a concurrent writer between probes, and 5 from a lying probe server.

   The relay never claims execution or attribution. Its message, however, says "verified the **intended** postcondition", which overstates what a presence-only predicate shows.
2. **The hold-time bound in the implementation report is wrong.** It is not "≈20 s plus probe timeouts". The wait loop runs `max_restarts + 2` times, so the hold is measured at **40.3 s (3/3)** with `--restart 2` and a server that never lists. The derived worst case is **(N+2) × 30 s + Σ probe timeouts**: 240 s for N=2 with 4 × 30 s probes.

## 2. Repository state and files changed

**Preflight:** branch `claude/serene-bohr-o28snr`, HEAD `b6f7ba0` = `origin/main`. The Week 3 implementation was uncommitted:

- **Modified:** `harness.py`, `stdio_proxy.py`, `adapters/mcp/__init__.py`, `ledger.py`.
- **Untracked:** the reconcile modules, tests and reports.

Every hash matched the implementation report at preflight.

**Added by this audit.** All of it is isolated tooling, tests and this report; none of it is a runtime change.

| File | Purpose |
|---|---|
| `benchmarks/domains/mcp_server_filesystem/relay_audit_shim.py` | Fault shim between the production relay and the real server. Per-generation faults, other actors, probe tampering, failing restarts, a no-listing restart. It logs ids, tools and generations only. |
| `benchmarks/domains/mcp_server_filesystem/rollback_server.py` | Minimal test MCP server that breaks the termination assumption. Modes: `detached` (a descendant rolls back late), `detached_late` (a descendant executes late), `survivor` (the direct child outlives its stdout). |
| `benchmarks/domains/mcp_server_filesystem/relay_audit.py` | Runs 32 scenarios × configurations through `stdio_proxy.py main()`, plus 18 config-validation cases. Takes ground truth from disk snapshots and the delivery log. |
| `benchmarks/domains/mcp_server_filesystem/relay_client_semantics.py` | Exact JSON-RPC response, plus Python `mcp` 2.3.0 and TS SDK 1.32.1 client behaviour, through the same relay. |
| `tests/examples/test_relay_reconciliation_audit.py` | Three `xfail(strict=True)` tests asserting the safe behaviour for defects A, B and C. Each fails today, deterministically: 6/6 repeated runs gave `xxx`. |
| `docs/runtime/WEEK3_REAL_RELAY_VERIFICATION.md` | This report. |

**Production code unchanged.** Hashes after the audit (sha256, first 16 hex) equal the preflight and the implementation report:

| File | sha256 |
|---|---|
| `src/stateguard/adapters/mcp/reconcile.py` | `11208ec6d4db3c66` |
| `ledger.py` | `4fcc6e1840aaa11e` |
| `stdio_proxy.py` | `466f9a7fa342e598` |
| `__init__.py` | `bebcea566519dbe1` |
| `harness.py` | `55fc97d105d37c2f` |
| `test_reconcile.py` | `ddffcb433c8849be` |
| `test_relay_reconciliation.py` | `af64fbdf18abc31c` |
| `test_week3_reconciliation.py` | `b30ab2c86c054617` |
| `test_domain2_filesystem_testbed.py` | `717ff67dbde43b69` |

**Prior evidence unchanged.**

| Evidence | sha256 |
|---|---|
| `WEEK2_DOMAIN2_FILESYSTEM.md` | `9386533c92597fbd` |
| `WEEK3_RECONCILIATION_EXPERIMENT.md` | `7914662a4fa7bfe5` |
| `WEEK3_RECONCILIATION_IMPLEMENTATION.md` | `19dc0b1b9ba67b9f` |
| Week 1 recordings | `5952c1898ede45ce`, `44d4798335097769`, `636880d507888b38`, `bdc65c64ecc0de41` |
| Week 2 recording manifest | `cef453720aac8b7e`; all 10 recordings match their listed hashes |
| Week 2 `results-raw` / `results-shadow` / `results-tee` / `fidelity` | `28a9beb529b42d9b` / `097a7763becf561a` / `d701de99240a782d` / `57697bcd7beaaaf2` |
| Week 3 experiment `results.json` | `24304f58c991964f` |
| Week 3 implementation `live-final/results.json` | `82c0131815c56285` |
| Week 3 implementation `shadow-fidelity/fidelity.json` | `81de74780f9faaf9` |

The benchmark files `fixture.py`, `rawclient.py`, `workflow.py`, `shim.py`, `workload.py`, `fidelity.py`, `probe.py`, `reconcile*.py` and `reconcile_live.py` are all unchanged.

> One process note. While formatting my new files, I ran `ruff format` once on the whole `benchmarks/domains/mcp_server_filesystem/` directory. It changed none of the pre-existing files, which were already formatted; all of their hashes above were re-checked afterwards.

## 3. Code paths and assumptions (audit)

### Paths traced

All references are to `examples/mcp/stdio_proxy.py` unless named otherwise.

- **Lost response → unknown.**
  - `_pump_server` reaches EOF, then `_on_server_lost` (`:1526`).
  - The link goes to RESTARTING.
  - `_fail_probes`, then `ledger.transport_closed()`.
  - `_resolve_unknown(..., probe_channel=after-unknown ∧ restarts left)`, then `_begin_reconciliation` (`:945`).
  - Gates are checked in order: tool declared; not shadow; `Observed.UNKNOWN_TRANSPORT`; probe channel; call arguments held; `bind_probes`. Any failure gives an immediate `undetermined`, with no probe.
  - Otherwise a `pending` entry and a worker thread are created, and the client's answer is held.
- **Restart.** `_on_server_lost` calls the factory up to `max_restarts` times, then `_connect`. The generation is incremented under `_to_server`, and the initialize and listing are replayed.
- **Probe dispatch.** `_run_reconciliation` (`:1031`) waits for a new generation with a valid listing, at most `max_restarts + 2` rounds of `_await_link` and `_await_discovery`.
  - Each probe tool is re-checked against the *current* listing and annotations (`probe_tool_problem`).
  - `_send_probe` writes with `_send_decided(None, …, generation)`, so a probe can never reach another generation. Each probe waits for its own `timeout_seconds`.
- **Probe responses.** `_pump_server` routes any response whose id has the `stateguard-reconcile:` prefix to `_on_probe_response` (`:1708`), before ledger handling. Unknown ids are logged as `reconcile_probe_late` (defect C).
- **Verdict.** `decide(..., executor_replaced=True, generation_stable=…)` (`:1081`) evaluates in this order:
  - stale → incomplete → any unusable probe → inconsistent same-key reads give `undetermined`;
  - all satisfied gives `verified`;
  - otherwise `not_verified`, worded "does not show the request did not execute".
- **Ledger and client answer.** `_finish_reconciliation` pops the entry under `_reconcile_lock`, then `_record_verdict` (ledger and audit, statuses only), then `_surface_unknown`, which writes `unknown_outcome_response`. Defect B is the gap between the pop and the write.
- **Shadow.** `_begin_reconciliation` returns before probing, and `_on_server_lost` never restarts in shadow mode.
- **Deadline unknown.** The deadline is `Observed.UNKNOWN_DEADLINE`, so the call is answered `undetermined` and nothing is probed.
- **Restart failure.** A factory exception is counted. On exhaustion the link goes DOWN, the worker's `_await_link` returns False, and the answer is `undetermined`: "no probe channel". Otherwise `pump_server`'s `finally` abandons the call with `undetermined`.

### Property checklist

| Property | Result | How established |
|---|---|---|
| **A. Executor terminated before probing** | **Not established in general (defect A).** It holds only for `main()` **and** a server whose effects all happen in the direct child. | Code: there is no attestation in the `StdioRelay` API, and `main()` signals only the direct child (no `start_new_session`, no `killpg`). Live: T3, a surviving direct child, was handled correctly 5/5. T1, a detached descendant, gave a false `verified` 5/5. The pinned filesystem server had **no child processes** before or after a real `move_file` (checked with `ps`). The real-server results therefore don't depend on the gap. |
| B1. Probe tools explicitly read-only | Holds. The operator must declare each probe tool `read_only` in the same file, and the server can only veto (`readOnlyHint:false` or `destructiveHint:true`). A missing annotation is accepted. | Config cases `probe_tool_not_read_only` and `probe_tool_undeclared` are refused. S12d shows an unlisted probe tool gives `invalid` and then `undetermined`. |
| B2. Invalid config fails closed | Holds. 15/15 invalid cases exit with status 2 **before any server starts**. | §4, config validation. `$nope` cannot be checked at load time, because the schema is not known yet. It fails closed at call time instead: S12c, 5/5 `undetermined`, no probe. |
| B3. Probes and timeouts bounded | Holds for count and timeout: ≤ 4 probes, each timeout in (0, 30] s. The hold is longer than documented (§1, finding 2). | S7a: 2 × 1 s timeouts gave 2.38 s. S12e: 40.3 s. |
| B4. Probe arguments reach no unrelated data | Holds. Arguments are `$ref` only, bound to the call's own string arguments. Literals are refused (`literal_argument`, `/etc/passwd`). The server's own allowed-directory sandbox still applies. | Config and live runs. The sentinel outside the sandbox was unchanged in 243/243 trials. |
| B5. Malformed or contradictory evidence → unresolved | Holds for RPC errors, malformed results, hangs, server death, inconsistent re-reads and unlisted tools (30/30 `undetermined`: S6a, S6b, S6c, S7a, S7b, S12d). **It does not hold for a state that changes between two different probes (S6d)**, because each read is consistent on its own. A server that lies (S6e) cannot be detected (assumption A2). | §5, group 6. |
| B6. No leak to the audit log or ledger | Holds. 551 reconciliation audit events contain 0 paths and 0 contents. None of 243 audit logs contains fixture contents or probe error text. The ledger holds statuses and forms only (unit-tested). Probe responses *are* tapped into `--record` recordings, subject to recording redaction (unchanged from earlier). | Audit-log scans. |
| B7. A probe never causes a resend | Holds. Probe ids are never in `_calls`, and retries are off. | 0/243 deliveries of the original id to a later generation, and 0 non-read calls on later generations. |
| C. Postcondition vs execution vs causation vs correctness | `verified` establishes only "the configured predicate held when read". | §6: other actor (S4a), pre-existing state (S4b), wrong content (S4c, S4d), partial effects (S5, always `not_verified`). |
| D1. Shadow: no probes, no restart, traffic unchanged | Holds. | S10a–c, 15/15: 0 probes, 1 server process. The client saw the same pipe-close or late real answer as with no relay logic. |
| D2. Deadline unknown never probed | Holds. | S8a and S8b, 15/15 (10 reconcile, 5 raw): 0 probes, answered at about 1.04 s. |

## 4. Testbed, server, configuration and fault method

- **Server:** `@modelcontextprotocol/server-filesystem` **2026.8.31** (TS SDK 1.32.1, zod 4.6.7), Node 22.22.0. It is installed outside the repo at `/root/stateguard-captures/domain2-fs/server/`, from the Week 2 pins (`env/package.json` `c8cd7146ba218537`, `env/package-lock.json` `404c089bc4b2d2b8`).
- **Relay:** production entry point `python examples/mcp/stdio_proxy.py --audit F --effects F --retry off`, plus one of:
  - `--restart 2 --restart-policy after-unknown`
  - `--shadow`
  - `--call-deadline 1.0` added for groups 8 and S10c.

  The `--effects` loading and validation path is the production one.
- **Python:** Python 3.11, `mcp` 2.3.0.
- **Configurations:**
  - **reconcile:** `move_file` with probes `read_text_file($source)` expecting absent (marker `ENOENT`) and `read_text_file($destination)` expecting present, timeout 5 s. Variants: `reconcile_reread` (source read twice), `reconcile_badref` (`$nope`), `reconcile_unlisted` (`stat_file`).
  - **raw:** the same file and flags, without the `reconcile` block.
  - **naive:** raw, plus one client re-send under a new id after the unknown answer.
- **Faults:** applied by `relay_audit_shim.py` between the relay and the real server. The relay starts the shim as its server command, so every restart is a new shim plus a new server.
  - **Generation 0, on the original `move_file`:**
    - `drop_response`: deliver, read the answer, drop it, kill and reap the server, exit.
    - `swallow_request`: never deliver, then exit.
    - `delay:3`.
    - `hang`.
  - **Generation 1:**
    - another-actor operations before the server starts;
    - probe tampering: `rpc_error`, `malformed`, `hang`, `die`, `lie_post`, `flip:OP`.
  - **Every restarted generation:** `--fail-restart` (exit 3) and `--no-listing-on-restart`.
  - **Group T:** uses `rollback_server.py` directly as the server, a test server rather than the pinned one.
- **Fixture:** the Week 2 sandbox at checkpoint 2, with the approved order in `inbox/`. The call is workflow step S3, `move_file(inbox/order-001.txt → done/order-001.txt)`; the intended state is checkpoint 3.
- **Ground truth:**
  - a sandbox snapshot (sha256 per file) taken right after the client's answer, and again at trial end, after any late effect;
  - the shim or server delivery log;
  - never the relay's answer or audit log.
- **Repetitions:** 5 per scenario and configuration, plus extra batches: S9 × 20 (reconcile and raw) and S12e × 3.
- **Volume:** 243 trials in total — 228 against the real server, 15 against the test server (group T).
- **Config validation:** 18 effects files run through `main()`. The "server" in these cases is a command that only touches a marker file, so "server started" is observed rather than assumed.
- **Concurrency:** the client-semantics run (§9) overlapped the full run for about one minute. Latencies may include that contention.

## 5. Per-scenario results

Counts are trials (n = 5 unless stated). Columns:

- **verified ok:** `verified`, the predicate held on disk at the answer and at trial end, and the state equals the intended checkpoint.
- **verified ≠ intent:** `verified`, but the intended operation is not what happened.
- **verified, false at answer:** `verified`, but the predicate was already false on disk when the answer arrived.

| # | Scenario (configuration) | verified ok | verified ≠ intent | verified, false at answer | not_verified (correct) | undetermined | other | Notes |
|---|---|---|---|---|---|---|---|---|
| 1 | S1 executed, answer lost (reconcile) | **5** | 0 | 0 | 0 | 0 | 0 | 2 probes each |
| | S1 (raw) | – | – | – | – | – | 5 unknown | |
| | S1 (naive) | – | – | – | – | – | 5 unknown, then resend | resend → `isError` (ENOENT) 5/5 |
| 2 | S2 never delivered (reconcile) | 0 | 0 | 0 | **5** | 0 | 0 | |
| | S2 (raw / naive) | – | – | – | – | – | 5 / 5 unknown | naive resend → success 5/5 |
| 3 | S3 executed, then undone by another actor (reconcile) | 0 | 0 | 0 | **5** | 0 | 0 | raw: 5 unknown |
| 4 | S4a never delivered, another actor moved it (reconcile) | 0 | **5** (state correct, original not the cause) | 0 | 0 | 0 | 0 | raw: 5 unknown |
| | S4b state already held, original failed with ENOENT (reconcile) | 0 | **5** (state correct, original failed) | 0 | 0 | 0 | 0 | raw: 5 unknown |
| | S4c never delivered, actor deleted the source and wrote other content | 0 | **5** (content wrong) | 0 | 0 | 0 | 0 | |
| | S4d executed, actor then overwrote the destination | 0 | **5** (content wrong) | 0 | 0 | 0 | 0 | |
| 5 | S5a partial: copied, source kept | 0 | 0 | 0 | **5** | 0 | 0 | |
| | S5b partial: source deleted, nothing written | 0 | 0 | 0 | **5** | 0 | 0 | |
| | S5c both exist (source restored) | 0 | 0 | 0 | **5** | 0 | 0 | |
| 6 | S6a probe answered with an RPC error | 0 | 0 | 0 | 0 | **5** | 0 | 10/10 probes failed |
| | S6b malformed probe result | 0 | 0 | 0 | 0 | **5** | 0 | 10/10 probes failed |
| | S6c state flips between reads, re-read config | 0 | 0 | 0 | 0 | **5** | 0 | "inconsistent" |
| | **S6d state flips between probes, default config** | 0 | 0 | **5** | 0 | 0 | 0 | time-of-check gap |
| | **S6e probe server lies; never delivered** | 0 | 0 | **5** | 0 | 0 | 0 | outside the trust model (A2) |
| 7 | S7a probe never answered (1 s timeout) | 0 | 0 | 0 | 0 | **5** | 0 | 2.38 s hold |
| | S7b probe server dies mid-probe | 0 | 0 | 0 | 0 | **5** | 0 | "stale" |
| 8 | S8a executes late (3 s; deadline 1 s), reconcile / raw | 0 | 0 | 0 | 0 | **5** / – | – / 5 unknown | 0 probes |
| | S8b hangs (deadline 1 s) | 0 | 0 | 0 | 0 | **5** | 0 | 0 probes |
| | **T1 descendant rolls back after the probe** (test server) | 0 | 0 | 0 (**5 reverted after answer**) | 0 | 0 | 0 | **defect A** |
| | T2 descendant executes after the probe (test server) | 0 | 0 | 0 | 5 (**then executed late**) | 0 | 0 | the answer says "does not show it did not execute" |
| | T3 direct child survives stdout close (test server) | **5** | 0 | 0 | 0 | 0 | 0 | `main()` terminates it |
| 9 | S9 restart fails, reconcile (n = 25) | 0 | 0 | 0 | 0 | **23** | **2 no answer** | **defect B** |
| | S9, raw (n = 25) | – | – | – | – | – | 25 unknown | |
| 10 | S10a / S10b / S10c, shadow | – | – | – | – | – | 5 / 5 / 5 | no probe, 1 process. The client saw a pipe close (a, b) or the server's real late answer at 3.0 s (c). |
| 11 | S11 verified, then follow-up `edit_file` and read | **5** | 0 | 0 | 0 | 0 | 0 | follow-ups succeeded 5/5; final state = checkpoint 4 |
| 12 | S12a client request sent during reconciliation | **5** | 0 | 0 | 0 | 0 | 0 | the concurrent read was answered 5/5, at about 0.35 s |
| | S12b client id with the probe prefix | – | – | – | – | – | **5 unanswered** | **defect C**; the next call succeeded |
| | S12c probe references a missing argument | 0 | 0 | 0 | 0 | **5** | 0 | 0 probes |
| | S12d probe tool not listed | 0 | 0 | 0 | 0 | **5** | 0 | status `invalid` |
| | S12e restarted server never lists (n = 3) | 0 | 0 | 0 | 0 | **3** | 0 | **40.3 s hold** |

**Config validation (production `main()`, 18 cases).**

- **Refused** with exit status 2, no server started, and the error message given: `no_restart`, `idle_only_policy`, `probe_tool_not_read_only`, `probe_tool_undeclared`, `probe_is_the_call_itself`, `literal_argument`, `five_probes`, `zero_probes`, `timeout_zero`, `timeout_31`, `resend_key`, `unknown_key`, `absent_without_marker`, `unknown_expect`, `reconcile_on_read_only_tool`.
- **Accepted:** `valid`; `shadow_with_reconcile` (shadow never probes, D1); and `unknown_ref`, which fails closed at call time (S12c).

**Totals against the real server, reconcile configuration (n = 163):**

| Answer | Count |
|---|---|
| `verified` | 45 |
| `not_verified` | 25 (all correct) |
| `undetermined` | 71 |
| Shadow pass-through | 15 |
| No answer (S9, defect B) | 2 |
| Unanswered colliding id (S12b) | 5 |

## 6. False verification against ground truth

Each of the 45 real-server `verified` answers is broken down below against the four questions the task separates. "At answer" is the disk state when the client received the answer.

| Question | Yes | No | Scenarios where it was "no" |
|---|---|---|---|
| Configured predicate (source absent ∧ destination exists) held at answer | 35 | **10** | S6d (concurrent writer between probes), S6e (lying probe server) |
| Intended state (checkpoint 3; checkpoint 4 after S11's follow-up edit) held at answer and at end | 25 | 20 | S4c, S4d (wrong destination content), S6d, S6e |
| The original invocation executed successfully (server answered it without error) | 25 | 20 | S4a, S4c, S6e (never delivered); S4b (original failed with ENOENT) |
| The original caused the intended state | 15 | 30 | Only S1, S11 and S12a are "yes" |

- **Claimed beyond the evidence:** 0. Every answer carried `executionState: unknown` and `attribution: unknown` (243/243 trials). The relay itself made **no false execution or attribution claim**.
- **False against the configured predicate:** 10/45.
  - **S6d** is inherent to multi-read probes. The two reads are individually true, but the state changed between them (a time-of-check gap). Re-reading a key (S6c) catches this only when the change touches a re-read key. The default config re-reads nothing.
  - **S6e** violates the documented assumption that the probe channel tells the truth. No relay logic can detect it.
- **False against the intended operation:** 20/45 (adds S4c and S4d). Expected for a presence-only predicate on `move_file`: it cannot see content. The `text_equals_argument` form is stronger, but only applies to `write`-like tools whose content is an argument.
- **Reverted after answer (defect A):** T1, 5/5 on the test server. The predicate held at the answer, but the original's still-running descendant undid it. This is the case the design says must stay unresolved.
- **"Not verified" followed by late execution:** T2, 5/5 on the test server. The answer's reasons include "this does not show the request did not execute". That is not a false claim, but a client that resends on `not_verified` would duplicate the effect. With defect A fixed, T2 becomes `undetermined`.

## 7. Duplicates and resends

| Measure | Result |
|---|---|
| Original id delivered to any server more than once | **0 / 243** |
| Relay-originated non-read call on a later generation | **0 / 243** |
| Relay resends | **0** |
| Client resends (naive configuration, by design) | 10 delivered, 0 duplicate effects |

On the naive client resends: in S1 the resend failed with ENOENT, because the source was already moved (5/5). In S2 the resend performed the move (5/5).

- **Naive retry in S1 (equivalent conditions, same fault and server):** the client is told the move *failed* (`isError`) when it had in fact succeeded, 5/5. That is a false failure.
- **Naive retry in S2:** it completes the work, 5/5. Reconciliation does not, by design; it answers `not_verified` and leaves the decision to the client.

`move_file` is not naturally repeatable here, which is why naive retry produced no duplicate *effect*. That outcome is specific to this tool and is not evidence about retries in general.

## 8. Probe overhead and latency

| Measure | Result |
|---|---|
| Probes sent | 230 (200 real-server + 30 test-server), **all read-only `read_text_file` or declared probes**, all on generation 1 |
| Probe failures | **45 / 230 (19.6%)**, all in scenarios that inject probe faults (S6a, S6b, S7a, S7b, S12d). Outside those: 0 / 185. |
| Client answer latency, reconcile, probed scenarios without probe hang (n = 95) | **median 0.386 s, p95 0.456 s** |
| Equivalent comparison, S1–S4b, raw vs reconcile (n = 25 each) | raw median 0.035 s / p95 0.044 s; reconcile median **0.412 s** / p95 **0.726 s**. Reconcile adds about 0.38 s at the median, mostly the server restart (Node start, initialize, listing); raw answers *before* restarting. |
| Probe timeout (S7a, 2 probes × 1 s) | 2.38 s |
| Restart never usable (S12e, `--restart 2`) | **40.3 s** (3/3) = (2 + 2) × `LISTING_WAIT_SECONDS` |
| Derived worst case | (N + 2) × (10 + 10 + 10) s + Σ timeouts ≤ **240 s** for N = 2 and 4 × 30 s probes (not measured) |
| Deadline unknowns (S8, 1 s) | 1.03–1.07 s, identical with and without reconcile |
| Test server (T) | median 0.047 s (Python server, fast start) |

## 9. Client semantics (unchanged — measured, not modified)

**Exact response** (S1, from `relay_client_semantics.py`, sandbox path redacted):

```json
{"jsonrpc": "2.0", "id": "T-1", "error": {"code": -32000,
 "message": "Outcome unknown: the MCP server's connection closed before it answered. Read-only probes verified the intended postcondition, but not that this request produced it. Do not resend it.",
 "data": {"io.stateguard/executionState": "unknown", "io.stateguard/reason": "transport_closed",
  "io.stateguard/postcondition": "verified", "io.stateguard/attribution": "unknown",
  "io.stateguard/reconciliation": {"reasons": ["the intended postcondition was observed by 2 read-only probe(s); whether this request produced it is unknown"],
   "probes": [{"index": 0, "tool": "read_text_file", "status": "satisfied", "form": "absent", "detail": ""},
              {"index": 1, "tool": "read_text_file", "status": "satisfied", "form": "present", "detail": ""}]}}}}
```

The `not_verified` message instead reads: "…The request may or may not have been executed; do not retry it unless it is safe to run twice. Reconciliation could not verify the intended postcondition."

| Client | `verified` case | `not_verified` case | `data` reachable | Session afterwards |
|---|---|---|---|---|
| Raw JSON-RPC | an error object, code −32000 | the same, code −32000 | yes | follow-up read succeeded |
| Python `mcp` 2.3.0 `Client.call_tool` | **raises** `mcp.shared.exceptions.MCPError`, `.code == -32000`; `str(exc)` is the message only | raises the same | via `exc.data` (`exc.error.data`) only | follow-up succeeded |
| TS SDK 1.32.1 `Client.callTool` | **rejects** with `McpError`; `.message` = "MCP error -32000: …" | the same | via `e.data` only | follow-up succeeded |

- **Is verified distinguished from failure?** Not by any standard field. `verified` and `not_verified` have the same code, the same exception type and the same JSON-RPC error shape. They differ only in `error.data["io.stateguard/postcondition"]`, a vendor key no SDK interprets, and in the English message.
- **A client that checks only for an error treats every reconciled call as failed.** That is true of any client that uses the SDK's exception or rejection as its signal (both SDKs, 4/4 runs).
  - A framework that turns exceptions into text, e.g. `str(exc)` handed to a model, keeps only the message. There, "verified" survives only as prose.
  - The existing tests read `data` directly (`test_relay_reconciliation.py`), as do this audit's scripts.
- **Does the distinction reach clients without a protocol change?** Only to clients that read `data` or the message text. JSON-RPC permits `data`, so no protocol change is needed, but nothing standard consumes it.

**Recommended contract (not implemented).**

1. Keep the JSON-RPC error for every reconciled answer, including `verified`. Never present it as a successful `CallToolResult`: execution is unknown, and an error-only client failing safe is the right default. The cost: an error-only client may retry, which the message forbids. The S1 naive measurements show such a retry is reported as a failure (5/5).
2. Make the message say exactly what was checked. Replace "the intended postcondition" with "the configured postcondition (N read-only checks)", and keep "not that this request produced it".
3. Document the `io.stateguard/*` keys as a versioned extension. Add an explicit version key so clients can depend on it.
4. Advise clients to treat `verified` as "state check passed; do not resend". It is not "succeeded".

## 10. Differences from earlier reports

| Earlier claim | This audit |
|---|---|
| Implementation report A1: "`main()`'s factory terminates and waits for the old process … detached server children are assumed absent". The custom-factory requirement is "documented and not verified". | Confirmed as an **unenforced assumption**, and **material** (defect A). `main()` covers only the direct child (T3 safe, T1 unsafe). The API accepts any factory, and the in-process xfail test reproduces it. |
| Implementation report §4 and A5: the wait is "at most about 20 s plus the probe timeouts". | **Measured 40.3 s** with `--restart 2`. The bound is (N + 2) rounds of up to 30 s, plus the probe timeouts. |
| Implementation report: "A relay that stops answers every pending reconciliation as `undetermined`." | **Not always**: 2/25 S9 trials gave no answer (defect B). |
| Implementation report and live check L1–L4: 12/12 passed. | Consistent. L1–L4 correspond to S1, S2, S8a and S10a here, all as expected. |
| Experiment report: no false "satisfied" in the controlled corpus. | Within its scope, consistent. This audit adds cases outside that scope: concurrent change between probes (S6d), a lying probe server (S6e), and a detached descendant (T1). Each gives a `verified` that is false at or after the answer. |
| Message: "verified the intended postcondition". | Overstates what is checked: 10/45 `verified` answers had wrong destination content (§6). |

## 11. Untested cases and risks

**Untested** (explicitly not run):

- the `text_equals_argument` probe form against the real server;
- `write_file` and `edit_file` reconciliation;
- probes with more than 2 distinct tools;
- a deadline unknown followed by a transport close for the same call (the Rig test covers deadline and hang only);
- `--record` recordings during reconciliation (probe responses are tapped there and depend on recording redaction);
- a real server that spawns descendants. Only the test server does here; the pinned server spawns none for `move_file`;
- non-Linux platforms;
- `--restart` values other than 2;
- protocol 2026-07-28 stateless sessions;
- multiple concurrent reconciliations;
- representative agent frameworks beyond the two SDKs.

**Risks:**

- (i) Defect A applies to any server whose effects can outlive its main process: worker pools, shells, queued jobs.
- (ii) Presence-only predicates are easy to configure and weak.
- (iii) Multi-read probes have a time-of-check gap against concurrent writers.
- (iv) The probe server's honesty is assumed.
- (v) A worst-case hold of minutes may exceed client timeouts. A client that times out first gets no StateGuard answer at all.
- (vi) No prevalence or customer-value claim follows from this controlled testbed.

## 12. Next step (awaiting approval — nothing below is implemented)

Smallest safe fixes, in priority order. Each comes with its xfail test, which flips to pass, and its marker removed.

**A. Executor termination.** Required before any `verified` is trusted.

1. `StdioRelay` must know, per generation, whether the restart established that the old executor ended. For example, the factory returns an attestation, or a separate `executor_stopped()` callable is supplied.
2. `_run_reconciliation` passes `executor_replaced=<that attestation>` instead of `True`. With no attestation, the answer is `undetermined` and no probe is sent. This is fail-closed.
3. `main()` attests only what it can verify.
   - **Minimal:** start the server with `start_new_session=True`. On restart, send `killpg(SIGTERM → SIGKILL)` to the old group even if its leader already exited. Attest only once `killpg(pgid, 0)` raises `ProcessLookupError`.
   - **Limitation:** a descendant that calls `setsid` itself, as T1's does, escapes the process group. Covering T1 needs a Linux child subreaper (`PR_SET_CHILD_SUBREAPER`) plus a check that no descendants remain. Otherwise `main()` must not attest, and reconciliation stays `undetermined` for such servers.
   - **Recommendation:** the group kill plus an explicit operator-visible limitation as a first step, or the subreaper check if T1-class servers must be covered. This is a design decision for review.

**B. Silent answer on stop.**

1. Keep a count of reconciliations being answered: increment under `_reconcile_lock` at the pop, decrement after the write.
2. `pump_server`'s `finally` waits (bounded, as it already does for `_link_waiters`) until that count is 0, then sets `_stopped`.

**C. Prefix collision.**

1. Route a response to probe handling only if its id is one the relay issued (it is in `_probe_waiters` or in a set of issued ids). Everything else continues to the normal client path.
2. Optionally add a per-session random nonce to probe ids.

**Then:**

1. Re-run this audit unchanged with `relay_audit.py --reps 5` and the S9 × 20 batch. Acceptance:
   - T1 → `undetermined` 5/5;
   - S9 answered 25/25;
   - S12b answered 5/5;
   - all other rows unchanged.
2. Make the wording and documentation corrections (§9 items 2–3, §1 finding 2) as a separate, reviewed contract change.

## Appendix — evidence and commands

**New private evidence**, under `/root/stateguard-captures/week3-real-relay/` (`EVIDENCE_SHA256.txt` holds the full hashes). Per-trial audit and delivery logs live under `run*/trials/`. They contain statuses, ids and generations; the audit logs' `relay_started` line includes the server command's sandbox path. They contain no file contents.

| File | sha256 (16) |
|---|---|
| `results.json` (200 trials + config validation) | `8fd4132c628acfc4` |
| `results-s9x20.json` | `883d833ef482aa36` |
| `results-s12e.json` | `204862c0a1ca9622` |
| `client_semantics.json` | `01db692090ebb3da` |
| `run.log` / `run-s9.log` / `run-s12e.log` | `b7869cf450a983ce` / `6f84fdd9770a5842` / `1aaa85099012d5f7` |
| `smoke.json`, `smoke2.json`, `smoke3.json` (preliminary; not counted above except where stated) | `51377927c6eed3f7`, `f239efd6a935f8af`, `c29999dcfb868e82` |

The preliminary smoke runs also showed defect B, in 2/8 S9 trials. They are reported separately and are not part of the 25-trial denominator.

**Commands** (repository root; `SERVER=/root/stateguard-captures/domain2-fs/server/node_modules/.bin/mcp-server-filesystem`):

```
.venv/bin/python benchmarks/domains/mcp_server_filesystem/relay_audit.py $SERVER /root/stateguard-captures/week3-real-relay/run /root/stateguard-captures/week3-real-relay/results.json --reps 5
.venv/bin/python benchmarks/domains/mcp_server_filesystem/relay_audit.py $SERVER .../run-extra-s9 .../results-s9x20.json --reps 20 --only S9
.venv/bin/python benchmarks/domains/mcp_server_filesystem/relay_audit.py $SERVER .../run-extra-s12e .../results-s12e.json --reps 3 --only S12e
.venv/bin/python benchmarks/domains/mcp_server_filesystem/relay_client_semantics.py $SERVER /root/stateguard-captures/domain2-fs/server/node_modules/@modelcontextprotocol/sdk .../clients .../client_semantics.json
```

**Gates** (actual results):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest -q --junitxml=…` (full suite) | exit 0, 207.6 s. 5,913 collected: **5,910 passed** (= the implementation report's baseline), **3 xfailed** (the strict-xfail defect tests A, B, C), 0 failed, 0 errors. |
| `pytest tests/examples/test_relay_reconciliation_audit.py -q`, repeated 6 times | `xxx` all 6 times (no XPASS) |
| `pytest … --runxfail` | 3 failed as intended: A `'verified' == 'undetermined'`; B "the client got no answer"; C "never answered" |
| `ruff check src tests examples/mcp/stdio_proxy.py benchmarks/domains/mcp_server_filesystem benchmarks/runtime` | All checks passed |
| `ruff format --check` (same paths) | 210 files already formatted |
| `mypy src` | Success: no issues found in 66 source files |
| `MYPYPATH=benchmarks:benchmarks/domains/mcp_server_filesystem mypy --strict` on the 4 new scripts and the new test file | Success: no issues found in 5 source files |

Repo-wide `ruff check .` and `ruff format --check .` report pre-existing issues in files this work does not touch: `benchmarks/runner.py` and `CORE_HARDENING_PLAN.md`. They are out of scope and were left unchanged.

**Post-run edits.** After the experiment ran, the scripts received lint and typing edits only (`str(...)` and `dict(...)` wrappers, line wraps). A re-run in a scratch workspace (S1, S2 and T1 with raw/naive configs; and client semantics) reproduced the same classifications. The recorded evidence was not re-generated.

## 13. Post-fix re-verification (2026-10-16)

The fixes in §12 were approved, then implemented with one deliberate difference for A. `docs/runtime/WEEK3_AUDIT_FIXES.md` has the details. This audit's runner, shim, test server and client-semantics script were re-run **unchanged** against the fixed code.

**What changed for A.** The approval excluded a subreaper and ruled that process-group termination is not proof. So `main()` does not attest termination at all. It reports `direct_child_reaped`, which is insufficient, and therefore never probes. Probing requires `TerminationEvidence.scope_ended(...)`, a named claim an embedder's restart code makes and the relay logs.

**Acceptance criteria from §12, final run** (248 trials, 233 real-server; evidence under `/root/stateguard-captures/week3-audit-fixes/final/`):

| Criterion | Before | After |
|---|---|---|
| T1 (detached descendant undoes the move) → `undetermined` | 5/5 `verified`, then reverted | **5/5 `undetermined`, 0 probes**; rollback still observed 5/5 |
| S9 (restart fails) answered | 23/25 | **25/25**, exactly one client-side answer each (`relay_answer_count.py`, 5/5) |
| S12b (client id with the probe prefix) answered | 0/5 (15 s timeout) | **5/5** (about 0.03 s) |
| All other rows | — | **Not unchanged — by design.** Every real-server reconcile row that probed before is now `undetermined` with 0 probes, because `main()` cannot establish termination: `verified` 45 → 0, `not_verified` 25 → 0, probes 230 → 0. Shadow (S10), deadline (S8), the colliding id and configuration validation are as before. |

**Invariants across all 248 trials:**
- 0 resends;
- 0 duplicate deliveries of the original;
- exactly one unknown answer per call in every audit log;
- 0 execution or attribution claims;
- 0 probes;
- 0 audit content leaks;
- sentinel unchanged.

**Other checks.**
- Shadow byte-fidelity re-check: 10/10.
- SDK behaviour unchanged (−32000; `data` reachable).
- Full suite: 5,926 passed.

**Final finding.** The three defects are fixed. The implementation fails closed wherever executor termination cannot be established, and the standard entry point is one such place. `verified` remains reachable only through an explicit, logged `scope_ended` claim. Exercised here: the in-process harness, where the serve thread is joined. The relay cannot verify such a claim, so its correctness rests on the embedder. The semantic findings in §1 and §6 still apply wherever probing happens: presence-only predicates, concurrent writers between probes, and a lying probe server. The message-wording recommendation in §9 is still open.

## 14. Contract v2 re-verification (2026-10-16, later the same day)

Sections 1–13 are kept as written: they describe the pre-fix code and the audit-fix code. The reconciliation contract was then revised (`docs/runtime/RECONCILIATION_CONTRACT_V2.md`), and the decision record is `docs/runtime/WEEK3_RECONCILIATION_CONTRACT_V2.md`.

**What changed since §13:**
- **Outcome names.** `verified` and `not_verified` are now `observed_satisfied` and `observed_not_satisfied`. Answers state `durability: not_established` and `operationOutcome: unknown`, the observation window, and each check's strength (presence or content).
- **CLI.** The standard CLI **refuses** `reconcile` blocks at startup (20/20 runs, no server started). It no longer accepts them and answers `undetermined`.
- **Termination evidence.** It must name the executor identity of the generation the call was sent to. Reused, stale or mismatched evidence is refused.
- **Discovery.** One 20 s budget, however many restarts are allowed. A never-listing server: **40.3 s → 20.05 s**.
- **Defect D.** This class was found and fixed: the relay-owned `tools/list` ids and retry ids were also routed by prefix. A byte-exact reconstruction shows it failing on the audit-fix code and passing now.

**Re-run** (same pinned server, same shim and test servers; 426 trials; evidence in `/root/stateguard-captures/week3-contract-v2/`):
- **Invariants:** 0 resends; 0 duplicate deliveries; 0 execution, attribution, durability or success claims; every request answered exactly once (counted at end-of-stream).
- **With a kernel-enforced PID-namespace executor scope** (benchmark embedder only), against the disk at answer:
  - `observed_satisfied` 60 (50 correct, **10 false at answer**: S6d, a concurrent writer between probes; S6e, a lying probe server);
  - `observed_not_satisfied` 55 (all correct);
  - `undetermined` 53.
  - The audit's T1 counterexample is now a true, lasting observation, 5/5.
- **With direct-child evidence** (what the CLI can see): 168 `undetermined`, 0 probes.

**Findings from §1 and §6 that still stand, and are now labelled rather than hidden:**
- presence checks pass on wrong content (S4c, S4d) — content checks catch it (C2, C3: 10/10 `observed_not_satisfied`);
- another actor's work satisfies a predicate;
- state can change between probes;
- a probe server is trusted;
- external effects (X1) are outside any termination evidence.


## 15. CLI executor scope (2026-10-17, Week 4A)

This section is appended; §1–§14 are byte-unchanged (sha256 `8f2a05fa91905376…`, preserved in `/root/stateguard-captures/week4a/baseline/tree/`). The full report is `docs/runtime/WEEK4_CLI_EXECUTOR_SCOPE.md`.

**What changed since §14.**
- The CLI can now reconcile, but only with `--executor-scope pid-namespace`: Linux with `CAP_SYS_ADMIN`, checked before any server starts, never downgraded. Without the flag it still refuses `reconcile` blocks.
- Each server generation is init's only child in its own PID namespace. The relay confirms the namespace empty itself before it reports `scope_ended` for that generation's identity; otherwise it reports `scope_unconfirmed` and does not probe.

**This audit's open items, re-checked through the real CLI** against the pinned filesystem server and the unchanged test servers. 233 trials; code hashes in `week4a/batch/code-under-test.sha256`.

| Case | §14 (embedder) | Now: CLI with scope [measured] |
|---|---|---|
| T1: detached descendant rolls back later | correct only with the PID-namespace embedder | 5/5 `observed_satisfied`, correct; the descendant was killed with the namespace; no late effect |
| T2: detached descendant moves late | the same | 5/5 `observed_not_satisfied`, correct; no late effect |
| T3: survivor with stdout closed | the reference embedder's wrapper held stdio | 5/5 `observed_satisfied`; EOF seen at once (no wrapper holds the pipe) |
| X1: external worker | not covered | 5/5 not covered and not claimed; the worker acted after the answer in 5/5 |
| Negative control (`emb_direct`, direct child only) | `undetermined` | 30/30 `undetermined`; the descendant or worker then acted in 15 |
| Scope cannot be confirmed (U1, helper stopped) | — | 5/5 `scope_unconfirmed` → `undetermined`, nothing probed, 5.1 s |
| Answers | at most once | answering configurations: 208/208 requests exactly once; all configurations: 253/253 at most once |
| Orphan processes | — | 0 in 233 trials |
| Shadow byte fidelity, scope absent / present | 10/10 | client stream identical to no relay: 15/15 / 15/15 |

**New defects found, all fixed with regression tests:**
- a client leaving during a restart left the relay running;
- immediate reuse of the namespace inode caused false `scope_unconfirmed`;
- a working-directory package could hijack the scope helper.

**Metric wording of the Week 3 contract-v2 report:** corrected in its own dated §14. No figure was wrong.

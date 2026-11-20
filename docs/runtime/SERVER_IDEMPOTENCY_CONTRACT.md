# StateGuard server-side idempotency contract: reference implementation and evaluation (2026-10-25)

This document records one engineering run. The run:
- defines and tests the server side of StateGuard's idempotency-key contract;
- implements it for the official Python MCP SDK;
- measures strict RIFR through the real relay against independent ground truth.

The earlier record of this mandate is `AUTONOMOUS_RIFR_ENGINEERING.md`. This run inherits it and does not edit it.

**Bottom line.** RIFR < 1% is **not established**; §8 gives the full statement.

**What was built:**
- the server side of the contract, as store semantics v2 plus a recovery protocol for abandoned attempts;
- a Python MCP SDK adapter, either as one middleware for any `mcp` 2.x server on any transport, or as handler wrappers;
- two reference servers.

All of it is tested with real process crashes.

**Measured, strict RIFR against independent ground truth:**
- **Keyed reference server through the real relay:** 0/1080 faulted calls (95% upper bound 0.35%) and 0/510 fault-free calls, with 0 unsafe outcomes. The same server without recovery fails 36/360, all in the documented two-phase crash window.
- **Over Streamable HTTP with a keyed client:** 0/144.
- **The inherited third-party baseline:** unchanged, at 216/432 faulted, all writes on servers that do not honour keys.

## 1. Files this run changed

These are measured against the baseline frozen at the start of the run: `scratchpad/am2_baseline/tree_hashes.txt` (75 entries) plus `status.txt`.

**New (8):**

| File | What |
|---|---|
| `src/stateguard/adapters/mcp/idempotency_sdk.py` | Python MCP SDK adapter: `KeyedTools` (`middleware()`, `wrap_list`/`wrap_call`), `current_key()` |
| `examples/mcp/keyed_reference_server.py` | low-level `Server` reference: transactional `create_order`, two-phase `append_ledger` with recovery, test-only crash points |
| `examples/mcp/keyed_mcpserver.py` | `MCPServer` adopter: one middleware, stdio or Streamable HTTP, test-only crash points |
| `tests/adapters/mcp/test_idempotency_store.py` | the store contract, with real child-process crashes (50 tests) |
| `tests/adapters/mcp/test_idempotency_sdk.py` | the adapter in process (13) |
| `tests/examples/test_keyed_reference_server.py` | real relay plus reference server, crashes, an unmodified SDK client (28) |
| `tests/examples/test_keyed_mcpserver.py` | `MCPServer` on stdio, through the relay, and over HTTP with crashes (6) |
| `docs/runtime/SERVER_IDEMPOTENCY_CONTRACT.md` | this document |

**Inherited files this run edited (5)**, each verified against a hash-matched reconstruction of its baseline:

| File | Edit |
|---|---|
| `src/stateguard/adapters/mcp/idempotency.py` | Rewritten as contract v2: outcomes, ownership, scope, retention, operator release/resolve, `StoreUnavailable`, transactional mode. Plus, from this run: the exclusive store, `adopt`, `Recovery`, `recover_abandoned` and the `recovered` outcome. |
| `src/stateguard/adapters/mcp/effects.py` | +9 −1: `decide_retry(keyed=True)` resends a keyed call after a tool error, within the attempt budget. |
| `examples/mcp/stdio_proxy.py` | +47 −17: a resend answered `in_doubt`, `in_progress`, `rejected_store` or `rejected_mismatch` leaves the original unknown, and the reason is logged; the docstring now covers tool-error resends, recovery and the SDK adapter. |
| `benchmarks/runtime/harness.py` | +28 −16: the scripted server uses the v2 store (per-process instance), and frees a key it answers without executing. |
| `tests/examples/test_relay_idempotency.py` | One store test updated to v2 ownership semantics; two tests added (keyed resend after a tool error; no unkeyed resend). |

The other 70 inherited files are byte-identical to the baseline, and the `.hypothesis/` DB is unchanged (`8bae78e3…`). No commit, push or merge was made.

## 2. The wire contract

MCP has no idempotency key, so every name below is namespaced in `_meta`.

| Where | Name | Meaning |
|---|---|---|
| tool definition `_meta` | `io.stateguard/idempotency: {"version": 1}` | The server honours keys for this tool. It is per tool, and comes only from the server's own listing; a restarted server is re-listed. Annotations do not count. |
| `tools/call` `params._meta` | `io.stateguard/idempotencyKey` | The key. The relay adds a random UUID unless the client sent its own key, which then wins. A resend keeps the same key. |
| result `_meta`, or error `data` | `io.stateguard/idempotencyOutcome` | What the server did with this keyed request. |

Client-facing outcomes. Each one is distinct on the wire; requirement 7 is that replay, completion, rejection and unknown can be told apart.

| Outcome | Answered as | Executed by this request? | What the client may conclude |
|---|---|---|---|
| `executed` | result | yes | Done, and recorded. |
| `executed_unrecorded` | result | yes | Done, but the record failed: a later resend will be in doubt. |
| `replayed` | result | no | Done earlier; this is the recorded result. |
| `recovered` | result | no | An earlier attempt was cut off. The server's check found *this key's own* effect and recorded the result from that evidence. |
| `rolled_back` | tool error result | no (nothing committed) | A transactional tool failed; the key is free. |
| `not_executed` (-32014) | JSON-RPC error | no | The handler proved it failed before acting; the key is free. |
| `in_progress` (-32011) | JSON-RPC error | no | The same key is executing in this process now. |
| `in_doubt` (-32010) | JSON-RPC error | no | An earlier attempt started and recorded nothing. It may have acted. It is never re-executed automatically. |
| `expired` (-32012) | JSON-RPC error | no | Done earlier; the result is past retention. |
| `rejected_mismatch` (-32602) | JSON-RPC error | no | The same key was sent with another tool or other arguments. |
| `rejected_store` (-32013) | JSON-RPC error | no | The store is unavailable or corrupted. The server fails closed. |

A server that advertises the capability and does not honour it can duplicate effects. The advertisement is the server's promise. The relay cannot verify it and does not try.

## 3. The eight required semantics: mechanism and tests

Code:
- `src/stateguard/adapters/mcp/idempotency.py` (store, executor, wire helpers);
- `src/stateguard/adapters/mcp/idempotency_sdk.py` (Python MCP SDK adapter);
- `examples/mcp/keyed_reference_server.py` (reference server).

| # | Requirement | Mechanism | Tests |
|---|---|---|---|
| 1 | Same key and an equivalent request cause no second side effect. | `begin` claims the key with `BEGIN IMMEDIATE` (insert *started*, or judge the existing row). Equivalent means an equal `fingerprint(tool, arguments)`: sha256 of canonical JSON, so `1` and `1.0` differ. | `test_store_executes_once_replays_and_never_reruns_in_doubt`, `test_a_restarted_server_replays_*`, `test_transactional_writes_recover_from_every_crash_point` |
| 2 | Concurrent duplicates are serialised or deduplicated. | Within one instance, a duplicate waits (`wait_seconds`) and then replays. Across instances, the database write lock gives one winner, and the loser sees `in_doubt`. Transactional calls are serialised by the transaction. A recovery has one taker (`adopt`). | `test_concurrent_duplicates_in_one_process_run_once_and_replay`, `…_across_processes_never_run_twice`, `test_transactional_concurrent_duplicates_are_serialised`, `test_concurrent_duplicates_with_one_key_execute_once` (real server), `test_concurrent_resends_of_an_abandoned_key_check_and_run_once` |
| 3 | A key reused with another tool or other arguments is rejected. | Fingerprint mismatch gives `rejected_mismatch`. This is checked before replay and before recovery. | `test_same_key_other_arguments_or_tool_is_rejected`, `test_a_reused_key_with_other_arguments_is_rejected`, `test_adopt_has_one_taker_and_never_takes_a_live_attempt` |
| 4 | A completed operation returns its recorded result. | *done* rows keep the result JSON, answered `replayed`. | `test_a_restarted_server_replays_its_persisted_result` (content equality), `test_a_lost_answer_on_a_live_server_is_replayed_after_a_deadline` |
| 5 | Possibly executed with an unknown result: stays in doubt, never re-executed. | A *started* row with no record is `in_doubt`, forever, unless (a) an operator `release`s or `resolve`s it, or (b) a recovery check proves the outcome under §4.3. | `test_two_phase_crash_windows`, `test_a_handler_that_fails_otherwise_leaves_the_key_in_doubt`, `test_repeated_unknowns_never_execute_until_the_operator_decides`, `test_a_dead_non_exclusive_owner_is_never_presumed_dead`, `test_a_key_in_doubt_is_unknown_and_never_executed_again` |
| 6 | Scope, expiration, persistence, restart and storage failure are explicit. | See §4.4. | `test_scopes_are_separate`, `test_expiry_tombstones_and_the_reuse_horizon`, `test_retention_must_not_exceed_the_tombstone_horizon`, `test_a_corrupted_store_*`, `test_an_unreachable_store_cannot_be_opened`, `test_a_locked_store_fails_closed`, `test_a_result_that_cannot_be_recorded_leaves_the_key_in_doubt` |
| 7 | Results distinguish replay, completion, rejection and unknown. | §2: an outcome on every keyed answer. Results carry it in `_meta`, and not-executed outcomes are JSON-RPC errors with `data`. | `test_every_not_executed_outcome_round_trips`, `test_executed_outcomes_are_never_errors`, every reference-server test asserts the outcome |
| 8 | Where atomic coupling is impossible, document the window and do not claim exactly-once. | §4. Two-phase mode without recovery is **not** exactly-once. | §4 tables, each row a test |

## 4. Crash windows and guarantees

### 4.1 Two-phase (any side effect), no recovery

The steps are:
1. Commit *started*.
2. Act.
3. Commit *done* with the result.
4. Answer.

The tests crash a real child process (`os._exit(137)`) at each point. Ground truth is the effect file, read directly.

| Crash point | Resend gets | Effects on disk | Exactly once? |
|---|---|---|---|
| before 1 | `executed` | 1 | yes |
| after 1, before acting | `in_doubt` | 0 | **no**: the call never happened, and it is reported unknown |
| mid-effect | `in_doubt` | 0 complete, plus a torn line | **no** |
| after acting, before 3 | `in_doubt` | 1 | **no**: it happened, and it is reported unknown |
| after 3, before 4 | `replayed` | 1 | yes |

This mode never duplicates, but it is not exactly-once.

### 4.2 Transactional (effect in the store's own SQLite database)

The effect and the *done* record commit in one transaction. At every crash point the database holds one effect: a resend executes if nothing committed, and replays if it did. This is exactly-once **for that effect only**; anything the tool does outside the transaction is in the §4.1 position. It is tested in `test_transactional_crash_windows_are_exactly_once`, and end to end through the relay in `test_transactional_writes_recover_from_every_crash_point`.

### 4.3 Two-phase with recovery of abandoned attempts (new in this run)

The §4.1 window closes only when both of the following hold.

1. **The attempt is over.** An attempt is *abandoned* when it can no longer act, in one of three ways:
   - it was marked in doubt after its handler raised or was cancelled;
   - this instance started it and is no longer executing it (its record failed);
   - another instance started it, and that instance held the scope **exclusively** (`IdempotencyStore(..., exclusive=True)`: an `flock` on `<db>.<sha256(scope)[:16]>.lock`, held for the instance's lifetime, with the owner id prefixed `x-`). If this instance now holds that lock, the other one is gone.

   A *started* key of a non-exclusive owner is never abandoned, even if its process is dead, because nothing proves it is dead. A second live exclusive instance cannot be created.
2. **The check is about this key, not a postcondition.** The effect must carry its key, written in the same atomic step as the effect. A partial effect must be undone, or ruled out, before the check runs. The reference server:
   - appends `entry<TAB>key` in one `write`, then `fsync`s;
   - while holding the lock and before serving, cuts a torn last line (one with no newline);
   - treats a torn tail seen at run time as "cannot tell".

   The same entry written under another key is not this key's effect (`test_recovery_needs_the_key_on_the_effect_not_a_lookalike`).

`adopt` takes an abandoned key over atomically. A duplicate sent meanwhile gets `in_progress`, waits, and replays the verdict; a second check never runs. The verdicts are:
- found: recorded *done*, answered `recovered`;
- absent: executed now, once, answered `executed`;
- cannot tell, the check raised, or the check returned something that is not a `Recovery`: `in_doubt`, as in §4.1.

A crash during recovery leaves the key *started* by the recovering exclusive instance. It is then abandoned again and recovered again.

| Crash point | Resend gets (store test / real server through the relay) | Lines on disk |
|---|---|---|
| before 1 | `executed` | exactly 1, nothing torn |
| after 1, before acting | `executed` (absent) | exactly 1 |
| mid-effect | `executed` (torn line cut, then absent) | exactly 1 |
| after acting, before 3 | `recovered` (the key's own line found) | exactly 1 |
| after 3, before 4 | `replayed` | exactly 1 |

These are tested in `test_two_phase_crash_windows_close_with_recovery`, `test_two_phase_writes_with_recovery_succeed_exactly_once`, `test_a_crash_during_recovery_leaves_the_key_abandoned_again` and `test_recovery_never_runs_while_the_attempt_may_still_act`.

What recovery does **not** claim:
- The library cannot verify a server's check. Like the advertisement, a wrong check is the server's bug, and it can duplicate or falsely report success.
- An effect in a system that cannot carry the key (a third-party API with no idempotency of its own, an e-mail) has no sound check. It stays in §4.1.
- Multi-process deployments sharing one store non-exclusively get no recovery for each other's attempts.
- `fsync` bounds what survives power loss. A line the OS lost before `fsync` is "absent", and re-executing it is correct, because the effect never became durable.

### 4.4 Scope, expiration, persistence, restart, storage failure

- **Scope.** A key is unique per `scope` (a store parameter, e.g. server plus tenant). The same key in another scope is a different key.
- **Retention.** *done* results are kept for `retention_seconds` (default 7 days); a resend after that is `expired`, and is never re-executed. `purge()` turns them into tombstones (key plus fingerprint) for `tombstone_seconds` (default 30 days, and at least the retention).
  - **Hazard (documented and tested):** after the tombstone is gone, a reused key executes as new. Keys must never be reused across that horizon; the relay's keys are random UUIDs.
- ***started* / *in_doubt* rows never expire.** They leave only by operator `release` (it did not run), operator `resolve` (it ran, with this result), or a recovery verdict.
- **Persistence.** SQLite in WAL mode with `synchronous=FULL`. A committed record survives a process crash and a power loss. A restarted server replays from the file.
- **Storage failure.** Any store error (unavailable, locked past the timeout, corrupted) raises `StoreUnavailable`:
  - before execution, the request is `rejected_store` and executes nothing;
  - after execution, it is `executed_unrecorded`, and the key is left *started*, so a resend is in doubt or recovered, never re-executed blindly;
  - for a transactional call, a store failure is `rejected_store`, because the effect was in the same uncommitted transaction.

## 5. What the relay does with it

With `--retry auto`, and unless `--no-idempotency-keys` is given:
- A key is added only to tools whose current listing advertises the capability, and not to declared or trusted reads.
- After an unknown outcome (a deadline, or a lost connection followed by a restart), the same request with the same key is resent once, and only if the current server still advertises.
- **New in this run:** a keyed call answered with a tool error (`isError`) is also resent, within the retry budget. If the server recorded that error, it answers the same error again from its record; it executes only a key it never saw (for example, a gateway's "temporarily unavailable"). The rule is `effects.decide_retry(keyed=True)`, tested in `test_a_keyed_call_is_resent_once_after_a_tool_error` and `test_an_unkeyed_mutation_is_not_resent_after_a_tool_error`.
- If a resend is answered `in_doubt` or `in_progress` after a deadline, the relay waits one more deadline for the original's late answer, then tells the client "outcome unknown". The original's late answer wins if it arrives.
- Each request gets exactly one answer.
- A server that does not advertise gets no keys and no mutating resends (`test_a_server_that_does_not_advertise_gets_no_keys_and_no_resend`).
- An unmodified official SDK client works unchanged, with or without its own keys, directly or through the relay (`test_an_unmodified_sdk_client_works_with_and_without_keys`).

The scripted test server (`benchmarks/runtime/harness.py`) now frees a key when it answers without executing (a transient error, an auth error, an unknown tool, rejected arguments), as a real server's `NotExecuted` path does. Before this, such a key stayed *started*, so the resend that the new tool-error rule sends would have been in doubt.

## 5a. Adoption: the Python MCP SDK adapter (new in this run)

`stateguard.adapters.mcp.idempotency_sdk.KeyedTools` takes the configuration once: `two_phase(*names, recover=check)` and `transactional(name, effect)`. It then attaches in either of two ways.

- **`keyed.middleware()`** is an `mcp` 2.x `ServerMiddleware`. Passing it in `MCPServer(..., middleware=[...])` (the former FastMCP), or appending it to a low-level `Server.middleware`, is the whole adoption.
  - It sits below the transport, so it behaves the same on stdio and on Streamable HTTP.
  - It advertises keyed tools in `tools/list`, in model or dict form.
  - It handles only `tools/call` requests that carry a key for a keyed tool. Everything else passes through untouched: notifications, other methods, unkeyed calls, unkeyed tools.
  - It validates a keyed call's params before claiming the key, so the SDK's own rejection of malformed params never claims one.
- **`wrap_list` / `wrap_call`** wrap a low-level `Server`'s handlers. This is the form `examples/mcp/keyed_reference_server.py` uses.
- **`current_key()`** gives the running keyed call's key, through a context variable. A handler that writes it into its own effect makes a recovery check exact.

What a handler's answer means for its key:
- A `CallToolResult`, success or `isError`, is recorded and replayed. `MCPServer` turns a tool's exceptions into `isError` results, so under `MCPServer` even a `NotExecuted` is a recorded error. A resend gets the same error and never re-runs.
- `NotExecuted`, when it reaches the adapter (low-level `Server`), frees the key.
- **`InputRequiredResult`** (the 2026-07-28 multi-round flow) passes through unrecorded and frees the key. Recording it would make the client's retry with its inputs replay the same request for input forever. This also applies to `wrap_call`. A keyed tool must not act before it has its input.
- Any other exception, an `MCPError`, an answer that is not a result, or a cancellation leaves the key **in doubt**.

`examples/mcp/keyed_mcpserver.py` is a complete `MCPServer` adopter: one middleware, a key-carrying effect, a recovery check, and an exclusive store. It is tested in `tests/examples/test_keyed_mcpserver.py`:
- stdio;
- the relay;
- Streamable HTTP with real crashes and an unmodified SDK client resending with the same key: every window ends in exactly one effect;
- a negative control: a blind resend without a key duplicates.

The in-process routing and answer semantics are tested in `tests/adapters/mcp/test_idempotency_sdk.py`.

## 6. Required fault-injection tests

| Required test | Where |
|---|---|
| crash before execution | `test_two_phase_crash_windows[before_begin]`, `…_close_with_recovery[before_begin]`, `test_transactional_crash_windows_are_exactly_once[before_begin]`, reference server `[before_begin]` (relay end to end) |
| crash during execution | `[after_begin]` and `[mid_effect]` in the same four tests, plus `test_over_http_every_crash_window_ends_in_exactly_one_effect[after_begin]` (MCPServer, Streamable HTTP) |
| crash after the effect, before recording | `[after_effect]` in the same four tests, plus `test_over_http_…[after_effect]`, `test_through_the_relay_a_crash_after_the_effect_is_recovered` and `test_over_http_a_resend_without_a_key_duplicates` (negative control) |
| crash after recording, before the response | `[after_record]` in the same four tests, plus `hang_after_record` (`test_a_lost_answer_on_a_live_server_is_replayed_after_a_deadline`) and `test_over_http_…[after_record]` |
| concurrent duplicates with one key | `test_concurrent_duplicates_*` (store, cross-instance, transactional, real server), `test_concurrent_resends_of_an_abandoned_key_check_and_run_once`, `test_two_keyed_mutations_in_flight_at_loss_each_execute_once` |
| same key, different arguments | `test_same_key_other_arguments_or_tool_is_rejected`, `test_a_reused_key_with_other_arguments_is_rejected`, `adopt` mismatch |
| restart with a persisted result | `test_a_restarted_server_replays_a_persisted_result`, `test_a_restarted_server_replays_its_persisted_result`, `test_a_call_lost_with_its_server_is_replayed_by_the_restarted_one` |
| lost response, then retry | `test_a_lost_answer_is_replayed_from_the_record_after_a_deadline`, `test_a_lost_answer_on_a_live_server_is_replayed_after_a_deadline` |
| storage unavailable or corrupted | `test_a_corrupted_store_fails_closed`, `test_an_unreachable_store_cannot_be_opened`, `test_a_locked_store_fails_closed`, `test_a_result_that_cannot_be_recorded_leaves_the_key_in_doubt`, `test_a_corrupted_store_executes_nothing` (real server), `test_an_exclusive_store_that_fails_to_open_holds_no_lock` |
| key expiration and reuse | `test_expiry_tombstones_and_the_reuse_horizon` |
| repeated UNKNOWN outcomes | `test_a_handler_that_fails_otherwise_leaves_the_key_in_doubt` (3 more attempts), `test_repeated_unknowns_never_execute_until_the_operator_decides`, `test_recovery_verdicts[unknown…]` (re-checked, never run) |
| mixed read and mutating operations | `test_a_read_and_a_keyed_write_lost_together_both_recover`, `test_keys_go_only_to_advertised_non_read_tools` |
| existing clients and non-advertising servers | `test_an_unmodified_sdk_client_works_with_and_without_keys` (both paths), `test_one_middleware_adopts_the_contract` (an unkeyed call is unchanged), `test_unkeyed_calls_other_tools_other_methods_and_notifications_pass_through`, `test_a_server_that_does_not_advertise_gets_no_keys_and_no_resend`, `test_a_restarted_server_that_stops_advertising_gets_no_resend`, `test_an_unadvertised_mutation_is_never_resent`, and the third-party re-run (§7.3: 0 of 1512 outcomes changed) |
| answers that are not completions | `test_input_required_passes_through_unrecorded_and_frees_the_key`, `test_what_a_raised_handler_means_for_its_key`, `test_an_answer_that_is_not_a_result_leaves_the_key_in_doubt`, `test_malformed_params_claim_no_key` |

## 7. Strict-RIFR evaluation through the real relay

### 7.1 Setup

- **Processes.**
  - A client speaks raw JSON-RPC over stdio.
  - It talks to the **real relay** (`examples/mcp/stdio_proxy.py`, as a process).
  - The relay talks to the inherited TEST-ONLY fault shim (`real3/shim.py`, unchanged).
  - The shim wraps the **reference server** (SDK `mcp` 2.3.0, as a process).
  - Relay and server both come from a frozen snapshot: iter3 before recovery, iter4 after. Each snapshot is hash-listed in `snapshots/iterN.sha256`.
- **Faults.** One injected fault per faulted call, on the first call of the target tool.
  - *Transport* faults (from the shim): lost answer and exit, kill before forwarding, hang after executing, hang before forwarding, garbled answer, transient `isError` without forwarding.
  - *Crash* faults (the server's own `os._exit`, or a hang): before claiming the key, after claiming, mid-effect, after the effect, after recording, and hang after recording. Crash faults apply to the two mutating tools only.
- **Ops.**
  - `read` (`read_ledger`, annotated `readOnlyHint`);
  - `create_order` (transactional);
  - `append_ledger` (two-phase).
- **Cells.** 33 per arm: 30 faulted and 3 fault-free (`none`). The main batches use 12 trials per cell.
- **Ground truth**, read after the session with `sqlite3` and `open()`, independently of StateGuard:
  - order rows with the trial's unique item;
  - complete ledger lines whose entry is the trial's unique entry;
  - torn lines.
- **Strict success.**
  - A read must return one non-error answer containing the seeded line.
  - A mutation must leave the effect exactly once with no torn line, **and** the client must be told it succeeded, with exactly one answer.
  - UNKNOWN, `in_doubt`, no answer, errors and duplicates are all failures, and no faulted call is excluded.
- **Arms.**

| Arm | What it is |
|---|---|
| `raw` | relay `--passthrough` (StateGuard off) |
| `naive` | raw, plus a client that blindly resends once (new connection if needed), **without** a key |
| `naive_keyed` | raw, plus a contract-aware client that resends once **with its own key**: the server contract alone, no relay logic |
| `sg_reread` | the best configuration from the previous run, keys off: `--trust-read-only-hints --retry auto --restart 2 --call-deadline 1.0 --deadline-scope recoverable --no-idempotency-keys` |
| `sg_keyed` | the same with keys on (the default with `--retry auto`) |
| `sg_keyed_norecover` | (iter2 only) as `sg_keyed`, against the same snapshot's server run with `--no-recover`: the same-snapshot control for recovery |

- **Artifacts.** These live in `/root/stateguard-captures/autonomous-mandate/realkeyed/`:
  - `driver.py` (iter1), `driver2.py` (iter2), `summarize.py`, `PLAN_iter2.md` (written before any iter2 row existed);
  - `rows_iter*.jsonl` (every row), `summary_iter*.{txt,json}`;
  - `failing_runs_iter*/` (the full run directory of **every** failing run: audit log, shim log, database, ledger).

### 7.2 Results: faulted and fault-free, reported separately

Strict RIFR is failures over all calls. Intervals are Wilson 95%. Cells are 12 trials each. A dash means the measure does not apply to that arm (raw is the reference for the paired counts).

**Iteration 1** (snapshot iter3, no recovery; `rows_iter1.jsonl`, 1980 rows, 0 driver errors)

| Arm | Fault-free | Faulted | Transport faults | Crash faults | Unsafe (dup / false success / false failure) | Recovered vs raw | Induced vs raw (Z) |
|---|---|---|---|---|---|---|---|
| raw | 0/36 (≤ 9.6%) | 360/360 (100%) | 216/216 | 144/144 | 0 / 0 / 0 | – | – |
| naive | 0/36 | 168/360 (46.7%, 41.6–51.8) | 72/216 | 96/144 | **156** / **12** / 0 | 192 | 0 |
| naive_keyed | 0/36 | 36/360 (10.0%, 7.3–13.5) | 0/216 | 36/144 | 0 / 0 / 0 | 324 | 0 |
| sg_reread | 0/36 | 288/360 (80.0%, 75.6–83.8) | 144/216 | 144/144 | 0 / 0 / 0 | 72 | 0 |
| sg_keyed | 0/36 | 36/360 (10.0%, 7.3–13.5) | 0/216 | 36/144 | 0 / 0 / 0 | 324 | 0 |

All 36 sg_keyed failures are the three §4.1 cells, `append_ledger` × {after_begin, mid_effect, after_effect}. In each case the client was told "outcome unknown", which is honest. 12 of them left a torn line behind; in every arm the torn lines are the mid_effect crash's.

The naive arm's 12 false successes are its blind resend after a torn half line. The server appended the new line onto the half line (`entry-0-eentry-0-e751b95873`) and answered "appended", but no correct entry exists.

**Iteration 2** (snapshot iter4, with recovery; `rows_iter2.jsonl`, 2376 rows, 0 driver errors)

| Arm | Fault-free | Faulted | Transport faults | Crash faults | Unsafe (dup / false success / false failure) | Recovered vs raw | Induced vs raw (Z) |
|---|---|---|---|---|---|---|---|
| raw | 0/36 | 360/360 (100%) | 216/216 | 144/144 | 0 / 0 / 0 | – | – |
| naive | 0/36 | 156/360 (43.3%, 38.3–48.5) | 72/216 | 84/144 | **156** / 0 / 0 | 204 | 0 |
| naive_keyed | 0/36 | **0/360 (0.0%, 0.0–1.1)** | 0/216 | 0/144 | 0 / 0 / 0 | 360 | 0 |
| sg_reread | 0/36 | 288/360 (80.0%, 75.6–83.8) | 144/216 | 144/144 | 0 / 0 / 0 | 72 | 0 |
| sg_keyed | 0/36 | **0/360 (0.0%, 0.0–1.1)** | 0/216 | 0/144 | 0 / 0 / 0 | 360 | 0 |
| sg_keyed_norecover | 0/36 | 36/360 (10.0%, 7.3–13.5) | 0/216 | 36/144 | 0 / 0 / 0 | 324 | 0 |

The `sg_keyed` server outcomes, cell by cell, are exactly the mechanisms predicted in §4:
- `recovered` for all 12 `append_ledger/after_effect` trials;
- `executed`, once, for after_begin, mid_effect, before_begin, kill_before, hang_before and transient;
- `replayed` for after_record, lost_exit, hang, hang_after_record and garbage.

The same-snapshot control `sg_keyed_norecover` reproduces iteration 1's 36/360 exactly, so the difference is the recovery check and not a change elsewhere. The naive arm's mid_effect cell now succeeds: the recovering server cuts the torn line at startup before the blind resend appends. Its 156 duplicate effects remain; these are the lost-answer, hang and garble cells, where the first attempt executed.

**Fault-free control batch** (`rows_iter2_control.jsonl`, 2412 rows: 3 fault-free cells × 134 trials × 6 arms, planned in `PLAN_iter2.md`)

Every arm scored **0/402 (0.0%, 95% upper bound 0.95%)**, with 0 unsafe outcomes and 0 StateGuard-induced failures. The mean latency was 1.17–1.18 s per session in every arm, including process start.

**Extension batch** (`rows_iter2_ext.jsonl`, 792 rows, sg_keyed, 24 more trials per cell, planned in `PLAN_iter2.md`)

As recorded in the plan's addendum before the batch ran, its cell-list helper failed, so it ran all 33 cells instead of the 30 faulted ones. The result was faulted **0/720** (95% upper bound 0.53%) and fault-free 0/72. There were 0 unsafe outcomes and 0 torn lines.

**sg_keyed against this server, every iter2 batch combined** (main + control + extension; nothing excluded)

| | Calls | Failures | Strict RIFR | 95% upper bound |
|---|---|---|---|---|
| faulted (one injected fault per call) | 1080 | 0 | 0.0% | 0.35% |
| fault-free | 510 | 0 | 0.0% | 0.75% |
| all | 1590 | 0 | 0.0% | 0.24% |
| duplicate effects / false success / false failure / torn lines | | 0 / 0 / 0 / 0 | | |

These bounds treat trials as independent, but the injected faults are deterministic. They therefore bound **this workload** (one reference server, three tools, these 30 fault types, one fault per call), not traffic in general.

### 7.3 The inherited third-party baseline, re-run on this code

This is `real3_on_iter4/`: the inherited `real3/driver.py`, shim and scorer, **unchanged**, run against snapshot iter4.
- The servers are three pinned, unmodified third-party ones: `@modelcontextprotocol/server-filesystem` 2026.8.31, `server-memory` 2026.8.31 and `mcp-server-sqlite` 2025.4.25.
- The arms are the previous run's iter2 arms; 1512 rows.

| Arm | Faulted | Fault-free | Unsafe | Z |
|---|---|---|---|---|
| raw | 432/432 (100%) | 0/72 | 0 | – |
| sg_hints | 288/432 (66.7%, 62.1–71.0) | 0/72 | 0 | 0 |
| sg_scoped | 216/432 (50.0%, 45.3–54.7) | 0/72 | 0 | 0 |

**Per (server, op, mode, arm, trial), 0 of 1512 outcomes differ from the inherited `real3/rows_iter2_*.jsonl`.** None of these servers advertises the capability, so the keyed paths added in this run (keys, keyed resends after tool errors, recovery) are inert there.

The whole remaining failure set, 216/432 faulted calls for the best arm, is **writes**: `move_file`, `add_observations` and `write_query` under faults. Every one is reported "outcome unknown", or left unanswered by a hang, and none is a duplicate. The reads are 0/216.

### 7.4 Over Streamable HTTP, without a relay (`rows_http.jsonl`, planned in `PLAN_http.md`)

- **Setup.**
  - Server: `examples/mcp/keyed_mcpserver.py` from snapshot iter5 (`MCPServer` plus one middleware), over Streamable HTTP.
  - Client: an unmodified SDK client (`streamable_http_client` plus `ClientSession`).
  - Supervisor: the driver, which restarts a dead server.
  - Faults: the server's own `os._exit(137)` at three points in `add_note`, or none.
  - 48 trials per cell; 576 rows; 0 driver errors.
  - Ground truth: the notes file.
- **Arms**, by what the client does when its call fails:
  - `raw`: nothing;
  - `naive`: reconnect and resend without a key;
  - `keyed`: send with its own key, then reconnect and resend with the same key.

| Arm | Fault-free | Faulted (crash) | Duplicate effects | False success |
|---|---|---|---|---|
| raw | 0/48 | 144/144 (100%) | 0 | 0 |
| naive | 0/48 | 96/144 (66.7%, 58.6–73.8) | **96** | 0 |
| keyed | 0/48 | **0/144 (0.0%, 0.0–2.6)** | 0 | 0 |

Per cell:
- **keyed:** `executed` (after_begin), `recovered` (after_effect), `replayed` (after_record), 48/48 each.
- **naive:** succeeds only where the crash came before the effect (after_begin), and duplicates in every after_effect and after_record trial.
- **raw:** fails all 144 faulted calls, because every crash kills its call.

The fault fired in every faulted row. The client SDK printed `Session termination failed` 432 times (`rows_http.err`). This is its own warning when the server it was talking to died, and it does not affect any row.

**What this does and does not cover.** It is the server contract on HTTP, end to end. It does **not** cover HTTP faults that leave the process alive (a dropped connection with the server still running, a proxy timeout, the SDK's own retries). Those need either the client to resend with its key, which is what `keyed` does and which works the same way, or an HTTP relay. StateGuard has no HTTP relay.

### 7.5 Synthetic corpus, iteration 4 (`synthetic/rifr_iter4.{txt,json}`, same seed and corpus as iteration 3)

`benchmarks/runtime/rifr.py --seed 2 --per-fault 16 --control 400` on the working tree after this run. The run used a scripted in-process server; these are not production rates.

| Arm | Iteration 3 (inherited) | Iteration 4 | Change |
|---|---|---|---|
| raw, naive, shadow, auto, auto_ops, ops_reread, ops_scoped | 213, 181, 213, 177, 165, 130, 119 /267 | **identical** | none |
| ops_keyed | 91/267 | **80/267** | category F (timeout/transient) 22/48 → **11/48** |

- **Why F moved.** A keyed call answered with a transient tool error is now resent with its key (§5). The scripted server no longer leaves such a key claimed.
- **What stayed the same.**
  - Control is 0/400 in every arm (≤ 0.95%), and `ops_keyed` induced 0 failures.
  - The `ops_keyed` unsafe counts are as inherited: wrong_effect 1, false_success 1, false_failure 9, invalid_result_reported_success 16. Every StateGuard arm has had these since iteration 3, and they come from categories A, G and H, not from keys.
- **What remains for `ops_keyed`**, out of 80:

| Category | Residual | Why it stays |
|---|---|---|
| E, transport | 27/48 | unkeyed mutations lost with the connection |
| H, result integrity | 16/32 | invalid results detected but not repairable |
| K, auth | 16/16 | an expired token needs the client to re-authenticate |
| F, timeout/transient | 11/48 | unkeyed mutations |
| G, tool execution failure | 9/16 | |
| A, contract | 1/37 | |

## 8. Is RIFR < 1% established?

**No.** What the evidence does establish:

1. **For a server that adopts the contract**, the reference SDK server through the real relay with recovery on, strict RIFR is **0/1080 faulted** (95% upper bound **0.35%**) and **0/510 fault-free** (≤ 0.75%), with zero duplicates, false successes or torn effects. Over HTTP with a keyed client it is 0/144 faulted (≤ 2.6%). On this workload the measured rate and its upper bound are both below 1%.
2. **That is one server with three tools and 30 injected fault types, one fault per call.** It is not traffic. Production fault incidence and category shares are unmeasured, so no traffic-weighted RIFR can be computed from this evidence. The synthetic mixture figures are computed with **assumed** weights (`ops_keyed` 0.76% at an assumed 2% fault rate, upper bound 1.97%), and an assumption is not evidence.
3. **For unmodified third-party servers, the residual is unchanged by this run**: 216/432 faulted calls for the best arm, all of them writes. Those servers do not honour keys, so nothing in this run applies to them, and nothing in this run pretends otherwise.
4. **Two-phase recovery is exactly-once only under its two stated conditions**: an exclusive store, and an effect that carries its key. Without them, the crash window of §4.1 remains, and it is reported as unknown, never retried.


Each workload is measured on its own terms; the shares below are shares of that workload's own residual. No traffic mix is assumed.

| Workload | Best safe configuration | Residual | What it is |
|---|---|---|---|
| Keyed reference server, real relay (§7.2) | `sg_keyed` + recovery | 0/1080 faulted, 0/510 fault-free | Nothing measured remains. The open categories are what this workload does not inject: concurrent faults, faults during recovery under load, a lying server. |
| Keyed `MCPServer` over HTTP, no relay (§7.4) | keyed client | 0/144 faulted, 0/48 fault-free | Nothing measured remains. The workload does not inject HTTP faults that leave the process alive. |
| Three unmodified third-party servers, real relay (§7.3) | `sg_scoped` | 216/432 faulted (50.0%), 0/72 fault-free | **100%: writes on servers that do not honour keys.** Each is reported "outcome unknown" or left unanswered. None is fixable from the relay without duplicating effects. |
| Synthetic scripted corpus (§7.5) | `ops_keyed` | see §7.5 | see §7.5 |

**The next task, ranked by measured contribution.** The only residual measured on real servers is the third category: mutations on servers that do not adopt the contract.
- The relay cannot remove it safely, and nothing in this run tries to.
- What removes it is adoption. This run made adoption cheap for Python: one middleware for `MCPServer` or `Server`, on any transport.
- The pinned filesystem and memory servers are TypeScript. A TypeScript SDK equivalent (the same wire contract, with `node:sqlite`, available unflagged in the local Node 22.22, as the store) is therefore the next highest-leverage task, followed by measuring an adopting fork of one of those servers. That fork would have to be labelled as a modified server; it would not count toward the unmodified-server baseline.

**Measured as having no contribution here:**
- discovery freshness (synthetic category B: 0 residual);
- protocol conformance (C: 0);
- stdio transport faults on keyed tools (§7.2: 0/432 transport-faulted calls in the extension, 0/216 in main).

**Still unmeasured:**
- HTTP faults that leave the process alive, and any HTTP relay;
- production fault incidence and category shares;
- multi-instance deployments sharing a store, where recovery is deliberately disabled for each other's keys;
- servers whose recovery check is wrong.

## 10. Reproduce

```
cd /home/user/stateguard
export HYPOTHESIS_STORAGE_DIRECTORY=/tmp/hyp   # keeps the preserved .hypothesis DB untouched
.venv/bin/python -m pytest -p no:cacheprovider tests/adapters/mcp/test_idempotency_store.py \
  tests/adapters/mcp/test_idempotency_sdk.py tests/examples/test_keyed_reference_server.py \
  tests/examples/test_keyed_mcpserver.py tests/examples/test_relay_idempotency.py
.venv/bin/python -m pytest -p no:cacheprovider          # full suite
.venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/ && .venv/bin/mypy src/

cd /root/stateguard-captures/autonomous-mandate/realkeyed
SNAPSHOT=../snapshots/iter3 /home/user/stateguard/.venv/bin/python driver.py 12 > rows.jsonl          # iteration 1
SNAPSHOT=../snapshots/iter4 /home/user/stateguard/.venv/bin/python driver2.py 12 > rows.jsonl         # iteration 2 main
SNAPSHOT=../snapshots/iter4 TRIAL_OFFSET=1000 /home/user/stateguard/.venv/bin/python driver2.py 134 \
  read/none,create_order/none,append_ledger/none > control.jsonl                                       # fault-free
SNAPSHOT=../snapshots/iter5 /home/user/stateguard/.venv/bin/python http_driver.py 48 > http.jsonl     # HTTP
python3 summarize.py rows.jsonl
cd .. && SNAPSHOT=$PWD/snapshots/iter4 /home/user/stateguard/.venv/bin/python real3/driver.py fs 12 "" raw,sg_hints,sg_scoped
```

## 11. Gates on the final tree

| Gate | Result |
|---|---|
| Full suite (`pytest -p no:cacheprovider`, Hypothesis DB isolated) | **6237 passed**, rc 0. The previous run ended at 6138. The suite was 6218 after the recovery work, and 19 tests (13 adapter, 6 `MCPServer` end-to-end) came with the middleware. |
| `ruff check` / `ruff format --check` (src, tests, the changed examples, harness) | pass |
| `mypy --strict src/` (69 files); mypy on the relay, both reference servers and the harness | pass |
| Inherited state | 70 of 75 inherited files byte-identical to the baseline; the 5 in §1 were edited on purpose. `.hypothesis/` is `8bae78e3…`, unchanged. Branch `claude/serene-bohr-o28snr` and HEAD `b6f7ba0` are unchanged; nothing was committed. |
| Inherited artifacts | All 174 entries of `autonomous-mandate/MANIFEST.sha256` verify. Its 175th entry is the manifest's own hash, which no file can match. This run's artifacts are listed in `MANIFEST_server_idempotency.sha256` (11911 files). |

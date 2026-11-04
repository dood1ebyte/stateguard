# StateGuard idempotency contract: TypeScript server adapter (2026-10-26)

This document records one engineering run. It begins with the plan and compatibility matrix, which were written **before** any code changed. Implementation, tests and measurements are appended as they happen. The contract itself is `SERVER_IDEMPOTENCY_CONTRACT.md` (Python reference implementation: `src/stateguard/adapters/mcp/idempotency.py`, `idempotency_sdk.py`). This adapter implements the same contract and invents no new protocol.

**Not claimed:** sub-1% RIFR for MCP traffic in general.

## 1. What was inspected (Phase 1)

**Pinned versions.** These are the exact versions the real-server experiments use; none are modified on disk.

| Component | Version | Location |
|---|---|---|
| Node | 22.22.0 (npm 10.9.4) | `/opt/node22` |
| `@modelcontextprotocol/sdk` (TypeScript) | **1.32.1** (both servers carry an identical copy) | each server's `node_modules` |
| `@modelcontextprotocol/server-filesystem` | **2026.8.31** | `/root/stateguard-captures/domain2-fs/server` |
| `@modelcontextprotocol/server-memory` | **2026.8.31** | `/root/stateguard-captures/domain2-candidates/mem` |
| TypeScript (type checking only) | 6.0.2, from the sandbox image's `/opt/node-tools` | |

**The SDK boundary, verified in `dist/esm` of SDK 1.32.1:**
- **No middleware layer.** `Protocol.setRequestHandler(schema, handler)` is public; it derives the method from the schema's literal. `assertCanSetRequestHandler(method)` and `removeRequestHandler(method)` are public too.
- **Lazy registration.** `McpServer.registerTool` installs the `tools/list` and `tools/call` handlers lazily, through `this.server.setRequestHandler(...)`, the first time a tool is registered. Both pinned servers use `McpServer.registerTool` and nothing else for tools.
- **Errors become results.** `McpServer`'s `tools/call` handler turns *every* tool exception, including argument-validation failures and unknown tools, into an `isError` result. The one exception is an `McpError` with `ErrorCode.UrlElicitationRequired` (-32042), which it re-throws: the client must elicit and retry. That is this SDK's form of "input required".
- **Task augmentation.** `params.task` returns a `CreateTaskResult` immediately.
- **Custom `_meta` keys survive.** Request `params._meta` is a zod `looseObject`, so `io.stateguard/idempotencyKey` reaches the handler. Result `_meta` is a `looseObject` too.
- **Error serialization.** A thrown error with an integer `code` (and `data`) becomes a JSON-RPC error carrying them (`protocol.js`). On cancellation no response is sent, but the handler keeps running.

**The two pinned mutating tools:**
- `move_file` (filesystem) is `fs.rename`: not idempotent. A blind resend fails because the source is gone, which is a false failure.
- `add_observations` (memory) rewrites the JSONL file. It skips contents already present, so a blind resend adds nothing and answers `addedObservations: []`. It is idempotent by content, not by key.

Neither effect carries a key, so neither can support a sound recovery check (contract condition 2).

## 2. Compatibility matrix

| Contract element | Python reference | TypeScript adapter | Portable? |
|---|---|---|---|
| Wire names (`io.stateguard/idempotency`, `…/idempotencyKey`, `…/idempotencyOutcome`), version 1 | yes | identical | yes |
| Outcomes (10) and JSON-RPC codes and messages | yes | identical strings and codes | yes |
| Key scope (`scope` column) | yes | same table, same columns | yes; the store file is shared |
| Fingerprint: sha256 of canonical JSON `[tool, arguments]` | `json.dumps(sort_keys=True, separators=(",",":"))`, `ensure_ascii` | reimplemented: code-point key order, non-ASCII `\uXXXX`, Python float `repr` rules | **partly** (§3.1) |
| Mismatch rejection | fingerprint ≠ → `rejected_mismatch` | same | yes |
| Two-phase begin / complete / abort / mark in doubt | SQLite `BEGIN IMMEDIATE`, WAL, `synchronous=FULL` | `node:sqlite` (`DatabaseSync`), same statements and pragmas | yes |
| Transactional mode (effect in the store's transaction) | sync `effect(db)` | sync `effect(db)`; a Promise-returning effect is rolled back and refused | yes, synchronous only |
| Ownership: same instance means `in_progress`, another means `in_doubt` | per-instance owner id plus an in-memory active set | same; the active set is a `Map` of in-flight promises | yes |
| Waiting for an in-flight duplicate | `threading.Condition`, `wait_seconds` | awaits the in-flight promise, `waitSeconds` | yes (single process) |
| Exclusive store (recovery condition 1) | `fcntl.flock` on `<db>.<sha16(scope)>.lock` | Node has no `flock` | **no, so the protocol changes** (§3.2) |
| Retention, tombstones, purge, release, resolve | yes | same SQL | yes |
| `StoreUnavailable` fails closed | `sqlite3.Error` / `OSError` | node:sqlite errors and fs errors | yes |
| `current_key()` | `contextvars` | `AsyncLocalStorage` | yes |
| `InputRequiredResult` frees the key | yes (mcp 2.x) | SDK 1.32.1 has no such result; its equivalent, a thrown `UrlElicitationRequired`, frees the key. A `resultType: "input_required"` result is also passed through. | yes, mapped |
| Handler exception or cancellation → in doubt | yes | an exception → in doubt; cancellation sends no answer, but the handler finishes and its result is recorded | stronger in TS |
| Middleware attachment | `ServerMiddleware` (mcp 2.x) | none in SDK 1.32.1, so `installKeyedTools(server)` wraps the instance's public `setRequestHandler`. A preload patches the prototype for servers that cannot be edited. | different mechanism, same semantics |
| Blocking | store calls run in worker threads | `DatabaseSync` is synchronous: a store call blocks the event loop for its duration, up to the busy timeout under cross-process contention | documented limitation |

## 3. Design decisions

### 3.1 Fingerprints across languages

The TypeScript fingerprint reproduces Python's canonical JSON for:
- strings, escaped as ASCII-only JSON;
- object keys, sorted by code point;
- booleans and null;
- integers within ±2^53;
- non-integral floats, formatted with Python's `repr` rules: fixed notation for 1e-4 ≤ |x| < 1e16, otherwise `e±XX`.

It cannot reproduce two things, because JavaScript's `JSON.parse` discards the distinction:
- an integral value written with a decimal point or an exponent (`1.0`, `1e16`). Python keeps it as a float, so `1.0` and `1` are *different* requests in Python and the *same* request in TypeScript;
- integers beyond ±2^53.

The divergence matters only when two languages share one store **and** one scope. It is tested and documented (`fingerprint_corpus`), not hidden.

### 3.2 The exclusive lock (a protocol change, in both languages)

`flock` is unavailable in Node. A PID file or a lease cannot prove its holder is dead: PIDs are reused, and a paused process outlives its lease. So both languages switch to a lock that the kernel releases at process death and that both can take:
- an **SQLite `EXCLUSIVE` lock** (`PRAGMA locking_mode=EXCLUSIVE`, `BEGIN EXCLUSIVE`) on the same file, `<db>.<sha256(scope)[:16]>.lock`;
- held on one connection for the instance's lifetime;
- SQLite on Unix implements it with `fcntl` record locks, so a Python and a TypeScript instance exclude each other.

The Python flock variant existed only in this branch's uncommitted work and was never released; its tests are kept and re-run.

This is a **local** lock: one host, one filesystem with working POSIX locks. On NFS it is unreliable, and across hosts it means nothing. It is **not** distributed coordination, and the adapter does not present it as such.

### 3.3 Wrapping unmodified servers

For servers that cannot be edited, the measurement injects the adapter at process start: `node --import adapters/typescript/src/preload.ts <server>`.
- The preload resolves the SDK copy the server itself loads, from the server's own `node_modules`, so it patches the same module instance.
- It wraps `Server.prototype.setRequestHandler` for `tools/list` and `tools/call`.
- The server's files are byte-identical before and after.

The keyed arm is labelled "adapter injected". It is an adopting *deployment*, not an unmodified server. For server authors, the supported path is `installKeyedTools(server, …)`, called before tools are registered.

### 3.4 What the adapter will not claim

- `move_file` and `add_observations` are external effects that carry no key. A crash between the effect and the record leaves the key **in doubt**, reported as unknown and never re-run. There is no recovery check for them.
- Exactly-once is claimed only for transactional effects in the store's own database, and for two-phase effects with a sound recovery check: an exclusive store plus a key-carrying or natively keyed effect.

## 4. Plan

1. **Python lock protocol (§3.2).** Swap the mechanism and re-run the Python idempotency suites.
2. **The adapter** in `adapters/typescript/`, written in TypeScript with erasable syntax only, so Node 22.22 runs it directly:
   - `contract.ts`: names, outcomes, codes, fingerprint;
   - `store.ts`;
   - `executor.ts`: `Recovery`, `recoverAbandoned`, `KeyedExecutor`;
   - `sdk.ts`: `KeyedTools`, `installKeyedTools`, `currentKey`;
   - `preload.ts`: the env-configured injection, plus test-only crash points.
3. **Tests** (`node --test`, real child-process crashes) for the 16 required boundaries, plus pytest cross-language tests: a shared store file, fingerprint corpus equality, mutual exclusion of the lock, and Python operator release of a TypeScript key.
4. **Gates:** `tsc --strict --noEmit` against SDK 1.32.1's own `.d.ts` files, the node tests, the full pytest suite, ruff and mypy.
5. **Measurement:**
   - the two pinned TypeScript servers through the real relay, frozen in a new snapshot;
   - arms raw, naive, sg_scoped (adapter absent), naive_keyed_ts and sg_keyed_ts;
   - transport faults from the inherited shim, plus crash faults at the adapter boundary;
   - ground truth on disk; Wilson intervals; per server, tool and fault.
   The inherited third-party regression is re-run with the adapter absent.

## 5. What was built (Phase 2)

| File | What |
|---|---|
| `adapters/typescript/src/contract.ts` | Wire names, the 11 outcomes, the JSON-RPC codes and messages (byte-identical to Python), `OutcomeError`, wire helpers, and `fingerprint`/`canonicalJson` (Python's canonical JSON, §3.1) |
| `adapters/typescript/src/store.ts` | `IdempotencyStore` on `node:sqlite`. The same table, SQL, states, owner-id format (`x-` for exclusive) and lock file as Python. It provides begin, adopt, complete, abort, mark-in-doubt, wait, transact (synchronous effects only), purge, release, resolve and `stateOf`, plus the exclusive lock (§3.2). |
| `adapters/typescript/src/executor.ts` | `Execution`, `NotExecuted`, `Recovery`, `recoverAbandoned`, `claim` and `KeyedExecutor`. Framework-free; recovery checks may be async. |
| `adapters/typescript/src/sdk.ts` | `KeyedTools` for SDK 1.x: `twoPhase`, `transactional`, `install(server)` (wraps the instance's public `setRequestHandler`, and fails closed if the tool handlers already exist), `wrapCallHandler`, `wrapListHandler`, `currentKey()` (`AsyncLocalStorage`), and the answer mapping (§2). |
| `adapters/typescript/src/preload.ts` | `node --import` injection for servers that cannot be edited. It resolves the server's own SDK copy through its `exports`, and has a test-only fault mode (SIGKILL or hang at five points; fault-only without keys). |
| `adapters/typescript/examples/keyed_reference_server.ts` | `McpServer` reference: `create_order` (transactional), `append_ledger` (two-phase, key on the line, recovery), `charge` (downstream with native key idempotency, recovery by key lookup), `send_notice` (an external effect with no key and no check), and `read_ledger`. |
| `adapters/typescript/{package.json,tsconfig*.json,README.md,.gitignore}` | Exact pins: SDK 1.32.1, TypeScript 6.0.2, `@types/node` 22.20.5, zod 4.6.7. `npm install --ignore-scripts`. |
| `src/stateguard/adapters/mcp/idempotency.py` | **Protocol change (§3.2):** the exclusive lock is now an SQLite `EXCLUSIVE` lock on the same file, which is what makes it cross-language. |

The adapter's `dist/` copy of SDK 1.32.1 is byte-identical to the copies inside both pinned servers (698 `dist` files compared).

## 6. Tests (Phase 3)

**Node** (`npm test`; 73 tests, run three times, 73/73 each time):
- `store.test.ts`, 42 tests: the crash windows use a real child process that SIGKILLs itself;
- `sdk.test.ts`, 12 tests: `McpServer` and a low-level `Server`, driven by the official `Client` over `InMemoryTransport`;
- `server.test.ts`, 19 tests: the reference server as a real stdio process, real SIGKILLs, restart, and a resend with the same key.

**pytest** (`tests/examples/test_typescript_adapter.py`, 17 tests):
- the node suite and `tsc --strict` as gates;
- the wire contract compared to Python;
- the fingerprint corpus;
- one store file shared both ways, the same lock path, and the lock mutually exclusive across languages (including after a SIGKILLed holder);
- a Python operator releasing a TypeScript key that is in doubt;
- the **real Python relay** recovering the TypeScript server (recovered, executed, replayed, a hang replayed after a deadline, an un-keyable effect surfaced as unknown);
- the preload on both **pinned real servers**: advertisement only on the keyed tool, `executed` then `replayed`, an unkeyed second `move_file` still failing as the server's own behaviour, the memory server replaying the original answer, the package trees byte-identical after, and the fault-only mode advertising nothing.

| # | Required test | Where |
|---|---|---|
| 1 | duplicate requests, same key | `sdk.test` "duplicate requests…", `store.test` "duplicates…", `server.test` "restarted server replays…", pytest pinned servers |
| 2 | concurrent duplicates | `sdk.test` "concurrent duplicates…", `store.test` (in-process: one runs, the rest replay; across instances: in doubt) |
| 3 | key reused with other arguments | `sdk.test`, `store.test`, pytest shared store (`rejected_mismatch` across languages) |
| 4 | death before execution | `store.test` `[before_begin]` and `[after_begin]` (two-phase, recovery, transactional), `server.test` the same points |
| 5 | death during execution | `[mid_effect]` (torn line; transactional rolled back) |
| 6 | death after the effect, before the record | `[after_effect]`: two-phase in doubt; with recovery `recovered`; transactional exactly once; `send_notice` in doubt |
| 7 | death after the record, before the response | `[after_record]` (replayed), plus `server.test` hang with a client timeout and resend, and the pytest relay deadline |
| 8 | restart, then replay | `store.test` "restarted server…", `server.test` "restarted server…" |
| 9 | store failure before execution | `store.test` unreachable, corrupted and locked stores; `sdk.test` "a store failure…" (nothing executed) |
| 10 | expiry and scope collisions | `store.test` "expiry, tombstones…" and "scopes are separate…" |
| 11 | a tool error recorded and replayed | `sdk.test` "a tool error is recorded and replayed…" |
| 12 | input required is not a completion | `sdk.test` `UrlElicitationRequired` (not recorded; the retry executes once); `input_required` result pass-through |
| 13 | native idempotency | `server.test` `charge` through a payment service that dedupes by key: one charge at every crash point (`executed` / `recovered` / `replayed`) |
| 14 | external effect without native idempotency | `server.test` `send_notice`: in doubt, repeated resends never re-send; pytest through the relay: unknown |
| 15 | concurrent operations with different keys | `sdk.test`, `store.test` |
| 16 | negative control | `server.test` `--no-advertise`: a blind resend after a death after the effect writes the line **twice** |

In the first cross-language run one test failed: the fingerprint test expected *every* integral float to diverge. Floats at or above 1e21 print identically in both languages (`6.02214076e+23`). The test was corrected to assert the documented property: every divergence is an integral float, and everything else matches.

## 7. Measurement on the pinned TypeScript servers (Phase 4)

### 7.1 Setup

The plan is `realts/PLAN.md`, written before any row existed.

- **Path:** a client speaking raw JSON-RPC → the **real relay** (snapshot iter6) → the inherited fault shim (`real3/shim.py`, unchanged) → `node` → the pinned server.
- **Server variants:** run as installed, or with the adapter injected (`--import preload.ts`). The servers' package trees were hashed before and after.
- **Arms:**

| Arm | Relay | Adapter | Client |
|---|---|---|---|
| raw | `--passthrough` | no | one call |
| naive | `--passthrough` | no | blind resend, no key |
| sg_scoped | inherited best config: effects file, `--retry auto`, `--restart 2`, `--call-deadline 1.0`, `--deadline-scope recoverable` | no | one call |
| naive_keyed_ts | `--passthrough` | **injected** | resends with its own key |
| sg_keyed_ts | sg_scoped's config (the relay adds keys) | **injected** | one call |

- **Faults:**
  - transport faults (7, including none) for reads and writes;
  - crash faults (5) for writes at the adapter boundary. Arms without the adapter get the same points in the preload's fault-only mode.
- **Ground truth:** the moved file (filesystem) and the memory server's JSONL file (memory).
- **Strict success:** a write's effect exists exactly once and the client was told it succeeded, with one answer. UNKNOWN and in-doubt answers are failures.
- **Sample:** 12 trials per cell, 1140 rows per server, 0 driver errors.

### 7.2 `@modelcontextprotocol/server-filesystem` 2026.8.31 (`rows_fs.jsonl`)

| Arm | Fault-free | Faulted | Transport | Crash | `move_file` faulted | `read_text_file` faulted | Duplicate / false success / false failure | Unknown to client | Z |
|---|---|---|---|---|---|---|---|---|---|
| raw | 0/24 | 204/204 | 144/144 | 60/60 | 132/132 | 72/72 | 0 / 0 / 0 | 180 (no answer) | – |
| naive | 0/24 | 72/204 (35.3%, 29.1–42.1) | 36/144 | 36/60 | 72/132 | 0/72 | 0 / 0 / **72** | 0 | 0 |
| sg_scoped | 0/24 | 132/204 (64.7%) | 72/144 | 60/60 | 132/132 | 0/72 | 0 / 0 / 0 | 120 | 0 |
| naive_keyed_ts | 0/24 | 24/204 (11.8%, 8.0–16.9) | **0/144** (≤ 2.6%) | 24/60 | 24/132 | 0/72 | 0 / 0 / 0 | 24 (in doubt) | 0 |
| sg_keyed_ts | 0/24 | **24/204 (11.8%, 8.0–16.9)** | **0/144** (≤ 2.6%) | 24/60 | 24/132 | 0/72 | 0 / 0 / 0 | 24 (unknown) | 0 |

- **Per fault.** With the adapter, every write transport fault recovers (lost_exit, kill_before, hang, hang_before, garbage, transient), and so do before_begin, after_record and hang_after_record. The 24 failures are exactly `after_begin` (12) and `after_effect` (12): the key was claimed and no result recorded. `move_file`'s rename carries no key, so the attempt cannot be told apart from one that acted, and the client is told unknown or in doubt. None of them is a duplicate.
- **Without the adapter** (sg_scoped), all 132 faulted writes fail. The naive client's 72 failures are all **false failures**: the move happened, and the resend's "source missing" error told the client it failed.

### 7.3 `@modelcontextprotocol/server-memory` 2026.8.31 (`rows_mem.jsonl`)

| Arm | Fault-free | Faulted | Transport | Crash | `add_observations` faulted | `open_nodes` faulted | Duplicate / false success / false failure | Unknown to client | "Nothing added" answers |
|---|---|---|---|---|---|---|---|---|---|
| raw | 0/24 | 204/204 | 144/144 | 60/60 | 132/132 | 72/72 | 0 / 0 / 0 | 180 | 0 |
| naive | 0/24 | **0/204** (≤ 1.8%) | 0/144 | 0/60 | 0/132 | 0/72 | 0 / 0 / 0 | 0 | **72** |
| sg_scoped | 0/24 | 132/204 (64.7%) | 72/144 | 60/60 | 132/132 | 0/72 | 0 / 0 / 0 | 120 | 0 |
| naive_keyed_ts | 0/24 | 24/204 (11.8%, 8.0–16.9) | 0/144 | 24/60 | 24/132 | 0/72 | 0 / 0 / 0 | 24 | 0 |
| sg_keyed_ts | 0/24 | 24/204 (11.8%, 8.0–16.9) | 0/144 | 24/60 | 24/132 | 0/72 | 0 / 0 / 0 | 24 | 0 |

**A finding against the adapter, reported as measured.**
- `add_observations` skips contents already present. A blind resend therefore cannot duplicate this test's observation, so naive retry scores 0/204.
- Its resends answer `addedObservations: []` in 72 cases. That is a truthful answer for the resend, but it is not the original answer.
- The keyed arms replay the original answer, and they fail the same 24 crash-window calls as on the filesystem server: in doubt, never re-run.
- The server itself annotates the tool `idempotentHint: false`. The adapter does not override that declaration to re-run abandoned attempts, because the mandate requires keeping in-doubt outcomes rather than re-running blindly.

So on this server, under crash faults between the claim and the record, the adapter is **less available than a blind retry**, though never less safe.

An operator-declared "re-run abandoned attempts" mode is the identified follow-up. It would need an exclusive store to prove the attempt is over, plus an explicit declaration that contradicts the server's own hint. It is not implemented.

### 7.4 Fault-free controls and latency

The control batches are `rows_{fs,mem}_control.jsonl`: read/none and write/none, 60 trials, 600 rows per server.

| Server | Arm | Fault-free | Session latency mean / median / p95 (s) |
|---|---|---|---|
| filesystem | raw | 0/120 (≤ 3.1%) | 0.724 / 0.714 / 0.810 |
| filesystem | sg_scoped | 0/120 | 0.737 / 0.731 / 0.837 |
| filesystem | sg_keyed_ts | 0/120 | 0.841 / 0.835 / 0.949 |
| filesystem | naive_keyed_ts | 0/120 | 0.830 / 0.824 / 0.932 |
| memory | raw | 0/120 | 0.747 / 0.747 / 0.845 |
| memory | sg_scoped | 0/120 | 0.757 / 0.750 / 0.854 |
| memory | sg_keyed_ts | 0/120 | 0.872 / 0.865 / 0.972 |
| memory | naive_keyed_ts | 0/120 | 0.853 / 0.851 / 0.936 |

Every arm has 0 unsafe outcomes, 0 unknowns and 0 StateGuard-induced failures on fault-free calls.

**What the latency figures mean.**
- *Session* latency is one `initialize`, `tools/list` and `tools/call`, including the start of every process. The adapter adds about 0.10–0.12 s per session, mostly the preload's start-up: loading the TypeScript modules and opening SQLite.
- *Per call*, measured in process (`realts/latency_microbench.{ts,json}`: `McpServer` plus `Client` over `InMemoryTransport`, 400 calls, Node 22.22, ext4):

| Configuration | Mean | Median | p95 | p99 |
|---|---|---|---|---|
| no adapter | 0.092 ms | 0.071 | 0.155 | 0.276 |
| adapter installed, unkeyed call | 0.079 ms | 0.065 | 0.132 | 0.219 |
| keyed, new key (begin and complete: 2 fsync'd commits) | **4.818 ms** | 4.563 | 6.794 | 9.592 |
| keyed, replay | 0.459 ms | 0.402 | 0.623 | 0.792 |

`node:sqlite` is synchronous, so the event loop is blocked for those milliseconds.

### 7.5 Regression checks

- **The inherited third-party baseline, adapter not adopted** (`real3_on_iter6/`): the inherited driver ran unchanged on snapshot iter6, with raw, sg_hints and sg_scoped on the filesystem, memory and sqlite servers. **0 of 1512 per-trial outcomes differ** from `real3/rows_iter2_*.jsonl`: raw 432/432, sg_hints 288/432 and sg_scoped 216/432 faulted; 0/72 fault-free; 0 unsafe.
- **The Python keyed reference server under the new cross-language lock** (`realkeyed/rows_iter6_check.jsonl`, planned in `PLAN_iter6_check.md`): sg_keyed **0/360 faulted** and 0/36 fault-free, with 0 unsafe outcomes. This equals iter2.
- **The pinned servers' package trees** (`@modelcontextprotocol/*`, every file) are byte-identical before and after all runs (`realts/server_trees_{before,after}.sha`).

## 8. Does this support a RIFR claim?

**No general claim.** What the evidence supports, with its scope:

1. **Two pinned TypeScript servers, adapter injected, through the real relay**, on this run's injected-fault workload (19 cells per server, 12 trials):
   - transport faults: **0/144 per server** (95% upper bound 2.6%);
   - crash faults: 24/60 per server, all in the claimed-but-unrecorded window, all reported unknown;
   - overall faulted: **24/204 (11.8%, CI 8.0–16.9%) per server**;
   - fault-free: **0/144 per server** (main plus control; ≤ 2.6%);
   - zero duplicates, false successes, false failures or StateGuard-induced failures.
   Pooled over both servers (the identical workload twice, independent), faulted is 48/408 (11.8%, CI 9.0–15.3%) and fault-free is 0/288 (≤ 1.3%). That pool is these two servers and these faults, not MCP traffic.
2. **Compared with no adapter** (sg_scoped), faulted writes go from 132/132 failing to 24/132 per server.
   - On the filesystem server, the naive client's alternative is 72 false failures out of 204.
   - On the memory server, the naive client does better on crash faults, because the tool deduplicates by content (§7.3).
3. **None of this is a sub-1% claim.**
   - The crash-window residual is 11.8% of *faulted* calls, by design: these effects carry no key.
   - The real fault incidence of production traffic is unmeasured, so no traffic-weighted rate can be computed.
   - A rate in the style of "fault-free plus faulted" depends entirely on an assumed fault mix, so it is not given.

## 9. Unsupported and limited scenarios

- **External effects without a key or native idempotency**, such as `move_file`, `add_observations`, an e-mail or a payment API without idempotency keys, are **at most once** under the adapter. A crash between the effect and its record is reported unknown and never re-run. Exactly-once is claimed only for transactional effects in the store's database and for effects with a sound recovery check.
- **Multi-instance operation.**
  - Two server processes sharing one store and scope *without* `exclusive` stay correct: they never duplicate, and each sees the other's started keys as in doubt. But they get no recovery for each other's abandoned keys.
  - *With* `exclusive`, only one process may run per store and scope.
  - The lock is a local SQLite/`fcntl` lock: one host, a filesystem with working POSIX locks. It is not reliable on NFS, it is meaningless across hosts, and it is **not distributed coordination**.
- **Cross-language fingerprints** diverge for integral values written as floats and for integers beyond ±2^53 (§3.1). This matters only when both languages share a store *and* a scope.
- **The event loop blocks** for each store operation: about 4.8 ms for a new key on ext4, and up to the busy timeout under cross-process contention.
- **Not covered:**
  - the 2026-07-28 `InputRequiredResult` shape is handled by pass-through, but SDK 1.32.1 cannot produce it, so it is tested only at the handler boundary;
  - task-augmented calls (`params.task`) are keyed like others, so the record is of the task's *creation*. This is untested;
  - HTTP transports: tested since 2026-11-04 on a controlled server only.
    - Server: `test/fixtures/http_notes_server.ts`, SDK 1.32.1 over Streamable HTTP, with one
      `McpServer` and one transport per session, as the SDK example and Express-based servers do.
    - Setup: keys come from the preload alone, through `examples/mcp/http_proxy.py`.
    - Covered: lost response, 502 after execution and cut stream (each `replayed`, run once),
      session loss (reopened, run once), a crash after the effect (in doubt, never re-run), and
      the unkeyed control (unknown, never resent).
    - Tests: `tests/examples/test_http_proxy.py`.
    - Not tested: a third-party TypeScript HTTP server with a write tool. No pinned one has one.
- **Operator-declared re-run of abandoned attempts** for naturally idempotent tools is the follow-up §7.3 identifies, and it is not implemented.

## 10. Reproduce

Versions used throughout:
- Node 22.22.0 (`/opt/node22/bin/node`), Python 3.11.17, mcp 2.3.0 (Python);
- `@modelcontextprotocol/sdk` 1.32.1 (in the adapter and in both servers' own trees);
- `@modelcontextprotocol/server-filesystem` 2026.8.31, `@modelcontextprotocol/server-memory` 2026.8.31;
- TypeScript 6.0.2, `@types/node` 22.20.5, zod 4.6.7, ruff 0.16.6, mypy 2.3.1.

```
# the adapter (from adapters/typescript)
npm install --ignore-scripts          # exact pins from package-lock.json
npm run typecheck                     # tsc --strict --noEmit, skipLibCheck false
npm test                              # node --test --test-concurrency=1 'test/*.test.ts'

# cross-language tests and the full suite (from the repo root)
.venv/bin/python -m pytest -p no:cacheprovider tests/examples/test_typescript_adapter.py
.venv/bin/python -m pytest -p no:cacheprovider
.venv/bin/ruff check src/ tests/ && .venv/bin/ruff format --check src/ tests/ && .venv/bin/mypy src/

# Phase 4, from frozen snapshot iter6 (A=/root/stateguard-captures/autonomous-mandate, R=$A/realts)
export SNAPSHOT=$A/snapshots/iter6
for srv in fs mem; do
  RUNS_DIR=<scratch> FAILING_DIR=$R/failing_runs TRIAL_OFFSET=0 \
    .venv/bin/python $R/driver.py $srv 12 "" "" 4 > $R/rows_$srv.jsonl 2> $R/rows_$srv.err
  RUNS_DIR=<scratch> FAILING_DIR=$R/failing_runs_control TRIAL_OFFSET=1000 \
    .venv/bin/python $R/driver.py $srv 60 "read/none,write/none" "" 4 > $R/rows_${srv}_control.jsonl
done
python3 $R/summarize.py $R/rows_fs.jsonl --json $R/summary_fs.json   # and likewise mem, *_control

# regression: inherited third-party driver, adapter not adopted (from $A/real3_on_iter6)
for s in fs mem sqlite; do .venv/bin/python ../real3/driver.py $s 12 "" raw,sg_hints,sg_scoped > rows_$s.jsonl; done

# regression: Python keyed reference server under the new lock
RUNS_DIR=<scratch> FAILING_DIR=$A/realkeyed/failing_runs_iter6_check TRIAL_OFFSET=3000 \
  .venv/bin/python $A/realkeyed/driver2.py 12 "" sg_keyed 4 > $A/realkeyed/rows_iter6_check.jsonl

# per-call latency
node $R/latency_microbench.ts $SNAPSHOT/adapters/typescript/src <dir with SDK 1.32.1> <scratch> 400
```

`$R/rows.codestate` records the sha256 of `driver.py`, `PLAN.md`, the shim and the iter6 manifest the rows came from. `failing_runs_control/` was never created, because no control run failed.

## 11. Gates, preservation and manifests

**Gates on the final tree:**
- ruff check: pass; ruff format: pass (208 files);
- mypy: no issues (69 files in `src/`, plus the relay, the two keyed example servers and the benchmark harness);
- tsc 6.0.2 strict: pass;
- node `--test`: 73/73 (store 42, sdk 12, server 19), run three times with no flake;
- pytest, full suite: **6254 passed**, 0 failed, 0 skipped (516 s, rc 0). That includes the 17 cross-language tests.

**Preservation against this mandate's starting state** (`ts-adapter/baseline/`):
- Of the 83 inherited entries in `git status`, 82 are byte-identical.
- One was modified: the inherited, untracked `src/stateguard/adapters/mcp/idempotency.py` (58 diff lines, the lock change in §3.2). Its pre-change copy is in `baseline/copies/`.
- 21 files are new: 19 under `adapters/typescript/` (sources, tests, fixtures, example, configs and lockfile; `node_modules/` and `dist/` are git-ignored), `tests/examples/test_typescript_adapter.py` and this document.
- `.hypothesis/` is unchanged (tree digest 8bae78e3…). Tests ran with `HYPOTHESIS_STORAGE_DIRECTORY` pointed outside the repo.
- Branch `claude/serene-bohr-o28snr`, HEAD b6f7ba0. Nothing was committed, pushed, reset or cleaned.

**Manifests** (all under `/root/stateguard-captures/autonomous-mandate/`):
- `MANIFEST.sha256`: 174 inherited entries, all verify. Its 175th line, a self-entry holding the empty-file hash, cannot verify by construction. The file is byte-identical to its baseline hash `43d2d9b7…`;
- `MANIFEST_server_idempotency.sha256`: 11911 entries, all verify;
- `MANIFEST_ts_adapter.sha256` (new): 4792 entries covering every artifact in §7 and §10, all verify.

No inherited rows or evidence were overwritten: every Phase 4 output path was checked for absence before its run started.

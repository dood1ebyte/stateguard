# StateGuard Phase 1: engineering record

The running record of Phase 1 (2026-11-06 to 11-18). Execution state lives in `NEXT.md`. The
final account is `PHASE1_COMPLETION_REPORT.md`, written in Session 6.

**Every result here is controlled fault injection on named, pinned servers.** None is a
production rate, and none supports a sub-1% claim. Each batch has a plan written before its
first row, a `codestate*.json` (code commit, harness commit, versions) and its raw rows, all
under `benchmarks/evidence/`.

## Session 1 (Fri 2026-11-06, 09:00–14:00 IST)

### Defects found and fixed

| # | Defect | Effect | Fix | Evidence |
|---|---|---|---|---|
| D1 | The evidence and drivers behind M16–M20 lived outside the repository | lost with the container (2026-11-05); the results could not be reproduced | harness and evidence in the repository (`benchmarks/runtime/realhttp/`, `realstdio/`, `benchmarks/evidence/`) | B1 and S2 re-measure them |
| D2 | CI's mcp 1.x environment installed an unpinned pydantic, with which mcp 1.9.4 does not import | its 13 injector tests would skip silently in CI | pydantic 2.11.7 pinned; the test errors when the interpreter is set but unusable | 15/15 pass |
| D3 | The stdio relay resolved the server program before checking the executor scope's platform | on Python 3.13+ (in CI's matrix) a patched "win32" platform got "program not found"; a test failed, misdiagnosed in `NEXT.md` as sandbox-only | platform checked first | the test passes on 3.13 |
| D4 | The HTTP relay reused idle upstream connections for httpx's default 5 s, the same as uvicorn's and Node's keep-alive | a call sent as the server closes the connection is lost unread and reported "unknown" | `--pool-idle-timeout`, default 1 s | S1: 13/60 failed before, 0/60 after |
| D6 | `test_typescript_adapter.py` looked for the pinned servers only at the lost capture paths | 3 tests skipped silently | `STATEGUARD_FS_SERVER` / `STATEGUARD_MEM_SERVER`, else `/opt/sg-servers/` | 17/17 run and pass |
| D5 | `run_batch.py`: the `node_modules` link made the worktree "not clean"; parallel parts raced to create it | later parts refused to run | the link is excluded; worktree creation is locked | B1 and S1 ran |

### Finding: the relay never reuses a connection after an event-stream answer

It stops reading at the answer, before the stream's end, so httpx discards the connection.
Every event-stream call therefore pays for a new upstream connection. This is safe: there is no
stale reuse. It is also a cost, not yet measured on its own.

### B1: HTTP baseline versus StateGuard (`benchmarks/evidence/realhttp/B1`, code `237a332`)

12 trials per cell, 16 cells (8 for server-everything). Fault-free controls run 60 more trials
per fault-free cell. Strict scoring, Wilson 95%.

| Server | raw | naive (blind resend) | sg (relay) | sg_keyed | best arm |
|---|---|---|---|---|---|
| notes (controlled, Python) | 168/168 (24 false failures) | 48/168, **48 duplicates** | 84/168 | 24/168 | sg_keyed_deadline **0/168 (≤ 2.2%)** |
| tsnotes (controlled, TypeScript) | 168/168 (24 false failures) | 48/168, **48 duplicates** | 84/168 | 24/168 | sg_keyed_deadline **0/168 (≤ 2.2%)** |
| server-everything 2026.8.31 (reads) | 72/84 | 0/84 | 12/84 | — | sg_deadline **0/84 (≤ 4.4%)** |

- **Fault-free:** 0 failures in every arm (0/144 per arm on the notes servers, 0/72 on
  everything).
- **Unsafe outcomes:** none in any StateGuard arm (no duplicate, no false success, no false
  failure), and none induced.
- **Relay records:** all 2,103 `invocation` records written by the relay arms agree with ground
  truth.
- **Fault-free session latency** (mean, client process start included, 4 workers):
  - notes: raw 0.862 s, sg 0.896 s, sg_keyed_deadline 0.894 s;
  - tsnotes: raw 0.881 s, sg_keyed_deadline 0.910 s;
  - everything: raw 0.889 s, sg_deadline 0.950 s.
- **Reproduces the lost batches cell for cell** (iter8, iter9b, iter10).

### S1: the stale pooled connection race (`benchmarks/evidence/realhttp/S1`, code `8de3735`)

**Setup.**
- One SDK session makes 60 unkeyed `add_note` calls.
- Each call follows an idle gap of 4.8–5.1 s (seed 1, the same gaps in every arm).
- A 40 ms one-way delay sits between the relay and the server (uvicorn, keep-alive 5 s).
- The server answers JSON.

| Arm | Failed | Relay's recorded faults | Notes not written | Upstream connections |
|---|---|---|---|---|
| old: `--pool-idle-timeout 5` (httpx's default, which the relay used before) | **13/60 (21.7%, CI 13.1–33.6%)** | 13 `transport_lost`, answered unknown | 13 | 32 |
| new: `--pool-idle-timeout 1` (the new default) | **0/60 (≤ 6.0%)** | none | 0 | 63 |
| old, keyed server | 0/60 | 16 `transport_lost`, all recovered by key | 0 | 41 |
| old, event-stream answers | 0/60 | none | 0 | 64 |

- **The failures were all safe:** each lost call was unwritten and reported "unknown", never a
  success.
- **The fix costs** a new upstream connection after about a second of idle.
- **This is synthetic.** The gaps were chosen to sit near the timeout. How often real gaps land
  there, and real network delay, are unmeasured.

### Regression across CI's Python matrix (local; CI has not run on this branch since `ec339ab`)

- **Python 3.13:** 6,355 passed, 3 skipped (the pinned-server tests, before D6).
- **Python 3.11:** 6,355 passed, 3 skipped.
- **Python 3.14:** 6,347 passed, 8 failed. All 8 were executor-scope tests whose
  unprivileged child could not read an interpreter installed under `/root` (mode 700).
  With 3.14 installed world-readable, as CI's setup-python is, the four files involved
  pass 78/78, and the whole suite showed no failures.
- **Not run:** Python 3.12 and macOS.
- **TypeScript:** `tsc` clean, `npm test` 73/73.

### Adversarial tests added

- **Concurrent sessions across a mid-call server crash,** keyed and unkeyed.
  - Setup: four calls in flight when the server dies, and four sessions that call after the
    restart.
  - Checks: no duplicate, no false success, no answer delivered to the wrong session, and the
    late sessions recover on reopened sessions.
  - Result: passed 3/3 consecutive runs of both variants.
- **Pool idle timeout:** an idle connection is reused within the timeout and replaced after it.

### S2: stdio baseline (`benchmarks/evidence/realstdio/S2`, code `6181cc2`)

**Setup.**
- Servers, both third-party and unmodified:
  - mcp-server-sqlite 2025.4.25 (Python, mcp 1.9.4), keys via the injector;
  - server-memory 2026.8.31 (TypeScript, SDK 1.32.2), keys via the preload.
- Cells (13): reads and writes under four shim faults (kill_before, lost_exit, hang,
  transient), writes under three crash points, and the fault-free cells.
- 12 trials per cell; fault-free controls run 60 more trials per fault-free cell.
- The relay arms declare the write `non_idempotent` and the read `read_only`. A first start
  declared only the write; it is aborted and kept in `aborted/`.

| Server | raw | naive | sg (relay) | sg_keyed |
|---|---|---|---|---|
| sqlite | 132/132 (36 false failures) | 48/132, **48 duplicate rows** | 96/132 | **48/132** |
| memory | 132/132 (36 false failures) | 0/132 (the tool drops duplicates) | 96/132 | **48/132** |

- **The sg_keyed residual, per server:**
  - after_begin and after_effect, 24 in total: claimed and unrecorded, so in doubt by design;
  - read/transient and write/transient, 24 in total: a JSON-RPC `-32603` is a "protocol
    error", which the relay never retries.
  - The plan expected 24/132; the 24 transient failures are the deviation.
- **sg:** every declared read recovers except transient. Unkeyed writes are "unknown" and
  never resent.
- **Unsafe outcomes:** none in any StateGuard arm (0 duplicates, 0 false successes, 0 false
  failures), and none induced.
- **Fault-free:** 0/144 per arm per server.
- **Relay records:** all 1,104 relay `invocation` records match ground truth.
- **Session latency, fault-free mean** (on stdio this includes starting the relay process):
  - sqlite: raw 1.307 s, sg 1.535 s, sg_keyed 1.701 s;
  - memory: raw 1.129 s, sg 1.366 s, sg_keyed 1.453 s.

### D7: retrying a server's internal error (-32603) where it is safe (`a8ed63d`)

**The change.**
- `classify_response` gives JSON-RPC `-32603` its own class, `SERVER` (`Observed.SERVER_ERROR`).
- `decide_retry` treats it like a tool error: it is retried once for a keyed call, or for a tool
  the operator declared `read_only` or `idempotent`.
- Otherwise it is delivered as the server sent it, because an internal error may come after the
  effect.
- Every other JSON-RPC error is still never retried. Auth signals and an upstream relay's
  execution state still take precedence.

**Tests:** classification and precedence, the rule, the attempt limit, the property test, and
the stdio relay end to end.

**S3** (`benchmarks/evidence/realstdio/S3`, code `a8ed63d`): the transient cells, 12 trials,
both servers.

| Cell | S2 (before): sg / sg_keyed | S3 (after): sg / sg_keyed |
|---|---|---|
| read/transient | 12/12 / 12/12 | **0/12 / 0/12** |
| write/transient | 12/12 / 12/12 | 12/12 (never resent: unkeyed non-idempotent) / **0/12** |

- 0 unsafe outcomes; fault-free 0/24 per arm; all 192 relay records match ground truth.
- **Combined stdio picture, per server.** Taking S2's other cells with S3's transient cells, the
  keyed relay's residual is **24/132**: the after_begin and after_effect crash windows, in
  doubt by design.
  - These are two batches at different commits. The change affects only `-32603` handling, but
    a single-batch confirmation remains open (queue).

### S4: the stdio matrix in one batch (`benchmarks/evidence/realstdio/S4`, code `180fae6`)

S2's matrix again, on one commit after D7. The results are per server, and sqlite and memory
match cell for cell except the naive arm.

| Arm | Faulted failures | Transport | Unsafe outcomes |
|---|---|---|---|
| raw | 132/132 | 96/96 | 36 false failures |
| naive | sqlite 48/132, **48 duplicate rows**; memory 0/132 | — | sqlite: 48 duplicates |
| sg | 84/132 | 48/96 | 0 |
| **sg_keyed** | **24/132 (18.2%, CI 12.5–25.6%)** | **0/96 (≤ 3.9%)** | **0** |

- The sg_keyed residual is only after_begin and after_effect, reported unknown or in doubt,
  never success.
- Fault-free 0/144 per arm; all 1,104 relay records match ground truth.
- Session latency, fault-free mean (relay process start included):
  - sqlite: raw 1.251 s, sg 1.453 s, sg_keyed 1.607 s;
  - memory: raw 1.061 s, sg 1.280 s, sg_keyed 1.360 s.

### D8: the same `-32603` rule in the HTTP relay

**The gap.** The HTTP relay delivered a server's JSON-RPC error as it arrived.

**The change.** On a `-32603` answer, the relay asks `decide_retry` with `SERVER_ERROR`, as the
stdio relay does. A keyed call, a declared read or idempotent tool, or a request that changes
nothing is sent once more; anything else gets the server's error. This applies whether the
answer came as JSON or as an event stream. The incidence record names the fault
`internal_error`, apart from HTTP 5xx (`server_error`).

**Tests:**
- a declared read is requested again, recorded `recovered`;
- a keyed write is resent once and written once, as JSON and as an event stream;
- an unkeyed write is delivered the error and never resent.

The fault proxy gains TEST-ONLY `rpc_internal_error` and `rpc_internal_error_sse` modes.

### B2: the HTTP matrix on the current code (`benchmarks/evidence/realhttp/B2`, code `1852365`)

B1 repeated on the commit after D4 (pool idle timeout) and D8 (`-32603` retry where safe), as a
regression check. Same servers, arms, 12 trials, 60-trial controls and 4 workers.

- **Every failure, unsafe and unresolved count matches B1 cell for cell:**
  - notes and tsnotes: raw 168/168, naive 48/168 (48 duplicates), sg 84/168, sg_keyed 24/168,
    sg_keyed_deadline 0/168;
  - everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84.
- Fault-free 0 in every arm; no unsafe outcome in a StateGuard arm; 0 relay records disagree
  with ground truth.
- As expected: none of B1's faults injects a `-32603`, and the fault proxy closes every
  connection, so neither change could move a cell. This shows they broke nothing; S1 and the
  D8 tests are their evidence.
- **Observed gap in incidence records (B1 and B2 alike).** In the `sg` arm's `hang_after` cells
  (no deadline), 15 rows in B1 and 18 in B2 have no `invocation` record. The client gives up
  while the relay still waits on the hung server, and the run ends before the relay finishes
  the request. A relay writes a record when a request finishes, so one stopped mid-request
  writes none. The deadline arms always write one. The incidence denominator therefore misses
  requests that were still open when a relay stopped.

### Queue item 3: the crash-window residual is a floor without server cooperation

A key claimed and never recorded (`after_begin`, `after_effect`) leaves an effect that carries no
key. Nothing outside the server can tell whether it happened. A probe of the effect cannot
attribute it to this call either: another actor, or a state that already held, looks the same
(`RECONCILIATION_CONTRACT_V2.md`). Only these can close it:
- a transactional effect, where the key's record commits with the effect;
- a recovery check bound to the key, which the server or its operator supplies (the adapters'
  `recover=`, K-6).

Neither is available for an unmodified third-party server, so the residual stays reported in
doubt, never re-run, and never counted as success.

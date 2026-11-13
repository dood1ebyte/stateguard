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

### P1: relay overhead, paired and uncontended (`benchmarks/evidence/{realhttp,realstdio}/P1`, code `1852365`)

- **Setup:** one worker, with arms interleaved trial by trial, 20 trials per fault-free cell
  (TRIAL_OFFSET=2000).
- **Measure:** each arm's session latency minus raw's on the same trial
  (`paired_overhead.py`): the mean with a normal 95% interval, client process start included.
- **Failures:** 0 in every arm, and every trial paired.

| Relay | Server | Arm | Mean overhead (95% CI) | raw session |
|---|---|---|---|---|
| HTTP | notes | sg | read +0.008 s (−0.018–0.035), write +0.025 s (0.001–0.049) | 0.81–0.83 s |
| HTTP | notes | sg_keyed_deadline | read +0.006 s (−0.008–0.021), write +0.029 s (0.011–0.048) | |
| HTTP | tsnotes | sg | read +0.066 s (0.021–0.110), write +0.022 s (−0.003–0.048) | 0.81–0.82 s |
| HTTP | tsnotes | sg_keyed_deadline | read +0.026 s (−0.004–0.055), write +0.027 s (−0.002–0.056) | |
| HTTP | everything | sg / sg_deadline | +0.031 s (0.012–0.050) / +0.037 s (0.017–0.058) | 0.80 s |
| stdio | sqlite | sg | read +0.209 s, write +0.229 s | 1.21–1.22 s |
| stdio | sqlite | sg_keyed | read +0.370 s, write +0.340 s | |
| stdio | memory | sg | read +0.269 s, write +0.200 s | 0.97–0.99 s |
| stdio | memory | sg_keyed | read +0.295 s, write +0.278 s | |

(stdio intervals are ±0.03 to ±0.04 s; all are in `paired_overhead.txt`.)

- **HTTP:** within the plan's 0.15 s everywhere; the largest cell is 0.066 s. The relay's own
  start is outside the session and not counted.
- **stdio:** these numbers include starting the relay process, and for sg_keyed the key store.
  A host pays that once per server process, not per call. The per-call cost on a running stdio
  relay is not separated here. These are synthetic, single-machine measurements, not
  production latencies.

### P2: the cost of event-stream answers (`benchmarks/evidence/realhttp/P2`, code `1852365`)

**Setup:** one SDK session, 60 unkeyed calls with 0.2–0.3 s gaps, 40 ms one way, relay pool
idle timeout 1 s.

| Arm | Failures | Upstream connections | Call latency mean / p95 |
|---|---|---|---|
| json | 0/60 | 3 | 0.089 / 0.091 s |
| sse | 0/60 | 64 | 0.091 / 0.094 s |

- **Confirmed:** an event-stream answer costs one upstream connection per call (64 for 60,
  against 3).
- **Not measured: the network cost of those connections.**
  - The delay proxy accepts and connects upstream on loopback at once and delays only data, so
    no arm paid a TCP handshake round trip. The +2 ms is the local cost alone.
  - The plan's expectation (about +80 ms) assumed a delayed handshake that this setup does not
    have.
  - On a real path each new connection costs at least one round trip before the request, and
    TLS adds one or two more. That is an estimate, not a measurement.
- Measuring it needs a delay in the kernel (netem), which this container lacks (no `tc` or
  `ip`); queued.

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

## Session 2 (Mon 2026-11-09, 09:00–14:00 IST)

### Metrics and taxonomy, final (`7b3c4d2`)

`docs/runtime/PHASE1_METRICS.md` is the definition the Completion Report uses. It covers:
- the unit, and the three row outcomes: success, unresolved, failed and resolved;
- the RFR, and the counts reported beside it: unsafe, induced, recovered, overhead and record
  agreement;
- the reporting rules (never pooled);
- which `TASKMAP.md` categories the matrix covers: D, E, F and I, and A after D1;
- the traffic-weighted rate that the sub-1% target needs.

`benchmarks/runtime/phase1_table.py` is its implementation, and on B2 and S4 it reproduces
every figure above.

Two definitions changed; neither moves a number in B2 or S4, since neither has such a row:
- "not sent" is a resolved failure;
- a keyed server's `-32010`/`-32011` is unresolved.

### D1: schema drift, category A (`benchmarks/evidence/realhttp/D1`, code `7b3c4d2`)

**Setup:** `add_note` with its field named `Text`, not `text`, and no transport fault. 60
trials per arm on notes and tsnotes.

| Arm | notes | tsnotes |
|---|---|---|
| raw | 60/60 | 60/60 |
| naive | 60/60 | 60/60 |
| sg, sg_keyed, sg_keyed_deadline | **0/60 each (≤ 6.0%)** | **0/60 each (≤ 6.0%)** |

- The server rejects the misnamed field. A blind resend repeats the rejection. The relay
  repairs the field, and the note exists once.
- 0 unsafe outcomes and 0 induced failures; all 360 relay records agree with ground truth.
- This is a single drift (a case-only rename), so it covers category A for that case only.
- **Harness note:** `driver.py` was edited (`a00a269`) at 03:38:35 UTC, while the tsnotes part
  was running (started 03:37:02 UTC). The part's driver process had already loaded the
  committed code, and `harness_dirty` is taken at the start, so the part ran the clean harness.
  From now on, `benchmarks/runtime/` is not edited while a batch runs.

### D9: incidence records missed requests a relay never finished (`a00a269`)

**The gap (B1, B2):**
- A relay wrote an `invocation` record when a request finished.
- A relay stopped or killed while a request was open left that request out of the incidence
  denominator.
- A shutdown hook would not close the gap: the benchmark driver kills relays with SIGKILL,
  and a production relay can die the same way.

**The change:**
- Each request writes an `invocation_open` line as it arrives (no arguments), in both relays.
- Its record carries the same random `inv` token.
- `incidence.read_records` turns an open line with no record into an `unfinished` record,
  which is unresolved.
- The drivers read records the same way, so the record check sees these requests.
- A log read while its relay runs shows requests still in flight as `unfinished`.

**Tests:**
- pairing works across logs and in any order;
- a relay SIGKILLed during a hung call reads `unfinished`, and the call did run;
- a stdio call in flight reads `unfinished`, then `answered` with the same token.

### I1: every relayed request is counted (`benchmarks/evidence/realhttp/I1`, code `a00a269`)

**Setup:** notes and tsnotes; `hang_after` and fault-free cells; arms raw, sg and sg_keyed; 12
trials.

- **Records:** 96 of 96 relay rows per server have a record. Before D9, B1 and B2 missed 15 and
  18.
- **The hang rows** the relay never finished read `unfinished`: sg 14 + 14, sg_keyed 22 + 20.
  The rest read `unknown`.
- **0 record mismatches.** The failure rates equal B2's cells: the change touches records
  only, never traffic.

### What still fails, by configuration (B2, S4, `phase1_table.py --by-category`)

- **Unkeyed relay (`sg`).** The residual is writes left `unknown` after a fault that may have
  let them run: `kill_before`, `http_503_before`, `lost_response`, `http_502_after`,
  `cut_stream`, and stdio `lost_exit`, `hang` and `transient`. Hangs also fail without a
  deadline. Resending an unkeyed write is unsafe, so this is the floor without keys.
- **Keyed relay (`sg_keyed`):**
  - HTTP: `hang_after` only, 24/24 per server. With `--call-deadline` it is 0 (B2
    `sg_keyed_deadline`).
  - stdio: the two crash windows only, a floor without server cooperation.
- **The deadline is opt-in** because it once turned slow successes into failures (M5, 8 Z).
  - For a keyed call, the cause sits in `_race`. When the resend is answered "still running"
    (`-32011`), the relay waits one more deadline for the original, then delivers that
    unresolved answer.
  - So a keyed call slower than about two deadlines ends unresolved. Waiting on the original
    while the server reports "still running" would remove that path.
  - That would make a default deadline for recoverable requests (declared reads, keyed calls)
    worth evaluating. It is queued, not started: it is recovery engineering (Session 3), and a
    default is a product decision.

### P3: per-call overhead of a running stdio relay (`benchmarks/evidence/realstdio/P3`, code `0cee55c`)

**Setup:** one SDK session per arm and round, 20 rounds × 50 calls alternating read and write,
no fault, on an idle machine. The difference is taken from raw on the same (round, call, op),
with a normal 95% interval. Warm calls only; the first read and write of a session are in the
summary.

| Server | Op | raw mean | sg vs raw | sg_keyed vs raw |
|---|---|---|---|---|
| sqlite | read | 2.01 ms | +0.68 ms (0.64–0.72) | +0.75 ms (0.71–0.80) |
| sqlite | write | 3.78 ms | +0.87 ms (0.75–0.98) | **+6.31 ms (6.15–6.47)** |
| memory | read | 2.01 ms | +0.94 ms (0.90–0.99) | +1.22 ms (1.15–1.28) |
| memory | write | 3.10 ms | +0.89 ms (0.66–1.11) | **+6.86 ms (6.57–7.15)** |

- 6,000 calls: 0 failed, and 0 session errors.
- **The relay costs under 1 ms per call.** P1's +0.20 to +0.37 s per session is almost all
  the relay's process start, paid once per server process.
- **A keyed write costs about 6–7 ms more:** the key store commits before and after the
  effect. A keyed read is not keyed and costs the relay's share only.
- These are synthetic, single-machine measurements with sub-10 ms tools. They are not
  production latencies, and a slower tool hides the same absolute cost.

### D10: a slow keyed call under a deadline ended unresolved (HTTP relay)

**Reproduced** (`test_a_keyed_call_still_running_past_its_deadlines_is_waited_for`):
- a keyed `add_note` that takes 7 s, with `--call-deadline 0.5`;
- the relay resends it at 0.5 s;
- the keyed server holds that duplicate for up to 5 s (`wait_seconds`), then answers "still
  running" (`-32011`);
- the relay waited one more deadline and delivered `-32011` to the client, while the original
  went on to succeed.
- This is a StateGuard-induced failure. It hits any keyed call slower than about deadline +
  5 s + deadline.

**The fix (HTTP relay, `_race`):**
- After a "still running" answer, the relay waits for the original, however many deadlines
  that takes. Each attempt is still bounded by `--upstream-timeout`.
- The server says the call is executing, so its answer is coming.
- A resend answered "in doubt" still waits one deadline only.
- The test passes, with the note written once. The HTTP relay and idempotency tests pass
  (77).

**The stdio relay is different by contract (checked in Session 2, after the founder allowed
work to continue inside the window).**
- The stdio relay's `--call-deadline` is documented as "Answer 'outcome unknown' to a request
  unanswered after SECONDS". It is off by default because "a long-running tool is not a
  failure".
- So there the deadline is a promised answer time: a recoverable call is resent once, and if
  neither attempt answers by the second deadline, the client is told "unknown".
- A stdio test showed this with a keyed call of 0.45 s and a 0.1 s deadline. The scripted
  server handles one request at a time, the resend hit its own deadline, and the retry budget
  (2 attempts) was spent. The client was told "unknown", and the original's success arrived
  and was dropped as late.
- That is the documented contract, not a bug. With the real keyed server's 5 s duplicate hold,
  the resend also meets its own deadline first, so stdio's "still running" path needs a
  deadline over 5 s.
- A change was tried and discarded uncommitted: waiting up to 300 s on "still running" would
  have silently broken the stated bound.
- **Finding: the two relays' deadlines mean different things.**
  - HTTP: when to resend; the first answer wins.
  - stdio: the latest time an answer is given.
  - Whether to unify them is a product decision for the founder. The trade is a latency bound
    against induced failures on slow calls.

### E1: D10 before and after (`benchmarks/evidence/realhttp/E1`)

**Setup.**
- Notes server; `write/slow` (no fault: `add_note` takes 10 s; deadline 2 s; the client waits
  18 s); 12 trials.
- Part `old` runs code `1852365`; part `new` runs `68b5594` (D10).
- The `write/hang_after` check ran as its own part, `new_hang`, rather than inside `new` as
  the plan said, so `old` and `new` hold the same cells.

| Part (code) | raw | sg_keyed | sg_keyed_deadline |
|---|---|---|---|
| old (`1852365`) | 0/12 | 0/12 | **12/12 failed, all `-32011`; 12 induced** |
| new (`68b5594`) | 0/12 | 0/12 | **0/12** |
| new_hang (`68b5594`), `write/hang_after` | — | — | 0/12 |

- **Before D10, a deadline turned every 10 s keyed success into an unresolved answer.** After
  D10, none.
- In the old part, ground truth reads "not executed" only because each run ended when the
  client got its answer (9.9 s), before the 10 s write finished. The call would have run.
- Recovery from a hang is unchanged. Every relay record agrees with ground truth.
- **What it means for a default deadline (queue item 2).**
  - On the HTTP relay, the known way a deadline turns a slow keyed success into a failure is
    gone.
  - Declared reads are re-requested, never cut short. Unkeyed writes are never cut short (an
    existing test).
  - Still unmeasured: a slow read's double load on the server, slow calls through the
    TypeScript preload, and the stdio relay, which still has the one-deadline rule.
  - A default remains the founder's decision.

### E2 and E3: more evidence for a default deadline (HTTP relay, after D10)

Work continued inside the Session 2 window, with the founder's go-ahead.

| Batch (code) | Cell | raw | sg | sg_keyed | sg_deadline / sg_keyed_deadline |
|---|---|---|---|---|---|
| E2 (`ece85e7`), tsnotes, keyed by the TypeScript preload | `write/slow` (10 s) | 0/12 | — | 0/12 | **0/12** |
| E3 (`72fc67b`), notes | `read/slow` (10 s) | 0/12 | 0/12 | — | **0/12** |

- **E2:** with the TypeScript keyed server too, a deadline turns no slow keyed success into a
  failure. The note is written once.
- **E3:** a slow declared read is re-requested at the deadline and never cut short.
  - **Cost:** 2 reads reach the server per call, against 1 without a deadline.
  - **Incidence caveat:** its record reads `recovered` with fault `deadline`, though nothing
    failed. Under a deadline, a call slower than the deadline counts as a fault in the
    incidence rates.
- **Where a default deadline stands (HTTP), now measured:**
  - keyed writes (Python and TypeScript keyed servers) and declared reads are never cut short
    by it;
  - it recovers `hang_after` (B2, E1);
  - unkeyed writes are never cut short (an existing test).
- **Its costs:** a second request to the server for each call slower than the deadline, and
  incidence records that count slow calls as deadline faults.
- **Still open:** the stdio relay, whose deadline means something else (D10), and the choice of
  value. The default remains the founder's decision.

## Session 3 (Wed 2026-11-11, 09:00–14:00 IST): recovery engineering

### D11: the crash window of an operator-declared idempotent tool (`c4de122`)

**The residual:**
- S4's keyed residual was only the two crash windows: the server died between claiming the key
  and recording it (`after_begin`, `after_effect`).
- An unmodified server's effect carries no key, so no recovery check can find it, and the key
  stays in doubt.
- Queue item 3 (Session 1) concluded that this is a floor without server cooperation.

**The change:**
- **Some tools need no check:** a tool whose repeat is harmless (adding an item to a set).
  For those, the next server process can run the abandoned attempt again.
- **`Recovery.rerun()` (Python and TypeScript):** cannot tell, and need not; the operator
  declared the tool idempotent. The executor runs it, once more at most.
- **`STATEGUARD_IDEMPOTENT_TOOLS`** gives declared keyed tools that recovery, in the Python
  injector and the TypeScript preload.
- **It needs `STATEGUARD_IDEMPOTENCY_EXCLUSIVE=1`**, because only an exclusive store makes a
  dead process's key abandoned rather than in doubt. It is refused without it, and for an
  unkeyed tool.
- **It is not exactly-once:** the attempt may have taken effect, and the tool then runs twice.
  The state is right only if the declaration is true. The declaration is the evidence, as with
  any effect declaration.

**Tests:**
- the pinned memory server through the stdio relay, crashed `after_begin` and `after_effect`:
  declared, the call is `executed` and the observation exists once; undeclared, `unknown`;
- the mcp 2.x injector with a set-like tool: run again, then replayed;
- an undeclared tool under the same store stays in doubt;
- both refusals;
- the library's recovery verdicts.

### S5: crash windows of a declared idempotent tool (`benchmarks/evidence/realstdio/S5`, code `bc03223`)

**Setup:** memory server, every write cell (fault-free, 4 transport faults, 3 crash points), 12
trials.

| Arm | transport | crash | faulted, all |
|---|---|---|---|
| sg_keyed | 0/48 | **24/36** (`after_begin`, `after_effect`: unknown) | 24/84 (28.6%) |
| **sg_keyed_idempotent** | 0/48 | **0/36** | **0/84 (≤ 4.4%)** |

- **Both crash windows now succeed:** 24 rows answered `executed`, with the marker in the
  memory file exactly once. `after_record` still replays.
- Fault-free 0; 0 unsafe outcomes, 0 duplicates; all 192 relay records agree with ground truth.
- **On the memory server, with the tool declared idempotent, the keyed relay's residual on this
  matrix is 0/84.** That holds for this synthetic matrix and this tool only.
- sqlite's `INSERT` is not idempotent, so its crash windows stay in doubt (S4: 24/132 per
  server). A non-idempotent tool still needs a key-bound check or a transactional effect.

### C1: server crashes under the HTTP relay (`benchmarks/evidence/realhttp/C1`)

**New harness cells** (`9692d4d`): `write/after_begin`, `after_effect` and `after_record` on
notes and tsnotes.
- The server SIGKILLs itself at the point inside `add_note`: keyed arms through the key's two
  phases, unkeyed arms around the unmodified handler. A supervisor restarts it on the same
  port.
- The cells bypass the fault proxy. A dry run showed that a proxy in front of a dead server
  turns "refused" into "lost", which distorted the relay's recovery.
- The HTTP driver now scores a keyed server's `-32010`/`-32011` as unresolved, not as a false
  failure, as the stdio driver already did. No recorded HTTP row is affected.

| Arm, per server (12 trials per cell) | after_begin | after_effect | after_record, before D12 (`9692d4d`) | after_record, D12 (`51f6fdf`) |
|---|---|---|---|---|
| raw, naive | 12/12 | 12/12 | 12/12 | 12/12 |
| sg (unkeyed) | 12/12 unknown | 12/12 unknown | 12/12 unknown | 12/12 unknown |
| sg_keyed, sg_keyed_deadline | 12/12 unresolved (in doubt, a few unknown) | 12/12 unresolved (in doubt, a few unknown) | **3/12 (notes), 2/12 (tsnotes)** | **0/12 both** |

- **No StateGuard arm ever duplicated a note or misreported one:** 0 unsafe outcomes across
  360 StateGuard rows. Every relay record agrees with ground truth.
- **The floor:** `after_begin` and `after_effect` stay unresolved for this non-idempotent tool,
  as on stdio. D11 does not apply to it.

### D12: back off before a resend after a broken connection (`51f6fdf`)

- **The failure (C1, `after_record`):** the relay resent the keyed call at once, and the
  connection reached the server that was still going down. It was accepted and reset, which
  spent the second and last keyed attempt before the restarted server could replay.
- **The change:** the relay waits `--resend-backoff` (default 0.2 s) before a resend after a
  transport loss. Only the timing changes: what may be resent is decided as before.
- **Result:** keyed `after_record` went from 5 of 24 failing to 0 of 24 (C1 `*_d12`). The HTTP
  relay, idempotency and TypeScript adapter tests pass (98).
- **Cost:** a recovery after a dropped connection takes 0.2 s longer.

### B3: the HTTP matrix on the Session 2–3 code (`benchmarks/evidence/realhttp/B3`, code `51f6fdf`)

**Setup:** B2 repeated after D9 (open lines, `unfinished` records), D10 (waiting on "still
running") and D12 (resend backoff). Same servers, arms, 12 trials, 60-trial controls and 4
workers.

- **Every failure, unsafe, unresolved and induced count matches B2 cell for cell**, on all six
  parts:
  - notes and tsnotes: raw 168/168, naive 48/168 (48 duplicates), sg 84/168, sg_keyed 24/168,
    sg_keyed_deadline 0/168;
  - everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84;
  - fault-free 0.
- **Every relay row has a record** (D9 at full scale): 192/192 per arm on notes and tsnotes,
  where B2 had 172–177. All 2,184 records agree with ground truth.
- **None of the three changes moved a cell of this matrix.** D10 and D12 act on cells it does
  not have: a slow tool, a server crash. E1–E3 and C1 are their evidence.

## Session 4 (Fri 2026-11-13, 09:00–14:00 IST): hardening, and the two deadline decisions

### Decisions (delegated by the founder on 2026-11-11, recorded in `NEXT.md`)

1. **One meaning for `--call-deadline` in both relays: when to try again, never a point at
   which a call still running is answered as failed.**
   - An induced failure is the one outcome Phase 1 forbids.
   - stdio's old default answered "unknown" on every slow success.
2. **The HTTP relay gets a default `--call-deadline` of 30 s, for recoverable requests only.**

Both are **behaviour changes** to the defaults.

### Decision 2: the HTTP default deadline (`4c2256d`)

- **The default:** with `--retry auto`, a keyed call, a declared read or a request that changes
  nothing, still unanswered after 30 s, is sent once more. The first attempt stays open and the
  first answer that settles it wins. `--call-deadline 0` turns it off.
- **Records:** when the original answers first, the call was slow, not faulted. Its record says
  `answered`, with `deadline_resends: 1`, not a `deadline` fault (this fixes the E3 incidence
  caveat).
- **Harness:** benchmark arms without `_deadline` pass `--call-deadline 0`, so B1–B3 stay
  comparable.

**E4** (`benchmarks/evidence/realhttp/E4`, code `570d262`; notes, 12 trials):

| Cell | Arm | Result |
|---|---|---|
| `write/hang_after` | sg_keyed_default (no flag) | **0/12**, replayed at 30.8–31.0 s, written once |
| `write/hang_after` | sg_keyed (`--call-deadline 0`) | 12/12 unresolved |
| `write/slow` (10 s) | sg_keyed_default | 0/12, answered, no resend, no fault recorded |

0 record mismatches.

### Decision 1: the stdio deadline is when to try again (`2d8a676`)

- **`--deadline-scope recoverable` is now the default.** Only calls the relay could resend
  (declared reads, honoured keys) are resent at `--call-deadline`.
  - The resend is never cut short, and the first answer of either attempt is the client's.
  - A call past its deadline is answered "unknown" only once its server is replaced or gone
    for good, or after `--answer-timeout` (new, default 300 s).
  - A keyed resend answered "still running" waits for the original.
  - Calls the relay cannot resend wait for their answer.
- **`--deadline-scope all` keeps the old hard bound.**
- **Tests:**
  - a keyed call slower than two deadlines is answered and executed once, and is "unknown"
    under `all`;
  - an unkeyed slow call is never cut short;
  - a hung keyed call still running is "unknown" after the answer timeout.
  - The stdio half of D10 (2026-11-09) is resolved this way.

### S6: the stdio matrix on the new default, and slow answers (`benchmarks/evidence/realstdio/S6`, code `ba310ac`)

**Matrix** (every default cell, 12 trials): identical to S4 per server. sg 84/132, sg_keyed
24/132 (the two crash windows), fault-free 0.

**Slow answers** (the shim delays the answer 5 s, no fault; deadline 2 s; 12 trials):

| Arm, per server | read/slow | write/slow |
|---|---|---|
| raw | 0/12 | 0/12 |
| **sg** (new default) | 0/12 | **0/12** |
| sg_bound (`--deadline-scope all`, the old default) | 0/12 | **12/12 unknown, 12 induced vs raw** |
| sg_keyed, sg_keyed_bound | 0/12 | 0/12 (replayed) |

- **The old default turned every slow unkeyed write into "unknown", although the write
  succeeded.** That is an induced failure, and the new default removes it.
- 0 unsafe outcomes; all 816 relay records agree with ground truth.

### Regression across CI's Python matrix, on the Session 4 code (local; `8469759`)

| Python | Result |
|---|---|
| 3.11.18 | 6,589 passed, 0 failed, 0 skipped |
| 3.12.3 (first Phase 1 run on 3.12) | 6,589 passed, 0 failed, 0 skipped |
| 3.14.6 (installed world-readable, as CI's setup-python is) | 6,589 passed, 0 failed, 0 skipped |

3.13 runs in the session's final suite. These runs are local, not CI's: CI has not run on this
branch since `ec339ab`. macOS is not run.

### S7: the stdio matrix on a third pinned server, server-filesystem (`benchmarks/evidence/realstdio/S7`, code `8469759`)

**Setup:** `@modelcontextprotocol/server-filesystem` 2026.8.31 (TypeScript, keyed by the
preload); every default cell, 12 trials.
- read: `read_text_file`, declared read_only;
- write: `move_file`, declared non_idempotent;
- ground truth: the destination exists.

| Arm | transport | crash | faulted | unsafe |
|---|---|---|---|---|
| raw | 96/96 | 36/36 | 132/132 | 36 false failures |
| naive (blind resend) | 24/96 | 24/36 | 48/132 | **48 false failures** |
| sg | 48/96 | 36/36 | 84/132 (63.6%) | 0 |
| **sg_keyed** | **0/96 (≤ 3.9%)** | 24/36 | **24/132 (18.2%)** | **0** |

- **StateGuard's residual is the same as on sqlite and memory:** sg 84/132; sg_keyed 24/132,
  only the two crash windows, unresolved.
- **The blind resend fails differently:** a second `move_file` finds its source gone. Where
  sqlite showed 48 duplicates, the filesystem shows 48 false failures: the client is told an
  error for a move that happened. Both are unsafe outcomes; StateGuard arms had neither.
- Fault-free 0; all 312 relay records agree with ground truth.
- `move_file` is not idempotent, so D11 does not apply. Its crash windows stay in doubt.

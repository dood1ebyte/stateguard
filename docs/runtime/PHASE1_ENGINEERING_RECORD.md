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

### Next: retrying `-32603` where it is safe

`decide_retry` treats every JSON-RPC error as "the server rejected the request; it would
again". That is right for -32600, -32601 and -32602. A `-32603` (internal error) may be
transient. Retrying it once is safe for a declared read, a declared idempotent tool and a keyed
call, and never for an unkeyed non-idempotent write: an internal error may come after the
effect.

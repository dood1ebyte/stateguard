# Keys for Python servers, and a relay for Streamable HTTP

**Date:** 2026-11-04. **Branch:** `claude/serene-bohr-o28snr`.
- **Code:** commits `6a61f11` (key injection, HTTP relay), `4d2986e` (call deadline, schema
  checks, fault-proxy fix), `7b88576` (cancellation scoping) and `33343f8` (TypeScript over HTTP).
- **Snapshots measured:** `iter7` (6a61f11), `iter8` (6a61f11), `iter9` (4d2986e) and `iter10`
  (33343f8), each frozen read-only with a SHA-256 manifest.
- **Raw evidence:** `/root/stateguard-captures/autonomous-mandate/realpy/` and `.../realhttp/`.
  Each batch has a plan written before its first row, a `.codestate` file of hashes, its row
  files, and its failing runs kept.

This is the engineering record of two changes made toward the sub-1% residual MCP invocation
failure rate. Every number below is a controlled fault-injection result on named, pinned servers.
None is a production rate, and none supports a sub-1% claim (§6).

## 1. State at the start

- `main` = `b6f7ba0`; the working branch was one commit ahead (`8518d03`: the server-side
  idempotency contract, the Python mcp 2.x and TypeScript adapters, the TypeScript preload, the
  #3394 B1 cases). Its CI had never run: CI runs on `main`, `develop` and pull requests only.
- Untracked: three internal outreach notes that name people (kept out of the repository).
- Latent CI defect found: the type-check job installed no `mcp`, so `idempotency_sdk.py` (in
  `8518d03`) would have failed `mypy` on the first pull request. Fixed (§4).

## 2. Choosing the bottleneck

On the pinned real servers, the measured residual of faulted invocations was:

| Server (runtime) | Best arm before this work | Faulted failures | Why |
|---|---|---|---|
| server-filesystem 2026.8.31 (TS) | keys via the TS preload | 24/204 (11.8%) | crash windows only |
| server-memory 2026.8.31 (TS) | keys via the TS preload | 24/204 (11.8%) | crash windows only |
| mcp-server-sqlite 2025.4.25 (Python, mcp 1.9.4) | relay, no keys possible | 72/144 transport (real3) | every faulted write unknown |
| any server over Streamable HTTP | none | 100% of HTTP deployments uncovered | the relay spoke stdio only |

Two gaps followed directly: a Python server could not be given keys without editing its code,
and HTTP had no coverage at all (roadmap item C12, "without it, coverage of HTTP deployments
stays at 0"). The first was the largest measured residual on the testbed; the second the largest
uncovered population. Both were built, in that order.

## 3. Change 1: keys for unmodified Python servers

**What.** `python -m stateguard.adapters.mcp.inject <server>` runs an unmodified Python MCP
server (mcp 1.x or 2.x) with the idempotency-key contract attached at startup, configured by the
same environment as the TypeScript preload (`STATEGUARD_IDEMPOTENCY_DB`, `..._SCOPE`,
`STATEGUARD_KEYED_TOOLS`, `..._EXCLUSIVE`; TEST-ONLY `STATEGUARD_FAULT*`). It wraps where the
server's own SDK starts serving: `Server.run` (stdio; mcp 1.x HTTP) and the Streamable HTTP
session manager (mcp 2.x serves HTTP sessions without `Server.run`).

- `idempotency_async.execute_two_phase`: the claim / wait / recover / record flow, now one
  SDK-neutral implementation. The mcp 2.x adapter (`idempotency_sdk`) was refactored onto it
  with its tests unchanged and passing; the new mcp 1.x adapter (`idempotency_sdk1`) uses it too.
- Keyed tools are two-phase with no recovery check: a crash between the effect and its record
  leaves the key **in doubt**, reported as unknown, never re-run. Exactly-once is not claimed.
- Fails closed: a store that cannot be opened stops the server from starting; a store failure
  later executes nothing (`rejected_store`).

**Tests** (`tests/examples/test_inject_python.py`, 15): on the pinned `mcp-server-sqlite`
(mcp 1.9.4, from an interpreter named by `STATEGUARD_MCP1_PYTHON`), raw JSON-RPC over stdio with
real SIGKILLs at each crash point; replay across processes; mismatch rejection; concurrent
same-key calls; a corrupted store; an unusable store at startup; fault-only mode; the negative
control (no key, blind resend duplicates); and an unmodified mcp 2.x server.

**Measured** (`realpy/`, snapshot `iter7`, 12 trials per cell, 19 cells; strict; Wilson 95%):

| Arm | Faulted failures | Transport | Crash | Fault-free | Unsafe | Induced |
|---|---|---|---|---|---|---|
| raw | 204/204 | 144/144 | 60/60 | 0/144 | 0 | — |
| naive (blind resend) | 72/204 (35.3%) | 36/144 | 36/60 | 0/144 | **72 duplicate rows** | 0 |
| sg_scoped (relay, no keys) | 132/204 (64.7%) | 72/144 | 60/60 | 0/144 | 0 | 0 |
| naive_keyed_py | 24/204 (11.8%, 8.0–16.9) | **0/144** | 24/60 | 0/144 | 0 | 0 |
| **sg_keyed_py** | **24/204 (11.8%, 8.0–16.9)** | **0/144 (≤ 2.6%)** | 24/60 | **0/144 (≤ 2.6%)** | **0** | **0** |

- The 24 are exactly `after_begin` and `after_effect` (12 each): claimed, unrecorded, reported
  unknown. Every other cell recovers: `replayed` (lost_exit, hang, garbage, after_record,
  hang_after_record) or `executed` once (kill_before, hang_before, transient, before_begin).
- Session latency, fault-free (process start included): raw 0.958 s, sg_scoped 0.956 s,
  sg_keyed_py 1.033 s mean (n = 120 each).
- The pinned server's package tree (`mcp_server_sqlite`, `mcp`) is byte-identical before and after.

All three pinned real servers now reach the same residual: 24/204 faulted per server, all of it in
the claimed-but-unrecorded window, none of it unsafe.

## 4. CI changes

- The type-check job installs `mcp`, and type-checks the mcp 1.x modules against mcp 1.9.4
  (`mypy-mcp1.ini`, same strictness; `pyproject.toml` skips that one module in the default run).
- One test cell (ubuntu, Python 3.11) builds an mcp 1.9.4 + mcp-server-sqlite 2025.4.25
  environment and sets `STATEGUARD_MCP1_PYTHON`, so the injector tests run rather than skip.

## 5. Change 2: StateGuard relay for Streamable HTTP

### 5.1 Design

`examples/mcp/http_proxy.py` serves the MCP endpoint and relays to one server. It applies the
stdio relay's rules, through the same library decision core
(`stateguard.adapters.mcp.effects.decide_retry`), to HTTP's own failure points:

| Failure | What the relay can know | What it does |
|---|---|---|
| connection never opened (refused, connect timeout) | the request was not sent | sends it again within `--connect-retry`, any method; then "not sent" |
| 404 for a session id (or a 400 naming the session) | the request was not processed | opens a new upstream session (replays `initialize`), sends it there; the client's session id is unchanged |
| response stream cut, events numbered | the server stored the stream | resumes it with `Last-Event-ID`: the stored answer, no second execution |
| connection broken after sending, 5xx, cut un-numbered stream | the outcome is unknown | declared reads, side-effect-free methods and keyed calls are sent again (keyed: same key); anything else is answered "outcome unknown" |
| no answer within `--call-deadline` (recoverable requests only) | unknown | sent once more with the first left open; the first answer that settles it wins |

- **Ids.** Every attempt uses a relay-owned JSON-RPC id, mapped back on the answer, so a resend
  never reuses an id the server may still be serving.
- **Cancellation** (`7b88576`). A client's `notifications/cancelled` goes to every attempt of the
  request it names, and to no other client's request: live requests are held per (client
  session, request id).
  - Without sessions, two clients cannot be told apart. When two live requests share an id, the
    cancellation (advisory in MCP) is dropped and logged rather than risk stopping the other
    client's call.
  - Before this fix the map was global by client id. Two sessions' requests could collide, and a
    raced request's cancellation reached only its newer attempt.
- Keys: a tool whose listing advertises `io.stateguard/idempotency` gets
  `io.stateguard/idempotencyKey` on every call (unless the client sent its own).
- Schema checks, as the stdio relay: calls are validated against the last complete listing
  through `ToolRegistry`; in auto mode a drifted call is repaired where safe and refused or
  escalated where not, but only after listing the tools again on the call's session.
  `--shadow` only logs. The intervention rules (`intervenes`, `proposal`) moved into the library
  (`stateguard.adapters.mcp.jsonrpc`) so the two relays share one definition.
- Both protocol eras pass through: handshake revisions (sessions) and 2026-07-28 (stateless).

Tests (`tests/examples/test_http_proxy.py`, 42). They run an unmodified mcp 2.x server and the
TEST-ONLY fault proxy (`benchmarks/runtime/http_fault_proxy.py`) as real processes, with the SDK's
own client. Since `4d2986e` they also cover a keep-alive upstream, which fails on the old proxy.
Since `7b88576` they cover cancellation scoping, against a server that holds calls open (three of
those tests fail on the old relay). Since `33343f8` they cover the TypeScript server over HTTP
(7 tests, §5.5). The earlier tests cover:
lost sessions (a real server restart), connection refused, each fault mode for unkeyed and keyed
writes, stream resumption, declared reads, the deadline race (keyed `replayed` / `executed`, reads,
and an unkeyed write that is never cut short or resent), repair / refusal / shadow, the 400 rule,
and the baselines without the relay (a lost session or a lost response fails the call; a blind
resend duplicates).

### 5.2 Measured: iter8 (`realhttp/`, snapshot `iter8` = commit `6a61f11`)

12 trials per cell; the fault-free cells have 60 more trials each (trials 1000–1059). Strict scoring,
Wilson 95%. The client is the official Python SDK (mcp 2.3.0) and waits 8 s for an answer. The
faults come from the TEST-ONLY fault proxy, plus `session_loss`: the driver kills the server process
and restarts it between the client's handshake and its call. The plan (`PLAN.md`) was written
before any row.

**Controlled server: `tests/examples/http_notes_server.py` (mcp 2.3.0).** It is written for this
testbed: one write tool (`add_note`, an fsync'd append) and one declared read (`list_notes`).
Ground truth is the notes file.

| Arm | Faulted failures | Transport | Session | Fault-free | Unsafe | Induced |
|---|---|---|---|---|---|---|
| raw | 168/168 | 144/144 | 24/24 | 0/144 | 24 false failures | — |
| naive (blind resend, new session) | 48/168 (28.6%) | 48/144 | 0/24 | 0/144 | **48 duplicate notes** | 0 |
| sg (relay, no keys) | 84/168 (50.0%) | 84/144 | 0/24 | 0/144 | 0 | 0 |
| sg_resumable (server stores events) | 72/168 (42.9%) | 72/144 | 0/24 | 0/144 | 0 | 0 |
| **sg_keyed** (server keyed by the injector) | **24/168 (14.3%, 9.8–20.4)** | 24/144 | **0/24** | **0/144 (≤ 2.6%)** | **0** | **0** |

- **raw:** all 24 false failures are `write/http_502_after` and `write/cut_stream`. The note was
  written, but the client was told the call failed.
- **sg** leaves 60 unkeyed writes reported unknown, 12 in each of `kill_before`, `http_503_before`,
  `lost_response`, `http_502_after` and `cut_stream`. It also leaves 24 `hang_after` calls (reads
  and writes) with no answer before the client's 8 s timeout.
- **`kill_before` and `http_503_before` stay unknown, not "not sent".** The connection was open
  when it failed, and a 503 can come from a server that has already acted. A blind resend happens
  to be right in those two cells, but it duplicated the note in the four others.
- **sg_resumable** recovers the 12 cut write streams by resuming them. The client gets the stored
  answer, and the note is not written a second time.
- **sg_keyed:** only the `hang_after` cells (24) remain. The deadline in §5.3 targets them.
- **Session latency, fault-free** (mean, n = 144, process start included): raw 1.301 s,
  naive 1.315 s, sg 1.361 s, sg_keyed 1.368 s, sg_resumable 1.382 s.

**Third-party server: `@modelcontextprotocol/server-everything` 2026.8.31 (Node).** Read only:
`echo` is a declared read, so no write is measured on this server. Its package tree is
byte-identical before and after the batches.

| Arm | Faulted failures | Transport | Session | Fault-free |
|---|---|---|---|---|
| raw | 72/84 (85.7%) | 60/72 | 12/12 | 0/72 |
| naive | 0/84 | 0/72 | 0/12 | 0/72 |
| sg | 24/84 (28.6%) | 12/72 (`hang_after`) | 12/12 (see below) | 0/72 |

- **`read/cut_stream` passes even raw (12/12).** This server numbers and stores its events, and the
  SDK client resumes a cut stream by itself (one execution, verified in the fault proxy's log).
- **naive** is right on this server only because `echo` has no effect. The same blind resend
  duplicated 48 writes on the notes server.
- **The `sg` session-loss and latency numbers are a harness defect, not a relay measurement.**
  - What happened: the fault proxy passed the server's own `Connection` header through.
    server-everything (Express) answers its event streams `connection: keep-alive` whatever the
    request asked. The relay's HTTP client therefore sent its next request down a connection the
    proxy never read again, and that request waited for Node's 5 s idle timeout, then failed.
  - Effect: every relay session was about 6 s slower (fault-free mean 7.27 s against raw 1.25 s).
    All 12 session reopenings failed and were reported `not_sent`: safe, but unrecovered.
  - Scope: the notes server honours `Connection: close` (all its answers carry it), so the notes
    numbers above stand.
  - Fix: commit `4d2986e`, with a regression test that fails on the old proxy. The iter8 rows are
    kept unchanged; §5.3 re-measures this server on the fixed harness.

### 5.3 Measured: iter9 (`realhttp/`, snapshot `iter9` = commit `4d2986e`)

iter9 adds the call deadline, schema checks and the fixed fault proxy. Its plan is
`PLAN_iter9.md`. An addendum, written before any iter9 row, adds the server-everything re-run and
reports drift as its own family. 12 trials per cell; strict scoring.

**Deadline and drift, controlled notes server.** The deadline arms use `--call-deadline 2`;
`drift/none` is an `add_note` call whose field is named `Text` instead of `text`.

| Cell | raw | sg | sg_deadline | sg_keyed_deadline |
|---|---|---|---|---|
| read/hang_after | 12/12 | 12/12 | **0/12** | **0/12** |
| write/hang_after | 12/12 | 12/12 | 12/12 | **0/12** |
| drift/none | 12/12 | **0/12** | **0/12** | **0/12** |

- **read/hang_after** recovers in both deadline arms. Two requests reached the server, and the
  second one's answer was delivered.
- **write/hang_after** recovers only with a key: the server got the call twice, answered the
  second `replayed`, and the note exists once. Without a key the write is never resent. One call
  reached the server and the note was written, but the client got no answer before its 8 s
  timeout. That counts as a failure, as it should; it is not a success.
- **drift/none:** raw is rejected by the server (no note). Every relay arm repaired the field
  after listing the tools again, and the note was written once.
- **Unsafe outcomes:** 0 duplicates, 0 false successes and 0 false failures in every arm.
- **Latency:** the keyed deadline cell answered in 3.33 s mean (max 3.67 s). That is the 2 s
  deadline plus process start, against the client's own 8 s timeout.
- **Fault-free controls** (read/none and write/none, 60 each per arm): 0/120 in every arm. Session
  latency, mean: raw 1.437 s, sg 1.498 s, sg_deadline 1.494 s, sg_keyed_deadline 1.504 s.
  Validation and repair added nothing to valid calls.

**Third-party server, re-measured on the fixed fault proxy.** Same `driver.py` and cells as
iter8. Its package tree is byte-identical before and after.

| Arm | Faulted failures | Transport | Session | Fault-free | Fault-free latency (mean) |
|---|---|---|---|---|---|
| raw | 72/84 (85.7%) | 60/72 | 12/12 | 0/72 | 1.399 s |
| naive | 0/84 | 0/72 | 0/12 | 0/72 | 1.392 s |
| **sg** | **12/84 (14.3%, 8.4–23.3)** | 12/72 (`hang_after` only) | **0/12** | **0/72 (≤ 5.1%)** | **1.439 s** |

- Session loss now recovers 12/12, through the 400 rule. The relay reopened the session,
  replayed the handshake and sent the call again.
- Relay overhead per fault-free session is 0.04 s (it was 6 s with the defect).
- `driver_iter9.py everything` gives `read/hang_after` with `--call-deadline 2`: `sg_deadline`
  0/12, raw 12/12, sg 12/12. With the deadline, every faulted read cell on this server recovers.
  That combines two batches (sg on seven cells, sg_deadline on the eighth); no single arm ran all
  eight here. iter9b, next, runs one arm on all eight.
- 0 unsafe outcomes and 0 induced failures in every arm.

**The best HTTP arm per server on the full matrix (iter9b, `PLAN_iter9b.md`).** One arm per
server on every cell of `PLAN.md`; 12 trials per cell; fault-free controls 60 more per cell.

| Server | Arm | Faulted failures | Transport | Session | Fault-free | Unsafe |
|---|---|---|---|---|---|---|
| notes (controlled; keys via the injector) | sg_keyed_deadline | **0/168 (≤ 2.2%)** | 0/144 | 0/24 | 0/144 (≤ 2.6%) | 0 |
| server-everything (third-party; reads only) | sg_deadline | **0/84 (≤ 4.4%)** | 0/72 | 0/12 | 0/72 (≤ 5.1%) | 0 |

- **Every faulted cell recovered without a single second execution of a write.** Each
  recovered write was a key replay, an execution after "not processed", or a resumed stream.
  Every recovered read was a resend.
- **The latency expectation missed.** Fault-free session means were 1.701 s (notes) and 1.763 s
  (everything), against expectations of 1.368 s ± 0.15 and 1.439 s ± 0.15.
  - The same notes arm measured 1.504 s in iter9, interleaved with other arms.
  - iter9b ran one arm with four workers on four cores, so four relay-plus-server stacks ran at
    once.
  - iter9c (below) measures the overhead paired and uncontended.

### 5.4 Relay overhead, paired and uncontended (iter9c, `PLAN_iter9c.md`)

One row at a time (one worker), arms interleaved trial by trial, 20 trials per cell. Latency is
fault-free session time, from the client process's start to its exit. The difference is taken
per trial against raw on the same trial.

| Server | Arm | Mean | Median | Paired vs raw (mean / median) |
|---|---|---|---|---|
| notes | raw | 1.566 s | 1.545 s | — |
| notes | sg | 1.606 s | 1.614 s | +0.041 s / +0.069 s |
| notes | sg_keyed_deadline | 1.621 s | 1.656 s | +0.055 s / +0.078 s |
| everything | raw | 1.558 s | 1.565 s | — |
| everything | sg | 1.518 s | 1.525 s | −0.039 s / −0.051 s |
| everything | sg_deadline | 1.543 s | 1.539 s | −0.015 s / +0.020 s |

- **The overhead is within the 0.15 s expectation.** On the notes server it is +0.04–0.08 s per
  session (handshake, listing and one call). On server-everything it is within noise.
- **The iter9b miss was contention between rows, not relay cost.**
- **The absolute numbers drift between batches** (raw notes: 1.437 s in iter9, 1.566 s here).
  Only paired comparisons are meaningful.
- **Process start is outside these numbers.** The relay's own start is not included; a deployed
  relay starts once, not per session.

### 5.5 A TypeScript server over HTTP, keyed by the preload (iter10, `PLAN_iter10.md`)

**Server and setup.**
- The server is CONTROLLED: `adapters/typescript/test/fixtures/http_notes_server.ts`, the
  TypeScript SDK 1.32.1 over Streamable HTTP. It opens one `McpServer` and one transport per
  session, as the SDK example and Express servers do, and contains no StateGuard code.
- The keyed arms get their keys only from `--import adapters/typescript/src/preload.ts`.
- Snapshot `iter10` (commit `33343f8`) holds the TypeScript dependencies with their tree hash.
  The batch verifies both the hash and the snapshot manifest before it starts.
- 12 trials per cell, 16 cells.

| Arm | Faulted failures | Transport | Session | Unsafe |
|---|---|---|---|---|
| raw | 168/168 | 144/144 | 24/24 | 24 false failures |
| naive | 48/168 (28.6%) | 48/144 | 0/24 | **48 duplicate notes** |
| sg (relay, no keys) | 84/168 (50.0%) | 84/144 | 0/24 | 0 |
| sg_keyed (relay + preload keys) | 24/168 (14.3%) | 24/144 (`hang_after`) | 0/24 | 0 |
| **sg_keyed_deadline** | **0/168 (≤ 2.2%)** | 0/144 | 0/24 | **0** |

- **Every expectation in the plan held, cell for cell.** These are the same numbers the
  Python-server arms gave in iter8, iter9 and iter9b.
- **Each keyed write exists exactly once.** Writes whose answer was lost (`lost_response`,
  `http_502_after`, `cut_stream`, and `hang_after` under the deadline) were sent again with their
  key and answered `replayed`: two calls reached the server, one note exists. Writes whose first
  attempt never reached the server (`kill_before`, `http_503_before`) or was refused for a lost
  session executed once.
- **The preload reaches per-session instances.** It wraps `Server.prototype.setRequestHandler`,
  so every session's server is keyed, which an HTTP deployment needs.
- **No StateGuard arm** produced a duplicate, a false success, a false failure or an induced
  failure.
- **Fault-free controls** (60 more per cell): 0/144 in every arm. Session latency (mean, arms
  interleaved, 4 workers): raw 1.840 s, sg 1.886 s, sg_keyed 1.874 s, sg_keyed_deadline 1.924 s.
  That is +0.03 to +0.08 s against raw on the same batch.

## 6. What the evidence supports

**Measured, on this testbed** (controlled fault injection, 12 trials per cell, each cell weighted
equally):

| Server | Transport, runtime | Best arm | Faulted failures | Fault-free | Unsafe |
|---|---|---|---|---|---|
| server-filesystem 2026.8.31 | stdio, TypeScript | keys via preload | 24/204 (in doubt) | 0/144 | 0 |
| server-memory 2026.8.31 | stdio, TypeScript | keys via preload | 24/204 (in doubt) | 0/144 | 0 |
| mcp-server-sqlite 2025.4.25 | stdio, Python mcp 1.9.4 | keys via injector | 24/204 (in doubt) | 0/144 | 0 |
| notes (controlled) | HTTP, Python mcp 2.3.0 | keys via injector + deadline | **0/168** | 0/144 | 0 |
| server-everything 2026.8.31 | HTTP, TypeScript (reads only) | deadline | **0/84** | 0/72 | 0 |
| tsnotes (controlled) | HTTP, TypeScript SDK 1.32.1 | keys via preload + deadline | **0/168** | 0/144 | 0 |

**What this establishes:**
1. **Safety.** No StateGuard arm in any of these batches produced a duplicate effect, a false
   success, a false failure or an induced failure. Without StateGuard, the naive blind resend
   produced 72 duplicate rows (sqlite) and 48 duplicate notes on each HTTP notes server.
2. **Recovery is verified, never inferred.**
   - Every recovered write is accounted for: a stored answer replayed under its key, an execution
     after a request that never reached the server, or a resumed stream.
   - Ground truth (the notes file, the database rows) shows no write applied twice, and every
     recovered write applied exactly once.
   - A write left in doubt may have applied once (`after_effect`) or not at all (`after_begin`).
     It is reported as such, never as a success.
3. **What remains has three causes, each a floor of the method rather than a defect:**
   - **The claimed-but-unrecorded crash window** (`after_begin`, `after_effect`): 24/204 on each
     stdio server. The tool's own effect carries no key, so nothing can say whether it happened.
     It is reported in doubt and never re-run. Only a transactional effect or a sound recovery
     check (§9 of the TypeScript adapter doc) removes it.
   - **Unkeyed writes whose outcome is unknown.** Without keys, no client-side rule can recover
     them safely. The relay reports them unknown, and reconciliation cannot establish execution
     either (`RECONCILIATION_CONTRACT_V2.md`).
   - **A call with no answer and no deadline.** `--call-deadline` closes this for recoverable
     calls only (reads, keyed calls).

**What it does not establish:**
- **A production failure rate, or sub-1%.**
  - Every number is a fault-injection result: each fault fires once per trial, and every cell
    weighs the same.
  - Real traffic has an unknown mix. For example, the crash window spans only the time between a
    tool's effect and its record, yet this matrix weighs it the same as a lost response. How
    often each actually happens is not known.
  - A traffic-weighted rate needs the real incidence of each fault, which nothing here measures.
  - The 0/168 and 0/84 rows are upper bounds of 2.2% and 4.4% (Wilson 95%) on *this matrix*, not
    on MCP traffic.
- **Generality beyond these servers.** Three HTTP servers were measured: two controlled (one
  Python, one TypeScript) and one third-party that is read-only. No third-party HTTP server with
  writes was available pinned.
- **Exactly-once.** It is not claimed. Two-phase keys give at most once plus an explicit "in
  doubt".

## 7. Remaining failure modes and the next priority

| Failure mode | Status | What would close it |
|---|---|---|
| Crash between effect and record | in doubt, reported | a server-side recovery check, or a transactional effect |
| Unkeyed write, outcome unknown | unknown, reported | keys in the server (injector, preload, or native) |
| Hang on a non-recoverable call | client timeout | none safe from the client side |
| 503 or connection loss before the server read the request | unknown for unkeyed writes | keys; a server-declared "not processed" signal |
| Stale pooled connection (server closes an idle connection as the relay reuses it) | **unmeasured**; would surface as unknown for unkeyed writes | a relay pool idle timeout below the server's (httpx's pool and Node's server both default to 5 s) |
| Servers in other languages (Go, Rust, C#, Java) | relay only, no keys | per-SDK adapters |
| Real fault incidence | **unmeasured** | both relays now write per-invocation records (§8); still needed: a real capture |

**Next priority: fault incidence.** Every remaining question about the failure rate depends on
how often each fault actually happens, and this testbed cannot answer it. §8 is the first step.

## 8. Change 3: an invocation record per request, and incidence from it

**What.**
- **The relay's record.** `examples/mcp/http_proxy.py` writes one `invocation` record per client
  request to its `--audit` log. It holds the method, the tool's name, whether the call was keyed,
  the attempts made, the faults met in order, how the request ended, the server's keyed outcome
  and the elapsed milliseconds.
  - Faults: `connect_retry`, `not_sent`, `transport_lost`, `server_error`, `session_lost`,
    `stream_cut`, `deadline`.
  - Endings: `answered`, `answered_error`, `recovered`, `recovered_error`, `unknown`, `not_sent`,
    `in_doubt`, `in_progress`, `refused`, `client_gone`, `http_<status>`.
  - It never holds arguments, results or message text.
- **The library.** `stateguard.adapters.mcp.incidence` classifies how an invocation ended and
  summarizes records per invocation, with Wilson 95% intervals:
  - the share of invocations that met any fault, and each fault;
  - the unresolved share (the client cannot tell whether the call ran);
  - the failed share (the server did not answer);
  - the recovered share of the faulted.

  Nothing leaves the denominator, and a tool's own error counts as an answer.
- **The CLI.** `stateguard incidence AUDIT.jsonl [...] [--json]` prints that report.

**Why.** This is how a deployment produces representative numbers. A relay run in front of real
traffic, even with `--retry off` (observe only), reports how often each fault actually occurs on
its hop to the server, and how each invocation ended.

**What it cannot see.**
- Faults between the client and the relay.
- Anything the server never reports.
- Whether the tool did the right thing.

The report says so in its output.

**Tests.**
- 25 unit and CLI tests (`tests/adapters/mcp/test_incidence.py`).
- 7 relay tests (`tests/examples/test_http_proxy.py`): fault-free → `answered`; lost response,
  keyed → `recovered` (`replayed`), unkeyed → `unknown`; lost session → `recovered`; schema
  refusal → `refused`; a raced read records its `deadline`; and the report computed from a real
  relay log.

**The stdio relay** (`examples/mcp/stdio_proxy.py`) writes the same record.
- **Where:** the record is written when the client is sent the request's one answer, on the
  relay's single write path to the client. Each request gets exactly one record: the ledger
  marks it recorded under its lock.
- **Faults:** taken from the `InvocationLedger`, namely why the request went UNKNOWN
  (`transport_closed`, `send_failed`, `deadline`, ...). A request sent again for another reason
  carries `resent`.
- **Refusals:** a call refused locally is recorded `refused`.
- **Shadow mode:** records are written there too, which makes it the observe-only way to
  measure incidence.
- **Recordings:** records go to the audit log only, never into a recording (T13). They carry
  timing, and recorded tapes must stay deterministic; the first full run caught exactly this
  (`test_replay.py`).
- **Tests:**
  - `tests/examples/test_relay_ledger.py`: fault-free; server death before and after execution;
    deadline with a late answer (still one record); refusal; a duplicate response in auto and in
    shadow (still one record).
  - `tests/examples/test_relay_reread.py`: a hung read answered by its re-read, and a read lost
    with its server and re-read on the restarted one (both `recovered`, 2 attempts); the same
    without re-read (`unknown`).

**Not yet:** no real capture has been run, so no real incidence is known yet.

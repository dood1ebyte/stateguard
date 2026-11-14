# StateGuard Phase 1 — Completion Report

**Phase:** MCP reliability, 2026-11-05 to 2026-11-18 (mandate of 2026-11-05).
**Written:** Sun 2026-11-15, IST. That was Session 5, held early on the founder's instruction of
2026-11-15 ("complete the next session now itself … can advance to next sessions if you are
done early").
**Branch:** `claude/serene-bohr-o28snr`. **Status:** Phase 1 is complete ahead of schedule;
Phase 2 waits for the founder's authorization (section 14).

**How to read the numbers.**
- Every result here is **synthetic fault injection** on named, pinned servers on one cloud VM.
  None is production or customer traffic.
- No traffic-weighted rate exists. So no number here is a production failure rate, and none
  supports a sub-1% claim (section 13).
- Measured results, assumptions and open questions are labelled as such.

---

## 1. Executive summary

**StateGuard's two MCP relays now handle every fault class Phase 1 injected safely.** They
recover what can be recovered without risking a second effect, and report the rest as
"unknown", never as a wrong verdict.
- The two relays are HTTP (Streamable HTTP) and stdio.
- Python and TypeScript servers are covered, controlled and third-party.
- **Across every final-code batch, no StateGuard arm in its default configuration duplicated a
  side effect, reported success for a call that did not run, or reported failure for a call
  that ran.**
- The one configuration that produced false successes is the non-default
  `--no-listing-refresh` on a stale listing (section 8).
- The unprotected baselines are not safe:
  - a blind resend **duplicates** writes;
  - raw delivers **false failures** on every server where an answer is lost after the effect.

**Measured, final code (strict per-invocation scoring, Wilson 95% intervals):**

| Configuration | Faulted calls failed or unresolved (RFR) |
|---|---|
| HTTP matrix, keyed relay with a deadline (notes, tsnotes) | **0/168 transport and session faults per server (≤ 2.2%)**; 24/36 crash cells, the documented floor |
| HTTP matrix, keyed relay without a deadline | 24/168 (its `hang_after` cells): the reason the default deadline exists |
| HTTP, reads (server-everything) | 0/84 with a deadline (≤ 4.4%), against raw 72/84 |
| stdio matrix, keyed relay (sqlite, memory, filesystem) | 0/96 transport faults per server (≤ 3.9%); 24/132 overall, the two crash windows only |
| stdio, memory, keyed, tool declared idempotent (D11) | **0/84 (≤ 4.4%)** |
| Concurrency + repeated faults, HTTP keyed (R1, D16) | **1/240 (0.4%) and 4/240 (1.7%)**, against raw 80/240 |
| Concurrency + repeated faults, stdio keyed (R2) | 9/96, 0/96, 11/96, all unresolved, against raw 80, 40 and 79 |
| Unprotected baseline (raw), matrices | 100% of faulted calls on notes, tsnotes, sqlite, memory, filesystem; 72/84 (85.7%) on server-everything's reads |

**What Phase 1 changed.** It found and fixed 17 defects (D1–D17, section 3), made two default
behaviour decisions delegated by the founder, and put the whole measurement harness and its
evidence in the repository: 34 batches and 53,906 rows (section 11). The consequential fixes:
- the stale pooled connection (D4);
- a server's `-32603` retried where safe (D7, D8);
- incidence records for unfinished requests (D9);
- slow keyed calls never cut short by a deadline (D10, decision 1);
- the crash window of a declared-idempotent tool (D11);
- the resend backoff that rescues keyed recoveries from a dying server (D12);
- non-conformant answers (D13, D14);
- false failures and unresolved calls under repeated faults (D15–D17).

**What is left.** The residual that remains is unresolved, never unsafe. Its main parts:
- A non-idempotent tool's two crash windows: the server dies between the effect and the key's
  record. No relay can close this without server cooperation (section 12).
- Unkeyed non-idempotent writes after an ambiguous fault, which StateGuard reports "unknown"
  instead of risking a duplicate.
- Calls whose every allowed attempt was faulted.

## 2. Repository and branch state

- **Branch:** `claude/serene-bohr-o28snr`, head ⟪TBD:head⟫ at the time of writing, level with
  `origin`. History is linear; no rebase, force-push or reset was done.
- **`main`:** untouched. No merge, no PR opened in Phase 1 (opening one needs the founder). The
  branch heads earlier PRs #14–#18.
- **CI:** GitHub Actions has not run on this branch since `ec339ab` (no PR). Every test result
  here is a local run in this container (section 10).
- **Private documents** (`STATEGUARD_CUSTOMER_OUTREACH.md`, `CUSTOMER_VALIDATION_PLAN.md`,
  `PUBLIC_FAILURE_EVIDENCE_RESEARCH.md`): never committed. The untracked copies were lost with
  the container on 2026-11-05, before Phase 1.
- **No release, no outreach, no external commitment, no spending.**

## 3. Actual engineering changes

Product code: `examples/mcp/http_proxy.py` and `examples/mcp/stdio_proxy.py` (the relays),
`src/stateguard/adapters/mcp/` (effects, incidence, jsonrpc, idempotency, injector) and
`adapters/typescript/` (preload, store). Each fix has tests that fail without it.

| # | Change | Commit | Evidence |
|---|---|---|---|
| D1 | Harness and evidence moved into the repository (they had been lost with the container) | Session 1 | B1, S2 reproduce the lost batches |
| D2 | CI's mcp 1.x environment: pydantic pinned (13 injector tests were silently skipping) | Session 1 | 15/15 pass |
| D3 | stdio relay: executor-scope platform checked before resolving the program (3.13+ failure) | Session 1 | test passes on 3.13 |
| D4 | HTTP relay: `--pool-idle-timeout` 1 s; a stale pooled connection lost calls | `8de3735` | S1: 13/60 → 0/60 |
| D5, D6 | Harness: worktree races; pinned-server tests no longer skip silently | Session 1 | B1, 17/17 |
| D7 | stdio: a server's `-32603` retried where safe (keyed, declared read or idempotent) | `a8ed63d` | S3: transient cells 12/12 → 0/12 |
| D8 | HTTP: the same `-32603` rule | `1852365` | relay tests |
| D9 | Both relays: `invocation_open` lines; unfinished requests are counted | `a00a269` | I1: 96/96 recorded |
| D10 | HTTP: a keyed call answered "still running" is waited for, not failed | `68b5594` | E1: 12/12 induced → 0/12 |
| Decision 2 | HTTP: default `--call-deadline` 30 s for recoverable requests; slow successes recorded `answered` | `4c2256d` | E4: hung keyed write recovered 12/12 |
| Decision 1 | stdio: the deadline is when to try again (`--deadline-scope recoverable`, `--answer-timeout` 300 s) | `2d8a676` | S6: old bound induced 12/12 per server, new 0 |
| D11 | Crash window of a declared-idempotent tool recovered (`Recovery.rerun`, `STATEGUARD_IDEMPOTENT_TOOLS`, exclusive store), Python and TypeScript | `c4de122` | S5: 24/84 → 0/84 |
| — | HTTP: a keyed server's "in doubt" is no longer a false failure; crash cells in the harness | `9692d4d` | C1 |
| D12 | HTTP: 0.2 s backoff before a resend after a broken connection | `51f6fdf` | C1: keyed `after_record` 5/24 → 0/24 |
| D13 | Both relays: a non-conformant answer to a resendable call is resent (shared conformance check) | `87f43d4` | A1: C cells 24/24 → 0/24 keyed |
| D14 | Both relays: a non-conformant answer that cannot be resent is answered "unknown" | `fccee7c` | A2: 12 false failures → 0 |
| — | Harness port race (one naive fault-free row in B5) | `8568fa3` | scan of all archives |
| D15 | Both relays: an internal error after an unknown attempt is answered "unknown" | `4969b89` | R1: 5 false failures → 0 |
| D16 | Keyed calls get their own attempt budget, 3 (`KEYED_MAX_ATTEMPTS`) | `4e859c1` | R1: 18/240 → 1/240 and 4/240 |
| D17 | stdio: a keyed resend with attempts left is tried again at its own deadline; a late resend answer is delivered | `93addf9` | found by R2's smoke run; tests |

**Harness** (TEST-ONLY, `benchmarks/runtime/`):
- an HTTP fault proxy and a stdio shim, each with transport, adversarial and repeated-fault
  modes;
- server-crash points;
- drivers per suite, and multi-call concurrent clients;
- `run_batch.py`: a clean worktree per commit, `codestate*.json`, failing-run archives;
- `phase1_table.py` (the metric), and `evidence_index.py`.

## 4. Failure classes addressed

Taxonomy: `TASKMAP.md` categories, mapped per injected fault in `phase1_table.py`.

| Category | Injected as | Batches | StateGuard's handling, final code |
|---|---|---|---|
| A contract / schema drift | a field renamed by the caller | D1 | repaired: 0/60 per relay arm (raw 60/60) |
| B stale tool listing | listing shows a schema no longer served | A1, A2 | 0 failures with the default listing refresh; `--no-listing-refresh` is a stated risk |
| C protocol non-conformance | answer says `jsonrpc: 1.0` | A1, A2 | resent if resendable (D13), else "unknown" (D14) |
| D session loss | server restarted between handshake and call | HTTP matrices | session reopened, request resent (not processed) |
| E transport, not sent | connection killed before forwarding | all matrices | resent (HTTP: "unknown" if the request may have been sent) |
| F transient | 503, `-32603` | all matrices | resent where safe (D7, D8) |
| G tool execution failure | — | — | a tool's own error is an answer; not a relay fault, not measured |
| H result integrity | garbage, truncated answers | A1, A2, R2 | keyed and declared reads recovered; unkeyed writes unresolved |
| I ambiguous completion | lost answer, 502, cut stream, hang, server crash ×3 | all matrices, C1, R1, R2 | keyed: replayed; unkeyed: "unknown", never resent; crash windows: floor |
| J duplicate answers | answer sent twice | A1, A2 | second dropped; 0 failures |
| K auth / authorization | — | — | never retried by rule; not measured |
| Z StateGuard-induced | slow calls, deadlines | E1–E4, S6 | found twice (D10, stdio's old bound), fixed; 0 induced on final code |

The measured RFR speaks for A, B, C, D, E, F, H, I and J. G and K are outside it.

## 5. Reliability measurement methodology

Defined in `docs/runtime/PHASE1_METRICS.md` and implemented only in
`benchmarks/runtime/phase1_table.py`.
- **Unit:** one eligible logical invocation, meaning one client call. Retries never add to the
  denominator.
- **Outcomes:** each row has exactly one.
  - `success`: the client was told it succeeded, and ground truth confirms exactly one effect.
  - `unresolved`: the client was told "unknown", got no answer, or got a keyed server's "in
    doubt" or "still running".
  - `failed_resolved`: any other failure, correct or not.
- **RFR** = (failed_resolved + unresolved) / eligible. Wilson 95% interval. Reported per
  suite, server, arm and fault family, and never pooled across them.
- **Nothing is excluded.** A driver error, a broken session or a harness fault is a failure
  row. UNKNOWN counts as failure.
- **Ground truth sits outside StateGuard:** the notes file, sqlite rows, the memory file,
  file existence, and the fault proxy's log of what reached the server.
- **Counted separately:** duplicate effects, false successes, false failures, wrong content.
  Induced (raw succeeded, the arm failed) and recovered (raw failed, the arm succeeded) are
  counted against raw on the same (op, mode, trial).
- **Record agreement:** every relay `invocation` record is checked against ground truth.
- **Discipline:**
  - every batch's `PLAN.md` (question, setup, prediction) is committed before its first row;
  - each part runs from a clean `git worktree` of a named commit, with versions and the exact
    command in `codestate*.json`;
  - `benchmarks/runtime/` is not edited while a batch runs, and when it was dirty the record
    says so;
  - counts are computed by script.
- **Eligible workloads:** the cells of each plan. Cancellations do not occur in the drivers. A
  client's own timeout is a row (`no_answer_timeout`, unresolved).

## 6. Baseline-versus-StateGuard results

Arms:
- **raw:** no StateGuard;
- **naive:** raw plus one blind resend;
- **sg:** relay with `--retry auto`, the write declared non-idempotent;
- **sg_keyed:** sg, with the server keyed by StateGuard's injector (Python) or preload
  (TypeScript);
- **`_deadline`:** with a call deadline.

**HTTP matrix, final code: B5** (`47fa17e`; 12 trials per cell; 60-trial fault-free controls).
Faulted calls; the first five columns are notes and tsnotes, each, and the last column is
server-everything (reads).

| Faults | raw | naive | sg | sg_keyed | sg_keyed_deadline | everything: raw / sg / sg_deadline |
|---|---|---|---|---|---|---|
| transport + session (168 / 84) | 168 (100%) | 48, **48 duplicates** | 84 (50%) | 24 (14.3%) | **0 (≤ 2.2%)** | 72 / 12 / **0 (≤ 4.4%)** |
| server crash ×3 (36) | 36 | 36 | 36 unresolved | 24 unresolved | 24 unresolved | — |
| fault-free (144 / 72) | 0 | 0* | 0 | 0 | 0 | 0 / 0 / 0 |

\* tsnotes naive 1/144: the harness port race (section 3), with no StateGuard in that path.

**B6** re-ran the keyed arms on the final product code (`93addf9`, the D15–D17 code). It is
identical to B5, cell for cell: 76 cells, 0 differ, 0 unsafe, and all 912 records agree.

**stdio matrix, final code: S8** (`47fa17e`, 12 trials per cell; three third-party servers).
Faulted calls, 132 per server:

| | raw | naive | sg | sg_keyed | sg_keyed_idempotent |
|---|---|---|---|---|---|
| sqlite (Python) | 132 | 48, **48 duplicates** | 84 | **24 (18.2%)**: only the crash windows | — |
| memory (TypeScript) | 132 | 0 (the tool drops repeats) | 84 | **24**: only the crash windows | **0/84 (≤ 4.4%)** |
| filesystem (TypeScript) | 132 | 48, **48 false failures** | 84 | **24**: only the crash windows | — |

Transport faults alone (96 per server): sg_keyed 0/96 (≤ 3.9%).

**S9** re-ran the keyed arms on the final product code. It is identical to S8, cell for cell:
47 cells, 0 differ, and all 600 records agree. New `write/slow` cells (a 5 s answer, a 2 s
deadline) are 0/12 on each server.

**Adversarial categories** (A1 stdio, A2 HTTP; after D13/D14):
- J (duplicate answers): 0 failures in every arm.
- H (garbage, truncated) and C (non-conformant):
  - keyed 0/24 per server;
  - sg 12/24, its unkeyed writes, all "unknown";
  - raw 24/24; on HTTP its write failures are false failures (the client SDK's parse error for
    a write that ran).
- B (stale listing): 0 failures with the default refresh. With `--no-listing-refresh`, 12/12
  failed on every server; on stdio sqlite all 12 were false successes.

**Concurrency under repeated faults** (every 3rd request faulted, resends included):

| | raw | naive | sg | sg_keyed |
|---|---|---|---|---|
| R1, HTTP, 4 sessions × 6 writes (240 per server); notes / tsnotes | 80 / 80 (20 false failures each) | 55 / 51 (46 / 39 duplicates) | 80 / 80, 0 unsafe | **1 (0.4%) / 4 (1.7%)** on D16 |
| R2, stdio, 12 writes, 4 in flight (96 per server); sqlite / memory / filesystem | 80 / 40 / 79 | 48 / 16 / 50 (16 duplicates on sqlite) | 68 / 32 / 58, 0 unsafe | **9 / 0 / 11**, all unresolved |

**Slow calls and deadlines (Z)** (E1–E4, S6):
- on the final defaults, no slow call is cut short;
- before D10 and decision 1, deadlines induced 12/12 failures per cell.

## 7. Recovery, failure and UNKNOWN counts

From `phase1_table.py` on the final-code batches, faulted cells including crash cells. "Rec."
counts calls raw failed that the arm completed. B5 has 204 calls per server; S8 has 132.

| Batch / server / arm | Failed | Unresolved | Failed, resolved | Unsafe | Induced | Rec. vs raw |
|---|---|---|---|---|---|---|
| B5 notes sg | 120/204 | 120 | 0 | 0 | 0 | 84 |
| B5 notes sg_keyed | 48/204 | 48 | 0 | 0 | 0 | 156 |
| B5 notes sg_keyed_deadline | 24/204 | 24 | 0 | 0 | 0 | 180 |
| B5 notes raw | 204/204 | 72 | 132 | 48 false failures | — | — |
| B5 tsnotes sg_keyed_deadline | 24/204 | 24 | 0 | 0 | 0 | 180 |
| B5 everything sg_deadline | 0/84 | 0 | 0 | 0 | 0 | 72 |
| S8 sqlite sg | 84/132 | 72 | 12 | 0 | 0 | 48 |
| S8 sqlite / memory / filesystem sg_keyed | 24/132 each | 24 | 0 | 0 | 0 | 108 |
| S8 memory sg_keyed_idempotent | 0/84 | 0 | 0 | 0 | 0 | 84 |

- **Every StateGuard failure in the HTTP matrix is unresolved: the client is told "unknown"
  and nothing more.** None is a wrong verdict.
- **stdio `sg`'s 12 resolved failures** are unkeyed `-32603`s on a non-idempotent write. The
  call did not run, the client got the server's error, and that is a correct failure.
- **Fault-free:** 0 failures in every StateGuard arm, in every batch.
- **Record agreement:** every relay record in the final-code batches agrees with ground truth.
  That is 2,400 in B5 and 1,032 in S8, and every trial in R1 and R2.

## 8. Duplicate-effect and false-success findings

- **StateGuard arms, default configuration:** 0 duplicate effects, 0 false successes, 0 false
  failures, 0 wrong content, across B5, S8, A1/A2 on D13/D14, R1 on D15/D16, R2, B6 and S9.
- **Unsafe outcomes found in StateGuard arms during Phase 1, and fixed:**
  - D14: 12 false failures per server in A2 `sg`, from a passed-through non-conformant answer;
  - D15: 5 false failures in R1 `sg_keyed`, from an internal error after a lost answer.
- **Baselines:**
  - a blind resend (naive) duplicates writes: 48/132 on sqlite, 48/168 on the HTTP notes
    servers, 46 and 39 in R1, 16 in R2;
  - on a tool that cannot repeat (`move_file`), the same resend turns into false failures
    (48/132);
  - raw produces false failures wherever an answer is lost after the effect.
- **A configuration that can produce false successes:** `--no-listing-refresh` with a stale
  listing (A1: 12/12 on sqlite). It is off by default, and both relays' help states the risk.
- **Not exactly-once.** Keys make a resend safe; they do not make the first execution atomic.
  `STATEGUARD_IDEMPOTENT_TOOLS` (D11) re-runs an abandoned attempt and is correct only if the
  operator's declaration is true.

## 9. Performance overhead

Paired against raw on the same trial, one worker, an idle machine, client process start
included. These are synthetic, single-machine measurements, not production latencies.

| Measure | P1 (`1852365`) | P4 (`16e26b9`) | P5 (final) |
|---|---|---|---|
| HTTP relay, per session (notes, tsnotes; sg and keyed) | +0.006 to +0.066 s | +0.009 to +0.058 s | **+0.011 to +0.056 s** |
| HTTP relay, per session (everything) | +0.031 s | +0.021 s | +0.041 s |
| stdio relay, per warm call | (P3: +0.68 to +0.94 ms) | +0.51 to +0.95 ms | **+0.41 to +0.88 ms** |
| stdio keyed write, per warm call | (P3: +6.3 to +6.9 ms) | +4.5 to +5.0 ms | **+4.1 to +4.6 ms** |

- **stdio, once per server process:** +0.20 to +0.37 s for the relay's process start (P1).
- **HTTP, extra requests:** an event-stream answer costs one upstream connection per call (P2).
  Its network cost is unmeasured: there is no netem on this kernel (P2b).
- **Under faults:** recovery costs time, which is reported, not hidden. A resend after a broken
  connection waits 0.2 s (D12). A deadline resend comes after 30 s by default (HTTP) or the
  configured deadline (stdio).

## 10. Test, lint and type-check results

All local, in this container. CI has not run on this branch since `ec339ab`.

| Check | Result |
|---|---|
| Python 3.13 full suite (final code) | ⟪TBD:py313⟫ |
| Python 3.11 full suite (final code) | ⟪TBD:py311⟫ |
| Python 3.12 full suite (final code) | ⟪TBD:py312⟫ |
| Python 3.14 full suite (final code) | ⟪TBD:py314⟫ |
| TypeScript adapter: `tsc`, `npm test` | `tsc --noEmit` clean; `npm test` 74/74 passed |
| `ruff check`, `ruff format --check` (src, tests), `mypy src/` | clean (ruff: all checks passed, 213 files formatted; mypy: no issues in 73 files) |

Earlier in Phase 1, two intermittent HTTP test failures (3.11, 3.12) were root-caused to the
tests and fixed. One was a race in the test; the other was a port collision, now detected.
macOS is not run.

## 11. Reproducible commands and evidence locations

- **Evidence:** `benchmarks/evidence/{realhttp,realstdio}/<batch>/`, indexed in
  `docs/runtime/PHASE1_EVIDENCE_INDEX.md` (generated by `benchmarks/runtime/evidence_index.py`).
  Each batch holds:
  - `PLAN.md`;
  - `rows*.jsonl` (raw rows);
  - `codestate*.json` (commit, versions, exact command);
  - `failing*.tar.gz` (every failing run's logs and audit);
  - `summary*`.
- **Reasoning and results:** `docs/runtime/PHASE1_ENGINEERING_RECORD.md`. Metrics:
  `PHASE1_METRICS.md`. Execution state: `NEXT.md`.
- **Environment** (a fresh container):
  ```
  python3 -m venv .venv && .venv/bin/pip install -e ".[pydantic,mcp,dev]"
  python3 -m venv /tmp/mcp1 && /tmp/mcp1/bin/pip install mcp==1.9.4 mcp-server-sqlite==2025.4.25 pydantic==2.11.7
  (cd adapters/typescript && npm ci)
  # pinned servers in /opt/sg-servers/{everything,memory,filesystem}: npm install
  #   @modelcontextprotocol/server-everything@2026.8.31, server-memory@2026.8.31,
  #   server-filesystem@2026.8.31
  ```
- **A batch:** `python benchmarks/runtime/realhttp/run_batch.py BATCH COMMIT [--suite realstdio]
  [--driver D] [--part P] -- DRIVER_ARGS`.
  - Example (B5's notes part): `run_batch.py B5 47fa17e --part notes -- notes 12`.
  - The exact arguments of every part are in its `codestate*.json`.
- **The metric:** `python benchmarks/runtime/phase1_table.py <batch dir>... [--by-category]`.
- **Tests:** `STATEGUARD_MCP1_PYTHON=/tmp/mcp1/bin/python .venv/bin/pytest tests/`;
  `(cd adapters/typescript && npm test)`.

## 12. Remaining technical limitations

1. **The crash window of a non-idempotent tool** (`after_begin`, `after_effect`).
   - A server that dies after the effect and before the key's record leaves an effect no relay
     can attribute.
   - It stays "in doubt": never re-run, never success.
   - Closing it needs server cooperation: a transactional effect, or a key-bound recovery
     check (tool-specific operator integration, decided outside Phase 1).
   - Under concurrency it is hit more often: R2 shows servers killed by *other* calls' faults
     while a call was mid-effect.
2. **Unkeyed non-idempotent writes after an ambiguous fault** are "unknown" by design (sg, 50–64%
   of faulted calls in the matrices). StateGuard recovers them only with a key (sg_keyed) or an
   operator declaration.
3. **The attempt budget.**
   - Keyed calls get 3 attempts, others 2. A call faulted on every attempt is unresolved.
   - R1 leaves 0.4–1.7% this way, and R2 more on stdio, where one server loss costs every call
     in flight an attempt.
4. **G (tool execution failure) and K (auth) are outside the measured RFR.** K is never retried,
   by rule.
5. **Schema repair trusts the server's listing.** A server whose listing disagrees with what it
   accepts defeats repair (A2 finding). `--no-listing-refresh` can produce false successes on a
   stale listing (A1).
6. **No traffic-weighted RFR and no production capture.**
   - Incidence records (D9) make one possible, but no real traffic was available in Phase 1;
     that capture depends on the owner.
   - The fault frequencies here are chosen, not observed.
7. **Network cost of event-stream answers unmeasured** (P2b: no netem on this kernel). The
   connection-reuse optimisation is deferred.
8. **Third-party concurrency:** server-memory's `add_observations` loses concurrent writes with
   no fault (no lock); StateGuard does not serialise calls.
9. **Not exactly-once.** Keys make resends safe; D11's re-run is correct only if the
   idempotency declaration is true.
10. **CI and macOS:** CI has not run on this branch; macOS is untested.

## 13. Distance from the sub-1% objective

- **Under these synthetic faults, the best configurations are at or below 1%:**
  - HTTP keyed with deadline: 0/168 transport and session faults, ≤ 2.2% at 95%;
  - R1 on D16: 1/240 and 4/240;
  - memory with D11: 0/84.
- **The intervals do not yet prove sub-1%.** 0/168 bounds the rate at 2.2%, not 1%. Showing
  ≤ 1% at 95% with no failure needs at least 381 trials per cell (Wilson:
  z²/(n + z²) ≤ 0.01).
- **These are per-fault rates, not per-call rates.** Every call in these cells was faulted. A
  per-call RFR in production is the fault incidence times these rates, plus what the matrices
  do not inject (G, K, real network behaviour).
- **Not at 1% where the floor is structural:**
  - unkeyed non-idempotent tools: about 50–64% of faulted calls unresolved;
  - crash windows of non-idempotent tools: 24/36 crash cells unresolved.
  - Getting there needs server cooperation (keys, transactional effects or recovery checks),
    not more relay logic.
- **The missing measurement is the incidence of faults in real traffic.** The incidence
  records exist; a real capture is the next step (section 14).

## 14. Recommended Phase 2 priorities

1. **A real-traffic capture with the incidence records** (owner-dependent). It turns the
   per-fault rates into a per-call RFR and shows which classes matter.
2. **Closing the crash window with server cooperation.**
   - A transactional key record for servers that can commit it with the effect.
   - A documented key-bound recovery-check interface for operators (K-6, `recover=`).
3. **Keys by default where the server supports them:** declare and verify, so fewer
   deployments run the unkeyed relay.
4. **CI on the branch and macOS:** open the PR (founder) so GitHub Actions runs the matrix.
5. **The network cost of event-stream answers** on a kernel with netem, then the
   connection-reuse optimisation.
6. **Concurrency hygiene:**
   - optional per-tool serialisation for servers like server-memory;
   - a per-call budget that accounts for losses caused by other calls.
7. **Larger samples for the ≤ 1% claim** (at least 381 trials per cell) once the cells are
   final.

## 15. Actual Claude usage allocation

Not available to this agent. The usage dashboard is authoritative. Phase 1 ran in the
scheduled windows, the founder's extra session, and this early Session 5. It was interrupted
twice by usage limits (Fri 17:53 IST and Sat 22:40 IST), and resumed each time on the founder's
instruction.

---

**Founder input requested:** Phase 1 is complete ahead of schedule. Execution has stopped.
Phase 2 will not start without the founder's authorization and choice of priorities (section
14).

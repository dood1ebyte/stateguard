# M1 results: the compatibility matrix (Phase 2 S4.2)

- **Code under test:** `290ba5d` (S4.1).
- **Harness:** `61e0edd` (`harness_dirty: false`).
- **How it was run:** through `run_batch.py` on a clean worktree, on 2026-11-25. Each part's
  `codestate_<server>.json` records its run.
- **Plan:** `PLAN.md`, committed before any row.
- **What this is:** synthetic fault injection on pinned public servers, not production traffic.
  3 trials per cell.

**Totals:**
- 315 rows across 10 servers;
- **0 unsafe outcomes**: no duplicate effect, false success, false failure, wrong content or
  duplicate send;
- 0 harness errors.

| Server | Listing (shadow / auto) | No fault | `--keys auto` | Faulted declared reads | Faulted writes | Unsafe |
|---|---|---|---|---|---|---|
| filesystem | identical 3/3, 3/3 | 9/9 | preload, `move_file` keyed, executed 3/3 | 9/9 recovered | 0/9 unresolved: UNKNOWN 6, client timeout 3 | 0 |
| memory | identical | 9/9 | preload, `add_observations` keyed | 9/9 | 0/9 (UNKNOWN 6, timeout 3) | 0 |
| everything | identical | 6/6 | preload (`echo` probe) | 9/9 | no write | 0 |
| sqlite | identical | 9/9 | injector, `write_query` keyed | 9/9 | 0/9 (UNKNOWN 6, timeout 3) | 0 |
| git | identical | 9/9 | injector, `git_commit` keyed | 9/9 | 0/9 (UNKNOWN 6, timeout 3) | 0 |
| fetch | identical | 6/6 | injector (`fetch` probe) | 9/9 | no write | 0 |
| time | identical | 6/6 | injector (`convert_time` probe) | 9/9 | no write | 0 |
| sequential-thinking | identical | 6/6 | preload, `sequentialthinking` keyed | n/a (its one tool is declared non_idempotent): 0/9 unresolved (UNKNOWN 6, timeout 3) | | 0 |
| FastMCP example | identical | 6/6 | injector (`add` probe) | 9/9 | no write | 0 |
| TS SDK Streamable HTTP example | identical | 6/6 | n/a (HTTP relay) | 9/9 (`kill_before`, `lost_response`, `hang_after`) | no write | 0 |

Each cell is 3 trials. "Recovered" means the client was told success with the right content.

## Against the predictions

**As predicted:**
- the listings, every no-fault call, and every faulted declared read (81/81, recovered by the
  relay's resend);
- `--keys auto` on all nine stdio servers: the preload on the four Node servers, the injector
  on the five Python servers, keys advertised, outcome `executed`;
- 0 unsafe.
- The "not certain" servers (git, fetch, time) were all wrapped by the injector and advertised
  keys.

**Missed, and not unsafe:** a hung write (and a hung `sequentialthinking`) was predicted to end
as "surfaced UNKNOWN or an honest error". It ended as a **client timeout** (8 s) in 3/3 on each
of the five servers.
- **Why:** under the relay's default `--deadline-scope recoverable`, `--call-deadline` is when to
  try again. A call that cannot be resent waits for its answer up to `--answer-timeout` (300 s),
  so the relay's "unknown" comes after the client has stopped waiting.
- The write's effect is consistent with that: never claimed, and never re-run.
- An operator who wants the bound can set `--deadline-scope all`. The relay then answers
  "unknown" at the deadline (Phase 1, Decision 1).

**`kill_before` on a write:** the call was never forwarded (`not_executed`), and it surfaced as
UNKNOWN. That is correct: the relay cannot tell that the shim dropped the call, so it does not
claim "not sent".

## Status per server

The rule is in `PLAN.md`. The table is published in `docs/runtime/COMPATIBILITY_MATRIX.md`.

| # | Server | Status | Limitation |
|---|---|---|---|
| 1 | filesystem | **Pass** | none |
| 2 | memory | **Pass with a documented limitation** | It loses concurrent writes (each told success). See SER1. `--serialize-tools add_observations` removes the loss. |
| 3 | everything | **Pass with a documented limitation** | No tool with a lasting effect: writes untested. |
| 4 | sqlite | **Pass with a documented limitation** | It reports a failed query as an ordinary result (`isError` false), so neither StateGuard nor a client can tell it failed. This is the mechanism of Phase 1's A1 stale-listing false successes. |
| 5 | git | **Pass** | none |
| 6 | fetch | **Pass with a documented limitation** | No write. Tested against a local site only. |
| 7 | time | **Pass with a documented limitation** | No write. |
| 8 | sequential-thinking | **Pass with a documented limitation** | Its one tool appends to the process's own history: no ground truth outside the process, and nothing to recover after a fault (declared non_idempotent; faults end unresolved). |
| 9 | FastMCP example | **Pass with a documented limitation** | No write. It is the SDK's README quickstart, not a production server. |
| 10 | TypeScript SDK Streamable HTTP example | **Pass with a documented limitation** | `--keys auto` does not apply over HTTP. No write. It is an SDK example, not a production server. |

**Not attempted:** a browser server (puppeteer or playwright), which PHASE2_PLAN §7 makes
optional and last. It was left out because S4 had no time for it, not because it failed.

# M2 results: the compatibility cells on the servers' current releases

- **Code:** `45b684e` for M1's cells and the concurrency check, and `e76475c` for the re-run
  `keys/auto` cells.
- **How it was run:** `run_batch.py` on clean worktrees, on 2026-11-27. The relay is unchanged
  since `0dd8604`.
- **The plan:** `PLAN.md`, with its addendum written before the re-run's rows.
- **What this is:** synthetic fault injection on public servers, not production traffic. 3
  trials per cell.

**0 unsafe outcomes in every StateGuard cell** (264 rows of M1's cells, 12 rows re-run).

| Server (current) | Listing, no-fault calls, faulted reads | Faulted writes | `--keys auto` before `e76475c` | `--keys auto` on `e76475c` |
|---|---|---|---|---|
| server-filesystem 2.4.0 (TS SDK v2) | as M1 | as M1: UNKNOWN or client timeout, never re-run | the server did not start; 3/3 UNKNOWN, nothing ran | **unkeyed, with the reason; 3/3 succeeded** |
| server-memory 2.2.1 (TS SDK v2) | as M1 | as M1 | did not start, 3/3 UNKNOWN | unkeyed; 3/3 succeeded |
| server-everything 2.3.1 (TS SDK v2) | as M1 | no write | did not start | unkeyed; 3/3 succeeded |
| server-sequential-thinking 2.2.0 (TS SDK v2) | as M1 (faults end UNKNOWN or client timeout: its tool is declared non_idempotent) | — | did not start | unkeyed; 3/3 succeeded |
| mcp-server-git 2026.11.18 (mcp 2.3.0) | as M1 | as M1 | **keyed: the injector, `git_commit` advertised, outcome executed 3/3** | — |
| mcp-server-fetch 2026.11.18 (mcp 2.3.0) | as M1 | no write | keyed, injector, executed | — |
| mcp-server-time 2026.11.18 (mcp 2.3.0) | as M1 | no write | keyed, injector, executed | — |
| FastMCP quickstart on mcp 2.3.0 | **does not start**: `mcp.server.fastmcp` no longer exists in mcp 2.x (renamed `MCPServer`) | — | — | — |

**server-memory 2.2.1 under concurrency** (SER1's workload, 5 trials of 16 concurrent
`add_observations`):

| Arm | Calls | Told success | Present | Lost |
|---|---|---|---|---|
| raw | 80 | 80 | 80 | 0 |
| sg | 80 | 80 | 80 | 0 |
| sg_serial | 80 | 80 | 80 | 0 |

The current release did not lose a concurrent write in this workload. SER1 measured the pinned
2026.8.31 losing 149–150 of 160. The public issues of 2026-11 describe file locking added to
server-memory; that is Track B context, not tested here.

**Against the plan:**
- **The Python servers:** keyed by the injector on mcp 2.3.0, as predicted.
- **The SDK v2 Node servers:**
  - **Predicted:** not keyed.
  - **Found:** worse than predicted. Under `--keys auto` they did not start at all, because the
    preload threw at start.
  - That is never a false outcome: nothing ran, and every call was answered UNKNOWN. But it is
    a StateGuard-induced outage (taxonomy Z), against the planner's rule that a command it
    cannot read with certainty runs unkeyed.
  - **Fixed in `e76475c`:** the planner now checks for the SDK 1.x where the preload will look,
    and otherwise runs the server unkeyed with the reason. `stateguard doctor` reports it too.
  - **Not done:** keys for SDK v2 servers, which would need a preload for the v2 SDK. That is
    future work, outside S5.
- **The FastMCP quickstart:** as "not certain" predicted, it is a 1.x example and does not run on
  mcp 2.x. This is the example's limitation, not StateGuard's.
- **server-memory 2.2.1's concurrency:** measured, 0 lost.

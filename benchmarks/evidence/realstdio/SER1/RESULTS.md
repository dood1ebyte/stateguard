# SER1 results: server-memory under concurrency, with and without `--serialize-tools`

- **Code:** `572c74e` (S4.3), run through `run_batch.py` on a clean worktree on 2026-11-25.
- **Plan:** `PLAN.md`, committed in `572c74e` before any row.
- **Workload:** a real pinned server (@modelcontextprotocol/server-memory 2026.8.31) under a
  synthetic concurrent workload, not production traffic.
- **Shape:** 10 trials per arm; each trial is 16 `add_observations` calls in flight at once on
  one session.

| Arm | Calls | Told success | Present | **Lost writes** | Trials with loss | Duplicates | Median per trial |
|---|---|---|---|---|---|---|---|
| raw (no relay) | 160 | 160 | 10 | **150** | 10/10 | 0 | 0.94 s |
| sg (relay, no flag) | 160 | 160 | 11 | **149** | 10/10 | 0 | 1.06 s |
| sg_serial (`--serialize-tools add_observations`) | 160 | 160 | 160 | **0** | 0/10 | 0 | 1.05 s |

- **With the flag:** 0 lost writes in 160, as predicted. Every call was told success and its
  observation is in the file. The relay logged 15 `serialized` waits per trial: each call after
  the first waited for the one before it.
- **Without it:** about 15 of 16 writes are lost in every trial, with no relay and through the
  relay alike. Each lost write was told success: a **false success**.
  - It is the server's own defect. Every call reads the file, changes the graph and rewrites the
    file, so overlapping calls overwrite each other.
  - The default relay passes the answer through unchanged: it does not cause the loss (149 vs
    150 with no relay) and cannot detect it.
  - The flag is opt-in. The relay cannot know which servers lose concurrent writes, so the
    operator names the tools.
- **Prediction missed:** `sg_serial` was predicted to be slower per trial. It was not (median
  1.05 s against 1.06 s). The server answers each call in well under a millisecond, so session
  start-up dominates a trial. A server whose calls take longer would pay one call's latency per
  call held in the lane.

**Limits of what this shows:**
- **One server, one tool, on stdio.** The HTTP relay has no `--serialize-tools`; that is a
  documented limitation (S4.3).
- **The lane's scope is one relay process.** Two clients, each with its own relay, in front of
  one shared memory file are not serialised against each other.

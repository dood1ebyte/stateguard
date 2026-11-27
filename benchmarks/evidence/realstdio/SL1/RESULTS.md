# SL1 results: the A1/A2 stale-listing cells with refresh off, on S4.1's code

**Code:** `290ba5d` (S4.1), run through `run_batch.py` on a clean worktree on 2026-11-25. The
plan, `PLAN.md`, was written and committed (`b886b51`) before any row. Synthetic fault injection
(the shim or proxy serves a stale listing) on controlled and pinned servers; not production
traffic. The rows are in both `realstdio/SL1/` and `realhttp/SL1/`.

| Relay | Server | Arm | Phase 1 (A1/A2) | SL1 (S4.1) | Unsafe in SL1 |
|---|---|---|---|---|---|
| stdio | sqlite | sg_norefresh | 12/12, **12 false successes** | **0/12** | 0 |
| stdio | memory | sg_norefresh | 12/12 failed | **0/12** | 0 |
| HTTP | notes | sg_norefresh | 12/12 failed | **0/12** | 0 |
| HTTP | tsnotes | sg_norefresh | 12/12 failed | **0/12** | 0 |
| all four | | sg (refresh on) | 0/12 | 0/12 | 0 |

- **As predicted:** 0 false successes and 0 false failures on every server.
- In all 96 rows the fault fired, the client was told success, the write ran exactly once
  (ground truth), and the relay's invocation record agreed with it.
- With refresh off, the relay now forwards the client's call unchanged: the listing cannot be
  confirmed current, so the repair it would have made is logged as withheld. The tests
  `tests/examples/test_relay_staleness.py` and `test_relay_repair_safety.py` check that log.
- **The cost:** with refresh off, a call that does not match a stale listing is no longer
  repaired. The server, whose schema is the true one, judges it. Refresh on (the default) still
  repairs against a confirmed listing.

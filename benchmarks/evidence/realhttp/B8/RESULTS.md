# B8 results: the HTTP matrix on S4.1's code

**Code:** `290ba5d` (S4.1), run through `run_batch.py` on a clean worktree on 2026-11-25. The
plan, `PLAN.md`, was committed in `b886b51` before any row. The parts and commands are B7's.
Synthetic fault injection; not production traffic.

**Result: identical to B7 in every part and every arm, baselines included**
(`summary_*.compare.txt`). As predicted.

| Part | sg | sg_keyed | sg_keyed_deadline / sg_deadline | raw | naive | Unsafe in StateGuard arms |
|---|---|---|---|---|---|---|
| notes | 84/192 | 24/192 | 0/192 | 168/192 | 48/192 | 0 |
| tsnotes | 84/192 | 24/192 | 0/192 | 168/192 | 48/192 | 0 |
| everything | 12/96 | – | 0/96 | 72/96 | 0/96 | 0 |
| notes, tsnotes, everything controls | 0 | 0 | 0 | 0 | 0 | 0 |
| crash_notes | 36/36 | 24/36 | 24/36 | 36/36 | 36/36 | 0 |
| crash_tsnotes | 36/36 | 24/36 | 24/36 | 36/36 | 36/36 | 0 |

S4.1's two changes do not touch the default matrix:
- the refresh-off rule applies only with `--no-listing-refresh`;
- the repair fallback applies only to repaired calls.
The final HTTP code (S4.4, `0dd8604`) is checked separately in B9.

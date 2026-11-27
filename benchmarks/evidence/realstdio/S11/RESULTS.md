# S11 results: the stdio matrix on S4.1's code

**Code:** `290ba5d` (S4.1), run through `run_batch.py` on a clean worktree on 2026-11-25. The
plan, `PLAN.md`, was committed in `b886b51`, before any row. The parts and commands are S10's.
Synthetic fault injection on pinned servers; not production traffic.

**Result: identical to S10 in every part and every arm, baselines included**
(`summary_*.compare.txt`). As predicted.

| Part | sg | sg_keyed | raw | naive | Unsafe in StateGuard arms |
|---|---|---|---|---|---|
| sqlite | 84/156 | 24/156 | 132/156 | 48/156 | 0 |
| memory | 84/156 | 24/156 | 132/156 | 0/156 | 0 |
| filesystem (against S10's `filesystem_r2`) | 84/156 | 24/156 | 132/156 | 48/156 | 0 |
| memory_idempotent (sg_keyed_idempotent) | – | 0/96 | – | – | 0 |
| slow_sqlite, slow_memory, slow_filesystem (sg_keyed) | – | 0/12 each | – | – | 0 |
| keys_auto_sqlite, keys_auto_memory, keys_auto_filesystem (sg_keys_auto) | – | as S10 | – | – | 0 |

S4.1's two changes do not touch the default matrix:
- the refresh-off rule applies only with `--no-listing-refresh`;
- the repair fallback applies only to repaired calls.
The final stdio code (S4.3's lane, inert unless `--serialize-tools` is given) is checked
separately in S12.

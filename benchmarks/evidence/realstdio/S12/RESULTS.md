# S12 results: the stdio matrix on S4's final relay code

**Code:** `d429817`: S4.3's `--serialize-tools` lane, plus S3.2's adapter hook, which is inert
here. Run through `run_batch.py` on a clean worktree on 2026-11-27.
- **The plan:** `PLAN.md`, written before any row, with its commit change recorded before any
  row of the batch.
- **The parts and commands:** S10's.

Synthetic fault injection on pinned servers; not production traffic.

**Result: identical to S10 in every part and every arm, baselines included**
(`summary_*.compare.txt`). As predicted.

| Part | sg | sg_keyed | raw | naive | Unsafe in StateGuard arms |
|---|---|---|---|---|---|
| sqlite, memory, filesystem (against `filesystem_r2`) | as S10 | as S10 | as S10 | as S10 | 0 |
| memory_idempotent | – | as S10 | – | – | 0 |
| slow_sqlite, slow_memory, slow_filesystem | – | as S10 | – | – | 0 |
| keys_auto_sqlite, keys_auto_memory, keys_auto_filesystem | – | as S10 (sg_keys_auto) | – | – | 0 |

With `--serialize-tools` unset, the lane is never used, as the plan said. These cells, with the
preload and injector carrying S3.2's code, are unchanged.

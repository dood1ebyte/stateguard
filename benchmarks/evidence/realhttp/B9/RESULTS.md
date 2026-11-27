# B9 results: the HTTP matrix on S4's final relay code

**Code:** `d429817`: S4.4's event-stream drain, plus S3.2's adapter hook, which is inert here.
Run through `run_batch.py` on a clean worktree on 2026-11-27.
- **The plan:** `PLAN.md`, written before any row, with its commit change recorded before any
  row of the batch.
- **`notes`:** run as `notes_r2`. Its first attempt (`rows_notes_interrupted.jsonl`) ran beside
  B8 by mistake and was stopped.
- **`notes_control`:** run again. Its first attempt (`rows_notes_control_interrupted.jsonl`) was
  cut off when S4 stopped.
- Neither interrupted file is used.

Synthetic fault injection; not production traffic.

**Result: identical to B7 in every part and every arm, baselines included**
(`summary_*.compare.txt`). As predicted.

| Part | sg | sg_keyed | sg_keyed_deadline / sg_deadline | raw | naive | Unsafe in StateGuard arms |
|---|---|---|---|---|---|---|
| notes (notes_r2) | 84/192 | 24/192 | 0/192 | 168/192 | 48/192 | 0 |
| tsnotes | 84/192 | 24/192 | 0/192 | 168/192 | 48/192 | 0 |
| everything | 12/96 | – | 0/96 | 72/96 | 0/96 | 0 |
| the three controls | 0 | 0 | 0 | 0 | 0 | 0 |
| crash_notes, crash_tsnotes | 36/36 | 24/36 | 24/36 | 36/36 | 36/36 | 0 |

**What it shows about S4.4.** The relay now reuses its connection after an event-stream answer:
- tsnotes and everything answer over event streams;
- the crash cells restart the server on the same port, so the next request can meet a pooled
  connection to a dead process.
No cell changed: every such request was resolved by the existing rules, as the plan expected.

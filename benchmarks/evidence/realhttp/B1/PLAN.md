# B1: HTTP baseline versus StateGuard, re-established in the repository (written before any row)

**Why.** The iter8–iter10 evidence (M17–M19) was lost with its container. B1 re-runs the same
matrix with the in-repo harness, on commit `237a332`, so the comparison exists in the repository.

**Code:** clean worktree of `237a332` (`run_batch.py`). **Toolchain:** see `codestate*.json`.
This container runs Python 3.13; the lost batches ran Python 3.11.

**Servers and arms** (`driver.py` defaults):
- notes (controlled Python): raw, naive, sg, sg_keyed, sg_keyed_deadline; 16 cells
- tsnotes (controlled TypeScript): the same arms and cells
- everything (third-party, reads only): raw, naive, sg, sg_deadline; 8 read cells

**Trials:** 12 per cell; fault-free controls 60 more per fault-free cell (TRIAL_OFFSET=1000).
Workers 4, arms interleaved.

**Expectations** (from the lost batches; any deviation is reported as measured):
- notes and tsnotes, each:
  - raw 168/168;
  - naive 48/168, with 48 duplicate notes;
  - sg 84/168;
  - sg_keyed 24/168 (hang_after only);
  - sg_keyed_deadline 0/168.
- everything: raw 72/84, naive 0/84, sg 12/84 (hang_after), sg_deadline 0/84.
- Fault-free 0 in every arm; 0 unsafe outcomes and 0 induced in every StateGuard arm.
- 0 invocation-record mismatches against ground truth.

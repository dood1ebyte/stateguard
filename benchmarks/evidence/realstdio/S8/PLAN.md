# S8: the stdio matrix on the final Phase 1 code, three servers (written before any row)

**Why.** This is the evidence base for the Completion Report: every stdio cell on one commit
with every Phase 1 change. Since S4/S6/S7 the relay gained:
- decision 1 (the deadline is when to try again; S6);
- D13/D14 (non-conformant answers).

**Setup.** `realstdio/driver.py`, sqlite, memory and filesystem, every default cell (fault-free,
4 transport faults on read and write, 3 crash points on write), arms raw, naive, sg and
sg_keyed; 12 trials, TRIAL_OFFSET=22000, 4 workers. Plus memory's `sg_keyed_idempotent` arm on
the write cells (D11, as S5). The code is this commit.

**Expectations:**
- per server, sg 84/132 and sg_keyed 24/132 (the two crash windows);
- memory sg_keyed_idempotent 0/84 (S5);
- naive: sqlite 48 duplicates, memory 0/132, filesystem 48 false failures;
- fault-free 0; 0 unsafe in StateGuard arms; 0 record mismatches.

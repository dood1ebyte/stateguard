# B5: the HTTP matrix on the final Phase 1 code (written before any row)

**Why.** This is the evidence base for the Completion Report: every HTTP cell on one commit
with every Phase 1 change. Since B3 (`51f6fdf`) the relay gained:
- decision 2 (the 30 s default deadline; the matrix arms pass theirs explicitly);
- D13/D14 (non-conformant answers);
- the extra session's harness.

**Setup.** `driver.py`, 4 workers; the code is this commit:
- B3's parts: notes, tsnotes and everything, every default arm and cell, 12 trials, with
  60-trial fault-free controls (TRIAL_OFFSET=1000);
- C1's crash cells (`write/after_begin`, `after_effect`, `after_record`) on notes and tsnotes,
  every default arm, 12 trials, TRIAL_OFFSET=11000.

**Expectations:**
- B3's counts cell for cell:
  - notes and tsnotes: raw 168/168, naive 48/168, sg 84/168, sg_keyed 24/168,
    sg_keyed_deadline 0/168;
  - everything: raw 72/84, naive 0/84, sg 12/84, sg_deadline 0/84;
  - fault-free 0.
- C1-on-D12's crash counts: keyed `after_record` 0/12; `after_begin` and `after_effect`
  unresolved; unkeyed arms all fail.
- 0 unsafe outcomes and 0 induced in StateGuard arms; 0 record mismatches.

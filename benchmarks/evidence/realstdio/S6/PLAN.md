# S6: the stdio matrix on the new deadline default, and slow answers (written before any row)

**Why.** Decision 1 (`2d8a676`): the stdio relay's `--call-deadline` is now when to try again
(`--deadline-scope recoverable`, the new default), as in the HTTP relay. Before, the default
(`all`) was a hard bound. This batch:
- checks that the matrix's residual is unchanged (S4);
- shows the change on slow answers, the case it is about.

**Setup.** `realstdio/driver.py`, sqlite and memory, 12 trials, 4 workers; the code is this
commit. The relay gets `--call-deadline 2`, with no `--deadline-scope` unless the arm says
`_bound`.
- Part `matrix` (TRIAL_OFFSET=13000): every default cell, arms raw, sg and sg_keyed.
- Part `slow` (TRIAL_OFFSET=14000): `read/slow` and `write/slow` (the shim delays the
  answer 5 s; no fault), arms raw, sg, sg_bound (`--deadline-scope all`), sg_keyed and
  sg_keyed_bound.

**Expectations:**
- **matrix:** S4's failure counts per arm (sg 84/132, sg_keyed 24/132 per server). Some
  unkeyed `hang` rows may move from "unknown" at the deadline to no answer by the client's 8 s:
  both are unresolved.
- **slow:**
  - raw 0/24;
  - sg 0/24: the unkeyed write is never cut short, the read is re-requested;
  - sg_bound: `write/slow` 12/12 unknown, induced against raw (the old contract's cost);
    `read/slow` 0/12;
  - sg_keyed and sg_keyed_bound 0/24: the keyed write is replayed.
- 0 unsafe outcomes, 0 record mismatches.

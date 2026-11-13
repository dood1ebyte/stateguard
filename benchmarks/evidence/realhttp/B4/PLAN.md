# B4: the deadline arms on the full HTTP matrix, after decision 2 (written before any row)

**Why.** Decision 2 (`4c2256d`) restructured the HTTP relay's deadline race. Each answer is now
tagged with its attempt, and an original that answers first is recorded `answered`, not as a
`deadline` fault. The default also became 30 s. Only the `_deadline` arms exercise the race.
B4 re-runs them on every default cell, as a regression check against B3.

**Setup.** `driver.py`, 12 trials, 4 workers; the code is this commit:
- notes and tsnotes: arm sg_keyed_deadline (`--call-deadline 2`), every default cell;
- everything: arm sg_deadline, every default cell.

**Expectations:**
- B3's counts: sg_keyed_deadline 0/168 per notes server; sg_deadline 0/84 on everything;
- fault-free 0; 0 unsafe; 0 record mismatches.
- Records: hang rows (the resend wins) are `recovered` with fault `deadline`. No row has an
  original winning after the deadline unless the server was slow.
